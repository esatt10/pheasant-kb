from __future__ import annotations

import json
import threading
from collections import defaultdict
from typing import Any


def _search_blob(attrs: dict[str, Any]) -> str:
    """Every attribute value of a node, lowercased into one searchable string.

    A superset of what the scorer reads, deliberately: the blob is only ever
    used to *reject* nodes that cannot match, so covering extra fields can
    only cost a little scoring work, never a missed hit.
    """

    parts = []
    for value in attrs.values():
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (str, int, float)):
            parts.append(str(value))
        elif isinstance(value, (list, tuple, set)):
            parts.extend(str(item) for item in value)
        elif isinstance(value, dict):
            parts.extend(str(item) for item in value.values())
    return " ".join(parts).lower()


class NodeView:
    def __init__(self, graph):
        self.graph = graph

    def __getitem__(self, key):
        with self.graph._lock:
            return self.graph._nodes[key]

    def __iter__(self):
        # Snapshot the keys: a reader thread must never hold a live iterator
        # over ``_nodes`` while the sync thread adds or removes nodes.
        with self.graph._lock:
            return iter(tuple(self.graph._nodes))

    def __contains__(self, key):
        return key in self.graph._nodes

    def __call__(self, data: bool = False):
        with self.graph._lock:
            return list(self.graph._nodes.items()) if data else list(self.graph._nodes)

    def get(self, key, default=None):
        with self.graph._lock:
            return self.graph._nodes.get(key, default)


class SimpleMultiDiGraph:
    """Minimal multi-digraph backing the knowledge graph.

    The graph is written by the sync thread (startup index, watcher and
    scheduler beats) while the HTTP API, the MCP tools and the assistant read
    it from their own threads. Every mutation and every read primitive
    therefore takes ``_lock``, and readers are handed *snapshots* rather than
    live dict views — otherwise a sync running underneath a ``/graph`` or
    ``/overview`` request blows up with "dictionary changed size during
    iteration". Use :meth:`reading` to keep a multi-call read consistent.
    """

    def __init__(self):
        self._nodes: dict[str, dict[str, Any]] = {}
        self._edges: dict[tuple[str, str], dict[int, dict[str, Any]]] = defaultdict(dict)
        # Out-adjacency, kept in step with ``_edges``. Without it every
        # ``out_edges`` call scanned the whole edge table, so a breadth-first
        # walk cost O(nodes × edges) — 80s for a two-hop slice of a 500k-edge
        # graph, and minutes at three. Insertion-ordered dicts (not sets) keep
        # traversal order deterministic, which decides which neighbours survive
        # a caller's limit.
        self._out: dict[str, dict[str, None]] = {}
        # Re-entrant: the writer path nests reads inside writes (upsert_node
        # reads created_at, graph_store.save serializes) on the same thread.
        self._lock = threading.RLock()
        # Running aggregates. Summing 850k edge buckets or re-counting 240k
        # node types per request was costing seconds on endpoints the UI polls;
        # maintaining them on write makes those reads O(1).
        self._edge_count = 0
        self._type_counts: dict[str, int] = {}
        # Lowercased concatenation of every node attribute value, built on
        # write. Graph-mode search can only score a node above zero if a query
        # token appears somewhere in its text, so this lets a query reject the
        # overwhelming majority of nodes with one substring test instead of
        # stringifying and weighting every attribute of every node.
        self._search_blobs: dict[str, str] = {}
        # Nodes written or removed since the search index last flushed. The
        # index is a derived cache, so this is only bookkeeping for "what does
        # it still owe"; losing it costs a rebuild, never correctness.
        self._dirty_nodes: set[str] = set()
        self._removed_nodes: set[str] = set()
        # The same bookkeeping for the *persisted* graph, which is a second
        # reader of "what changed" and — unlike the search index — not a cache:
        # the row backend writes exactly this delta, so losing it would lose a
        # commit rather than cost a rebuild. Which is why `take_graph_delta`
        # only clears these once the write is durable, where
        # `take_index_delta` clears on claim.
        #
        # Edges are tracked by *endpoint pair*, not per parallel edge, because
        # that is the unit the writer replaces: parallel edges between one pair
        # have no stable identity of their own (`add_edge` keys them by arrival
        # order), so the only sound update is to rewrite the pair.
        self._pending_nodes: set[str] = set()
        self._pending_removed_nodes: set[str] = set()
        self._pending_pairs: set[tuple[str, str]] = set()
        self._pending_removed_pairs: set[tuple[str, str]] = set()
        self.nodes = NodeView(self)

    def reading(self):
        """Context manager pinning the graph for the duration of a read.

        ``with graph.reading():`` keeps counts and contents of one export
        consistent with each other; the individual primitives are safe on
        their own without it.
        """

        return self._lock

    def __contains__(self, node):
        return node in self._nodes

    def __len__(self):
        return len(self._nodes)

    def has_node(self, node):
        return node in self._nodes

    def add_node(self, node, **attrs):
        with self._lock:
            existing = self._nodes.get(node)
            merged = {**(existing or {}), **attrs}
            self._nodes[node] = merged
            self._retype(existing.get("type") if existing else None, merged.get("type"))
            self._search_blobs[node] = _search_blob(merged)
            self._dirty_nodes.add(node)
            self._removed_nodes.discard(node)
            self._pending_nodes.add(node)
            # Deliberately *not* discarded from `_pending_removed_nodes`. A
            # node removed and re-added in one delta (a full sync drops a
            # source and rebuilds it) still owes the row writer the cascade
            # that removal implies: its edges that were not re-added. Dropping
            # it from the set skipped that cascade, so an edge the rebuild no
            # longer emits -- a deleted link's `references` -- stayed in
            # `/state` and came back on the next restart. The writer deletes
            # before it inserts, so the node and its current edges land intact.

    def add_edge(self, source, target, **attrs):
        with self._lock:
            data = self._edges[(source, target)]
            data[len(data)] = attrs
            self._edge_count += 1
            self._out.setdefault(source, {})[target] = None
            self._pending_pairs.add((source, target))
            self._pending_removed_pairs.discard((source, target))

    def touch_edge(self, source, target):
        """Mark an endpoint pair as owing a write.

        ``GraphBuilder.upsert_edge`` updates an existing edge's attributes *in
        place*, through the dict ``get_edge_data`` hands back — so the change
        never passes through :meth:`add_edge` and the persisted-graph delta
        would not know about it. Free on the whole-file backend, which
        re-serializes everything anyway; a silently dropped update on the row
        backend, which writes only what it is told changed.
        """

        with self._lock:
            if (source, target) in self._edges:
                self._pending_pairs.add((source, target))
                self._pending_removed_pairs.discard((source, target))

    def _retype(self, old: Any, new: Any) -> None:
        """Keep the per-type tally in step with a node write. Lock held."""

        if old == new:
            return
        if old is not None:
            remaining = self._type_counts.get(str(old), 0) - 1
            if remaining > 0:
                self._type_counts[str(old)] = remaining
            else:
                self._type_counts.pop(str(old), None)
        if new is not None:
            self._type_counts[str(new)] = self._type_counts.get(str(new), 0) + 1

    def remove_nodes_from(self, nodes):
        remove = set(nodes)
        with self._lock:
            for node in remove:
                existing = self._nodes.pop(node, None)
                if existing is not None:
                    self._retype(existing.get("type"), None)
                self._search_blobs.pop(node, None)
                self._dirty_nodes.discard(node)
                self._pending_nodes.discard(node)
                if existing is not None:
                    self._removed_nodes.add(node)
                    # The persisted delta records the *node* removal and lets
                    # the writer cascade to its edges, which it can do with one
                    # indexed statement. Listing every pair here would rebuild
                    # in Python the work the reverse index exists to do in SQL.
                    self._pending_removed_nodes.add(node)
                self._out.pop(node, None)
            # One walk of the edge table, because an edge is dropped when
            # *either* endpoint goes and only the outgoing half is indexed.
            #
            # Two ways to make this O(degree) were measured and neither
            # earned its place. Taking the outgoing half off `_out` and
            # scanning only for incoming edges came out at 122.5ms against
            # this loop's 126.0ms (100k files, 630k edges) — inside the
            # noise, because the scan is what costs, not the test inside it.
            # An in-adjacency index removes the scan properly and costs about
            # what `_out` costs to maintain: ~215 bytes per node, 15% of the
            # working set, to save ~120ms on a call that fires once per full
            # sync and on a memory-maintenance beat. Written down rather than
            # left for the next reader to re-derive: the shape is O(total) and
            # the constant is small, so the fix is to stop holding the graph,
            # not to index it further.
            for edge in list(self._edges):
                if edge[0] in remove or edge[1] in remove:
                    dropped = self._edges.pop(edge, None)
                    self._edge_count -= len(dropped or ())
                    self._drop_adjacency(*edge)
                    self._pending_pairs.discard(edge)

    def remove_edges_from(self, edges):
        with self._lock:
            for source, target in edges:
                dropped = self._edges.pop((source, target), None)
                self._edge_count -= len(dropped or ())
                self._drop_adjacency(source, target)
                self._pending_pairs.discard((source, target))
                if dropped:
                    self._pending_removed_pairs.add((source, target))

    def remove_edges_where(self, source, target, predicate) -> int:
        """Remove only the edges between one pair that ``predicate(attrs)`` accepts.

        :meth:`remove_edges_from` drops the whole pair, which is right when a
        caller owns every edge in it and collateral damage otherwise: a body
        link is both the reference resolver's `references` edge and the OKF
        pass's `links_to`, and retracting one must not take the other. The
        survivors are re-keyed from zero because `add_edge` keys by arrival
        (``len(data)``), so a gap would make the next arrival overwrite one.
        Returns how many edges went.
        """

        with self._lock:
            data = self._edges.get((source, target))
            if not data:
                return 0
            kept = [attrs for attrs in data.values() if not predicate(attrs)]
            removed = len(data) - len(kept)
            if not removed:
                return 0
            self._edge_count -= removed
            if kept:
                self._edges[(source, target)] = dict(enumerate(kept))
                self._pending_pairs.add((source, target))
                self._pending_removed_pairs.discard((source, target))
            else:
                self._edges.pop((source, target), None)
                self._drop_adjacency(source, target)
                self._pending_pairs.discard((source, target))
                self._pending_removed_pairs.add((source, target))
            return removed

    def _drop_adjacency(self, source: str, target: str) -> None:
        """Unlink one endpoint pair. Caller holds the lock."""

        targets = self._out.get(source)
        if targets is None:
            return
        targets.pop(target, None)
        if not targets:
            self._out.pop(source, None)

    def get_edge_data(self, source, target, default=None):
        with self._lock:
            found = self._edges.get((source, target))
            return default if found is None else found

    def edges(self):
        """Snapshot of ``((source, target), {key: data})`` for every edge.

        Edge attribute dicts are copied because ``GraphBuilder.upsert_edge``
        updates them in place on the sync thread.
        """

        with self._lock:
            return [
                (endpoints, {key: dict(data) for key, data in edge_map.items()})
                for endpoints, edge_map in self._edges.items()
            ]

    def neighbors(self, node):
        with self._lock:
            return list(self._out.get(node, ()))

    def out_edges(self, node):
        """Outgoing edges of one node — O(degree) via the adjacency index."""

        with self._lock:
            edges = []
            for target in self._out.get(node, ()):
                edge_map = self._edges.get((node, target))
                if not edge_map:
                    continue
                edges.append((node, target, {key: dict(data) for key, data in edge_map.items()}))
            return edges

    def out_edges_batch(self, node_ids, targets=None, priority_types=(), limit_per_source=None):
        """``out_edges`` for a whole BFS frontier, under one lock hold.

        Exists so :mod:`pheasant.graph.traversal` can expand a level at a time
        without asking which backend it has. Here that is a convenience — the
        lookups were already O(1) — and on the row-backed graph it is the
        difference between one query per level and one per node.

        ``targets`` restricts the answer to edges landing inside that set: the
        *induced sub-graph* over ``node_ids`` x ``targets``, which is what a
        bounded slice actually wants. Filtering here rather than in the caller
        is free on this backend and is the whole cost on a stored one.

        ``priority_types`` and ``limit_per_source`` are the bounded walk's
        frontier. Applied here too — not because this backend needs them, but
        because the traversal stops re-sorting once it has asked for the
        order, so a graph that took the arguments and ignored them would hand
        back an unordered frontier and quietly change which neighbours a
        bounded walk returns.
        """

        keep = None if targets is None else set(targets)
        with self._lock:
            edges = {node_id: self.out_edges(node_id) for node_id in node_ids}
        if keep is not None:
            edges = {
                node_id: [entry for entry in entries if entry[1] in keep]
                for node_id, entries in edges.items()
            }
        if priority_types:
            wanted = set(priority_types)
            for node_id, entries in edges.items():
                # Stable, and by *pair*: a pair carrying one priority edge
                # ranks ahead whole, which is what the row backend's per-pair
                # window aggregate computes and what `_hierarchy_first` did.
                edges[node_id] = sorted(
                    entries,
                    key=lambda entry: (
                        0 if {data.get("type") for data in entry[2].values()} & wanted else 1
                    ),
                )
        if limit_per_source is not None:
            cut = max(0, int(limit_per_source))
            edges = {node_id: entries[:cut] for node_id, entries in edges.items()}
        return edges

    def prefetch_nodes(self, node_ids, materialized=False):
        """Attributes for a whole frontier. See :meth:`out_edges_batch`.

        ``materialized`` is accepted and ignored: these attributes are already
        plain dicts, and the copy below is already a full one. It exists so
        the traversal and the search arm can state what they need without
        asking which backend they have — the same reason the batch methods
        exist at all.
        """

        with self._lock:
            return {
                node_id: dict(self._nodes[node_id])
                for node_id in node_ids
                if node_id in self._nodes
            }

    def number_of_nodes(self):
        return len(self._nodes)

    def number_of_edges(self):
        return self._edge_count

    def type_counts(self) -> dict[str, int]:
        """Node count per type, maintained on write (was a full scan)."""

        with self._lock:
            return dict(self._type_counts)

    # --- lock-held iteration ------------------------------------------------
    # Snapshots are the safe default, but copying 240k nodes / 850k edges per
    # request cost seconds on the endpoints the canvas polls. These hand back
    # the live mappings for callers that hold ``reading()`` for the whole walk
    # — no copying, and the writer still cannot mutate underneath them.

    def iter_nodes(self):
        """``(node_id, attrs)`` pairs. Caller MUST hold ``reading()``."""

        return self._nodes.items()

    def search_blobs(self):
        """Live id→searchable-text mapping. Caller MUST hold ``reading()``."""

        return self._search_blobs

    def index_rows(self) -> list[tuple[str, str, str]]:
        """Every node as ``(node_id, source_id, body)`` for the search index."""

        with self._lock:
            return [
                (node_id, str(attrs.get("source_id") or ""), self._search_blobs.get(node_id, ""))
                for node_id, attrs in self._nodes.items()
            ]

    def take_index_delta(self) -> tuple[list[tuple[str, str, str]], list[str]]:
        """Claim what the search index still owes: ``(upserts, removals)``.

        Claiming clears the pending sets, so a failed flush loses updates
        rather than repeating them — acceptable because the index is a cache
        and a rebuild restores it exactly.
        """

        with self._lock:
            upserts = [
                (
                    node_id,
                    str((self._nodes.get(node_id) or {}).get("source_id") or ""),
                    self._search_blobs.get(node_id, ""),
                )
                for node_id in self._dirty_nodes
                if node_id in self._nodes
            ]
            removals = list(self._removed_nodes)
            self._dirty_nodes.clear()
            self._removed_nodes.clear()
            return upserts, removals

    def graph_delta(self) -> dict[str, Any]:
        """What the persisted graph still owes, without claiming it.

        Deliberately split from :meth:`clear_graph_delta`, where
        ``take_index_delta`` does both at once. The search index is a cache —
        a lost flush costs a rebuild — so claiming on read is right there. The
        persisted graph is not: a delta claimed and then lost to a crashed
        commit is a node that never reaches disk and that nothing will ever
        write again, because the in-memory graph no longer believes it is
        dirty. So the writer reads here, commits, and clears only after.

        Edge upserts carry every parallel edge of a touched pair, because the
        writer replaces a pair wholesale — see ``_pending_pairs``.
        """

        with self._lock:
            node_upserts = [
                (node_id, dict(self._nodes[node_id]))
                for node_id in self._pending_nodes
                if node_id in self._nodes
            ]
            edge_upserts = [
                (source, target, seq, dict(attrs))
                for (source, target) in self._pending_pairs
                for seq, attrs in enumerate(self._edges.get((source, target), {}).values())
            ]
            return {
                "node_upserts": node_upserts,
                "node_removals": sorted(self._pending_removed_nodes),
                "edge_upserts": edge_upserts,
                "edge_removals": sorted(self._pending_pairs | self._pending_removed_pairs),
            }

    def clear_graph_delta(self, delta: dict[str, Any]) -> None:
        """Forget a delta that is now durable.

        Takes the delta it is clearing rather than emptying the sets, so a
        write that raced a concurrent mutation does not discard the mutation's
        own bookkeeping. That race is real: the sync thread commits while a
        ``DELETE /sources`` handler mutates on a request thread.
        """

        with self._lock:
            self._pending_nodes.difference_update(
                node_id for node_id, _attrs in delta.get("node_upserts", ())
            )
            self._pending_removed_nodes.difference_update(delta.get("node_removals", ()))
            self._pending_pairs.difference_update(
                (source, target) for source, target, _seq, _attrs in delta.get("edge_upserts", ())
            )
            self._pending_removed_pairs.difference_update(delta.get("edge_removals", ()))

    def mark_all_pending(self) -> None:
        """Treat the whole graph as unwritten.

        For the first write of a graph the store has never seen — a fresh
        region, or the one-shot import of a graph file — where "the delta" is
        everything. A full write is the degenerate case of an incremental one,
        so it goes through the same path rather than a second one.
        """

        with self._lock:
            self._pending_nodes = set(self._nodes)
            self._pending_pairs = set(self._edges)
            self._pending_removed_nodes = set()
            self._pending_removed_pairs = set()

    def node_map(self):
        """The live id→attrs mapping. Caller MUST hold ``reading()``.

        For lock-held scans that look up many nodes: going through
        ``nodes.get`` re-acquires the lock per lookup, which is measurable
        when the scan touches every edge endpoint.
        """

        return self._nodes

    def iter_edges(self):
        """``((source, target), {key: attrs})``. Caller MUST hold ``reading()``."""

        return self._edges.items()

    def to_node_link(self):
        with self._lock:
            links = []
            for source, target in sorted(self._edges):
                # Edge keys are insertion-order implementation details and are
                # discarded by ``from_node_link``. Canonicalize parallel edges
                # by their semantic payload so concurrent source/file workers
                # cannot change persisted graph bytes merely by finishing in a
                # different order.
                edge_rows = sorted(
                    self._edges[(source, target)].values(),
                    key=lambda data: json.dumps(data, sort_keys=True, separators=(",", ":")),
                )
                for key, data in enumerate(edge_rows):
                    links.append({"source": source, "target": target, "key": key, **data})
            return {
                "directed": True,
                "multigraph": True,
                "graph": {},
                "nodes": [self._nodes[node_id] for node_id in sorted(self._nodes)],
                "links": links,
            }

    @classmethod
    def from_node_link(cls, data):
        graph = cls()
        for node in data.get("nodes", []):
            graph.add_node(node.get("id"), **node)
        for edge in data.get("links", []):
            attrs = dict(edge)
            source = attrs.pop("source")
            target = attrs.pop("target")
            attrs.pop("key", None)
            graph.add_edge(source, target, **attrs)
        return graph
