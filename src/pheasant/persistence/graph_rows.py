"""The knowledge graph as rows, written by delta (Phase 35.10).

The graph was one zstd node-link blob. Every commit re-serialized the whole
thing and every process that answered a graph query held the whole thing
resident. Measured with ``python -m pheasant.graph.capacity`` on this repo's
own synthetic corpus, at 100k files (630k nodes / 630k edges):

===================================  ==========  ==========
                                     file        rows
===================================  ==========  ==========
commit after a one-file change       9.1 s       0.001 s
load before a replica can serve      4.6 s       none
resident bytes to answer a query     1.5 GB      none
3-hop bounded traversal              in-RAM      0.13 ms
===================================  ==========  ==========

The commit number is the one that matters, and not because 9 seconds is slow:
it is **O(total graph)** where the change was O(one file), it runs while the
sync mutex is held, and one process is the sole commit authority per shard
(``sync/saturation.py``). That made corpus size, not write rate, the thing
that saturated the commit stream — and the only documented way past it was to
shard the region. As rows the same commit is a millisecond *and flat in graph
size*.

**What this deliberately does not change.**

* The graph is still published as immutable generations with a
  content-addressed id. See :meth:`GraphRowStore.publish`.
* Stable IDs are untouched (CLAUDE.md rule 3). A node id is a node id; this
  changes where it is stored, not what it is.
* Snapshots stay files. They are history, they are read whole or not at all,
  and they are interval-gated rather than per-commit — so materializing one
  from rows at O(N) is the right cost in the right place.
* Nothing new is introduced. These are two tables in the state store the
  region already runs, in both dialects, next to the artifacts and chunks the
  same commit writes — which also means the graph and the chunks it describes
  now land in **one transaction** instead of a database write followed by a
  separate file rename.

The row↔attribute codec and the generation fold live in
:mod:`pheasant.persistence.graph_codec` and are re-exported here, so every
existing import of this module keeps working.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from pheasant.persistence import graph_candidates
from pheasant.persistence.graph_codec import (
    _ZERO_FOLD,
    BATCH,
    DIGEST_HEX,
    EDGE_KEY,
    PROMOTED_EDGE_KEYS,
    PROMOTED_NODE_KEYS,
    _batched,
    _digest,
    _endpoint_clauses,
    _pair_clauses,
    edge_attrs,
    edge_attrs_dict,
    edge_row,
    fold,
    node_attrs,
    node_attrs_dict,
    node_row,
)

__all__ = [
    "BATCH",
    "DIGEST_HEX",
    "EDGE_KEY",
    "PROMOTED_EDGE_KEYS",
    "PROMOTED_NODE_KEYS",
    "GraphRowStore",
    "edge_attrs",
    "edge_attrs_dict",
    "edge_row",
    "fold",
    "node_attrs",
    "node_attrs_dict",
    "node_row",
]

logger = logging.getLogger(__name__)

#: What a whole-graph read needs of each table: the key, the promoted columns
#: and the attribute blob. ``kb_id`` is the filter and ``digest`` is the fold's
#: business (:meth:`GraphRowStore.recompute_folds` reads it with ``*``), so
#: shipping either for every row of a load was bytes nobody decoded.
_NODE_READ = ("node_id", *PROMOTED_NODE_KEYS, "attrs")
_EDGE_READ = (*EDGE_KEY, "source_id", "attrs")


def _ranked_frontier(where: str, priority_types: Sequence[str], limited: bool) -> str:
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


class GraphRowStore:
    """Read and write the graph as rows in the state store.

    Holds no graph. Every method is a query, which is the property that lets an
    API replica answer a neighbourhood request without the residency the file
    backend required of it.
    """

    def __init__(self, state: Any) -> None:
        self.state = state
        #: ``(kb_id, generation_id) -> edge types``; see :meth:`edge_types`.
        self._edge_types: dict[str, tuple[str, tuple[str, ...]]] = {}
        dialect = getattr(state, "dialect", None)
        self._postgres = bool(dialect is not None and dialect.is_postgres)

    # --- writing ---------------------------------------------------------

    def apply_delta(
        self,
        kb_id: str,
        *,
        node_upserts: Iterable[tuple[str, dict[str, Any]]] = (),
        node_removals: Iterable[str] = (),
        edge_upserts: Iterable[tuple[str, str, int, dict[str, Any]]] = (),
        edge_removals: Iterable[tuple[str, str]] = (),
    ) -> dict[str, int]:
        """Write what changed, and only what changed.

        Removing a node removes the edges on **both** of its endpoints, which
        is what the in-memory graph's ``remove_nodes_from`` does and therefore
        what a persisted graph has to keep doing — a dangling edge would show
        up in a traversal as a neighbour with no attributes. That delete is
        why ``idx_graph_edges_target`` exists: without it, removing one node
        scans the whole edge table, which is the O(edges) cost this change is
        here to take off the sync path.

        Returns the counts, so a caller can log what a commit actually cost
        rather than what it thinks it cost.
        """

        nodes = list(node_upserts)
        removed_nodes = [node for node in node_removals]
        edges = list(edge_upserts)
        removed_edges = list(edge_removals)
        if not (nodes or removed_nodes or edges or removed_edges):
            return {"nodes": 0, "edges": 0, "removed_nodes": 0, "removed_edges": 0}

        node_rows = [node_row(kb_id, node_id, attrs) for node_id, attrs in nodes]
        edge_rows = [
            edge_row(kb_id, source, target, seq, attrs) for source, target, seq, attrs in edges
        ]
        touched_nodes = [node_id for node_id, _attrs in nodes] + removed_nodes
        # An endpoint pair whose edges are being replaced wholesale, plus every
        # pair either of whose endpoints is disappearing.
        touched_pairs = {(source, target) for source, target, _seq, _attrs in edges}
        touched_pairs |= set(removed_edges)

        # A knowledge base with no rows has nothing to fold out and nothing to
        # delete, so the first write of a graph — a fresh region, or the
        # one-shot import of a graph file — skips both. Not a heuristic: it is
        # exactly true, and it is what keeps the O(N) case from also paying
        # O(N) round trips proving there was nothing there. Probed with a
        # ``LIMIT 1``, never a ``COUNT(*)``: counting the whole table on every
        # commit is the shape that made the file backend O(total) in the first
        # place, and it measured 364ms per commit at 100k files before this.
        fresh = self._is_empty(kb_id)
        node_out, edge_out = (
            ({}, {})
            if fresh
            else self._digests_for(kb_id, touched_nodes, touched_pairs, removed_nodes)
        )

        with self.state.conn:
            if not fresh:
                self._delete_nodes(kb_id, touched_nodes)
                self._delete_edges(kb_id, touched_pairs)
                self._delete_edges_touching(kb_id, removed_nodes)
            self._insert_nodes(node_rows)
            self._insert_edges(edge_rows)
            self._fold(
                kb_id,
                node_out=list(node_out.values()),
                node_in=[row[-1] for row in node_rows],
                edge_out=list(edge_out.values()),
                edge_in=[row[-1] for row in edge_rows],
                node_change=len(node_rows) - len(node_out),
                edge_change=len(edge_rows) - len(edge_out),
            )
        return {
            "nodes": len(node_rows),
            "edges": len(edge_rows),
            "removed_nodes": len(removed_nodes),
            "removed_edges": max(0, len(edge_out) - len(edge_rows)),
        }

    def _is_empty(self, kb_id: str) -> bool:
        """Does this knowledge base hold any graph row at all? One row, not a count."""

        return not self.state.rows(
            "SELECT node_id FROM graph_nodes WHERE kb_id=? LIMIT 1", (kb_id,)
        )

    def _digests_for(
        self,
        kb_id: str,
        touched_nodes: list[str],
        touched_pairs: set[tuple[str, str]],
        removed_nodes: list[str],
    ) -> tuple[dict[Any, str], dict[Any, str]]:
        """Digests of the rows this delta is about to replace or delete.

        Read *before* the write, because folding a row out needs the digest it
        had. Doing it in the same transaction as the write would be tidier and
        is not possible: the rows are gone by then.

        Keyed by the row's own primary key rather than accumulated in a list,
        and that is load-bearing twice. XOR is an involution, so folding one
        row out **twice** silently folds it back in — and an edge can be
        reached by both queries below, once as a replaced pair and once as the
        casualty of a removed endpoint. Keying by identity also makes the
        length an exact count of rows leaving, which is how the row counts
        stay maintained without a ``COUNT(*)``.
        """

        node_out: dict[Any, str] = {}
        for batch in _batched(touched_nodes):
            clause, params = self.state.dialect.in_clause("node_id", batch)
            for row in self.state.rows(
                f"SELECT node_id, digest FROM graph_nodes WHERE kb_id=? AND {clause}",
                (kb_id, *params),
            ):
                node_out[str(row["node_id"])] = str(row["digest"])
        edge_out: dict[Any, str] = {}
        columns = ", ".join(EDGE_KEY)
        for clause, group in _pair_clauses(kb_id, touched_pairs):
            for row in self.state.rows(
                f"SELECT {columns}, digest FROM graph_edges WHERE {clause}",
                tuple(group),
            ):
                edge_out[tuple(row[column] for column in EDGE_KEY)] = str(row["digest"])
        for clause, group in _endpoint_clauses(kb_id, removed_nodes):
            for row in self.state.rows(
                f"SELECT {columns}, digest FROM graph_edges WHERE {clause}",
                tuple(group),
            ):
                edge_out[tuple(row[column] for column in EDGE_KEY)] = str(row["digest"])
        return node_out, edge_out

    def _delete_nodes(self, kb_id: str, node_ids: list[str]) -> None:
        for batch in _batched(node_ids):
            clause, params = self.state.dialect.in_clause("node_id", batch)
            self.state.conn.execute(
                f"DELETE FROM graph_nodes WHERE kb_id=? AND {clause}",
                (kb_id, *params),
            )

    def _delete_edges(self, kb_id: str, pairs: set[tuple[str, str]]) -> None:
        for clause, group in _pair_clauses(kb_id, pairs):
            self.state.conn.execute(f"DELETE FROM graph_edges WHERE {clause}", tuple(group))

    def _delete_edges_touching(self, kb_id: str, node_ids: list[str]) -> None:
        for clause, group in _endpoint_clauses(kb_id, node_ids):
            self.state.conn.execute(f"DELETE FROM graph_edges WHERE {clause}", tuple(group))

    def _insert_nodes(self, rows: list[tuple]) -> None:
        columns = "kb_id,node_id,type,label,source_id,relative_path,artifact_id,attrs,digest"
        placeholders = ",".join("?" * 9)
        for batch in _batched(rows):
            self.state.conn.executemany(
                f"INSERT INTO graph_nodes({columns}) VALUES({placeholders})", batch
            )

    def _insert_edges(self, rows: list[tuple]) -> None:
        columns = "kb_id,source,target,type,seq,source_id,attrs,digest"
        placeholders = ",".join("?" * 8)
        for batch in _batched(rows):
            self.state.conn.executemany(
                f"INSERT INTO graph_edges({columns}) VALUES({placeholders})", batch
            )

    def delete_source(self, kb_id: str, source_name: str) -> dict[str, int]:
        """Drop everything one source contributed, edges on both endpoints.

        The bulk case (``remove_source_content``, ``replace_source``), and the
        one place a whole-source scan is the right shape: it is indexed on
        ``source_id`` and it is what the caller asked for.
        """

        rows = self.state.rows(
            "SELECT node_id FROM graph_nodes WHERE kb_id=? AND source_id=?",
            (kb_id, source_name),
        )
        return self.apply_delta(kb_id, node_removals=[str(row["node_id"]) for row in rows])

    # --- publication -----------------------------------------------------

    def publish(self, kb_id: str) -> dict[str, Any]:
        """Name the current row set, and record it as the published generation.

        Content-addressed and clock-free, exactly as the file backend's
        ``generation_id`` was: an unchanged graph keeps its name across a
        re-publish, and two replicas holding the same rows compute the same id
        without talking to each other. ``published_at`` is recorded beside it
        and is deliberately *not* an input to the digest.
        """

        rows = self.state.rows(
            "SELECT node_fold, edge_fold FROM graph_generations WHERE kb_id=?", (kb_id,)
        )
        node_fold = str(rows[0]["node_fold"]) if rows else _ZERO_FOLD
        edge_fold = str(rows[0]["edge_fold"]) if rows else _ZERO_FOLD
        nodes, edges = self.counts(kb_id)
        identifier = _digest(node_fold, edge_fold, str(nodes), str(edges))[:16]
        published_at = datetime.now(UTC).isoformat()
        self.state.execute(
            "INSERT INTO graph_generations"
            "(kb_id,generation_id,published_at,nodes,edges,node_fold,edge_fold)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(kb_id) DO UPDATE SET"
            " generation_id=excluded.generation_id, published_at=excluded.published_at,"
            " nodes=excluded.nodes, edges=excluded.edges",
            (kb_id, identifier, published_at, nodes, edges, node_fold, edge_fold),
        )
        return {
            "generation_id": identifier,
            "published_at": published_at,
            "nodes": nodes,
            "edges": edges,
        }

    def published_generation(self, kb_id: str) -> dict[str, Any] | None:
        rows = self.state.rows(
            "SELECT generation_id, published_at, nodes, edges FROM graph_generations WHERE kb_id=?",
            (kb_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "generation_id": str(row["generation_id"]),
            "published_at": row["published_at"],
            "nodes": int(row["nodes"]),
            "edges": int(row["edges"]),
        }

    def _fold(
        self,
        kb_id: str,
        *,
        node_out: list[str],
        node_in: list[str],
        edge_out: list[str],
        edge_in: list[str],
        node_change: int,
        edge_change: int,
    ) -> None:
        """Update the persisted aggregates and counts. Caller holds the transaction.

        The counts are maintained here, from the exact number of rows this
        delta inserted and replaced, for the same reason the folds are: a
        ``COUNT(*)`` is a full scan, and the point of the backend is that a
        commit costs what the change costs. :meth:`recompute_folds` recounts
        the expensive way when something needs checking.
        """

        rows = self.state.rows(
            "SELECT nodes, edges, node_fold, edge_fold FROM graph_generations WHERE kb_id=?",
            (kb_id,),
        )
        node_fold = fold(str(rows[0]["node_fold"]) if rows else _ZERO_FOLD, *node_out, *node_in)
        edge_fold = fold(str(rows[0]["edge_fold"]) if rows else _ZERO_FOLD, *edge_out, *edge_in)
        node_count = max(0, (int(rows[0]["nodes"]) if rows else 0) + node_change)
        edge_count = max(0, (int(rows[0]["edges"]) if rows else 0) + edge_change)
        if rows:
            self.state.conn.execute(
                "UPDATE graph_generations SET node_fold=?, edge_fold=?, nodes=?, edges=?"
                " WHERE kb_id=?",
                (node_fold, edge_fold, node_count, edge_count, kb_id),
            )
            return
        self.state.conn.execute(
            "INSERT INTO graph_generations"
            "(kb_id,generation_id,published_at,nodes,edges,node_fold,edge_fold)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                kb_id,
                "",
                datetime.now(UTC).isoformat(),
                node_count,
                edge_count,
                node_fold,
                edge_fold,
            ),
        )

    def recompute_folds(self, kb_id: str) -> dict[str, str]:
        """Re-derive both aggregates with a full scan, and store them.

        The check on the incremental path. An aggregate maintained by deltas is
        only as good as every delta that ever ran, so ``sync --mode repair``
        and the tests recompute it the expensive way and compare — the same
        posture ``pheasant.evaluation.benchmark`` takes toward the capacity
        coefficients, and for the same reason: an unverified derived number
        drifts silently and is believed anyway.
        """

        nodes = _ZERO_FOLD
        for row in self._stream("graph_nodes", ("node_id",), kb_id):
            nodes = fold(nodes, str(row["digest"]))
        edges = _ZERO_FOLD
        for row in self._stream("graph_edges", EDGE_KEY, kb_id):
            edges = fold(edges, str(row["digest"]))
        node_count, edge_count = self.recount(kb_id)
        self.state.execute(
            "INSERT INTO graph_generations"
            "(kb_id,generation_id,published_at,nodes,edges,node_fold,edge_fold)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(kb_id) DO UPDATE SET"
            " node_fold=excluded.node_fold, edge_fold=excluded.edge_fold",
            (
                kb_id,
                "",
                datetime.now(UTC).isoformat(),
                node_count,
                edge_count,
                nodes,
                edges,
            ),
        )
        return {"node_fold": nodes, "edge_fold": edges}

    # --- reading ---------------------------------------------------------

    def counts(self, kb_id: str) -> tuple[int, int]:
        """Node and edge counts, from the maintained row.

        Read, never computed. ``COUNT(*)`` over the graph tables is a full
        scan, these are on the endpoints the UI polls and on every commit, and
        that is exactly the mistake the in-memory graph already fixed by
        maintaining ``_type_counts`` on write. :meth:`recount` is the honest
        scan for when the maintained number needs checking.
        """

        rows = self.state.rows("SELECT nodes, edges FROM graph_generations WHERE kb_id=?", (kb_id,))
        if rows:
            return int(rows[0]["nodes"]), int(rows[0]["edges"])
        return self.recount(kb_id)

    def recount(self, kb_id: str) -> tuple[int, int]:
        """The real counts, by scanning. For repair and for the tests."""

        nodes = self.state.rows("SELECT COUNT(*) AS n FROM graph_nodes WHERE kb_id=?", (kb_id,))
        edges = self.state.rows("SELECT COUNT(*) AS n FROM graph_edges WHERE kb_id=?", (kb_id,))
        return (int(nodes[0]["n"]) if nodes else 0, int(edges[0]["n"]) if edges else 0)

    def type_counts(self, kb_id: str) -> dict[str, int]:
        rows = self.state.rows(
            "SELECT type, COUNT(*) AS n FROM graph_nodes WHERE kb_id=? GROUP BY type",
            (kb_id,),
        )
        return {str(row["type"]): int(row["n"]) for row in rows if row["type"] is not None}

    def get_node(self, kb_id: str, node_id: str) -> dict[str, Any] | None:
        rows = self.state.rows(
            "SELECT * FROM graph_nodes WHERE kb_id=? AND node_id=?", (kb_id, node_id)
        )
        return node_attrs(rows[0]) if rows else None

    def get_nodes(
        self, kb_id: str, node_ids: Iterable[str], materialized: bool = False
    ) -> dict[str, dict[str, Any]]:
        """Many nodes in one round trip — what a batched traversal hop needs.

        ``materialized`` picks the decoder. A traversal hop reads ``type`` and
        nothing else, so it wants :class:`_LazyAttrs`; the search arm's scorer
        reads every attribute of every candidate, so laziness there is a
        mapping-protocol copy it pays for and never uses. See
        :func:`node_attrs_dict`.
        """

        decode = node_attrs_dict if materialized else node_attrs
        found: dict[str, dict[str, Any]] = {}
        for batch in _batched(list(node_ids)):
            if not batch:
                continue
            clause, params = self.state.dialect.in_clause("node_id", batch)
            for row in self.state.rows(
                f"SELECT * FROM graph_nodes WHERE kb_id=? AND {clause}",
                (kb_id, *params),
            ):
                found[str(row["node_id"])] = decode(row)
        return found

    def nodes_matching(
        self,
        kb_id: str,
        subquery: str,
        params: Iterable[Any],
        materialized: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Nodes whose id a ``SELECT`` selects, without the ids leaving the server.

        The search arm's candidate set comes from ``graph_nodes_fts``, which
        lives in this same database — so the two-step version shipped up to
        2,000 ids and sent every one of them straight back down as a bind
        parameter, for a set the planner could have resolved as a semi-join.
        Measured on the fleet region: **22.0ms against 14.6ms**, and the gap
        grows with how *unselective* the query is, which is the shape that
        already bit twice here.

        ``ORDER BY node_id`` is not decoration. The rows come back as a dict
        the arm then iterates, and without it the order is whatever the plan
        yields — the arm's final sort is stable on ``(-score, title)``, so two
        candidates tying on both would swap between runs. A stated order is
        the same answer this backend gives everywhere else it cannot
        reproduce insertion order, and it measured *faster* than no order at
        all (14.6ms against 18.7ms) because the sort lets the planner take the
        index path.

        ``subquery`` is SQL this module does not author — it comes from
        :meth:`~pheasant.search.node_index.NodeIndex.candidate_query`, which
        owns what a candidate is. Its parameters are spliced after ``kb_id``.
        """

        decode = node_attrs_dict if materialized else node_attrs
        found: dict[str, dict[str, Any]] = {}
        for row in self.state.rows(
            f"SELECT * FROM graph_nodes WHERE kb_id=? AND node_id IN ({subquery}) ORDER BY node_id",
            (kb_id, *params),
        ):
            found[str(row["node_id"])] = decode(row)
        return found

    def out_edges(
        self,
        kb_id: str,
        sources: Iterable[str],
        targets: Iterable[str] | None = None,
        priority_types: Sequence[str] = (),
        limit_per_source: int | None = None,
    ) -> dict[str, list[tuple[str, str, dict[int, Any]]]]:
        """``{source: [(source, target, {seq: attrs})]}`` for a whole BFS frontier.

        One query per *level* rather than per node. The primary key already
        leads with ``(kb_id, source)``, so this is a range scan over the key
        and needs no index of its own.

        Ordered by ``(target, type, seq)`` and not by insertion. The in-memory
        graph enumerates neighbours in insertion order, which decides which
        ones survive a caller's ``max_nodes`` — an order the rows cannot
        reproduce and should not pretend to. A stable, stated ordering is the
        honest replacement; ``tests/test_graph_backends.py`` asserts the two
        backends return the same *set* and each is internally deterministic,
        rather than asserting an equality that is not true.

        Grouped by endpoint pair here rather than flat-then-regrouped by the
        caller. The two-pass version built one tuple per edge and then one dict
        per pair over the same rows, which is invisible on an ordinary node and
        not on a hub: a ``source`` node indexing 8,000 artifacts made that
        ~15ms of pure object churn per traversal. The ``ORDER BY`` already
        clusters a pair's rows together, so one pass is enough.

        ``targets`` asks for the **induced sub-graph** — edges from this set
        into that one — and is the difference between reading a hub's whole
        fan-out and reading the part of it a caller can use. A bounded slice
        of 51 nodes fetched **3,580** edge rows to keep ~150, because one of
        those nodes is a ``source`` that indexes every artifact in its
        repository; asking the database for the intersection is 0.96 ms
        against 16.7 ms and the answer is identical, since the caller
        discarded every other row anyway. Unlike :func:`_pair_clauses` the
        cross product is *wanted* here — "every edge from S to S" is the
        question — so this needs no ``OR`` chain and stays two ``IN`` lists.

        ``priority_types`` and ``limit_per_source`` let a *bounded* walk stop
        reading a hub's whole fan-out. A depth-3 walk asking for 100
        neighbours off a ``source`` node read **1,620 pairs to keep 100**,
        because the budget bound the walk and nothing bound the fetch. The
        ordering is what makes a limit safe to apply at all: a pair ranks
        ahead of another when *any* of its edges carries a priority type,
        which is exactly what
        :func:`~pheasant.graph.traversal._hierarchy_first` computes, so a
        prefix here is the same prefix the caller would have taken. It has to
        be a per-*pair* aggregate — a per-edge ``CASE`` would split a pair
        carrying both a priority edge and an ordinary one across two rank
        groups and hand the caller that pair twice with half its edges each.

        The window sort costs more on the server than shipping the rows does
        (2.96ms against 2.54ms). It still wins by 2.7× — **10.40ms against
        3.81ms** — because the rows that never arrive are also rows nobody
        decodes and groups, which is where the time actually was. Measuring
        the statement alone said the opposite.

        Whether a limit is *sound* is the caller's to decide, not this
        module's: see :func:`~pheasant.graph.traversal._out_edges_for`.
        """

        grouped: dict[str, list[tuple[str, str, dict[int, Any]]]] = {}
        target_batches: list[list[str] | None]
        if targets is None:
            target_batches = [None]
        else:
            target_batches = [batch for batch in _batched(sorted(set(targets))) if batch]
        dialect = self.state.dialect
        for batch in _batched(list(sources)):
            if not batch:
                continue
            source_clause, source_params = dialect.in_clause("source", batch)
            for target_batch in target_batches:
                where = f"kb_id=? AND {source_clause}"
                params: list[Any] = [kb_id, *source_params]
                if target_batch is not None:
                    target_clause, target_params = dialect.in_clause("target", target_batch)
                    where += f" AND {target_clause}"
                    params.extend(target_params)
                if not priority_types and limit_per_source is None:
                    statement = (
                        f"SELECT * FROM graph_edges WHERE {where}"
                        " ORDER BY source, target, type, seq"
                    )
                else:
                    statement = _ranked_frontier(
                        where, priority_types, limit_per_source is not None
                    )
                    # The priority list is spelled *before* the WHERE in the
                    # ranked statement, so its parameters go in front of it.
                    params = [*priority_types, *params]
                    if limit_per_source is not None:
                        params.append(int(limit_per_source))
                current: tuple[str, str] | None = None
                edge_map: dict[int, Any] = {}
                for row in self.state.rows(statement, tuple(params)):
                    pair = (str(row["source"]), str(row["target"]))
                    if pair != current:
                        current, edge_map = pair, {}
                        grouped.setdefault(pair[0], []).append((pair[0], pair[1], edge_map))
                    edge_map[len(edge_map)] = edge_attrs(row)
        return grouped

    def iter_nodes(self, kb_id: str) -> Iterator[tuple[str, dict[str, Any]]]:
        """Every node, streamed. For exports and whole-graph passes.

        Materialized, unlike the point lookups: a caller streaming the whole
        graph is going to read all of it, so :class:`_LazyAttrs` would be pure
        overhead — and these feed `json.dumps` (snapshots, the node-link
        export), which serializes a `dict` and refuses anything else.
        """

        for row in self._stream("graph_nodes", ("node_id",), kb_id, _NODE_READ):
            yield str(row["node_id"]), node_attrs_dict(row)

    def iter_edges(self, kb_id: str) -> Iterator[tuple[tuple[str, str], dict[int, dict]]]:
        """Every edge, streamed and grouped by endpoint pair.

        Grouped to match the in-memory graph's ``iter_edges`` shape, so the
        exporter, the analytics extract and the relationship scan read from
        either backend with no branch. Ordered by the pair, which is what lets
        the grouping be streaming rather than a dict of the whole graph.
        """

        current: tuple[str, str] | None = None
        parallel: dict[int, dict] = {}
        for row in self._stream("graph_edges", EDGE_KEY, kb_id, _EDGE_READ):
            pair = (str(row["source"]), str(row["target"]))
            if pair != current:
                if current is not None:
                    yield current, parallel
                current, parallel = pair, {}
            # Materialized, for the reason `iter_nodes` gives.
            parallel[len(parallel)] = edge_attrs_dict(row)
        if current is not None:
            yield current, parallel

    def candidate_edges(
        self,
        kb_id: str,
        tokens: list[str],
        source_name: str | None = None,
        limit: int = 2000,
    ) -> Iterator[tuple[tuple[str, str], dict[int, dict]]]:
        """Edges that could possibly score; see :mod:`.graph_candidates`."""

        return graph_candidates.candidate_edges(self, kb_id, tokens, source_name, limit)

    def edge_types(self, kb_id: str) -> tuple[str, ...]:
        """Every edge type, read once per published generation."""

        return graph_candidates.edge_types(self, kb_id)

    #: Rows per page when scanning a whole table. Big enough that a 630k-node
    #: graph is 126 round trips rather than 630, small enough that a page is
    #: never the memory problem the paging exists to avoid.
    PAGE = 5_000

    def _stream(
        self,
        table: str,
        key: tuple[str, ...],
        kb_id: str,
        columns: tuple[str, ...] = ("*",),
    ) -> Iterator[Any]:
        """Every row of one graph table for one kb, in key order, a page at a time.

        Keyset pagination rather than a server-side cursor. ``StateStore.rows``
        is list-returning by contract — right for the bounded call sites it
        exists for, wrong for a scan of every node in the graph, which is the
        exact shape ``persistence/migrate.py`` streams ``chunks`` for. Keyset
        paging gets the same bounded memory through the ordinary read path, so
        it needs no new backend method and behaves identically on both
        dialects; ``OFFSET`` would not, because its cost grows with how far in
        the scan has already got.

        ``columns`` must include ``key``. Where the backend allows it the next
        page is fetched on a helper thread while the caller decodes this one:
        the wait on the server releases the GIL, so it overlaps the decoding
        rather than following it. Pages were already separate statements, so
        reading one early changes no answer.
        """

        select = ", ".join(columns)
        names = ", ".join(key)
        placeholders = ", ".join("?" for _ in key)

        def page_after(after: tuple | None) -> list[Any]:
            if after is None:
                sql = f"SELECT {select} FROM {table} WHERE kb_id=? ORDER BY {names} LIMIT ?"
                params: tuple = (kb_id, self.PAGE)
            else:
                sql = (
                    f"SELECT {select} FROM {table} WHERE kb_id=? AND ({names}) > ({placeholders})"
                    f" ORDER BY {names} LIMIT ?"
                )
                params = (kb_id, *after, self.PAGE)
            return self.state.rows(sql, params)

        def last_key(page: list[Any]) -> tuple:
            return tuple(page[-1][column] for column in key)

        if not self.state.backend.reads_from_any_thread():
            page = page_after(None)
            while page:
                yield from page
                if len(page) < self.PAGE:
                    return
                page = page_after(last_key(page))
            return
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="pheasant-graph-page") as pool:
            page = page_after(None)
            while page:
                ahead = None
                if len(page) == self.PAGE:
                    ahead = pool.submit(contextvars.copy_context().run, page_after, last_key(page))
                yield from page
                if ahead is None:
                    return
                page = ahead.result()
