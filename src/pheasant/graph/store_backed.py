"""The indexer's working set, held as a cache over the row store.

A sync process used to start by loading the whole graph -- every node and edge
of the region, decoded into Python -- because the builder mutates it and a few
passes walk it. That load was the bottleneck once everything around it was
fast: 123k nodes / 762k edges took 14-38 s in a 2-CPU indexer, for a sync that
changed fifteen files. A columnar format would not fix it: the measured cost
was building the Python objects, not reading the bytes.

So the working set no longer starts full. :class:`StoreBackedGraph` is a
:class:`~pheasant.graph.simple.SimpleMultiDiGraph` whose maps hold only what
this process has read or written, and every read faults in what it needs from
the row store first (``persistence/graph_selections.WorkingSetReads``):

* a node, by id (``has_node``, ``nodes[...]``, ``node_map().get``);
* a node's out-edges, all of them (``out_edges``) -- the retraction passes
  judge an artifact by everything leaving it;
* one endpoint pair, whole (``get_edge_data``, ``add_edge``) -- the unit a
  delta replaces, so holding half a pair would write the other half out of
  existence.

Writes go through the base class unchanged, so the persisted delta is exactly
what it was: the rows a sync touched and nothing else. The whole-graph walks
four passes made are the base class's *selections* (``select_nodes``,
``edges_into_types``, ``targets_with_incoming``, ``out_edges_marked``), each
answered here by a query over the promoted columns and overlaid with what this
process holds, so a pass sees the store plus its own uncommitted writes.

Three rules keep the overlay honest. What this process holds is the truth for
it (``_known`` nodes, ``_out_known`` nodes, ``_pairs_known`` pairs), and a
stored row never overwrites it. A node this process removed is doomed in the
store too -- the writer cascades its edges on both endpoints -- so no stored
edge touching it is ever faulted in. And a read that genuinely needs the whole
graph still gets it, by loading everything once (``_whole``), logged and
recorded in ``whole_graph_reads``: correct and slow is the failure mode, never
wrong. `tests/test_store_backed_graph.py` asserts the sync paths never take it,
and that a sync through this graph writes exactly the rows the resident one
does.

Only a process that *writes* holds one of these, and never one that serves:
`SyncEngine.serving_graph` hands such a process the store. Retrieval reads are
untouched -- a serving replica still reads ``SqlGraph`` and a standalone server
still serves its resident graph.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pheasant.graph.simple import NodeView, SimpleMultiDiGraph, _search_blob

logger = logging.getLogger(__name__)


class _FaultingNodeView(NodeView):
    """``graph.nodes``: point reads fault the node in; iteration is whole-graph."""

    def __getitem__(self, key):
        self.graph._fault_nodes((key,))
        return super().__getitem__(key)

    def __contains__(self, key):
        self.graph._fault_nodes((key,))
        return super().__contains__(key)

    def get(self, key, default=None):
        self.graph._fault_nodes((key,))
        return super().get(key, default)

    def __iter__(self):
        self.graph._whole("nodes iteration")
        return super().__iter__()

    def __call__(self, data: bool = False):
        self.graph._whole("nodes(data=...)")
        return super().__call__(data)


class _FaultingNodeMap:
    """``node_map()``: the point-lookup map the passes hold under ``reading()``."""

    __slots__ = ("_graph",)

    def __init__(self, graph: StoreBackedGraph) -> None:
        self._graph = graph

    def get(self, key, default=None):
        self._graph._fault_nodes((key,))
        return self._graph._nodes.get(key, default)

    def __getitem__(self, key):
        self._graph._fault_nodes((key,))
        return self._graph._nodes[key]

    def __contains__(self, key):
        self._graph._fault_nodes((key,))
        return key in self._graph._nodes

    def __iter__(self):
        self._graph._whole("node_map() iteration")
        return iter(self._graph._nodes)

    def items(self):
        self._graph._whole("node_map().items()")
        return self._graph._nodes.items()

    def keys(self):
        self._graph._whole("node_map().keys()")
        return self._graph._nodes.keys()

    def values(self):
        self._graph._whole("node_map().values()")
        return self._graph._nodes.values()

    def __len__(self):
        self._graph._whole("len(node_map())")
        return len(self._graph._nodes)


class StoreBackedGraph(SimpleMultiDiGraph):
    """A working set that reads the store instead of loading it. See the module."""

    def __init__(self, rows: Any, kb_id: str) -> None:
        super().__init__()
        self._rows = rows
        self._kb_id = kb_id
        #: Nodes whose state here is the truth for this process: held, or
        #: known absent (never stored, or removed by this process).
        self._known: set[str] = set()
        #: Nodes every one of whose outgoing pairs is held here.
        self._out_known: set[str] = set()
        #: Endpoint pairs whose state here is the truth: held, or known absent.
        self._pairs_known: set[tuple[str, str]] = set()
        #: Set once the whole graph has been read; every fault is then a no-op.
        self._complete = False
        #: Why the whole graph was read, if it ever was. Empty on every sync
        #: path; the tests assert that it stays so.
        self.whole_graph_reads: list[str] = []
        #: Round trips made to fault things in, by kind. For the tests and for
        #: reasoning about a sync's cost.
        self.fault_queries = {"nodes": 0, "outs": 0, "pairs": 0, "selections": 0}
        self.nodes = _FaultingNodeView(self)

    # --- faulting -------------------------------------------------------

    def _doomed(self, node_id: str) -> bool:
        """Removed by this process: its stored edges die with the next commit."""

        return node_id in self._pending_removed_nodes

    def _fault_nodes(self, node_ids) -> None:
        if self._complete:
            return
        with self._lock:
            missing = sorted({node for node in node_ids if node not in self._known})
            if not missing:
                return
            self.fault_queries["nodes"] += 1
            stored = self._rows.select_nodes(self._kb_id, ids=missing)
            for node_id in missing:
                self._known.add(node_id)
                attrs = stored.get(node_id)
                if attrs is not None:
                    self._hold_node(node_id, attrs)

    def _hold_node(self, node_id: str, attrs: dict[str, Any]) -> None:
        """Hold a stored node. Not a change: nothing is marked for any writer."""

        self._nodes[node_id] = attrs
        self._search_blobs[node_id] = _search_blob(attrs)
        self._retype(None, attrs.get("type"))

    def _hold_pair(self, pair: tuple[str, str], stored: list[dict[str, Any]]) -> None:
        """Hold a stored pair, unless something here already says what it is."""

        if pair in self._pairs_known:
            return
        self._pairs_known.add(pair)
        source, target = pair
        if self._doomed(source) or self._doomed(target):
            return
        self._edges[pair] = dict(enumerate(stored))
        self._edge_count += len(stored)
        self._out.setdefault(source, {})[target] = None

    def _fault_out(self, sources) -> None:
        if self._complete:
            return
        with self._lock:
            missing = sorted({node for node in sources if node not in self._out_known})
            if not missing:
                return
            ask = [node for node in missing if not self._doomed(node)]
            if ask:
                self.fault_queries["outs"] += 1
                stored = self._rows.out_edge_pairs(self._kb_id, ask)
                for pair, edges in stored.items():
                    self._hold_pair(pair, edges)
            # A pair the store does not hold is absent; one held here was
            # already the truth. Either way every pair of these nodes is known.
            self._out_known.update(missing)

    def _fault_pairs(self, pairs) -> None:
        if self._complete:
            return
        with self._lock:
            missing = sorted(
                {
                    (source, target)
                    for source, target in pairs
                    if (source, target) not in self._pairs_known and source not in self._out_known
                }
            )
            if not missing:
                return
            ask = [pair for pair in missing if not (self._doomed(pair[0]) or self._doomed(pair[1]))]
            if ask:
                self.fault_queries["pairs"] += 1
                # A cross product, so it can return pairs nobody asked for.
                # Each comes back whole, so holding it is as sound as holding
                # the ones that were asked for.
                stored = self._rows.out_edge_pairs(
                    self._kb_id,
                    {source for source, _target in ask},
                    {target for _source, target in ask},
                )
                for pair, edges in stored.items():
                    if pair[0] not in self._out_known:
                        self._hold_pair(pair, edges)
            self._pairs_known.update(missing)

    def _whole(self, reason: str) -> None:
        """Read everything not yet held, once. Correct, and the slow path."""

        if self._complete:
            return
        with self._lock:
            if self._complete:
                return
            logger.warning(
                "The indexer's working set read the whole graph (%s); this sync pays a full load",
                reason,
            )
            self.whole_graph_reads.append(reason)
            for node_id, attrs in self._rows.iter_nodes(self._kb_id):
                if node_id not in self._known:
                    self._known.add(node_id)
                    self._hold_node(node_id, attrs)
            for pair, parallel in self._rows.iter_edges(self._kb_id):
                if pair[0] not in self._out_known:
                    self._hold_pair(pair, list(parallel.values()))
            self._complete = True

    def prefetch(self, nodes=(), outs=(), pairs=()) -> None:
        """Fault in a batch: one round trip per kind instead of one per read."""

        nodes = list(nodes)
        if nodes:
            self._fault_nodes(nodes)
        outs = list(outs)
        if outs:
            self._fault_out(outs)
        pairs = list(pairs)
        if pairs:
            self._fault_pairs(pairs)

    # --- reads that fault -------------------------------------------------

    def __contains__(self, node):
        self._fault_nodes((node,))
        return super().__contains__(node)

    def has_node(self, node):
        self._fault_nodes((node,))
        return super().has_node(node)

    def __len__(self):
        self._whole("len(graph)")
        return super().__len__()

    def __bool__(self):
        # Otherwise truthiness would go through `__len__` and read everything.
        return True

    def get_edge_data(self, source, target, default=None):
        self._fault_pairs(((source, target),))
        return super().get_edge_data(source, target, default)

    def neighbors(self, node):
        self._fault_out((node,))
        return super().neighbors(node)

    def out_edges(self, node):
        self._fault_out((node,))
        return super().out_edges(node)

    def out_edges_batch(self, node_ids, targets=None, priority_types=(), limit_per_source=None):
        node_ids = list(node_ids)
        self._fault_out(node_ids)
        return super().out_edges_batch(node_ids, targets, priority_types, limit_per_source)

    def prefetch_nodes(self, node_ids, materialized=False):
        node_ids = list(node_ids)
        self._fault_nodes(node_ids)
        return super().prefetch_nodes(node_ids, materialized)

    def index_rows_for(self, node_ids):
        node_ids = list(node_ids)
        self._fault_nodes(node_ids)
        return super().index_rows_for(node_ids)

    def node_map(self):
        return super().node_map() if self._complete else _FaultingNodeMap(self)

    def settled_node_link(self):
        # A partial graph is never the published generation, settled or not.
        return super().settled_node_link() if self._complete else None

    # --- reads that need everything ---------------------------------------

    def edges(self):
        self._whole("edges()")
        return super().edges()

    def in_edges_batch(self, node_ids):
        self._whole("in_edges_batch")
        return super().in_edges_batch(node_ids)

    def number_of_nodes(self):
        self._whole("number_of_nodes()")
        return super().number_of_nodes()

    def number_of_edges(self):
        self._whole("number_of_edges()")
        return super().number_of_edges()

    def type_counts(self):
        self._whole("type_counts()")
        return super().type_counts()

    def iter_nodes(self):
        self._whole("iter_nodes()")
        return super().iter_nodes()

    def iter_edges(self):
        self._whole("iter_edges()")
        return super().iter_edges()

    def search_blobs(self):
        self._whole("search_blobs()")
        return super().search_blobs()

    def index_rows(self):
        self._whole("index_rows()")
        return super().index_rows()

    def to_node_link(self):
        self._whole("to_node_link()")
        return super().to_node_link()

    # --- writes: hold what they change first ------------------------------

    def add_node(self, node, **attrs):
        # `add_node` merges over what the node already has, so the stored
        # version has to be here first or the merge drops its attributes.
        self._fault_nodes((node,))
        super().add_node(node, **attrs)

    def add_edge(self, source, target, **attrs):
        with self._lock:
            self._fault_pairs(((source, target),))
            super().add_edge(source, target, **attrs)
            self._pairs_known.add((source, target))

    def touch_edge(self, source, target):
        self._fault_pairs(((source, target),))
        super().touch_edge(source, target)

    def remove_edges_from(self, edges):
        edges = list(edges)
        with self._lock:
            self._fault_pairs(edges)
            super().remove_edges_from(edges)
            self._pairs_known.update(edges)

    def remove_edges_where(self, source, target, predicate) -> int:
        with self._lock:
            self._fault_pairs(((source, target),))
            return super().remove_edges_where(source, target, predicate)

    def remove_nodes_from(self, nodes):
        remove = set(nodes)
        if not remove:
            return
        with self._lock:
            unknown = sorted(node for node in remove if node not in self._known)
            if unknown and not self._complete:
                # Removing needs to know whether a node exists, not what it
                # holds -- a whole source can be thousands of nodes -- so
                # existence is read without decoding anything.
                self.fault_queries["nodes"] += 1
                for node_id in self._rows.existing_node_ids(self._kb_id, unknown):
                    self._removed_nodes.add(node_id)
                    self._pending_removed_nodes.add(node_id)
                self._known.update(unknown)
            super().remove_nodes_from(remove)
            self._known.update(remove)
            # Nothing leaves a removed node any more; the store's copy of its
            # edges goes with the commit.
            self._out_known.update(remove)

    # --- selections: a query, overlaid with what this process holds -------

    def select_nodes(
        self, *, types=None, source_id=None, ids=None, artifact_ids=None, promoted_only=False
    ):
        if self._complete:
            return super().select_nodes(
                types=types, source_id=source_id, ids=ids, artifact_ids=artifact_ids
            )
        with self._lock:
            self.fault_queries["selections"] += 1
            stored = self._rows.select_nodes(
                self._kb_id,
                types=types,
                source_id=source_id,
                ids=ids,
                artifact_ids=artifact_ids,
                promoted_only=promoted_only,
            )
            found = {
                node_id: attrs for node_id, attrs in stored.items() if node_id not in self._known
            }
            for node_id, attrs in super().select_nodes(
                types=types, source_id=source_id, ids=ids, artifact_ids=artifact_ids
            ):
                found[node_id] = attrs
            return list(found.items())

    def edges_into_types(self, target_types):
        if self._complete:
            return super().edges_into_types(target_types)
        wanted = set(target_types)
        with self._lock:
            self.fault_queries["selections"] += 1
            stored = self._rows.edge_pairs_into_types(self._kb_id, wanted)
            # The type of a target this process holds is what this process
            # says it is, which a stored join cannot know.
            self._fault_nodes({target for _source, target in self._edges})
            found = []
            for (source, target), edges in stored.items():
                if (source, target) in self._pairs_known or source in self._out_known:
                    continue
                if self._doomed(source) or self._doomed(target):
                    continue
                if (
                    target in self._known
                    and (self._nodes.get(target) or {}).get("type") not in wanted
                ):
                    continue
                found.append((source, target, dict(enumerate(edges))))
            found.extend(super().edges_into_types(wanted))
            return found

    def targets_with_incoming(self, node_ids) -> set:
        wanted = set(node_ids)
        if not wanted or self._complete:
            return super().targets_with_incoming(wanted)
        with self._lock:
            self.fault_queries["selections"] += 1
            linked = super().targets_with_incoming(wanted)
            for source, target in self._rows.pairs_into(self._kb_id, wanted):
                if (source, target) in self._pairs_known or source in self._out_known:
                    continue
                if self._doomed(source) or self._doomed(target):
                    continue
                linked.add(target)
            return linked

    def out_edges_marked(self, sources, key, value):
        sources = list(sources)
        if self._complete:
            return super().out_edges_marked(sources, key, value)
        with self._lock:
            held = [source for source in sources if source in self._out_known]
            found = super().out_edges_marked(held, key, value)
            rest = [source for source in sources if source not in self._out_known]
            if not rest:
                return found
            self.fault_queries["selections"] += 1
            needle = f"{json.dumps(key)}:{json.dumps(value)}"
            stored = self._rows.edge_pairs_containing(self._kb_id, rest, needle)
            for (source, target), edges in stored.items():
                if self._doomed(source) or self._doomed(target):
                    continue
                if (source, target) in self._pairs_known:
                    matching = [
                        attrs
                        for attrs in (self._edges.get((source, target)) or {}).values()
                        if attrs.get(key) == value
                    ]
                else:
                    matching = [attrs for attrs in edges if attrs.get(key) == value]
                if matching:
                    found.append((source, target, dict(enumerate(matching))))
            # A held pair from a source not fully held can carry a match the
            # stored query did not return (it was written here, not yet saved).
            stored_pairs = set(stored)
            for source, target in self._pairs_known:
                if source in rest and (source, target) not in stored_pairs:
                    matching = [
                        attrs
                        for attrs in (self._edges.get((source, target)) or {}).values()
                        if attrs.get(key) == value
                    ]
                    if matching:
                        found.append((source, target, dict(enumerate(matching))))
            return found
