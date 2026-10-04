"""Bounded walks over a knowledge graph.

Pure domain: a graph in, nodes out. No application context, no transport, no
knowledge of who is asking — which is what lets the HTTP surface, the MCP
surface and the assistant all call the same walk instead of the three
implementations that existed before (`api.app.graph_neighbors`,
`PheasantTools.get_graph_neighbors`, and `PheasantTools.get_graph_slice`'s own
partial third).

Each function takes the *serving* graph rather than reaching for one, because
on an API replica pointed at the graph service it is a remote proxy and not a
resident snapshot. The ``remote_*`` checks are the seam: when the graph is
remote, the walk happens where the graph lives.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from typing import Any

from pheasant.graph.simple import SimpleMultiDiGraph

#: Edges that describe structure rather than a shortcut. Walked first so a
#: bounded view shows the tree rather than a flat fan of artifacts.
HIERARCHY_EDGE_TYPES = ("contains",)

#: Hard ceiling on traversal depth, whatever a caller asks for. Ten hops off a
#: hub node is already most of the graph.
MAX_DEPTH = 10


def _hierarchy_first(out_edges: list) -> list:
    """Structural edges before shortcuts, order otherwise preserved."""

    def rank(entry) -> int:
        types = {data.get("type") for data in entry[2].values()}
        return 0 if types & set(HIERARCHY_EDGE_TYPES) else 1

    return sorted(out_edges, key=rank)


def neighbors(
    graph: SimpleMultiDiGraph,
    node_id: str,
    depth: int = 1,
    edge_types: list[str] | None = None,
    max_nodes: int | None = None,
    exclude_edge_types: set[str] | None = None,
    exclude_node_types: set[str] | None = None,
) -> dict[str, Any]:
    """Breadth-first neighbour expansion.

    ``max_nodes`` stops the walk once that many neighbours are collected.
    Callers that only keep the first N (the canvas asks for a bounded horizon)
    must pass it: three hops off a hub node reaches most of the graph, and
    enumerating all of it to then throw nearly all of it away is what made a
    depth-3 slice take minutes. Truncation is in BFS order either way, so the
    kept set is identical — only the work is smaller.

    The MCP tool used to implement this again, without the hierarchy-first
    ordering, without ``max_nodes`` and without either exclusion — so the two
    surfaces returned different neighbours for the same node, in a different
    order, and only one of them could be bounded.
    """

    remote = getattr(graph, "remote_neighbors", None)
    if callable(remote):
        return remote(
            node_id=node_id,
            depth=depth,
            edge_types=edge_types,
            max_nodes=max_nodes,
            exclude_edge_types=sorted(exclude_edge_types) if exclude_edge_types else None,
            exclude_node_types=sorted(exclude_node_types) if exclude_node_types else None,
        )
    if node_id not in graph:
        return {"node_id": node_id, "depth": depth, "neighbors": []}
    max_depth = max(1, min(int(depth or 1), MAX_DEPTH))
    allowed = set(edge_types or [])
    # One *level* at a time rather than one node at a time. The output is
    # identical — a FIFO queue expands in exactly this order, and the frontier
    # is iterated in the order its nodes were discovered — but it lets a
    # store-backed graph fetch the whole level's edges and node attributes in
    # two queries instead of two per node. On the in-memory graph the batch
    # methods are the same dict lookups under one lock hold.
    frontier: deque = deque([(node_id, 0, [node_id])])
    visited = {node_id}
    found: list[dict] = []
    while frontier:
        if max_nodes is not None and len(found) >= max_nodes:
            break
        current_depth = frontier[0][1]
        if current_depth >= max_depth:
            break
        level = list(frontier)
        frontier = deque()
        adjacency = _out_edges_for(
            graph,
            [current for current, _d, _p in level],
            priority_types=HIERARCHY_EDGE_TYPES,
            limit_per_source=_frontier_budget(
                max_nodes, len(visited), allowed, exclude_edge_types, exclude_node_types
            ),
        )
        # Attributes are needed for the targets this level actually *keeps* —
        # `max_nodes` of them — plus any it inspects and rejects. Fetching every
        # reachable target instead is the shape that made a hub node expensive:
        # one `source` node indexes every artifact, so a depth-3 walk asking for
        # 100 neighbours fetched **8,040** node rows to keep 100. Measured 163ms
        # against 13ms for the same walk over a resident graph, and it is the
        # kind of waste only a real corpus exposes — a fixture's hub has four
        # edges. `_BatchedNodes` keeps the batching (one query per block, not
        # per node) and spends it only on what the loop reaches.
        reachable = [
            target
            for entries in adjacency.values()
            for _source, target, _edge_map in entries
            if target not in visited
        ]
        attributes = _nodes_for(graph, reachable, budget=max_nodes)
        for current, current_depth, path in level:
            if max_nodes is not None and len(found) >= max_nodes:
                break
            # Hierarchy first. A source node carries an `indexes` shortcut to
            # every one of its artifacts, so a plain fan-out spends the whole
            # budget jumping straight to files and the directory tree between
            # them never gets walked — the parent/child structure is present in
            # the graph but invisible in any bounded view of it.
            # Already hierarchy-first: `_out_edges_for` was asked for that
            # order, and both backends promise it. Re-sorting here would be a
            # stable no-op that costs a sort per level — and on the bounded
            # path it would be sorting the wrong thing anyway, since the store
            # already took its prefix in this order.
            for _source, target, edge_map in adjacency.get(current, []):
                matching = [
                    data
                    for data in edge_map.values()
                    if (not allowed or data.get("type") in allowed)
                    and not (exclude_edge_types and data.get("type") in exclude_edge_types)
                ]
                if not matching:
                    continue
                # Types the caller hides are pruned here rather than after the
                # fetch: a concept-heavy graph otherwise spends the entire
                # budget on nodes the view is about to discard, and the
                # structure the caller actually asked for never fits.
                if exclude_node_types:
                    target_type = (attributes.get(target) or {}).get("type")
                    if target_type in exclude_node_types:
                        continue
                next_depth = current_depth + 1
                # One entry per *node*, not per edge into it. `visited` guarded the
                # queue but not the append, so a node reachable by two paths was
                # listed twice — every consumer treats this as a node list, and
                # `slice_` built its `nodes` payload straight off it, so the canvas
                # received duplicate element ids. It also charged the same node to
                # the budget twice, cutting a bounded slice short of the structure
                # it was asked for. First sighting wins, which is BFS order and
                # therefore the shortest path — the same rule `depths` applies. No
                # edge is lost: `slice_` derives links from the graph itself, so
                # both parents still draw.
                if target in visited:
                    continue
                visited.add(target)
                edge_type_values = sorted(
                    {data.get("type") for data in matching if data.get("type")}
                )
                found.append(
                    {
                        "node_id": target,
                        "depth": next_depth,
                        "edge_types": edge_type_values,
                        "path": [*path, target],
                        "node": dict(attributes.get(target) or {}),
                    }
                )
                frontier.append((target, next_depth, [*path, target]))
                # A single hub can have thousands of out-edges, so the budget
                # has to bind inside the fan-out, not just between hops.
                if max_nodes is not None and len(found) >= max_nodes:
                    break
    return {"node_id": node_id, "depth": depth, "neighbors": found}


def _frontier_budget(
    max_nodes: int | None,
    visited: int,
    allowed: set[str],
    exclude_edge_types: set[str] | None,
    exclude_node_types: set[str] | None,
) -> int | None:
    """How many pairs per source the walk can possibly need, or None.

    A hub returns 1,620 pairs so a walk can keep 100, and the fetch is where
    that costs — 10.4ms against 3.8ms once the rows are decoded and grouped.
    Bounding it is only *sound* when nothing between the fetch and the
    ``found`` list can reject a pair, because every rejection means the walk
    must read one more pair than the budget accounts for:

    - ``edge_types`` / ``exclude_edge_types`` reject by edge type. A hub whose
      1,600 ``indexes`` pairs are all filtered out needs every one of them to
      reach its 20 ``contains`` pairs.
    - ``exclude_node_types`` rejects by the *target's* type, which is
      unbounded for the same reason.

    With none of those set, the only reason a pair does not become a
    neighbour is that its target is already visited — and there are at most
    ``visited`` of those in the whole graph, let alone under one source. So
    ``max_nodes + visited`` pairs is provably enough, and asking for fewer is
    the only way this could change an answer.

    Returning ``None`` for the filtered walks keeps them exactly as they were:
    slower than they could be, and correct, which is the right way round.
    """

    if max_nodes is None or allowed or exclude_edge_types or exclude_node_types:
        return None
    return int(max_nodes) + int(visited)


def _out_edges_for(
    graph: Any,
    node_ids: list[str],
    targets: list[str] | None = None,
    priority_types: Sequence[str] = (),
    limit_per_source: int | None = None,
) -> dict[str, list]:
    """Outgoing edges for a whole frontier, batched when the graph can.

    ``RemoteGraph`` never reaches here — it answers the whole walk remotely —
    so the fallback is for any graph-shaped object a test or a caller passes
    that implements only the single-node primitive. Correct either way; the
    batch is the difference between one query per level and one per node.

    ``targets``, when given, asks only for edges landing inside that set. The
    caller filtered for exactly that afterwards, so nothing about the answer
    changes; what changes is who reads a hub node's 1,600 out-edges.
    """

    batch = getattr(graph, "out_edges_batch", None)
    if callable(batch):
        return batch(node_ids, targets, priority_types, limit_per_source)
    # A graph-shaped object implementing only the single-node primitive. It
    # cannot promise the priority order, so the sort happens here and no limit
    # is applied — correct, just not cheap.
    edges = {node_id: graph.out_edges(node_id) for node_id in node_ids}
    if targets is not None:
        keep = set(targets)
        edges = {
            node_id: [entry for entry in entries if entry[1] in keep]
            for node_id, entries in edges.items()
        }
    if priority_types:
        edges = {node_id: _hierarchy_first(entries) for node_id, entries in edges.items()}
    return edges


#: Node attributes fetched per block by :class:`_BatchedNodes`. Large enough
#: that a bounded walk is one or two queries, small enough that a hub node's
#: fan-out is not fetched to be thrown away.
PREFETCH_BLOCK = 256


class _BatchedNodes:
    """Attributes for the targets a walk actually reaches, a block at a time.

    Sits between "one query per node", which is the N+1 a store-backed walk
    cannot afford, and "one query for everything reachable", which is what a
    hub node makes ruinous. Candidates arrive in the order the walk will
    consume them, so a block usually satisfies many lookups; a miss outside the
    current block still resolves, at the cost of pulling that key in with the
    next one.
    """

    def __init__(self, fetch: Any, candidates: list[str], block: int) -> None:
        self._fetch = fetch
        self._candidates = candidates
        self._at = 0
        self._block = max(1, block)
        self._known: dict[str, dict | None] = {}

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self._known:
            self._fill(key)
        found = self._known.get(key)
        return default if found is None else found

    def _fill(self, key: str) -> None:
        batch: list[str] = []
        while self._at < len(self._candidates) and len(batch) < self._block:
            candidate = self._candidates[self._at]
            self._at += 1
            if candidate not in self._known:
                batch.append(candidate)
        if key not in batch and key not in self._known:
            batch.append(key)
        found = self._fetch(batch) if batch else {}
        for node_id in batch:
            self._known[node_id] = found.get(node_id)


def _nodes_for(graph: Any, node_ids: list[str], budget: int | None = None) -> Any:
    """A mapping of node attributes, batched and bounded by what is asked for.

    ``budget`` is the walk's ``max_nodes``: a level cannot keep more than that,
    so fetching every candidate spends queries on rows the truncation is about
    to discard.
    """

    batch = getattr(graph, "prefetch_nodes", None)
    if not callable(batch):
        return {node_id: (graph.nodes.get(node_id) or {}) for node_id in node_ids}
    block = PREFETCH_BLOCK if budget is None else max(1, min(int(budget), PREFETCH_BLOCK))
    if len(node_ids) <= block:
        # Small enough that laziness would only add indirection.
        return batch(node_ids)
    return _BatchedNodes(batch, node_ids, block)


def slice_(
    graph: SimpleMultiDiGraph,
    node_id: str,
    depth: int = 1,
    edge_types: list[str] | None = None,
    limit: int = 100,
    exclude_edge_types: set[str] | None = None,
    exclude_node_types: set[str] | None = None,
) -> dict[str, Any]:
    """Connected sub-graph around a node.

    Named with the trailing underscore because ``slice`` is a builtin; the
    surfaces expose it as ``graph_slice`` / ``get_graph_slice``, which are the
    names in their respective public contracts.
    """

    remote = getattr(graph, "remote_slice", None)
    if callable(remote):
        return remote(
            node_id=node_id,
            depth=depth,
            edge_types=edge_types,
            limit=limit,
            exclude_edge_types=sorted(exclude_edge_types) if exclude_edge_types else None,
            exclude_node_types=sorted(exclude_node_types) if exclude_node_types else None,
        )
    # Ask for one more neighbour than the caller will receive. Without that
    # sentinel a bounded UI slice silently looked complete whenever it filled
    # its budget, which made large documents appear to have lost chunks.
    neighbour_limit = max(0, int(limit))
    traversal = neighbors(
        graph,
        node_id,
        depth,
        edge_types,
        max_nodes=neighbour_limit + 1,
        exclude_edge_types=exclude_edge_types,
        exclude_node_types=exclude_node_types,
    )
    all_neighbors = traversal["neighbors"]
    kept = all_neighbors[:neighbour_limit]
    node_ids = [node_id] + [item["node_id"] for item in kept]
    node_set = set(node_ids)
    # Hop distance per node, nearest wins (BFS order, so the first sighting is
    # the shortest path). The UI rings the canvas by this and lets the user
    # widen the horizon a layer at a time instead of rendering the whole graph.
    depths: dict[str, int] = {node_id: 0}
    for item in kept:
        target = str(item["node_id"])
        hop = int(item.get("depth") or 0)
        if hop < depths.get(target, hop + 1):
            depths[target] = hop
    links = []
    allowed = set(edge_types or [])
    # Both of these are batched for the same reason the walk above is: a slice
    # of 100 nodes was 200 single-node lookups, which is free in RAM and 200
    # round trips against a store. The edges are asked for as an *induced
    # sub-graph* — from this node set into itself — because that is the only
    # part of the answer the loop below keeps. Without the second set a slice
    # containing one `source` node read that node's whole fan-out: 3,580 edge
    # rows to draw ~150 links, and 16.7ms of a 46ms call.
    inside = sorted(node_set)
    adjacency = _out_edges_for(graph, inside, inside)
    # Bounded by construction: `node_ids` is the start plus at most `limit`
    # kept neighbours, so this is the eager path and stays one query.
    attributes = _nodes_for(graph, node_ids)
    for source in node_set:
        for _src, target, edge_map in adjacency.get(source, []):
            # Already true of everything `adjacency` holds — kept because it is
            # the invariant the induced fetch above encodes, and a backend that
            # answered more loosely would silently draw links to nodes the
            # slice does not contain.
            if target not in node_set:
                continue
            for key, data in edge_map.items():
                if allowed and data.get("type") not in allowed:
                    continue
                links.append({"source": source, "target": target, "key": key, **data})
    return {
        "node_id": node_id,
        "depth": depth,
        "nodes": [dict(attrs) for attrs in (attributes.get(item) for item in node_ids) if attrs],
        "links": links,
        "depths": depths,
        "truncated": len(all_neighbors) > neighbour_limit,
    }


#: Node attributes an expanded neighbour carries. The walk hands back every
#: attribute a node holds, which for a search payload is mostly noise; these
#: are what a reader needs to decide whether to follow a neighbour up, and
#: `explain_node` has the rest.
EXPANSION_FIELDS = ("type", "label", "source_id", "relative_path", "artifact_id")

#: Characters of a neighbour's own summary kept in an expansion. Chunk nodes
#: carry the first 180 characters of their text; this is a ceiling, not a pad.
EXPANSION_SUMMARY_CHARS = 240


def expand(
    graph: SimpleMultiDiGraph,
    seeds: Sequence[str],
    *,
    depth: int = 1,
    edge_types: list[str] | None = None,
    exclude_edge_types: set[str] | None = None,
    max_neighbors: int = 8,
    fetch: int | None = None,
    admit: Any = None,
) -> dict[str, dict[str, Any]]:
    """The graph neighbourhood of each seed, compact and bounded.

    One :func:`neighbors` walk per *distinct* seed — two hits from one file
    share a seed and are walked once — so this is the same hierarchy-first,
    budget-bounded walk every other graph read takes, and it works unchanged
    against a remote graph.

    ``fetch`` is how many neighbours each walk collects before ``admit`` runs,
    when it is larger than ``max_neighbors``: a post-filter needs the walk to
    over-collect or a filtered neighbourhood comes back short. ``admit`` takes
    the walk's neighbour list and returns the subset a caller may see; a
    neighbour reached *through* one it removed is removed too, because its
    ``via`` would name the hidden node.

    Each neighbour reports ``via``, the node it was reached from, rather than
    the whole path: depth and parent are enough to rebuild the tree, and they
    keep a depth-3 expansion from repeating the seed's id on every line.
    """

    budget = max(int(max_neighbors), int(fetch or 0))
    expanded: dict[str, dict[str, Any]] = {}
    for seed in seeds:
        if seed in expanded:
            continue
        walk = neighbors(
            graph,
            seed,
            depth,
            edge_types,
            max_nodes=budget + 1,
            exclude_edge_types=exclude_edge_types,
        )
        found = list(walk.get("neighbors") or [])
        collected = len(found)
        if callable(admit) and found:
            allowed = admit(found)
            hidden = {str(item["node_id"]) for item in found} - {
                str(item["node_id"]) for item in allowed
            }
            found = [
                item
                for item in allowed
                if not hidden.intersection(str(step) for step in (item.get("path") or [])[1:-1])
            ]
        kept = found[: int(max_neighbors)]
        expanded[seed] = {
            "neighbors": [_compact_neighbor(item) for item in kept],
            # More was there than came back. Judged on the walk's own count
            # (it asked for one past the budget) as well as on what survived
            # the filter, so a neighbourhood cut by the budget never reads as
            # complete.
            "truncated": collected > budget or len(found) > len(kept),
        }
    return expanded


def _compact_neighbor(item: dict[str, Any]) -> dict[str, Any]:
    node = item.get("node") or {}
    path = item.get("path") or []
    compact: dict[str, Any] = {
        "node_id": item["node_id"],
        "depth": item.get("depth"),
        "edge_types": item.get("edge_types") or [],
        "via": path[-2] if len(path) >= 2 else None,
    }
    for field in EXPANSION_FIELDS:
        value = node.get(field)
        if value not in (None, ""):
            compact[field] = value
    summary = node.get("summary")
    if summary:
        compact["summary"] = str(summary)[:EXPANSION_SUMMARY_CHARS]
    return compact
