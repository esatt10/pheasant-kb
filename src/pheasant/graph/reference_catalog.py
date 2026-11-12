"""Compact path/reference metadata and the dependencies changed files invalidate.

The first pass reconciles existing links. Later passes read only touched nodes
and referrers, and match changed target paths against cached reference specs.
The existing resolver still decides the matches against the complete path set.
"""

from __future__ import annotations

from typing import Any

from pheasant.graph.enrichment import (
    ARTIFACT_NODE_TYPES,
    _ArtifactRef,
    _norm_rel,
    _resolve_reference,
)
from pheasant.graph.media_links import MEDIA_NODE_TYPES, resolve_image_edges
from pheasant.persistence.graph_codec import edge_attrs_dict

_KINDS = ARTIFACT_NODE_TYPES | MEDIA_NODE_TYPES | {"external_reference"}
_FIELDS = ("type", "source_id", "relative_path", "reference", "reference_type")


def _compact(attrs):
    return {key: attrs[key] for key in _FIELDS if key in attrs}


class ReferenceCatalog:
    def __init__(self, graph: Any):
        self.nodes = {key: _compact(attrs) for key, attrs in graph.typed_nodes(_KINDS)}
        self.references: dict[str, list[tuple]] = {}
        self.resolved: dict[tuple, dict] = {}
        for row in graph.rows.state.rows(
            "SELECT e.* FROM graph_nodes n JOIN graph_edges e "
            "ON e.kb_id=n.kb_id AND e.target=n.node_id "
            "WHERE n.kb_id=? AND n.type='external_reference' "
            "AND e.type IN ('imports','references','embeds') "
            "ORDER BY e.source,e.target,e.type,e.seq",
            (graph.kb_id,),
        ):
            a, b = str(row["source"]), str(row["target"])
            if b in self.nodes:
                attrs = edge_attrs_dict(row)
                self.references.setdefault(a, []).append(
                    (a, b, attrs["type"], attrs.get("reference_type"))
                )

    def refresh(self, graph: Any) -> set[str]:
        dirty_nodes, dirty_origins = graph.catalog_dirty_nodes, graph.catalog_dirty_origins
        found = graph.rows.get_nodes(graph.kb_id, sorted(dirty_nodes), materialized=True)
        changed_targets = []
        for key in dirty_nodes:
            before = self.nodes.pop(key, None)
            attrs = found.get(key) if key not in graph._deleted else None
            after = _compact(attrs) if attrs and attrs.get("type") in _KINDS else None
            if after is not None:
                self.nodes[key] = after
            if before != after:
                changed_targets.extend(
                    (key, item)
                    for item in (before, after)
                    if item and item.get("type") in ARTIFACT_NODE_TYPES | MEDIA_NODE_TYPES
                )
        refresh_origins = {
            key
            for key in dirty_origins | dirty_nodes
            if key in self.references or self.nodes.get(key, {}).get("type") in ARTIFACT_NODE_TYPES
        }
        for key in refresh_origins:
            self.references.pop(key, None)
        for origin, groups in graph.rows.out_edges(graph.kb_id, sorted(refresh_origins)).items():
            for a, b, parallel in groups:
                if self.nodes.get(b, {}).get("type") != "external_reference":
                    continue
                for attrs in parallel.values():
                    if attrs.get("type") in {"imports", "references", "embeds"}:
                        self.references.setdefault(origin, []).append(
                            (a, b, attrs["type"], attrs.get("reference_type"))
                        )
        affected = set(refresh_origins)
        affected.update(a for (a, b, _), _attrs in self.resolved.items() if b in dirty_nodes)
        by_path: dict[str, list] = {}
        media = []
        for key, attrs in changed_targets:
            path, source = attrs.get("relative_path"), attrs.get("source_id")
            if path and source:
                by_path.setdefault(_norm_rel(path), []).append(_ArtifactRef(key, source, path))
            if attrs.get("type") in MEDIA_NODE_TYPES:
                media.append((key, attrs))
        for origin, specs in self.references.items():
            if origin in affected:
                continue
            for a, b, kind, reference_type in specs:
                ext = self.nodes.get(b, {})
                if b in dirty_nodes:
                    affected.add(origin)
                    break
                if kind == "embeds":
                    match = media and resolve_image_edges(
                        {a: self.nodes.get(a, {})}, media, {b: ext}, [(a, b)]
                    )
                else:
                    match = by_path and _resolve_reference(
                        str(ext.get("reference") or ""),
                        reference_type,
                        by_path,
                        self.nodes.get(a, {}).get("relative_path"),
                    )
                if match:
                    affected.add(origin)
                    break
        return affected
