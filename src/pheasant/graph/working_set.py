"""A mutable row-backed graph: cache touched nodes and pairs, persist their delta.

The cache is an implementation detail, never the universe of graph content.
Hydration does not create a write, and an updated pair retains all its parallel
edges. Whole-graph iteration is reserved for exports and explicit bulk work.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pheasant.graph.simple import SimpleMultiDiGraph, _search_blob
from pheasant.persistence.graph_codec import _pair_clauses, edge_attrs_dict, node_attrs_dict


class RowNodes(Mapping):
    def __init__(self, graph: RowWorkingSet):
        self.graph = graph

    def __getitem__(self, key):
        self.graph.prefetch_nodes([key])
        return self.graph._nodes[key]

    def __iter__(self):
        return (key for key, _ in self.graph.iter_nodes())

    def __len__(self):
        return self.graph.number_of_nodes()

    def __call__(self, data=False):
        return list(self.graph.iter_nodes()) if data else list(self)


class RowWorkingSet(SimpleMultiDiGraph):
    is_row_working_set = True

    def __init__(self, rows: Any, kb_id: str):
        super().__init__()
        self.rows, self.kb_id = rows, kb_id
        self.nodes = RowNodes(self)
        self._seen_nodes: set[str] = set()
        self._seen_pairs: set[tuple[str, str]] = set()
        self._seen_out: set[str] = set()
        self._deleted: set[str] = set()
        self.reference_catalog = None
        self.catalog_dirty_nodes: set[str] = set()
        self.catalog_dirty_origins: set[str] = set()

    def prefetch_nodes(self, node_ids, materialized=True):
        node_ids = list(node_ids)
        ids = set(node_ids) - self._seen_nodes - self._deleted
        if ids:
            found = self.rows.get_nodes(self.kb_id, sorted(ids), materialized=True)
            for key, attrs in found.items():
                self._nodes[key] = dict(attrs)
                self._search_blobs[key] = _search_blob(attrs)
            self._seen_nodes.update(ids)
        return {key: self._nodes[key] for key in node_ids if key in self._nodes}

    def has_node(self, node):
        self.prefetch_nodes([node])
        return node in self._nodes

    __contains__ = has_node

    def add_node(self, node, **attrs):
        self.catalog_dirty_nodes.add(node)
        self.prefetch_nodes([node])
        self._deleted.discard(node)
        self._seen_nodes.add(node)
        super().add_node(node, **attrs)

    def _hydrate_pair(self, source, target, parallel):
        pair = (source, target)
        if pair in self._seen_pairs:
            return
        self._seen_pairs.add(pair)
        if source in self._deleted or target in self._deleted:
            return
        if parallel:
            self._edges[pair] = {key: dict(attrs) for key, attrs in parallel.items()}
            self._out.setdefault(source, {})[target] = None

    def _pair(self, source, target):
        if (source, target) not in self._seen_pairs:
            found = self.rows.out_edges(self.kb_id, [source], targets=[target])
            for a, b, parallel in found.get(source, []):
                self._hydrate_pair(a, b, parallel)
            self._seen_pairs.add((source, target))

    def prefetch_pairs(self, pairs):
        missing = set(pairs) - self._seen_pairs
        for clause, params in _pair_clauses(self.kb_id, missing):
            grouped: dict[tuple[str, str], dict] = {}
            for row in self.rows.state.rows(f"SELECT * FROM graph_edges WHERE {clause}", params):
                pair = (str(row["source"]), str(row["target"]))
                parallel = grouped.setdefault(pair, {})
                parallel[len(parallel)] = edge_attrs_dict(row)
            for (a, b), parallel in grouped.items():
                self._hydrate_pair(a, b, parallel)
        self._seen_pairs.update(missing)

    def _outgoing(self, sources):
        ids = set(sources) - self._seen_out - self._deleted
        if ids:
            found = self.rows.out_edges(self.kb_id, sorted(ids))
            endpoints = set(ids)
            for groups in found.values():
                for a, b, parallel in groups:
                    self._hydrate_pair(a, b, parallel)
                    endpoints.add(b)
            self.prefetch_nodes(endpoints)
            self._seen_out.update(ids)

    def get_edge_data(self, source, target, default=None):
        self._pair(source, target)
        return super().get_edge_data(source, target, default)

    def add_edge(self, source, target, **attrs):
        self.catalog_dirty_origins.add(source)
        self._pair(source, target)
        super().add_edge(source, target, **attrs)

    def touch_edge(self, source, target):
        self.catalog_dirty_origins.add(source)
        super().touch_edge(source, target)

    def out_edges(self, node):
        self._outgoing([node])
        return super().out_edges(node)

    def out_edges_batch(self, node_ids, **kwargs):
        self._outgoing(node_ids)
        return super().out_edges_batch(node_ids, **kwargs)

    def remove_edges_from(self, edges):
        edges = list(edges)
        self.catalog_dirty_origins.update(a for a, _ in edges)
        for a, b in edges:
            self._pair(a, b)
        super().remove_edges_from(edges)

    def remove_edges_where(self, source, target, predicate):
        self._pair(source, target)
        removed = super().remove_edges_where(source, target, predicate)
        if removed:
            self.catalog_dirty_origins.add(source)
        return removed

    def remove_nodes_from(self, nodes):
        nodes = set(nodes)
        self.catalog_dirty_nodes.update(nodes)
        self.catalog_dirty_origins.update(nodes)
        self.prefetch_nodes(nodes)
        super().remove_nodes_from(nodes)
        self._deleted.update(nodes)

    def node_map(self):
        return self.nodes

    def typed_nodes(self, types):
        clause, params = self.rows.state.dialect.in_clause("type", sorted(types))
        found = {
            str(row["node_id"]): node_attrs_dict(row)
            for row in self.rows.state.rows(
                f"SELECT * FROM graph_nodes WHERE kb_id=? AND {clause}",
                (self.kb_id, *params),
            )
            if str(row["node_id"]) not in self._deleted
        }
        for key in self._pending_nodes:
            attrs = self._nodes[key]
            if attrs.get("type") in types:
                found[key] = attrs
            else:
                found.pop(key, None)
        return sorted(found.items())

    def iter_nodes(self):
        seen = set()
        for key, attrs in self.rows.iter_nodes(self.kb_id):
            seen.add(key)
            if key not in self._deleted:
                yield key, self._nodes.get(key, attrs) if key in self._pending_nodes else attrs
        for key in self._pending_nodes - seen:
            yield key, self._nodes[key]

    def iter_edges(self):
        seen = set()
        changed = self._pending_pairs | self._pending_removed_pairs
        for pair, parallel in self.rows.iter_edges(self.kb_id):
            seen.add(pair)
            if not self._deleted.intersection(pair):
                value = self._edges.get(pair, {}) if pair in changed else parallel
                if value:
                    yield pair, value
        for pair in self._pending_pairs - seen:
            if self._edges.get(pair):
                yield pair, self._edges[pair]

    def index_rows(self):
        return [
            (key, str(attrs.get("source_id") or ""), _search_blob(attrs))
            for key, attrs in self.iter_nodes()
        ]

    def number_of_nodes(self):
        return self.rows.counts(self.kb_id)[0]

    def number_of_edges(self):
        return self.rows.counts(self.kb_id)[1]

    def __len__(self):
        return self.number_of_nodes()

    def release_clean_cache(self) -> bool:
        if any(self.graph_delta().values()) or self._dirty_nodes or self._removed_nodes:
            return False
        self._nodes.clear()
        self._edges.clear()
        self._out.clear()
        self._search_blobs.clear()
        self._type_counts.clear()
        self._seen_nodes.clear()
        self._seen_pairs.clear()
        self._seen_out.clear()
        self._deleted.clear()
        self._edge_count = 0
        return True

    def to_node_link(self):
        # History/export is intentionally whole-graph work. Never serialize
        # only the cache, which would turn a partial working set into data loss.
        graph = SimpleMultiDiGraph.from_stored(self.iter_nodes(), self.iter_edges())
        return graph.to_node_link()
