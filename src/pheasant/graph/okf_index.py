"""What a document's nearest OKF index says about it, read at retrieval.

An OKF bundle's ``index.md`` files list what each folder holds (OKF §8), and a
curator records there what a frontmatter edit has not caught up with: an entry
filed under *Deprecated*, "replaced by …", "needs review", "updated
2026-07-01". The **nearest** index is the one that speaks for a document: the
closest ``index.md`` in its own folder or above, inside its bundle.

* **At indexing** (``graph/okf.py``), every Markdown document in a bundle gets
  one ``indexed_by`` edge to its nearest index, carrying whether that index
  lists it and the entry's own words: title, description, section, and the
  statement :func:`classify` reads in them. No model and no clock, so the edge
  is a pure function of the files and the graph generation stays put.
* **At retrieval** (:func:`annotate`), a hit on such a document follows that one
  edge (a one-hop traversal) and gains an ``okf`` block: the index, what it
  says, the document's own status and ``stale_after``, and a verdict. The
  clock is read here, never stored. The index is the authority when the two
  disagree, and the disagreement is said, not hidden. Answers carry the block
  into the prompt beside the passage (``assistant/chat.build_prompt``).

Only hits on ``.md`` files are looked up, which is all OKF defines, so a region
without bundles pays nothing; one batched read serves every hit of a search.
"""

from __future__ import annotations

import posixpath
import re
from datetime import UTC, datetime
from typing import Any

#: The edge from a bundle document to its nearest index.
EDGE_TYPE = "indexed_by"
RELATION = "nearest_index"
#: The entry text a block quotes, and so what it adds to a prompt.
MAX_QUOTE = 160

#: What an index can say about an entry, strongest first.
_STATEMENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "superseded",
        re.compile(
            r"\b(superseded|replaced)\s+by\b|\buse\b[^\n]{0,60}?\binstead\b|\bsee\b[^\n]{0,60}?\binstead\b",
            re.I,
        ),
    ),
    (
        "deprecated",
        re.compile(
            r"\b(deprecated|retired|obsolete|archived|legacy|end[- ]of[- ]life|sunset)\b"
            r"|\bno longer\s+(used|maintained|valid|supported|current)\b",
            re.I,
        ),
    ),
    (
        "stale",
        re.compile(
            r"\b(stale|outdated|out[- ]of[- ]date|needs?\s+(review|update|updating)"
            r"|under\s+review|pending\s+review|to\s+be\s+updated|unverified)\b",
            re.I,
        ),
    ),
    ("draft", re.compile(r"\b(draft|experimental|work[- ]in[- ]progress|wip|proposed)\b", re.I)),
    ("current", re.compile(r"\b(current|latest|up[- ]to[- ]date|canonical|active)\b", re.I)),
)
_DATE = re.compile(
    r"\b(?:updated|revised|as of|last (?:updated|reviewed|verified)|reviewed|verified)"
    r"\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})",
    re.I,
)


def classify(title: str | None, description: str | None, section: str | None) -> dict[str, Any]:
    """What an index entry states about its target: deterministic, no model.

    The description is read first, then the section heading it sits under,
    then the link text; the strongest statement any of them makes wins
    (superseded, deprecated, stale, draft, current). ``stated_date`` is a date
    the entry gives for the target's last update or review.
    """

    found: str | None = None
    phrase: str | None = None
    for text in (description, section, title):
        if not text:
            continue
        for rank, (statement, pattern) in enumerate(_STATEMENTS):
            match = pattern.search(text)
            if match and (found is None or rank < _rank(found)):
                found, phrase = statement, match.group(0)
    date = None
    for text in (description, title):
        match = _DATE.search(text or "")
        if match:
            date = match.group(1)
            break
    return {"statement": found, "matched": phrase, "stated_date": date}


def _rank(statement: str) -> int:
    return next(index for index, (name, _) in enumerate(_STATEMENTS) if name == statement)


def edge_attrs(
    index_path: str,
    levels: int,
    entry: dict[str, Any] | None,
    listed_via: str | None,
) -> dict[str, Any]:
    """The ``indexed_by`` edge's attributes, from the entry that names the document."""

    entry = entry or {}
    words = classify(entry.get("title"), entry.get("description"), entry.get("section"))
    return {
        "relation": RELATION,
        "index_path": index_path,
        "levels": levels,
        "listed": listed_via is not None,
        "listed_via": listed_via,
        "title": entry.get("title"),
        "description": entry.get("description"),
        "section": entry.get("section"),
        **words,
    }


# ------------------------------------------------------------------ reading


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _last_reviewed(okf: dict[str, Any]) -> str | None:
    """The document's own latest generated/verified date, as written."""

    dates = [str((okf.get("generated") or {}).get("at") or "")]
    dates += [str(v.get("at") or "") for v in okf.get("verified") or [] if isinstance(v, dict)]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def freshness(
    edge: dict[str, Any], okf: dict[str, Any] | None, now: datetime | None = None
) -> dict[str, Any]:
    """The ``okf`` block a hit carries: what its nearest index says, and the verdict."""

    okf = okf or {}
    now = now or datetime.now(UTC)
    says = edge.get("statement")
    status = str(okf.get("status") or "stable") if okf else None
    stale_after = okf.get("stale_after")
    expiry = _parse_time(stale_after)
    past = bool(expiry and now >= expiry)
    reviewed = _last_reviewed(okf)
    stated = edge.get("stated_date")
    newer = bool(stated and reviewed and stated > reviewed[:10])

    if says in {"superseded", "deprecated"}:
        verdict = says
    elif status == "deprecated":
        verdict = "deprecated"
    elif says == "stale" or past or newer:
        verdict = "stale"
    elif says == "draft" or status == "experimental":
        verdict = "draft"
    elif not edge.get("listed"):
        verdict = "unlisted"
    else:
        verdict = "current"
    conflict = bool(
        (says in {"superseded", "deprecated", "stale"} and status == "stable" and okf)
        or (says == "current" and status == "deprecated")
    )

    quote = (
        " — ".join(str(part) for part in (edge.get("section"), edge.get("description")) if part)[
            :MAX_QUOTE
        ]
        or None
    )
    index = edge.get("index_path")
    said = f': "{quote.rstrip(". ")}"' if quote else ""
    if not edge.get("listed"):
        note = f"{index} is this document's nearest index and does not list it."
    elif says:
        note = f"{index} lists it as {says}{said}."
    else:
        note = f"{index} lists it{said}."
    if conflict:
        note += f" The document itself says {status}; the index is newer evidence."
    if past:
        note += f" Its own stale_after ({stale_after}) has passed."
    if newer:
        note += (
            f" The index records an update on {stated}, "
            f"after the document's last review ({reviewed[:10]})."
        )
    return {
        "index": index,
        "levels": edge.get("levels"),
        "listed": bool(edge.get("listed")),
        "index_says": says,
        "index_text": quote,
        "stated_date": stated,
        "status": status,
        "stale_after": stale_after,
        "past_stale_after": past,
        "verdict": verdict,
        "conflict": conflict,
        "note": note,
    }


def collect(graph: Any, artifact_ids: list[str], now: datetime | None = None) -> dict[str, Any]:
    """``{artifact id: okf block}`` for the ids that have a nearest index.

    One batched read of the artifacts' out-edges and attributes on a resident
    or row-backed graph; one call on the graph service's proxy.
    """

    ids = sorted({str(i) for i in artifact_ids if i})
    if graph is None or not ids:
        return {}
    remote = getattr(graph, "remote_okf_index", None)
    if callable(remote):
        return dict(remote(artifact_ids=ids) or {})
    batch = getattr(graph, "out_edges_batch", None)
    edges = batch(ids) if callable(batch) else {i: graph.out_edges(i) for i in ids}
    prefetch = getattr(graph, "prefetch_nodes", None)
    fetched: dict[str, Any] = {}
    if callable(prefetch):
        try:
            fetched = dict(prefetch(ids, materialized=True) or {})
        except Exception:  # noqa: BLE001 - each node is read below instead
            fetched = {}
    blocks: dict[str, Any] = {}
    for artifact_id in ids:
        for _source, _target, edge_map in edges.get(artifact_id) or []:
            edge = next(
                (dict(data) for data in edge_map.values() if data.get("type") == EDGE_TYPE),
                None,
            )
            if edge is None:
                continue
            attrs = fetched.get(artifact_id) or graph.nodes.get(artifact_id) or {}
            blocks[artifact_id] = freshness(edge, attrs.get("okf"), now)
            break
    return blocks


def prompt_note(block: dict[str, Any] | None) -> str:
    """The line an answer's prompt carries under a passage, or ``""``."""

    if not block:
        return ""
    note = block.get("note") or ""
    return f"\nOKF index check: {block.get('verdict')}. {note}".rstrip()


def artifact_of(hit: dict[str, Any]) -> str | None:
    """The file a hit belongs to: its ``artifact_id``, or itself when it is one."""

    artifact = hit.get("artifact_id") or (hit.get("provenance") or {}).get("artifact_id")
    if artifact:
        return str(artifact)
    node = str(hit.get("node_id") or "")
    return node if node.startswith("file:") else None


def annotate(hits: list[dict[str, Any]], graph: Any) -> list[dict[str, Any]]:
    """Give each hit inside an OKF bundle its nearest index's ``okf`` block.

    In place, and returns ``hits``. Best-effort: retrieval never fails for it.
    """

    wanted = {}
    for hit in hits:
        if not str(hit.get("relative_path") or "").lower().endswith(".md"):
            continue
        artifact = artifact_of(hit)
        if artifact:
            wanted.setdefault(artifact, []).append(hit)
    if not wanted:
        return hits
    try:
        blocks = collect(graph, list(wanted))
    except Exception:  # noqa: BLE001 - freshness decorates retrieval, never breaks it
        return hits
    for artifact, block in blocks.items():
        for hit in wanted.get(artifact, []):
            hit["okf"] = block
    return hits


# ----------------------------------------------------------------- planning


def plan_edges(
    members: list[Any], reserved: list[Any], resolve: Any, bundle_root: str
) -> list[tuple[str, str, dict[str, Any]]]:
    """Every bundle document's edge to its nearest ``index.md``, with what it says.

    The nearest index is the closest ``index.md`` in the document's own folder
    or above it, inside the bundle: the listing that speaks for it. The edge
    carries whether that index names the document (by file, or by a folder it
    sits in) and the entry's words, classified by :func:`classify`. Every
    Markdown member gets one, concept or not, except the reserved files and
    repository boilerplate (``README.md`` ...), which the bundle does not speak for.
    ``resolve(target, document)`` is the planner's in-bundle link resolver.
    """

    from pheasant.graph.okf_detect import (
        BOILERPLATE_FILENAMES,
        _ancestors,
        _depth_key,
        _is_external,
        _norm,
        _within,
    )
    from pheasant.ingestion.okf import INDEX_FILENAME, LOG_FILENAME

    indexes = {
        posixpath.dirname(_norm(a.relative_path)): a
        for a in reserved
        if (a.okf or {}).get("role") == "index"
    }
    if not indexes:
        return []
    named: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for directory, index in indexes.items():
        index_path = _norm(index.relative_path)
        files: dict[str, Any] = {}
        folders: dict[str, Any] = {}
        for entry in (index.okf or {}).get("entries") or []:
            raw = str(entry.get("target") or "").split("#", 1)[0].strip()
            if not raw or _is_external(raw):
                continue
            target = resolve(raw, index_path)
            if target is not None:
                resolved = _norm(target.relative_path)
                if posixpath.basename(resolved).lower() == INDEX_FILENAME:
                    folders.setdefault(posixpath.dirname(resolved), entry)
                else:
                    files.setdefault(resolved, entry)
                continue
            if raw.lower().endswith(".md"):
                continue
            base = bundle_root if raw.startswith("/") else directory
            folder = _norm(posixpath.normpath(posixpath.join(base, raw.lstrip("/"))))
            if folder and folder != "." and _within(folder, bundle_root):
                folders.setdefault(folder, entry)
        named[directory] = (files, folders)

    edges: list[tuple[str, str, dict[str, Any]]] = []
    for artifact in members:
        path = _norm(artifact.relative_path)
        name = posixpath.basename(path).lower()
        if not name.endswith(".md"):
            continue
        role = (artifact.okf or {}).get("role")
        if name in {INDEX_FILENAME, LOG_FILENAME} and role in {"index", "log"}:
            continue
        if name in BOILERPLATE_FILENAMES:
            continue  # a repository's README is not the bundle's to speak for
        directory = posixpath.dirname(path)
        chain = [d for d in reversed(_ancestors(directory)) if _within(d, bundle_root)]
        nearest = next((d for d in chain if d in indexes), None)
        if nearest is None:
            continue
        files, folders = named[nearest]
        entry = files.get(path)
        via = "file" if entry is not None else None
        if entry is None:
            holding = [f for f in folders if _within(directory, f) and f != nearest]
            if holding:
                entry, via = folders[max(holding, key=_depth_key)], "folder"
        index = indexes[nearest]
        attrs = edge_attrs(_norm(index.relative_path), chain.index(nearest), entry, via)
        edges.append((artifact.node_id, index.node_id, attrs))
    return edges
