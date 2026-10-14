"""Relationship search's narrowing over the graph rows.

``search.graph_search`` scores edges on their type, their endpoints' labels and
their attributes; this is the part of that a database can do first, so a query
reads the edges that could score rather than all of them. Split out of
:mod:`.graph_rows` (its methods delegate here) because it is a read for one
caller, not part of the row store's write and fold machinery.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pheasant.persistence.graph_codec import edge_attrs


def candidate_edges(
    store: Any,
    kb_id: str,
    tokens: list[str],
    source_name: str | None = None,
    limit: int = 2000,
) -> Iterator[tuple[tuple[str, str], dict[int, dict]]]:
    """Edges that could possibly score, instead of all of them.

    Relationship search scored every edge — O(edges), on the request path,
    with no index. ``_scan_edges`` matches on the edge's type, its
    endpoints' labels and its attribute values, so the narrowing here is
    the part a database can do: edge type, and endpoints whose node row
    matches. A token that matches nothing returns nothing, which is the
    common case and the one that used to cost the most.

    Deliberately a *superset* of what will score, never a subset: the
    scorer is unchanged and still decides. ``limit`` bounds the work rather
    than the answer — an edge beyond it is one the in-memory scan would
    also have had to reach past ``max_results`` to return.
    """

    if not tokens:
        return
    like = [f"%{token}%" for token in tokens]
    label_clause = " OR ".join("LOWER(n.label) LIKE ?" for _ in like)
    source_clause = " AND e.source_id=?" if source_name else ""
    source_params: list[Any] = [source_name] if source_name else []
    # The type half asks for the types a token appears in, by name. As
    # `LOWER(e.type) LIKE '%token%'` it was a scan of every edge per
    # search — 217 ms of a 310 ms query at 170k edges — to test a
    # vocabulary of ten words, and it usually matched none of them.
    matched = [
        edge_type
        for edge_type in edge_types(store, kb_id)
        if any(token in edge_type.lower() for token in tokens)
    ]
    sql, params = "", []
    if matched:
        clause, type_params = store.state.dialect.in_clause("e.type", matched)
        sql = f"SELECT * FROM graph_edges e WHERE e.kb_id=? AND {clause}{source_clause} UNION "
        params = [kb_id, *type_params, *source_params]
    # DISTINCT because an edge whose source *and* target labels match is
    # joined twice; the UNION used to remove that, and still does when
    # the type half is present.
    sql += (
        "SELECT DISTINCT e.* FROM graph_edges e JOIN graph_nodes n"
        " ON n.kb_id=e.kb_id AND (n.node_id=e.source OR n.node_id=e.target)"
        f" WHERE n.kb_id=? AND ({label_clause}) AND e.kb_id=?{source_clause}"
        " ORDER BY source, target, type, seq LIMIT ?"
    )
    params.extend([kb_id, *like, kb_id, *source_params, int(limit)])
    current: tuple[str, str] | None = None
    parallel: dict[int, dict] = {}
    for row in store.state.rows(sql, tuple(params)):
        pair = (str(row["source"]), str(row["target"]))
        if pair != current:
            if current is not None:
                yield current, parallel
            current, parallel = pair, {}
        parallel[len(parallel)] = edge_attrs(row)
    if current is not None:
        yield current, parallel


def edge_types(store: Any, kb_id: str) -> tuple[str, ...]:
    """Every edge type in the graph, read once per published generation.

    The generation id is content-addressed and moves with any committed
    edge, so keying on it is exact rather than a TTL that could miss a
    type committed a moment ago. Reading it is one primary-key row; the
    ``DISTINCT`` scan it saves costs 30-90 ms at 170k edges.
    """

    published = store.published_generation(kb_id)
    generation = published["generation_id"] if published else ""
    cached = store._edge_types.get(kb_id)
    if cached is not None and cached[0] == generation and generation:
        return cached[1]
    types = tuple(
        sorted(
            str(row["type"])
            for row in store.state.rows(
                "SELECT DISTINCT type FROM graph_edges WHERE kb_id=?", (kb_id,)
            )
            if row["type"] is not None
        )
    )
    store._edge_types[kb_id] = (generation, types)
    return types
