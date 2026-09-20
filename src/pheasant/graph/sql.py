"""A graph you can query without holding it.

:class:`~pheasant.graph.simple.SimpleMultiDiGraph` is the indexer's working
set: the builder mutates it, whole-graph enrichment passes walk it, and it
belongs in RAM because that is what those passes need. This is the other half —
the read surface every *serving* process actually uses — backed by the
``graph_nodes`` / ``graph_edges`` rows instead of by residency.

The distinction is the point of the change. Before it, answering "what is
adjacent to this node" required the whole graph in memory, so every API
replica paid 1.5 GB at 100k files to serve bounded three-hop walks — and the
``graph`` role exists precisely to stop each replica paying it. A bounded walk
is a bounded query; it needed a store, not a snapshot.

**It implements the read protocol, not the graph.** ``out_edges``,
``nodes.get``, ``__contains__``, counts and the streaming iterators — enough
for :mod:`pheasant.graph.traversal`, :mod:`pheasant.search.graph_search`, the
exporter and the analytics extract, and nothing else. Every mutator raises:
this is not a place to write a graph from, and a silently-accepted write would
be a node that exists in one process and nowhere else.

Two methods have no counterpart on the in-memory graph, and both exist so a
walk costs one query per *level* rather than one per node:
:meth:`out_edges_batch` and :meth:`prefetch_nodes`. ``SimpleMultiDiGraph``
implements them too — trivially, since its lookups are already O(1) — so the
traversal has one code path rather than a branch.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from contextlib import nullcontext
from typing import Any

from pheasant.persistence.graph_rows import GraphRowStore


class _NodeView:
    """``graph.nodes`` over rows, with the mapping surface callers already use."""

    def __init__(self, graph: SqlGraph) -> None:
        self.graph = graph

    def __getitem__(self, key: str) -> dict[str, Any]:
        found = self.graph.rows.get_node(self.graph.kb_id, key)
        if found is None:
            raise KeyError(key)
        return found

    def __contains__(self, key: str) -> bool:
        return self.graph.rows.get_node(self.graph.kb_id, key) is not None

    def get(self, key: str, default: Any = None) -> Any:
        found = self.graph.rows.get_node(self.graph.kb_id, key)
        return default if found is None else found

    def __iter__(self) -> Iterator[str]:
        return (node_id for node_id, _attrs in self.graph.iter_nodes())

    def __call__(self, data: bool = False):
        if data:
            return list(self.graph.iter_nodes())
        return [node_id for node_id, _attrs in self.graph.iter_nodes()]


class _LazyNodeMap:
    """``node_map()`` for a store-backed graph: fetch on ask, remember the answer.

    ``_scan_edges`` reads the label of both endpoints of every edge it scores.
    On the in-memory graph that is a dict lookup; here it would be a query per
    endpoint, and the same handful of hub nodes are asked for over and over
    within one scan. Bounded because it lives for one scan and the scan is
    already bounded by its candidate set.

    **It answers ``get`` and deliberately nothing else.** The in-memory graph's
    ``node_map()`` is the live node dict, so a caller can iterate it, call
    ``.items()`` on it, or test membership — and every one of those is a *full
    scan of the region* once the graph lives behind a store. Two call sites had
    already been written that way against the dict and crashed here with a bare
    ``TypeError: '_LazyNodeMap' object is not iterable``, on the row backend,
    which is the default: ``/graph/diagnostics`` iterated it and ``/taxonomy``
    called ``.items()``.

    Supplying those methods was the tempting fix and the wrong one. It would
    make an accidental whole-graph scan *work*, silently, on the exact path the
    row backend exists to keep off a serving replica — the same shape as every
    other "free while it was a dict in front of you" entry in this codebase's
    history. So they refuse, and the refusal names the accessor that is honest
    about its cost. A caller that genuinely wants every node wants
    :meth:`SqlGraph.iter_nodes`, which streams and says so.

    **And yes, :class:`_NodeView` next door does iterate.** That is deliberate
    rather than an oversight in one of them. ``graph.nodes`` is the general
    read surface, shaped like the node view callers already write against, and
    iterating a node view reads as a whole-graph pass because it is one. This
    is ``node_map()``: a cache handed out *for one bounded scan* precisely so
    repeated point lookups of the same hub nodes cost one query instead of
    many. Walking it would bypass the only thing it is for and scan the region
    anyway, so the two surfaces differ because the questions differ.
    """

    #: Named in every refusal below, so the message carries the fix.
    _WHOLE_GRAPH_HINT = (
        "a store-backed node_map() answers get() only, because walking it is a "
        "full scan of the region. Use graph.iter_nodes() for a whole-graph pass "
        "(it streams, on both backends), or get() for point lookups."
    )

    def __init__(self, graph: SqlGraph) -> None:
        self.graph = graph
        self._seen: dict[str, dict[str, Any] | None] = {}

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self._seen:
            self._seen[key] = self.graph.rows.get_node(self.graph.kb_id, key)
        found = self._seen[key]
        return default if found is None else found

    def __iter__(self) -> Any:
        raise TypeError(self._WHOLE_GRAPH_HINT)

    def items(self) -> Any:
        raise TypeError(self._WHOLE_GRAPH_HINT)

    def keys(self) -> Any:
        raise TypeError(self._WHOLE_GRAPH_HINT)

    def values(self) -> Any:
        raise TypeError(self._WHOLE_GRAPH_HINT)

    def __len__(self) -> int:
        # Not a scan either: `number_of_nodes` is a maintained count.
        raise TypeError(f"{self._WHOLE_GRAPH_HINT} For a count, use number_of_nodes().")


class SqlGraph:
    """Read-only graph over the state store's ``graph_*`` tables."""

    #: Read by the serving code the way ``RemoteGraph.is_remote_graph`` is, so
    #: a caller can tell "this graph is not resident" without an isinstance.
    is_stored_graph = True

    #: TTL on the per-type tally. The in-memory graph maintains that tally on
    #: write because "re-counting 240k node types per request was costing
    #: seconds on endpoints the UI polls"; here it is a ``GROUP BY``, and the
    #: same polling would put a full scan on every one. Same 2s as
    #: ``RemoteGraph.stats``, and the same reasoning.
    TYPE_COUNT_TTL = 2.0

    def __init__(self, state: Any, kb_id: str) -> None:
        self.rows = GraphRowStore(state)
        self.kb_id = kb_id
        self.nodes = _NodeView(self)
        self._types: dict[str, int] | None = None
        self._types_at = 0.0

    # --- reads -----------------------------------------------------------

    def reading(self):
        """No-op pin.

        The in-memory graph hands readers a lock because a sync thread mutates
        it underneath them. Here the database provides the isolation, and
        promising a cross-call transaction this does not open would be worse
        than promising nothing — the same reasoning ``RemoteGraph.reading``
        already applies.
        """

        return nullcontext()

    def __contains__(self, node_id: str) -> bool:
        return self.rows.get_node(self.kb_id, node_id) is not None

    def has_node(self, node_id: str) -> bool:
        return node_id in self

    def __len__(self) -> int:
        return self.number_of_nodes()

    def number_of_nodes(self) -> int:
        return self.rows.counts(self.kb_id)[0]

    def number_of_edges(self) -> int:
        return self.rows.counts(self.kb_id)[1]

    def type_counts(self) -> dict[str, int]:
        now = time.monotonic()
        if self._types is None or now - self._types_at > self.TYPE_COUNT_TTL:
            self._types = self.rows.type_counts(self.kb_id)
            self._types_at = now
        return dict(self._types)

    def out_edges(self, node_id: str) -> list[tuple[str, str, dict[int, dict]]]:
        return self.out_edges_batch([node_id]).get(node_id, [])

    def out_edges_batch(
        self,
        node_ids: list[str],
        targets: list[str] | None = None,
        priority_types: Sequence[str] = (),
        limit_per_source: int | None = None,
    ) -> dict[str, list[tuple[str, str, dict[int, dict]]]]:
        """One query for a whole BFS frontier, grouped the way callers expect.

        Parallel edges between one pair are collapsed into the ``{key: attrs}``
        map ``out_edges`` returns, so a caller cannot tell which backend
        answered.

        ``targets`` narrows to the induced sub-graph, which is what a bounded
        slice asks for and what keeps a hub node's fan-out off the wire.
        ``priority_types`` and ``limit_per_source`` do the same for a bounded
        *walk*: the pairs come back priority-first, so a prefix of them is the
        prefix the caller would have taken anyway.
        """

        # Already grouped by pair in the store, which is the shape callers
        # want: regrouping here meant a second pass and a second set of
        # objects over every row a hub node returns.
        return self.rows.out_edges(self.kb_id, node_ids, targets, priority_types, limit_per_source)

    def prefetch_nodes(
        self, node_ids: list[str], materialized: bool = False
    ) -> dict[str, dict[str, Any]]:
        """Attributes for a whole frontier, in one query.

        ``materialized`` says the caller will read every attribute, so the row
        is decoded straight into a dict instead of into the lazy mapping a
        traversal hop wants. See :func:`~pheasant.persistence.graph_codec.node_attrs_dict`.
        """

        return self.rows.get_nodes(self.kb_id, node_ids, materialized)

    def search_candidates(
        self, node_index: Any, tokens: list[str], source_name: str | None = None
    ) -> dict[str, dict[str, Any]] | None:
        """Candidate nodes *with* their attributes, in one round trip.

        The index and the nodes are two tables in one database, so asking the
        index for ids and then asking for those ids is a round trip that
        exists only because the two live behind different objects in Python.
        ``None`` keeps the caller's existing contract: the index could not
        answer, so scan.

        The in-memory graph has no counterpart and needs none — there the ids
        come back and the lookup is a dict — so
        :func:`~pheasant.search.graph_search.search_graph` asks for this and
        falls back to the two-step, exactly as it does for the batch methods.
        """

        built = node_index.candidate_query(tokens, source_name=source_name)
        if built is None:
            return None
        subquery, params = built
        try:
            found = self.rows.nodes_matching(self.kb_id, subquery, params, materialized=True)
        except Exception:
            # Same posture as `NodeIndex.candidates`: a malformed MATCH must
            # degrade to the scan rather than fail the search.
            return None
        if found:
            return found
        return found if node_index.still_populated() else None

    def neighbors(self, node_id: str) -> list[str]:
        return [target for _source, target, _edges in self.out_edges(node_id)]

    def get_edge_data(self, source: str, target: str, default: Any = None) -> Any:
        for edge_source, edge_target, edge_map in self.out_edges(source):
            if edge_source == source and edge_target == target:
                return edge_map
        return default

    def node_map(self):
        return _LazyNodeMap(self)

    def search_blobs(self) -> dict[str, str]:
        """Deliberately empty.

        The in-memory graph keeps a lowercased concatenation of every node's
        attributes so a query can reject most nodes with one substring test.
        That is a residency optimization, and materializing it here would
        re-materialize the graph. ``search_graph`` treats a missing blob as
        "cannot reject" and scores the candidate instead, which is correct:
        by the time it gets here the ``graph_nodes_fts`` index has already
        done the narrowing the blob existed to do.
        """

        return {}

    def iter_nodes(self) -> Iterator[tuple[str, dict[str, Any]]]:
        return self.rows.iter_nodes(self.kb_id)

    def iter_edges(self) -> Iterator[tuple[tuple[str, str], dict[int, dict]]]:
        return self.rows.iter_edges(self.kb_id)

    def edges(self) -> list[tuple[tuple[str, str], dict[int, dict]]]:
        return list(self.iter_edges())

    def candidate_edges(
        self, tokens: list[str], source_name: str | None = None, limit: int = 2000
    ) -> Iterator[tuple[tuple[str, str], dict[int, dict]]]:
        """Edges worth scoring, for relationship search. See the row store."""

        return self.rows.candidate_edges(self.kb_id, tokens, source_name, limit)

    def index_rows(self) -> list[tuple[str, str, str]]:
        """Rebuild input for ``graph_nodes_fts``.

        Streamed from the same database the index lives in, which is why a
        rebuild on a serving replica is now possible at all.
        """

        from pheasant.graph.simple import _search_blob

        return [
            (node_id, str(attrs.get("source_id") or ""), _search_blob(attrs))
            for node_id, attrs in self.iter_nodes()
        ]

    def to_node_link(self) -> dict[str, Any]:
        """The node-link payload, materialized from rows.

        O(graph) and honestly so: the callers are the exporter, a snapshot and
        the Synapse contract, all of which want the whole graph by definition.
        Byte-compatible with what the file backend produced — same key order,
        same edge canonicalization — so a snapshot taken from rows and one
        taken from a file are the same document.
        """

        nodes = [attrs for _node_id, attrs in self.iter_nodes()]
        links = []
        for (source, target), edge_map in self.iter_edges():
            for key, data in enumerate(edge_map.values()):
                links.append({"source": source, "target": target, "key": key, **data})
        return {
            "directed": True,
            "multigraph": True,
            "graph": {},
            "nodes": nodes,
            "links": links,
        }

    # --- writes ----------------------------------------------------------

    def _read_only(self, *_args: Any, **_kwargs: Any):
        raise TypeError(
            "SqlGraph is the serving read surface and cannot be mutated. The indexer "
            "builds into a SimpleMultiDiGraph and commits the delta through GraphStore; "
            "a write here would be a node that exists in one process and nowhere else."
        )

    add_node = _read_only
    add_edge = _read_only
    remove_nodes_from = _read_only
    remove_edges_from = _read_only
