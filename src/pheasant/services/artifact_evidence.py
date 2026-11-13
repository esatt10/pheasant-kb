"""Stable evidence identity and compact numbering, shared by every artifact form."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any
from urllib.parse import quote

from markdown_it import MarkdownIt

MARKER = re.compile(r"(?<![\w\\])\[(\d+)\](?!\()")
_INLINE_CODE = re.compile(r"(`+)[^`]*(?:`(?!\1)[^`]*)*?\1|!?\[[^\]\n]*\]\([^\n]*?\)")
_PARSER = MarkdownIt("commonmark")


def remap(text: str, mapping: dict[str, int | str], *, unknown: str | None = None) -> str:
    """Rewrite prose markers; preserve fenced/indented/inline code and links."""
    lines = text.splitlines(keepends=True)
    protected: set[int] = set()
    for token in _PARSER.parse(text):
        if token.type in {"fence", "code_block", "html_block"} and token.map:
            protected.update(range(*token.map))

    def substitute(value: str) -> str:
        return MARKER.sub(lambda m: str(mapping.get(m.group(1), unknown or m.group(0))), value)

    out = []
    for number, line in enumerate(lines):
        if number in protected:
            out.append(line)
            continue
        start, parts = 0, []
        for match in _INLINE_CODE.finditer(line):
            parts.extend([substitute(line[start : match.start()]), match.group(0)])
            start = match.end()
        parts.append(substitute(line[start:]))
        out.append("".join(parts))
    return "".join(out)


def identity(citation: dict[str, Any]) -> str:
    fields = [
        citation.get(k)
        for k in ("source", "path", "section", "revision", "chunk_id", "start_line", "end_line")
    ]
    return "ev-" + hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:24]


def target(citation: dict[str, Any]) -> str:
    return (
        "pheasant://document/"
        + quote(str(citation.get("source") or ""), safe="")
        + "/"
        + quote(str(citation.get("path") or ""), safe="/")
    )


def normalize(payload: dict[str, Any]) -> dict[str, Any]:
    """One first-use numbering; original files and local maps remain available."""
    if payload.get("evidence_numbering") == "artifact-v1":
        return payload
    out = copy.deepcopy(payload)
    from pheasant.services.artifact_visual_evidence import prepare, renumber

    prepare(out)
    original = [dict(c) for c in out.get("citations") or []]
    if not original:
        return out
    by_number = {str(c.get("index")): c for c in original}
    encountered: list[str] = []
    unresolved: set[str] = set()

    # remap knows exactly which regions are prose; collect only its callbacks.
    class Encounter(dict):
        def get(self, key: str, default: Any = None) -> Any:
            if key in by_number and key not in encountered:
                encountered.append(key)
            elif key not in by_number:
                unresolved.add(key)
            return default

    for field in ("answer", "summary", "not_covered"):
        remap(str(out.get(field) or ""), Encounter())
    for file in [] if out.get("turns") else out.get("files") or []:
        if str(file.get("name", "")).lower().endswith((".md", ".markdown")):
            remap(str(file.get("content") or ""), Encounter())
    numbers = [*encountered, *(n for n in by_number if n not in encountered)]
    evidence: dict[str, dict] = {}
    mapping: dict[str, int] = {}
    for number in numbers:
        citation = by_number[number]
        key = identity(citation)
        evidence.setdefault(
            key,
            {
                **citation,
                "index": len(evidence) + 1,
                "evidence_id": key,
                "target": target(citation),
            },
        )
        mapping[number] = evidence[key]["index"]
    markers = {key: f"[{value}]" for key, value in mapping.items()}
    markers.update({key: "[?]" for key in unresolved})
    if unresolved:
        out["unresolved_citations"] = sorted(unresolved, key=int)
    for field in ("answer", "summary", "not_covered"):
        if out.get(field):
            out[field] = remap(str(out[field]), markers)
    # Keep executable bytes intact and carry their local numbering map.
    # Structured visuals can adopt the artifact numbering directly.
    out["citation_map"] = mapping
    for turn in out.get("turns") or []:
        turn["citation_map"] = {
            local: mapping.get(str(number), number)
            for local, number in (turn.get("citation_map") or {}).items()
        }
        for citation in turn.get("citations") or []:
            citation["index"] = mapping.get(str(citation.get("index")), citation.get("index"))
    out["citations"] = list(evidence.values())
    renumber(out, mapping)
    out["evidence_numbering"] = "artifact-v1"
    return out


def capture(context: Any, payload: dict[str, Any], principal: str | None = None) -> dict[str, Any]:
    """Pin indexed evidence at save time; do not fetch external/private bytes."""
    from pheasant.security.acl import expand_principal, is_allowed
    from pheasant.security.identity import effective_principal
    from pheasant.services.artifact_visual_evidence import prepare

    prepare(payload)

    who = effective_principal(principal)
    security = context.config.security
    identities = expand_principal(who, None, security.groups)
    for citation in payload.get("citations") or []:
        if citation.get("snapshot") is not None:
            continue
        chunk = citation.get("chunk_id")
        rows = context.state.rows(
            "SELECT a.id, a.sha256, a.acl, c.text FROM artifacts a "
            "JOIN chunks c ON c.artifact_id=a.id WHERE a.source_id=? AND a.relative_path=? "
            + ("AND c.id=? " if chunk else "")
            + "ORDER BY c.chunk_index LIMIT 8",
            (citation.get("source"), citation.get("path"), *([chunk] if chunk else [])),
        )
        if not rows:
            citation["availability"] = "unavailable"
            continue
        acl = json.loads(rows[0]["acl"]) if rows[0]["acl"] else None
        if acl and not acl.get("public") and payload.get("scope", "org") == "org":
            citation["availability"] = "withheld"
            continue
        if security.acl_enforced and not is_allowed(
            rows[0]["acl"], identities, default_public=security.default_visibility == "public"
        ):
            citation["availability"] = "withheld"
            continue
        citation["revision"] = rows[0]["sha256"]
        citation["node_id"] = rows[0]["id"]
        citation["snapshot"] = "\n\n".join(str(r["text"]) for r in rows)[:8000]
        citation["availability"] = "captured"
    return payload


def file_mapping(payload: dict, name: str) -> dict[str, int]:
    """Original file numbers mapped to the artifact's current evidence numbers."""
    mapping = payload.get("citation_map") or {}
    for turn in payload.get("turns") or []:
        if name in (turn.get("files") or []):
            mapping = turn.get("citation_map") or {}
            break
    content = next(
        (f.get("content", "") for f in payload.get("files") or [] if f.get("name") == name), ""
    )
    cited = set(MARKER.findall(str(content)))
    return {key: value for key, value in mapping.items() if key in cited}
