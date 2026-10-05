"""One source, one document, and the links between documents.

``services.inventory`` answers "what is there": the sources, the documents, how
many of each. This module answers the three questions a reader asks next, and
like that one it answers them from the index rather than from a search:

* :func:`source` — one source in detail: where it reads from, its status, its
  documents by type and by top-level directory, the newest of them, and which
  other sources its documents link to and are linked from;
* :func:`document` — one document in detail: its row, its outline, the symbols
  it defines, the documents it links to and the documents that link to it, and
  the references it makes that resolve to nothing this region holds;
* :func:`links` — document-to-document links, summarised per source pair and
  edge type and listed one pair per row, filtered and paged.

A *link* is a graph edge whose two ends are both indexed documents, minus the
structural edges (``contains``, ``indexes``, ``has_chunk``, ``has_heading`` and
git lineage): resolved ``imports``, ``references``, ``embeds``, ``links_to``,
OKF ``derived_from`` and the rest. The edges are drawn at sync time by the
deterministic resolvers (``docs/graph_model.md``), so the answer is the graph's
own, and no part of this calls a model.

The graph may be resident, row-backed or remote. :func:`_outgoing` and
:func:`_incoming` take whatever the serving graph offers (the batch methods
both local backends implement, or the graph service's ``neighbors_many``)
and fall back to a scan when nothing better exists. Incoming edges are a seek
on the row backend's target index, one in-process pass over the edge keys on
the resident graph, and a scan of every document's out-edges through the graph
service. The last is slow and correct, which is the right way round.

What the listings leave out is what ``services.inventory`` leaves out: memory
records, internal and removed sources, and, under ``security.acl_enforced``,
every document the caller may not read. A link is shown only when the caller
may read *both* ends, because naming the far end of a link names the document.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from pheasant.ingestion.content_types import MEMORY_ARTIFACT_TYPE
from pheasant.services import ServiceContext
from pheasant.services.errors import DocumentNotFound, InvalidRequest, SourceNotFound
from pheasant.services.inventory import (
    ARTIFACT_SCOPE,
    MAX_EXTENSIONS,
    document_row,
    extension_of,
    reader,
)

#: Edges that describe where a document sits rather than what it points at.
STRUCTURAL_EDGES = frozenset(
    {"contains", "indexes", "has_chunk", "has_heading", "belongs_to_branch", "at_commit"}
)
#: Link rows one call may return. Page with ``offset`` past it.
MAX_LINKS = 500
#: Top-level directories a source description names before folding the rest.
MAX_DIRECTORIES = 12
#: Headings, symbols, links and references a document description lists of each.
MAX_DETAIL_ITEMS = 100
#: Graph nodes asked about per batch call.
_BLOCK = 500
_COLUMNS = "a.id, a.source_id, a.relative_path, a.type, a.size_bytes, a.last_indexed_at"


@dataclass(frozen=True)
class LinksRequest:
    """Which document-to-document links to list."""

    knowledge_base: str | None = None
    #: Links with at least one end in this source.
    source_name: str | None = None
    #: With ``source_name``: links between the two sources, either direction.
    other_source: str | None = None
    #: Keep only these edge types (``imports``, ``references``, ...).
    edge_types: list[str] = field(default_factory=list)
    #: Keep only links whose two ends are in different sources.
    cross_source_only: bool = False
    #: Links touching one document (any path :func:`document` accepts, which
    #: must name exactly one), and which way: ``in`` (links *to* it), ``out``
    #: (links *from* it) or ``None`` (both). Pages a document's backlinks.
    document: str | None = None
    direction: str | None = None
    limit: int = 50
    offset: int = 0
    principal: str | None = None
    principal_groups: list[str] | None = None


# ---------------------------------------------------------------------------
# One source
# ---------------------------------------------------------------------------


def source(
    context: ServiceContext,
    name: str,
    knowledge_base: str | None = None,
    principal: str | None = None,
    principal_groups: list[str] | None = None,
) -> dict[str, Any]:
    """One source in detail: what it reads, what it holds, what it links to."""

    kb_id = context.knowledge_base(knowledge_base)
    row = _registered(context, name)
    source_name = str(row["name"])
    everything = _artifacts(context, principal, principal_groups)
    own = [r for r in everything if str(r["source_id"]) == source_name]

    extensions: Counter[str] = Counter(extension_of(r["relative_path"]) for r in own)
    common = sorted(extensions.items(), key=lambda item: (-item[1], item[0]))[:MAX_EXTENSIONS]
    by_extension = [{"extension": ext, "documents": count} for ext, count in common]
    folded = len(own) - sum(count for _, count in common)
    if folded:
        by_extension.append({"extension": "other", "documents": folded})

    directories: Counter[str] = Counter(_top_directory(r["relative_path"]) for r in own)
    ranked = sorted(directories.items(), key=lambda item: (-item[1], item[0]))
    by_directory = [
        {"directory": directory, "documents": count}
        for directory, count in ranked[:MAX_DIRECTORIES]
    ]
    if len(ranked) > MAX_DIRECTORIES:
        rest = sum(count for _, count in ranked[MAX_DIRECTORIES:])
        by_directory.append({"directory": "other", "documents": rest})

    recent = sorted(
        (r for r in own if r["last_indexed_at"]),
        key=lambda r: (str(r["last_indexed_at"]), str(r["relative_path"])),
        reverse=True,
    )[:5]
    links = _source_links(context, source_name, own, everything)
    settings = _settings(row)
    return {
        "knowledge_base": kb_id,
        "source": {
            "name": source_name,
            "type": row.get("type"),
            "enabled": bool(row.get("enabled")),
            "status": row.get("last_status"),
            "location": _location(row, settings),
            "description": settings.get("description"),
            "last_indexed_at": row.get("last_indexed_at")
            or max((str(r["last_indexed_at"]) for r in own if r["last_indexed_at"]), default=None),
        },
        "totals": {
            "documents": len(own),
            "size_bytes": sum(int(r["size_bytes"] or 0) for r in own),
        },
        "by_extension": by_extension,
        "by_directory": by_directory,
        "recent": [document_row(r) for r in recent],
        "links": links,
    }


def _source_links(
    context: ServiceContext, name: str, own: list[Any], everything: list[Any]
) -> dict[str, Any] | None:
    """Link counts out of and into one source, per other source and edge type."""

    if context.graph is None:
        return None
    by_id = {str(r["id"]): r for r in everything}
    own_ids = [str(r["id"]) for r in own]
    pairs = _pairs(
        [
            *_outgoing(context.graph, own_ids),
            *_incoming(context.graph, own_ids, everyone=list(by_id)),
        ],
        by_id,
    )
    outgoing: Counter[tuple[str, str]] = Counter()
    incoming: Counter[tuple[str, str]] = Counter()
    for (src, tgt), types in pairs.items():
        src_source, tgt_source = str(by_id[src]["source_id"]), str(by_id[tgt]["source_id"])
        for edge_type in types:
            if src_source == name:
                outgoing[(tgt_source, edge_type)] += 1
            if tgt_source == name and src_source != name:
                incoming[(src_source, edge_type)] += 1

    def rows(counter: Counter[tuple[str, str]]) -> list[dict[str, Any]]:
        return [
            {"source": other, "edge_type": edge_type, "links": count}
            for (other, edge_type), count in sorted(
                counter.items(), key=lambda item: (-item[1], item[0])
            )
        ]

    return {"outgoing": rows(outgoing), "incoming": rows(incoming)}


# ---------------------------------------------------------------------------
# One document
# ---------------------------------------------------------------------------


def document(
    context: ServiceContext,
    path: str,
    source_name: str | None = None,
    knowledge_base: str | None = None,
    principal: str | None = None,
    principal_groups: list[str] | None = None,
) -> dict[str, Any]:
    """One document in detail, found by path.

    ``path`` is a relative path, ``<source>/<relative path>``, a tail of one
    (``deploy.md``, ``runbooks/rotation.md``) or, last, any part of one
    (``rotation``), matched without regard to case; the first reading that
    finds anything wins. When it names several documents the answer is the
    candidates, ``document: null``, rather than a guess. When it names none
    that the caller may read, the refusal is ``UNKNOWN_DOCUMENT``.
    """

    kb_id = context.knowledge_base(knowledge_base)
    wanted = _clean_path(path)
    if not wanted:
        raise InvalidRequest("path must name a document")
    if source_name:
        _registered(context, source_name)
    found = _find(context, wanted, source_name, principal, principal_groups)
    if not found:
        raise DocumentNotFound(path)
    if len(found) > 1:
        return {
            "knowledge_base": kb_id,
            "query": wanted,
            "document": None,
            "candidates": [document_row(r) for r in found[:MAX_DETAIL_ITEMS]],
            "total_candidates": len(found),
        }
    row = found[0]
    artifact_id = str(row["id"])
    detail = document_row(row)
    detail["sha256"] = row["sha256"]
    detail["mime_type"] = row["mime_type"]
    chunks = int(
        context.state.rows("SELECT COUNT(*) AS n FROM chunks WHERE artifact_id=?", (artifact_id,))[
            0
        ]["n"]
    )
    return {
        "knowledge_base": kb_id,
        "query": wanted,
        "document": detail,
        "chunks": chunks,
        "outline": _outline(context, artifact_id),
        "symbols": _symbols(context, artifact_id),
        **_document_links(context, artifact_id, principal, principal_groups),
    }


def _find(
    context: ServiceContext,
    wanted: str,
    source_name: str | None,
    principal: str | None,
    groups: list[str] | None,
) -> list[Any]:
    """The readable documents ``wanted`` names, most exact reading first."""

    admit = reader(context, principal, groups)
    acl = ", a.acl" if admit is not None else ""
    base = f"SELECT {_COLUMNS}, a.sha256, a.mime_type{acl} FROM artifacts a WHERE {ARTIFACT_SCOPE}"
    lowered = wanted.lower()
    attempts: list[tuple[str, tuple[Any, ...]]] = []
    if source_name:
        attempts.append(
            (" AND a.source_id = ? AND LOWER(a.relative_path) = ?", (source_name, lowered))
        )
        attempts.append(
            (
                " AND a.source_id = ? AND LOWER(a.relative_path) LIKE ? ESCAPE '\\'",
                (source_name, f"%/{_escape_like(lowered)}"),
            )
        )
    else:
        attempts.append((" AND LOWER(a.relative_path) = ?", (lowered,)))
        head, _, rest = wanted.partition("/")
        if rest:
            attempts.append(
                (" AND a.source_id = ? AND LOWER(a.relative_path) = ?", (head, rest.lower()))
            )
        attempts.append(
            (" AND LOWER(a.relative_path) LIKE ? ESCAPE '\\'", (f"%/{_escape_like(lowered)}",))
        )
    # Last, and loosest: any path containing it ("rotation" finds
    # `runbooks/rotation.md`). Several matches come back as candidates.
    attempts.append(
        (
            " AND LOWER(a.relative_path) LIKE ? ESCAPE '\\'"
            + (" AND a.source_id = ?" if source_name else ""),
            (f"%{_escape_like(lowered)}%", *([source_name] if source_name else [])),
        )
    )
    for clause, params in attempts:
        rows = context.state.rows(
            f"{base}{clause} ORDER BY a.source_id, a.relative_path",
            (MEMORY_ARTIFACT_TYPE, *params),
        )
        if admit is not None:
            rows = admit(rows)
        if rows:
            return list(rows)
    return []


def _outline(context: ServiceContext, artifact_id: str) -> dict[str, Any]:
    """The document's section headings, in reading order, from its chunks."""

    rows = context.state.rows(
        "SELECT heading_path, MIN(chunk_index) AS first FROM chunks "
        "WHERE artifact_id=? AND heading_path IS NOT NULL AND heading_path <> '' "
        "GROUP BY heading_path ORDER BY first, heading_path",
        (artifact_id,),
    )
    headings = [str(row["heading_path"]) for row in rows]
    return {"headings": headings[:MAX_DETAIL_ITEMS], "total": len(headings)}


def _symbols(context: ServiceContext, artifact_id: str) -> dict[str, Any]:
    rows = context.state.rows(
        "SELECT symbol_type, name, qualified_name, start_line, language FROM symbols "
        "WHERE artifact_id=? ORDER BY start_line, qualified_name, name",
        (artifact_id,),
    )
    listed = [
        {
            "kind": row["symbol_type"],
            "name": row["qualified_name"] or row["name"],
            "line": row["start_line"],
            "language": row["language"],
        }
        for row in rows[:MAX_DETAIL_ITEMS]
    ]
    return {"items": listed, "total": len(rows)}


def _document_links(
    context: ServiceContext, artifact_id: str, principal: str | None, groups: list[str] | None
) -> dict[str, Any]:
    if context.graph is None:
        return {"links_to": None, "linked_from": None, "unresolved_references": None}
    graph = context.graph
    out = [(s, t, types) for s, t, types in _outgoing(graph, [artifact_id]) if types]
    if callable(getattr(graph, "in_edges_batch", None)):
        into = list(_incoming(graph, [artifact_id], everyone=[]))
    else:
        everyone = [str(r["id"]) for r in _artifacts(context, principal, groups)]
        into = list(_incoming(graph, [artifact_id], everyone=everyone))
    endpoints = {t for _s, t, _ in out} | {s for s, _t, _ in into} | {artifact_id}
    readable = _rows_for(context, sorted(endpoints), principal, groups)
    own_source = str(readable[artifact_id]["source_id"]) if artifact_id in readable else None

    def listed(pairs: dict[tuple[str, str], set[str]], far: int) -> list[dict[str, Any]]:
        entries = [
            {
                "document": _short(readable[pair[far]]),
                "edge_types": sorted(types),
                "cross_source": str(readable[pair[far]]["source_id"]) != own_source,
            }
            for pair, types in pairs.items()
        ]
        entries.sort(key=lambda e: (e["document"]["source"], e["document"]["path"]))
        return entries

    links_to = listed(_pairs(out, readable), far=1)
    linked_from = listed(_pairs(into, readable), far=0)

    # Out-edges to nodes that are not documents: what the text refers to that
    # this region does not hold (a package, a URL, a page never indexed).
    stubs = sorted({t for _s, t, types in out if t not in readable and types & _REFERENCE_EDGES})
    resolved = _resolved_references(graph, artifact_id)
    unresolved = sorted(
        {
            (str(attrs.get("reference_type") or "reference"), str(attrs.get("reference") or label))
            for node_id, attrs in _node_attributes(graph, stubs).items()
            if attrs
            and attrs.get("type") == "external_reference"
            and (label := str(attrs.get("label") or node_id))
            and (attrs.get("reference_type"), attrs.get("reference")) not in resolved
        }
    )
    unresolved_items = [{"reference": ref, "reference_type": kind} for kind, ref in unresolved]
    return {
        "links_to": {"items": links_to[:MAX_DETAIL_ITEMS], "total": len(links_to)},
        "linked_from": {"items": linked_from[:MAX_DETAIL_ITEMS], "total": len(linked_from)},
        "unresolved_references": {
            "items": unresolved_items[:MAX_DETAIL_ITEMS],
            "total": len(unresolved_items),
        },
    }


_REFERENCE_EDGES = frozenset({"references", "imports", "embeds", "links_to"})


def _resolved_references(graph: Any, artifact_id: str) -> set[tuple[Any, Any]]:
    """``(reference_type, reference)`` of every reference the resolvers resolved.

    Each link draws two edges: one to its ``external_reference`` stub at
    enrichment, and one to the document it names once the resolver found it,
    carrying the same ``reference``. A stub whose reference resolved is not
    unresolved. The graph service hands back no edge attributes, so there
    every stub is listed, which overstates and never hides.
    """

    batch = getattr(graph, "out_edges_batch", None)
    if not callable(batch):
        return set()
    found: set[tuple[Any, Any]] = set()
    for entries in batch([artifact_id]).values():
        for _src, _tgt, edge_map in entries:
            for data in edge_map.values():
                if data and data.get("enrichment_pass") and data.get("reference"):
                    found.add((data.get("reference_type"), data.get("reference")))
    return found


# ---------------------------------------------------------------------------
# Links between documents
# ---------------------------------------------------------------------------


def links(context: ServiceContext, request: LinksRequest) -> dict[str, Any]:
    """Document-to-document links, summarised per source pair and listed per pair."""

    kb_id = context.knowledge_base(request.knowledge_base)
    limit = max(1, min(int(request.limit), MAX_LINKS))
    offset = max(0, int(request.offset))
    for name in (request.source_name, request.other_source):
        if name:
            _registered(context, name)
    if request.other_source and not request.source_name:
        raise InvalidRequest("other_source needs source_name: links between which two sources?")
    if request.direction not in (None, "in", "out"):
        raise InvalidRequest(f"direction must be in or out; got {request.direction!r}")
    if request.direction and not request.document:
        raise InvalidRequest("direction needs document: links into or out of which document?")
    anchor = None
    if request.document:
        found = _find(
            context,
            _clean_path(request.document),
            request.source_name,
            request.principal,
            request.principal_groups,
        )
        if not found:
            raise DocumentNotFound(request.document)
        if len(found) > 1:
            named = ", ".join(f"{r['source_id']}/{r['relative_path']}" for r in found[:5])
            raise InvalidRequest(
                f"document {request.document!r} matches {len(found)} documents ({named}"
                f"{', …' if len(found) > 5 else ''}); name one"
            )
        anchor = str(found[0]["id"])
    wanted_types = {str(t).strip().lower() for t in request.edge_types or [] if str(t).strip()}
    filters = {
        "source_name": request.source_name,
        "other_source": request.other_source,
        "edge_types": sorted(wanted_types),
        "cross_source_only": bool(request.cross_source_only),
        "document": anchor,
        "direction": request.direction,
    }
    if context.graph is None:
        return {
            "knowledge_base": kb_id,
            "available": False,
            "summary": [],
            "links": [],
            "total": 0,
            "pagination": _pagination(limit, offset, 0, 0),
            "filters": filters,
        }

    everything = _artifacts(context, request.principal, request.principal_groups)
    by_id = {str(r["id"]): r for r in everything}
    a, b = request.source_name, request.other_source
    if anchor:
        # The document's own source is the scope it was found in, not a filter
        # on the far end: "links to deploy.md" includes links from code.
        a = b = None
        edges: Iterable[tuple[str, str, set[str]]] = [
            *(_outgoing(context.graph, [anchor]) if request.direction != "in" else []),
            *(
                _incoming(context.graph, [anchor], everyone=list(by_id))
                if request.direction != "out"
                else []
            ),
        ]
    elif a and b:
        scope = [i for i, r in by_id.items() if str(r["source_id"]) in {a, b}]
        edges = _outgoing(context.graph, scope)
    elif a:
        scope = [i for i, r in by_id.items() if str(r["source_id"]) == a]
        edges = [
            *_outgoing(context.graph, scope),
            *_incoming(context.graph, scope, everyone=list(by_id)),
        ]
    else:
        edges = _outgoing(context.graph, list(by_id))
    pairs = _pairs(edges, by_id)

    kept: list[dict[str, Any]] = []
    summary: Counter[tuple[str, str, str]] = Counter()
    for (src, tgt), types in pairs.items():
        if anchor and anchor not in (src, tgt):
            continue
        from_row, to_row = by_id[src], by_id[tgt]
        from_source, to_source = str(from_row["source_id"]), str(to_row["source_id"])
        if a and b and {from_source, to_source} != {a, b}:
            continue
        if a and not b and a not in (from_source, to_source):
            continue
        cross = from_source != to_source
        if request.cross_source_only and not cross:
            continue
        if wanted_types:
            types = types & wanted_types
            if not types:
                continue
        for edge_type in types:
            summary[(from_source, to_source, edge_type)] += 1
        kept.append(
            {
                "from": _short(from_row),
                "to": _short(to_row),
                "edge_types": sorted(types),
                "cross_source": cross,
            }
        )
    kept.sort(
        key=lambda e: (e["from"]["source"], e["from"]["path"], e["to"]["source"], e["to"]["path"])
    )
    page = kept[offset : offset + limit]
    return {
        "knowledge_base": kb_id,
        "available": True,
        "summary": [
            {"from_source": f, "to_source": t, "edge_type": e, "links": n}
            for (f, t, e), n in sorted(summary.items(), key=lambda item: (-item[1], item[0]))
        ],
        "links": page,
        "total": len(kept),
        "pagination": _pagination(limit, offset, len(page), len(kept)),
        "filters": filters,
    }


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def _registered(context: ServiceContext, name: str) -> dict[str, Any]:
    """The registry row for ``name``, refusing internal, removed and unknown sources."""

    row = context.state.get_source(name) if name else None
    if not row or str(row.get("name") or "").startswith("__"):
        raise SourceNotFound(name)
    removed = context.state.rows(
        "SELECT 1 AS gone FROM removed_sources WHERE source_id=?", (str(row["id"]),)
    )
    if removed:
        raise SourceNotFound(name)
    return row


def _artifacts(
    context: ServiceContext, principal: str | None, groups: list[str] | None
) -> list[Any]:
    """Every listable document the caller may read: one narrow scan."""

    admit = reader(context, principal, groups)
    acl = ", a.acl" if admit is not None else ""
    rows = context.state.rows(
        f"SELECT {_COLUMNS}{acl} FROM artifacts a WHERE {ARTIFACT_SCOPE}",
        (MEMORY_ARTIFACT_TYPE,),
    )
    return list(admit(rows) if admit is not None else rows)


def _rows_for(
    context: ServiceContext, ids: list[str], principal: str | None, groups: list[str] | None
) -> dict[str, Any]:
    """``{id: row}`` for the listable, readable documents among ``ids``."""

    if not ids:
        return {}
    admit = reader(context, principal, groups)
    acl = ", a.acl" if admit is not None else ""
    found: dict[str, Any] = {}
    for start in range(0, len(ids), _BLOCK):
        block = ids[start : start + _BLOCK]
        clause, params = context.state.dialect.in_clause("a.id", block)
        rows = context.state.rows(
            f"SELECT {_COLUMNS}{acl} FROM artifacts a WHERE {ARTIFACT_SCOPE} AND {clause}",
            (MEMORY_ARTIFACT_TYPE, *params),
        )
        for row in admit(rows) if admit is not None else rows:
            found[str(row["id"])] = row
    return found


def _types(edge_map: Any) -> set[str]:
    values = edge_map.values() if hasattr(edge_map, "values") else edge_map
    found = {str(data.get("type")) for data in values if data and data.get("type")}
    return found - STRUCTURAL_EDGES


def _outgoing(graph: Any, ids: list[str]) -> Iterator[tuple[str, str, set[str]]]:
    """``(source, target, link types)`` for every out-edge of ``ids``."""

    batch = getattr(graph, "out_edges_batch", None)
    if callable(batch):
        for start in range(0, len(ids), _BLOCK):
            for entries in batch(ids[start : start + _BLOCK]).values():
                for src, tgt, edge_map in entries:
                    yield str(src), str(tgt), _types(edge_map)
        return
    remote = getattr(graph, "remote_neighbors_many", None)
    if callable(remote):
        for answer in remote(list(ids), depth=1):
            src = str(answer["node_id"])
            for item in answer.get("neighbors") or []:
                if int(item.get("depth") or 1) == 1:
                    types = {str(t) for t in item.get("edge_types") or []} - STRUCTURAL_EDGES
                    yield src, str(item["node_id"]), types
        return
    for node_id in ids:  # a graph-shaped object with only the one-node primitive
        for src, tgt, edge_map in graph.out_edges(node_id):
            yield str(src), str(tgt), _types(edge_map)


def _incoming(
    graph: Any, ids: list[str], *, everyone: list[str]
) -> Iterator[tuple[str, str, set[str]]]:
    """``(source, target, link types)`` for every in-edge of ``ids``.

    Off the graph's own in-edge lookup when it has one; otherwise by reading
    the out-edges of ``everyone`` and keeping the ones that land in ``ids``.
    """

    batch = getattr(graph, "in_edges_batch", None)
    if callable(batch):
        for start in range(0, len(ids), _BLOCK):
            for entries in batch(ids[start : start + _BLOCK]).values():
                for src, tgt, edge_map in entries:
                    yield str(src), str(tgt), _types(edge_map)
        return
    wanted = set(ids)
    for src, tgt, types in _outgoing(graph, everyone):
        if tgt in wanted:
            yield src, tgt, types


def _pairs(
    edges: Iterable[tuple[str, str, set[str]]], documents: dict[str, Any]
) -> dict[tuple[str, str], set[str]]:
    """Document-to-document pairs and their link types; self-links dropped."""

    pairs: dict[tuple[str, str], set[str]] = {}
    for src, tgt, types in edges:
        if types and src != tgt and src in documents and tgt in documents:
            pairs.setdefault((src, tgt), set()).update(types)
    return pairs


def _node_attributes(graph: Any, ids: list[str]) -> dict[str, Any]:
    if not ids:
        return {}
    batch = getattr(graph, "prefetch_nodes", None)
    if callable(batch):
        found: dict[str, Any] = {}
        for start in range(0, len(ids), _BLOCK):
            found.update(batch(ids[start : start + _BLOCK], materialized=True))
        return found
    return {node_id: graph.nodes.get(node_id) for node_id in ids}


def _short(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "source": row["source_id"], "path": row["relative_path"]}


def _pagination(limit: int, offset: int, returned: int, total: int) -> dict[str, Any]:
    return {
        "limit": limit,
        "offset": offset,
        "returned": returned,
        "has_more": offset + returned < total,
    }


def _top_directory(path: Any) -> str:
    parts = PurePosixPath(str(path or "")).parts
    return f"{parts[0]}/" if len(parts) > 1 else "(top level)"


def _settings(row: dict[str, Any]) -> dict[str, Any]:
    try:
        settings = json.loads(row.get("config_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return settings if isinstance(settings, dict) else {}


def _location(row: dict[str, Any], settings: dict[str, Any]) -> str | None:
    urls = settings.get("urls")
    if isinstance(urls, list) and urls:
        return f"{len(urls)} URL{'s' if len(urls) != 1 else ''}"
    path = str(row.get("path") or "")
    return None if not path or path == "/unused" else path


def _clean_path(path: Any) -> str:
    text = str(path or "").strip().strip("`'\"“”").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
