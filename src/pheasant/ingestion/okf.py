"""Open Knowledge Format (OKF) documents, read per file.

OKF (https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf,
v0.2) is a directory of Markdown files with YAML frontmatter: every
non-reserved ``.md`` file is a *concept* carrying a required ``type``, an
``index.md`` lists a directory for progressive disclosure, and a ``log.md``
records dated changes. Relationships are ordinary Markdown links plus a few
path-valued frontmatter fields (``sources[].resource``, ``executor``,
``attester``, ``computation``).

This module is the *per-file* half. It decides what one file says about
itself — its role, its frontmatter families, the links its body makes — and
nothing about the directory it sits in, because a file is parsed alone (on
the indexer or on a stateless worker) and whether a directory *is* a bundle
can only be decided once every file in it has been read. That decision, and
every edge that follows from it, is ``graph.okf``.

Three rules shape it:

* **Deterministic, no network, no model** (pillar 3). A pure function of the
  bytes, so a worker and the indexer produce the same answer.
* **Never reject.** The spec says a consumer MUST NOT reject a concept for a
  missing optional family, an unknown type or a broken link; a file that does
  not parse as OKF returns ``None`` and is indexed exactly as before.
* **Timestamps stay strings.** PyYAML's safe loader turns
  ``2026-06-30T14:00:00Z`` into a ``datetime``, which is not JSON and would
  re-render as ``+00:00`` — a different string from the one the author wrote,
  in a graph whose generation id is a digest of what it stores.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

import yaml

#: Bumped when what this module extracts changes shape. Carried on the parse
#: result so a reader can tell which rules produced a stored ``okf`` attribute.
OKF_PARSER_VERSION = "okf-0.2-1"

#: Filenames the spec reserves at every level of a bundle (OKF §3.1).
INDEX_FILENAME = "index.md"
LOG_FILENAME = "log.md"

# Bounds on what one file can put on a graph node. Frontmatter is authored by
# agents as often as by people, and an attribute is copied into every graph
# write of the node — so a runaway `sources` list must cost a bounded number
# of bytes rather than whatever the producer emitted.
MAX_FRONTMATTER_BYTES = 64 * 1024
MAX_LIST_ITEMS = 200
MAX_STRING_CHARS = 2_000
MAX_DEPTH = 6

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_FENCE_RE = re.compile(r"(?ms)^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$")
# `[text](target)` but not `![alt](image)`; the target stops at whitespace so
# a `[x](path "title")` form keeps only the path.
_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]\s]+)\](?!:)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_DATE_HEADING_RE = re.compile(r"^#{1,6}\s+(\d{4}-\d{2}-\d{2})\b")
_LIST_ENTRY_RE = re.compile(
    r"^\s*[*+-]\s+\[([^\]]*)\]\(\s*<?([^)\s>]+)>?\s*\)\s*(?:[-–—:]\s*(.*))?$"
)
_ACTION_RE = re.compile(r"^\s*[*+-]\s+\*\*([^*]+)\*\*")
_BACKTICK_PATH_RE = re.compile(r"`([^`\s]+\.md)`")

#: Frontmatter keys this module lifts into named fields. Anything else is a
#: producer extension (OKF §4.1) and is kept, bounded, under ``extensions``.
_KNOWN_KEYS = frozenset(
    {
        "type",
        "title",
        "description",
        "resource",
        "tags",
        "status",
        "stale_after",
        "generated",
        "verified",
        "timestamp",
        "sources",
        "usage_window",
        "runtime",
        "parameters",
        "computation",
        "executor",
        "attester",
        "okf_version",
    }
)

#: Keys only an OKF producer would write. ``type`` alone is also how Hugo and
#: a few other site generators route a page, so bundle detection asks for one
#: of these as corroboration when a directory carries no ``index.md``.
OKF_SIGNAL_KEYS = frozenset(
    {"sources", "generated", "verified", "stale_after", "resource", "runtime", "executor"}
)

VALID_STATUSES = frozenset({"draft", "stable", "deprecated"})


class _StringTimestampLoader(yaml.SafeLoader):
    """``SafeLoader`` without the implicit timestamp resolver."""


_StringTimestampLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str, bool]:
    """``(frontmatter, body, had_block)`` for one Markdown document.

    ``frontmatter`` is ``None`` when there is no block, when it is not YAML, or
    when it is YAML but not a mapping; ``had_block`` says whether a delimited
    block was present at all, which is what tells an OKF ``index.md`` (no
    frontmatter, OKF §8) from a Hugo leaf-bundle ``index.md`` (frontmatter).
    """

    text = text.lstrip("﻿")
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None, text, False
    raw = match.group(1)
    body = text[match.end() :]
    if len(raw.encode("utf-8", errors="ignore")) > MAX_FRONTMATTER_BYTES:
        return None, body, True
    try:
        loaded = yaml.load(raw, Loader=_StringTimestampLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError:
        return None, body, True
    if not isinstance(loaded, dict):
        return None, body, True
    return loaded, body, True


def parse_okf(relative_path: str, text: str) -> dict[str, Any] | None:
    """What this file says as an OKF document, or ``None`` when it says nothing.

    * ``index.md`` → role ``index`` when it has no frontmatter (OKF §8) or
      declares ``okf_version`` (§12), with its listing entries.
    * ``log.md`` → role ``log`` when it has at least one ISO-dated heading
      (§9), with its entries.
    * any other ``.md`` → role ``concept`` when its frontmatter carries a
      non-empty string ``type`` (§4.1).

    A ``None`` here is not a judgement about the file, only that it carries
    no OKF structure; it is indexed exactly as it always was.
    """

    name = posixpath.basename(relative_path.replace("\\", "/")).lower()
    if not name.endswith(".md"):
        return None
    frontmatter, body, had_block = split_frontmatter(text)
    if name == INDEX_FILENAME:
        return _parse_index(frontmatter, body, had_block)
    if name == LOG_FILENAME:
        return _parse_log(body)
    if frontmatter is None:
        return None
    okf_type = frontmatter.get("type")
    if not isinstance(okf_type, str) or not okf_type.strip():
        return None
    return _parse_concept(frontmatter, body)


def _parse_concept(frontmatter: dict[str, Any], body: str) -> dict[str, Any]:
    status_raw = _text(frontmatter.get("status"))
    status = status_raw.lower() if status_raw else "stable"
    generated = _generated(frontmatter)
    verified = normalize_verified(frontmatter.get("verified"))
    result: dict[str, Any] = {
        "role": "concept",
        "parser": OKF_PARSER_VERSION,
        "type": _text(frontmatter.get("type")),
        "title": _text(frontmatter.get("title")),
        "description": _text(frontmatter.get("description")),
        "resource": _text(frontmatter.get("resource")),
        "tags": _tags(frontmatter.get("tags")),
        # Absent ⇒ stable (§5.4). An unknown value is kept as written rather
        # than coerced: the spec names three, and a consumer reading
        # `status: retired` should see that, not a guess.
        "status": status,
        "status_declared": status_raw is not None,
        "stale_after": _text(frontmatter.get("stale_after")),
        "generated": generated,
        "verified": verified,
        "trust_tier": trust_tier(verified),
        "sources": _sources(frontmatter.get("sources"), frontmatter.get("usage_window")),
        "links": _body_links(body),
        "citations": _citations(body),
        "signals": sorted(key for key in OKF_SIGNAL_KEYS if key in frontmatter),
    }
    if frontmatter.get("runtime") is not None or result["type"] == "Attested Computation":
        result["computation"] = {
            "runtime": _text(frontmatter.get("runtime")),
            "parameters": _parameters(frontmatter.get("parameters")),
            "computation": _text(frontmatter.get("computation")),
            "executor": _resource_block(frontmatter.get("executor"), with_receipt=True),
            "attester": _resource_block(frontmatter.get("attester")),
            "inline": bool(re.search(r"(?mi)^#{1,6}\s+computation\s*$", body)),
        }
    extensions = {
        str(key): _bounded(value)
        for key, value in frontmatter.items()
        if str(key) not in _KNOWN_KEYS
    }
    if extensions:
        result["extensions"] = extensions
    return result


def _parse_index(
    frontmatter: dict[str, Any] | None, body: str, had_block: bool
) -> dict[str, Any] | None:
    version = _text((frontmatter or {}).get("okf_version"))
    # OKF §8: index files carry no frontmatter, except a bundle-root
    # `okf_version`. A frontmatter block without one is some other tool's
    # index page (a Hugo leaf bundle is exactly this), not an OKF listing.
    if had_block and version is None:
        return None
    entries: list[dict[str, Any]] = []
    section: str | None = None
    for line in _strip_fences(body).splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            section = heading.group(2).strip()
            continue
        entry = _LIST_ENTRY_RE.match(line)
        if entry is None:
            continue
        entries.append(
            {
                "title": entry.group(1).strip(),
                "target": entry.group(2).strip(),
                "description": (entry.group(3) or "").strip() or None,
                "section": section,
            }
        )
        if len(entries) >= MAX_LIST_ITEMS:
            break
    if version is None and not entries:
        return None
    return {
        "role": "index",
        "parser": OKF_PARSER_VERSION,
        "okf_version": version,
        "entries": entries,
    }


def _parse_log(body: str) -> dict[str, Any] | None:
    entries: list[dict[str, Any]] = []
    date: str | None = None
    for line in _strip_fences(body).splitlines():
        dated = _DATE_HEADING_RE.match(line)
        if dated:
            date = dated.group(1)
            continue
        if date is None or not line.lstrip().startswith(("*", "-", "+")):
            continue
        targets = [target for _text_, target in _LINK_RE.findall(line)]
        targets.extend(_BACKTICK_PATH_RE.findall(line))
        action = _ACTION_RE.match(line)
        entries.append(
            {
                "date": date,
                "action": action.group(1).strip() if action else None,
                "targets": _dedupe(targets),
            }
        )
        if len(entries) >= MAX_LIST_ITEMS:
            break
    if not entries:
        return None
    return {"role": "log", "parser": OKF_PARSER_VERSION, "entries": entries}


def normalize_verified(value: Any) -> list[dict[str, str | None]]:
    """``verified`` as a list of ``{by, at}`` — a bare mapping is one entry (§5.2)."""

    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[dict[str, str | None]] = []
    for entry in value[:MAX_LIST_ITEMS]:
        if not isinstance(entry, dict):
            continue
        by = _text(entry.get("by"))
        if by is None:
            continue
        out.append({"by": by, "at": _text(entry.get("at"))})
    return out


def trust_tier(verified: list[dict[str, str | None]]) -> str:
    """OKF §5.3: unverified, machine-confirmed, or human-reviewed."""

    if not verified:
        return "unverified"
    if any(str(entry.get("by") or "").startswith("human:") for entry in verified):
        return "human-reviewed"
    return "machine-confirmed"


def _generated(frontmatter: dict[str, Any]) -> dict[str, str | None] | None:
    generated = frontmatter.get("generated")
    if isinstance(generated, dict) and _text(generated.get("by")):
        return {"by": _text(generated.get("by")), "at": _text(generated.get("at"))}
    # OKF §13.1: a v0.1 `timestamp` stands in for `generated.at`.
    legacy = _text(frontmatter.get("timestamp"))
    if legacy:
        return {"by": None, "at": legacy}
    return None


def _sources(value: Any, usage_window: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    shared_window = _window(usage_window)
    out: list[dict[str, Any]] = []
    for entry in value[:MAX_LIST_ITEMS]:
        if isinstance(entry, str):
            entry = {"resource": entry}
        if not isinstance(entry, dict):
            continue
        resource = _text(entry.get("resource"))
        if resource is None:
            continue
        usage_count = entry.get("usage_count")
        out.append(
            {
                "id": _text(entry.get("id")),
                "resource": resource,
                "title": _text(entry.get("title")),
                "author": _text(entry.get("author")),
                "usage_count": usage_count
                if isinstance(usage_count, int) and not isinstance(usage_count, bool)
                else None,
                "last_modified": _text(entry.get("last_modified")),
                "usage_window": _window(entry.get("usage_window")) or shared_window,
            }
        )
    return out


def _window(value: Any) -> dict[str, str | None] | None:
    if not isinstance(value, dict):
        return None
    window = {"from": _text(value.get("from")), "to": _text(value.get("to"))}
    return window if any(window.values()) else None


def _parameters(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in value[:MAX_LIST_ITEMS]:
        if not isinstance(entry, dict) or _text(entry.get("name")) is None:
            continue
        out.append(
            {
                "name": _text(entry.get("name")),
                "type": _text(entry.get("type")),
                "required": bool(entry.get("required", False)),
            }
        )
    return out


def _resource_block(value: Any, *, with_receipt: bool = False) -> dict[str, Any] | None:
    if isinstance(value, str):
        value = {"resource": value}
    if not isinstance(value, dict) or _text(value.get("resource")) is None:
        return None
    block: dict[str, Any] = {"resource": _text(value.get("resource"))}
    if with_receipt:
        receipt = value.get("receipt")
        block["receipt"] = (
            [str(item) for item in receipt[:MAX_LIST_ITEMS]] if isinstance(receipt, list) else []
        )
    return block


def _tags(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [part for part in re.split(r"[,\s]+", value) if part]
    if not isinstance(value, list):
        return []
    return _dedupe(str(tag).strip() for tag in value[:MAX_LIST_ITEMS] if str(tag).strip())


def _body_links(body: str) -> list[dict[str, str]]:
    """Every ``[text](target)`` in the body, outside fenced code, in order."""

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for text, target in _LINK_RE.findall(_strip_fences(body)):
        target = target.strip()
        if not target or target.startswith("#") or target in seen:
            continue
        seen.add(target)
        out.append({"target": target, "text": text.strip()[:MAX_STRING_CHARS]})
        if len(out) >= MAX_LIST_ITEMS:
            break
    return out


def _citations(body: str) -> dict[str, int]:
    """Footnote references per label (§5.1), definitions excluded.

    The label is the join key into ``sources[].id``; the footnote prose is
    deliberately not read, exactly as the spec says a consumer should not.
    """

    counts: dict[str, int] = {}
    for label in _FOOTNOTE_REF_RE.findall(_strip_fences(body)):
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


def _strip_fences(body: str) -> str:
    return _FENCE_RE.sub("", body)


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text[:MAX_STRING_CHARS] if text else None


def _bounded(value: Any, depth: int = 0) -> Any:
    """A JSON-safe, size-bounded copy of an arbitrary YAML value."""

    if depth >= MAX_DEPTH:
        return None
    if isinstance(value, dict):
        return {
            str(key)[:MAX_STRING_CHARS]: _bounded(item, depth + 1)
            for key, item in list(value.items())[:MAX_LIST_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth + 1) for item in value[:MAX_LIST_ITEMS]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_STRING_CHARS]


def _dedupe(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out
