from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from pheasant.graph.simple import SimpleMultiDiGraph

logger = logging.getLogger(__name__)

# Attribute keys that carry no useful search signal (timestamps, opaque hashes,
# foreign keys). Skipping them keeps attribute matches meaningful instead of
# surfacing every node on a stray hex match.
_SKIP_KEYS = {
    "created_at",
    "updated_at",
    "knowledge_base_id",
    "hash",
    "text_hash",
    "sha256",
    "id",
    "key",
}

_STRUCTURED_KEYS = {"label", "name", "qualified_name", "type"}


def search_graph(
    graph: SimpleMultiDiGraph,
    query: str,
    max_results: int = 10,
    source_name: str | None = None,
    include_relationships: bool = True,
    node_index: Any = None,
    wasm_relationship_search: bool = False,
) -> list[dict[str, Any]]:
    """Search across graph nodes, relationships and their attributes.

    Nodes are scored on their label, name, type and remaining attribute values;
    relationships are matched on edge type, endpoint labels and edge attributes.
    Returns result items in the same shape the SQLite store emits so callers can
    merge text and graph hits uniformly.
    """

    remote = getattr(graph, "remote_search", None)
    if callable(remote):
        return remote(
            query=query,
            max_results=max_results,
            source_name=source_name,
            include_relationships=include_relationships,
            wasm_relationship_search=wasm_relationship_search,
        )

    q = query.strip().lower()
    tokens = _tokens(query)
    if not q and not tokens:
        return []

    node_hits: list[tuple[float, dict[str, Any]]] = []
    # One lock hold for the whole scan, iterating live: the previous snapshot
    # copied every node and edge before scoring a single one, which on a
    # 240k-node graph was most of the query's latency.
    # Ask the index which nodes could match. It returns None when it cannot
    # answer (absent, empty, or FTS5 unavailable), and then — and only then —
    # we fall back to scanning every node, which is correct but O(graph).
    # A graph whose nodes live in the same database as the index can resolve
    # both in one statement — the ids never travel. Asked for the way the
    # batch methods are: offered by the backend that has it, absent on the one
    # that does not need it. `resolved is None` means the index could not
    # answer, which is the same signal `candidates` gives.
    candidates = None
    resolved: dict[str, dict[str, Any]] | None = None
    joined = getattr(graph, "search_candidates", None)
    if node_index is not None:
        if callable(joined):
            resolved = joined(node_index, tokens, source_name)
        else:
            candidates = node_index.candidates(tokens, source_name=source_name)

    # One compiled alternation runs the reject inside the regex engine rather
    # than as a Python loop per token per node — on a 400k-node graph that
    # loop overhead was most of what remained of the query.
    matcher = _reject_matcher(tokens, q)
    with graph.reading():
        blobs = graph.search_blobs()
        if resolved is not None:
            walk: Any = resolved.items()
        elif candidates is not None:
            walk = _candidate_nodes(graph, candidates)
        else:
            walk = graph.iter_nodes()
        for node_id, attrs in walk:
            if not attrs:
                continue
            if source_name and attrs.get("source_id") != source_name:
                continue
            # Exact fast reject: scoring can only exceed zero when the query
            # or one of its tokens appears somewhere in the node's text, and
            # the blob is that text prepared once at write time.
            blob = blobs.get(node_id)
            if blob is not None and not _may_match(blob, tokens, q, matcher):
                continue
            score, field = _node_score(attrs, tokens, q)
            if score <= 0:
                continue
            node_hits.append((score, _node_result(node_id, attrs, score, field)))

    node_hits.sort(key=lambda item: (-item[0], str(item[1].get("title") or "")))
    results = [hit for _, hit in node_hits[:max_results]]

    if include_relationships and len(results) < max_results:
        rel_hits = _relationship_hits(graph, tokens, q, source_name, wasm_relationship_search)
        rel_hits.sort(key=lambda item: -item[0])
        for _, hit in rel_hits[: max_results - len(results)]:
            results.append(hit)

    for rank, item in enumerate(results, start=1):
        item["rank"] = rank
    return results


#: Candidate nodes fetched per query when the graph is store-backed. The
#: index hands back everything matching the tokens, which on a corpus with a
#: shared vocabulary is thousands; this bounds the round trips without
#: bounding the answer.
CANDIDATE_BLOCK = 500


def _candidate_nodes(graph: SimpleMultiDiGraph, candidates: Iterable[str]):
    """``(node_id, attrs)`` for the index's candidates, batched where it helps.

    ``graph.nodes.get`` is a dict lookup on the resident graph and a `SELECT`
    on a store-backed one, so the obvious loop is an N+1 that scales with how
    *unselective* the query is: measured **~2,000 single-row queries per
    search** on an 8,000-file corpus, which made the graph arm slower than the
    resident scan it replaced. The scorer reads every attribute of everything
    it is given, so these are asked for *materialized* rather than lazy: a
    lazy mapping is right for a traversal hop, which reads `type` alone, and
    is a dict the store already holds handed over one `__getitem__` at a time
    when the reader wants all of it.
    """

    batch = getattr(graph, "prefetch_nodes", None)
    ids = list(candidates)
    if not callable(batch):
        for node_id in ids:
            yield node_id, (graph.nodes.get(node_id) or {})
        return
    for start in range(0, len(ids), CANDIDATE_BLOCK):
        block = ids[start : start + CANDIDATE_BLOCK]
        found = batch(block, materialized=True)
        for node_id in block:
            attrs = found.get(node_id)
            if attrs is not None:
                yield node_id, attrs


def _reject_matcher(tokens: list[str], q: str) -> re.Pattern[str] | None:
    """Alternation over the query terms, or None when there is nothing to match."""

    terms = [re.escape(term) for term in {*tokens, q} if term]
    if not terms:
        return None
    return re.compile("|".join(terms))


def _may_match(
    blob: str,
    tokens: list[str],
    q: str,
    matcher: re.Pattern[str] | None = None,
) -> bool:
    """Could any field of this node score above zero? Cheap and exact.

    ``_field_score`` returns 0 unless ``q`` or a token is a substring of the
    field, so "nothing matches the whole blob" implies "nothing matches any
    field" — rejecting here can never drop a hit the full scorer would keep.
    """

    if matcher is not None:
        return matcher.search(blob) is not None
    if q and q in blob:
        return True
    return any(token in blob for token in tokens)


def _relationship_hits(
    graph: SimpleMultiDiGraph,
    tokens: list[str],
    q: str,
    source_name: str | None,
    wasm_relationship_search: bool = False,
) -> list[tuple[float, dict[str, Any]]]:
    hits: list[tuple[float, dict[str, Any]]] = []
    with graph.reading():
        if wasm_relationship_search:
            hits.extend(_scan_edges_accelerated(graph, tokens, q, source_name))
        else:
            hits.extend(_scan_edges(graph, tokens, q, source_name))
    return hits


def _scan_edges_accelerated(
    graph: SimpleMultiDiGraph,
    tokens: list[str],
    q: str,
    source_name: str | None,
) -> list[tuple[float, dict[str, Any]]]:
    """Synapse Step 34.5b: WASM-accelerated ``_scan_edges``, pure-Python fallback.

    Opt-in via ``search.wasm_relationship_search`` (caller already checked
    the flag; this function's job is just the try/fallback). Any failure —
    the ``[wasm]`` extra missing, a sandbox error, anything — falls back to
    the pure-Python scan rather than dropping relationship results;
    acceleration is a performance path, never a correctness dependency.
    Caller already holds ``graph.reading()``.
    """
    try:
        from pheasant.sandbox.accel import scan_edges_wasm

        return scan_edges_wasm(graph, tokens, q, source_name)
    except Exception:
        logger.warning(
            "WASM relationship search failed; falling back to pure Python", exc_info=True
        )
        return _scan_edges(graph, tokens, q, source_name)


def _edge_candidates(graph: SimpleMultiDiGraph, tokens: list[str], source_name: str | None):
    """Edges worth scoring — from an index where there is one.

    Node search stopped scanning every node when ``graph_nodes_fts`` arrived;
    relationship search kept doing exactly that, O(edges) per query with no
    index, because the edges only existed inside a resident Python dict. A
    row-backed graph can narrow first, so it is asked to.

    The narrowing is a **superset** of what will score: the scorer below is
    unchanged and still decides. Falling back to the full iteration is
    correct, not degraded — it is what the in-memory graph has always done,
    and its dicts are why it can afford to.
    """

    narrow = getattr(graph, "candidate_edges", None)
    if callable(narrow) and tokens:
        return narrow(tokens, source_name)
    return graph.iter_edges()


def _primed_candidates(graph: SimpleMultiDiGraph, nodes: Any, tokens: list[str], source_name):
    """The candidate edges, with every endpoint's node fetched in one query.

    A store-backed ``node_map()`` can ``prime`` its cache; the resident
    graph's is a dict and needs nothing. Without it each distinct endpoint
    was a query of its own — up to 480 per search, 200-420 ms of the graph
    arm on a 53k-chunk Postgres region. Both the Python and the WASM scan
    read their edges through here.
    """

    edges = _edge_candidates(graph, tokens, source_name)
    prime = getattr(nodes, "prime", None)
    if not callable(prime):
        return edges
    edges = list(edges)
    prime(endpoint for pair, _edges in edges for endpoint in pair)
    return edges


def _scan_edges(
    graph: SimpleMultiDiGraph,
    tokens: list[str],
    q: str,
    source_name: str | None,
) -> list[tuple[float, dict[str, Any]]]:
    """Score every candidate edge. Caller holds ``reading()``."""

    hits: list[tuple[float, dict[str, Any]]] = []
    nodes = graph.node_map()
    candidates = _primed_candidates(graph, nodes, tokens, source_name)
    for (source, target), edge_map in candidates:
        source_label = str((nodes.get(source) or {}).get("label") or source)
        target_label = str((nodes.get(target) or {}).get("label") or target)
        for _key, data in edge_map.items():
            if source_name and data.get("source_id") != source_name:
                continue
            etype = str(data.get("type") or "related")
            type_score = _field_score(etype, tokens, q, 0.65, 0.55, 0.45)
            endpoint_score = max(
                _field_score(source_label, tokens, q, 0.5, 0.4, 0.3),
                _field_score(target_label, tokens, q, 0.5, 0.4, 0.3),
            )
            attr_score = 0.0
            for attr_key, value in data.items():
                if attr_key in _SKIP_KEYS or attr_key == "type":
                    continue
                attr_score = max(
                    attr_score, _field_score(_stringify(value), tokens, q, 0.5, 0.35, 0.3)
                )
            score = max(type_score, endpoint_score, attr_score)
            if score <= 0:
                continue
            hits.append(
                (
                    score,
                    {
                        "kind": "relationship",
                        "node_id": source,
                        "type": "relationship",
                        "edge_type": etype,
                        "source": source,
                        "target": target,
                        "source_label": source_label,
                        "target_label": target_label,
                        "title": f"{source_label} —{etype}→ {target_label}",
                        "label": etype,
                        "score": round(score, 3),
                        "reason": "Graph relationship match",
                        "summary": f"{source_label} {etype} {target_label}",
                    },
                )
            )
    return hits


def _node_result(
    node_id: str, attrs: dict[str, Any], score: float, field: str | None
) -> dict[str, Any]:
    label = str(attrs.get("label") or node_id)
    result = {
        "kind": "node",
        "node_id": node_id,
        "type": attrs.get("type"),
        "title": label,
        "label": label,
        "relative_path": attrs.get("relative_path"),
        "source_id": attrs.get("source_id"),
        "score": round(score, 3),
        "match": field,
        "reason": f"Graph node match on {field}" if field else "Graph node match",
        "summary": _node_summary(attrs),
        "provenance": attrs.get("provenance")
        or {
            "source_id": attrs.get("source_id"),
            "relative_path": attrs.get("relative_path"),
            "path": attrs.get("path"),
        },
    }
    # Which artifact a derived node came from. `chunk`, `symbol` and `entity`
    # nodes have carried this attribute since enrichment first wrote them; it
    # simply never reached a search hit, so nothing downstream could tell which
    # file — or which memory record — a symbol or entity was extracted from.
    #
    # That was the Step 33.6 residual: a superseded memory's entity label
    # survived a validity filter because the hit exposed no identity the policy
    # could resolve. Added only when the attribute exists (artifact nodes *are*
    # the artifact and carry none), so payloads that had no answer are unchanged.
    if attrs.get("artifact_id"):
        result["artifact_id"] = attrs["artifact_id"]
    return result


def _node_summary(attrs: dict[str, Any]) -> str:
    summary = attrs.get("summary")
    if summary:
        return str(summary)[:240]
    relative = attrs.get("relative_path")
    if relative:
        return str(relative)
    return str(attrs.get("label") or "")[:240]


def _node_score(attrs: dict[str, Any], tokens: list[str], q: str) -> tuple[float, str | None]:
    candidates: list[tuple[float, str]] = []
    label = str(attrs.get("label") or "")
    if label:
        candidates.append((_field_score(label, tokens, q, 1.0, 0.9, 0.6), "label"))
    name = str(attrs.get("name") or attrs.get("qualified_name") or "")
    if name:
        candidates.append((_field_score(name, tokens, q, 0.95, 0.85, 0.55), "name"))
    ntype = str(attrs.get("type") or "")
    if ntype and any(token in ntype.lower() for token in tokens):
        candidates.append((0.5, "type"))
    for key, value in attrs.items():
        if key in _SKIP_KEYS or key in _STRUCTURED_KEYS:
            continue
        text = _stringify(value)
        if not text:
            continue
        score = _field_score(text, tokens, q, 0.7, 0.45, 0.3)
        if score > 0:
            candidates.append((score, f"attribute:{key}"))
    if not candidates:
        return 0.0, None
    return max(candidates, key=lambda item: item[0])


def _field_score(
    text: str,
    tokens: list[str],
    q: str,
    base_exact: float,
    base_all: float,
    base_any: float,
) -> float:
    """One tier of the ladder: exact, contains-query, all-tokens, any-token.

    Counted in a single pass rather than as ``all(...)`` then ``any(...)``.
    Those were the same scan twice in the "some but not all" case and, more
    than that, two generator objects created per field — this runs about six
    times per node over 1,836 candidates a query, and the profile put ~1.1M
    generator ``next`` calls plus the ``any``/``all`` frames around them at a
    fifth of the whole graph arm. Measured **10.85µs against 3.66µs** for one
    node's twelve fields. Identical output: duplicate tokens count the same
    both ways, and an empty ``tokens`` still falls through to zero.
    """

    if not text:
        return 0.0
    low = text.lower()
    if q:
        if low == q:
            return base_exact
        if q in low:
            return (base_exact + base_all) / 2
    if not tokens:
        return 0.0
    hits = 0
    for token in tokens:
        if token in low:
            hits += 1
    if hits == len(tokens):
        return base_all
    return base_any if hits else 0.0


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return " ".join(_stringify(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_stringify(item) for item in value.values())
    return str(value)


def _tokens(query: str) -> list[str]:
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9_]{2,}", query.lower()):
        tokens.add(raw)
        for part in re.split(r"[_\W]+", raw):
            if len(part) > 1:
                tokens.add(part)
    return sorted(tokens)
