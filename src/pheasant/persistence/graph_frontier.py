"""A BFS frontier's out-edges as rows, ranked and, when bounded, read in index order.

Split out of :mod:`.graph_rows` (``GraphRowStore.out_edges`` delegates here)
because the statements are the whole subject: which pairs of a source a walk
sees first, and how much of a hub the database reads to say so.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Any

#: What a walk reads of an edge it keeps: the key, which the primary key
#: index already holds. ``source_id`` and the attribute blob live in the heap,
#: and a hub's edges are spread across it by the order pages were indexed,
#: so reading them cost one random page per kept edge -- ~9,000 of the 13,465
#: pages one 7-hub fetch touched. A walk looks at nothing but ``type``.
_KEY_COLUMNS = "e.kb_id, e.source, e.target, e.type, e.seq, NULL AS source_id, NULL AS attrs"

#: An edge type spelled into the statement rather than bound: a partial index
#: (`derived_indexes._graph_edges_hierarchy`) is only used when the query's
#: predicate visibly implies the index's, which a parameter never does.
_LITERAL_TYPE = re.compile(r"^[a-z_]+$")


def ranked_frontier(where: str, priority_types: Sequence[str], limited: bool) -> str:
    """The frontier statement, ranked per source so a ``LIMIT`` is meaningful.

    ``MIN(CASE …) OVER (PARTITION BY source, target)`` is the per-*pair*
    priority — 0 when any edge of the pair carries a priority type — and
    ``DENSE_RANK`` then numbers pairs within each source, so ``pr <= N`` is
    "the first N pairs this source would have offered". Both halves are
    ordinary SQL:2003 window functions, present in SQLite since 3.25 and in
    every Postgres this supports.

    Used whenever a caller asks for the order, ``limited`` or not. Asking for
    priority order and getting insertion order back on the unbounded path is
    the bug this shape was introduced with: the traversal stopped re-sorting
    once it could ask, so an unordered answer silently changed which
    neighbours a *filtered* walk returned — and filtered walks are exactly the
    ones that lift the limit. A promise with an exception is not a promise.

    The bounded walk with no ``targets`` no longer comes here: the window has
    to read a source's whole fan-out before it can number it, which is what
    :func:`bounded_rows` exists to avoid.

    Written as text rather than composed because the shape is fixed; the only
    variable is how many priority types there are, and those are parameters.
    """

    if priority_types:
        placeholders = ",".join("?" for _ in priority_types)
        priority = f"MIN(CASE WHEN type IN ({placeholders}) THEN 0 ELSE 1 END)"
    else:
        priority = "MIN(0)"
    ranked = (
        f"SELECT *, {priority} OVER (PARTITION BY source, target) AS rank_group"
        f" FROM graph_edges WHERE {where}"
    )
    order = " ORDER BY source, rank_group, target, type, seq"
    if not limited:
        # Ordering alone needs no numbering. The unbounded path is the
        # *filtered* walk, which is already the expensive one, so paying for a
        # DENSE_RANK nothing reads there would be a tax on the case that can
        # least afford it.
        return f"WITH ranked AS ({ranked}) SELECT * FROM ranked{order}"
    return (
        f"WITH ranked AS ({ranked}),"
        " numbered AS (SELECT *, DENSE_RANK() OVER"
        " (PARTITION BY source ORDER BY rank_group, target) AS pair_rank FROM ranked)"
        f" SELECT * FROM numbered WHERE pair_rank <= ?{order}"
    )


def bounded_rows(
    state: Any,
    kb_id: str,
    sources: list[str],
    priority_types: Sequence[str],
    limit: int,
    keys_only: bool = False,
) -> Iterator[Any]:
    """The edges of each source's first ``limit`` pairs, read in index order.

    The same pairs, in the same order, as :func:`ranked_frontier` with a
    limit, without reading what is not kept. The window there numbers a
    source's pairs only after reading all of them, so a bounded walk paid for
    a hub's whole fan-out: an entity every page of a 25k-page web source
    mentions is 24,970 edges, read with their attributes and sorted to keep
    213, once per entity per walk. On the Azure fleet a two-hop answer
    expansion through eleven of those took longer than its 10 s budget.

    Here a source contributes two scans of the primary key, which is ordered
    ``(kb_id, source, target, …)``, and each stops after ``limit`` distinct
    targets: the first priority targets (those with an edge of a priority
    type) and the first targets of any kind. Their union holds the answer.
    The first ``limit`` priority targets are by definition the priority
    targets the ranking keeps; and when it keeps fewer than ``limit`` of
    them, the non-priority targets it adds are a prefix of the source's
    targets in key order, which the second scan has read at least that far.
    A target the second scan reads that is also a priority one is a priority
    target ahead of the ``limit``-th target, so the first scan read it too
    and ``MIN`` ranks it 0. Only then are the kept pairs' edges read.

    One statement per frontier batch on Postgres (``LATERAL``); SQLite has
    no ``LATERAL``, so it runs the same per-source statement once per source,
    in process, where a statement costs microseconds rather than a round trip.

    ``keys_only`` returns each kept edge's key and ``type`` without its
    ``source_id`` or attributes (see :data:`_KEY_COLUMNS`), for a caller that
    reads nothing else -- the bounded walk.
    """

    dialect = state.dialect
    columns = _KEY_COLUMNS if keys_only else "e.*"
    if dialect.is_postgres:
        yield from _postgres_rows(state, kb_id, sources, priority_types, limit, columns)
        return
    statement, extra = _source_statement(dialect, "?", priority_types, columns)
    for source in sources:
        params = [*_arm_params(kb_id, priority_types, limit, source, extra), limit, kb_id, source]
        yield from state.rows(statement, tuple(params))


def _arms(dialect: Any, source_ref: str, priority_types: Sequence[str]) -> tuple[str, list[Any]]:
    """The two index-ordered target scans of one source, as one ``UNION ALL``."""

    every = (
        "SELECT * FROM (SELECT DISTINCT target, {rank} AS rank_group FROM graph_edges"
        f" WHERE kb_id = ? AND source = {source_ref} ORDER BY target LIMIT ?) AS every_target"
    )
    if not priority_types:
        return every.format(rank=0), []
    clause, values = _type_clause(dialect, priority_types)
    first = (
        "SELECT * FROM (SELECT DISTINCT target, 0 AS rank_group FROM graph_edges"
        f" WHERE kb_id = ? AND source = {source_ref} AND {clause}"
        " ORDER BY target LIMIT ?) AS priority_target"
    )
    return f"{first} UNION ALL {every.format(rank=1)}", values


def _type_clause(dialect: Any, priority_types: Sequence[str]) -> tuple[str, list[Any]]:
    """The priority-type predicate, literal when it safely can be."""

    types = list(priority_types)
    if not all(_LITERAL_TYPE.match(str(value)) for value in types):
        return dialect.in_clause("type", types)
    if len(types) == 1:
        return f"type = '{types[0]}'", []
    return "type IN (" + ",".join(f"'{value}'" for value in types) + ")", []


def _arm_params(
    kb_id: str,
    priority_types: Sequence[str],
    limit: int,
    source: str | None,
    type_values: list[Any],
) -> list[Any]:
    """Parameters for :func:`_arms`, ``source`` omitted when it is a column."""

    def scan(*middle: Any) -> list[Any]:
        return [kb_id, *([source] if source is not None else []), *middle, limit]

    if not priority_types:
        return scan()
    return [*scan(*type_values), *scan()]


def _source_statement(
    dialect: Any, source_ref: str, priority_types: Sequence[str], columns: str
) -> tuple[str, list[Any]]:
    arms, values = _arms(dialect, source_ref, priority_types)
    statement = (
        f"SELECT {columns}, p.rank_group FROM ("
        " SELECT c.target, MIN(c.rank_group) AS rank_group"
        f" FROM ({arms}) AS c GROUP BY c.target"
        " ORDER BY MIN(c.rank_group), c.target LIMIT ?"
        ") AS p JOIN graph_edges e"
        " ON e.kb_id = ? AND e.source = ? AND e.target = p.target"
        " ORDER BY p.rank_group, p.target, e.type, e.seq"
    )
    return statement, values


def _postgres_rows(
    state: Any,
    kb_id: str,
    sources: list[str],
    priority_types: Sequence[str],
    limit: int,
    columns: str,
) -> Iterator[Any]:
    # The edge join lives *inside* the lateral subquery. Beside it, the planner
    # was free to join a source to all of its edges first and re-run the pair
    # selection once per edge: with statistics taken while the table was
    # small, one 11-hub frontier ran for over an hour on a local Postgres.
    # Nested, the pairs are chosen once per source and only their edges read.
    arms, values = _arms(state.dialect, "s.source", priority_types)
    statement = (
        "SELECT x.* FROM unnest(CAST(? AS text[])) AS s(source)"
        " CROSS JOIN LATERAL ("
        f" SELECT {columns}, p.rank_group FROM ("
        " SELECT c.target, MIN(c.rank_group) AS rank_group"
        f" FROM ({arms}) AS c GROUP BY c.target"
        " ORDER BY MIN(c.rank_group), c.target LIMIT ?"
        ") AS p JOIN graph_edges e"
        " ON e.kb_id = ? AND e.source = s.source AND e.target = p.target"
        ") AS x ORDER BY x.source, x.rank_group, x.target, x.type, x.seq"
    )
    params = [
        list(sources),
        *_arm_params(kb_id, priority_types, limit, None, values),
        limit,
        kb_id,
    ]
    yield from state.rows(statement, tuple(params))
