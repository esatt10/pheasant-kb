"""Enrichment inputs from indexed rows rather than the resident corpus graph."""

from __future__ import annotations

from typing import Any

from pheasant.graph.enrichment import ARTIFACT_NODE_TYPES
from pheasant.graph.media_links import MEDIA_NODE_TYPES, resolve_image_edges
from pheasant.graph.reference_catalog import ReferenceCatalog


def cross_source(builder: Any) -> int:
    graph = builder.graph
    rows, kb = graph.rows, graph.kb_id
    detached, builder._detached = builder._detached, set()
    if detached:
        clause, params = rows.state.dialect.in_clause("target", sorted(detached))
        linked = {
            str(r["target"])
            for r in rows.state.rows(
                f"SELECT DISTINCT target FROM graph_edges WHERE kb_id=? AND {clause}", (kb, *params)
            )
        }
        graph.remove_nodes_from(detached - linked)
    if graph.reference_catalog is None:
        graph.reference_catalog = ReferenceCatalog(graph)
        affected = set(graph.reference_catalog.references)
    else:
        affected = graph.reference_catalog.refresh(graph)
    catalog = graph.reference_catalog
    nodes = sorted(catalog.nodes.items())
    ext = {key: attrs for key, attrs in nodes if attrs.get("type") == "external_reference"}
    references, images = [], []
    for origin in sorted(affected):
        for a, b, kind, reference_type in catalog.references.get(origin, []):
            if kind == "embeds":
                images.append((a, b))
            else:
                references.append((a, b, kind, reference_type))
    resolved = builder._resolve_cross_source_edges(nodes, references)
    if images:
        resolved += resolve_image_edges(
            graph.prefetch_nodes({a for a, _ in images}),
            [(key, attrs) for key, attrs in nodes if attrs.get("type") in MEDIA_NODE_TYPES],
            ext,
            images,
        )
    dirty = graph.catalog_dirty_nodes | graph.catalog_dirty_origins
    changed = [
        edge
        for edge in resolved
        if (
            catalog.resolved.get((edge.source, edge.target, edge.type)) != edge.attrs
            or edge.source in dirty
            or edge.target in dirty
        )
    ]
    graph.prefetch_pairs((edge.source, edge.target) for edge in changed)
    for edge in changed:
        builder.upsert_edge(edge.source, edge.target, edge.type, dict(edge.attrs))
    catalog.resolved = {
        key: attrs for key, attrs in catalog.resolved.items() if key[0] not in affected
    }
    catalog.resolved.update({(e.source, e.target, e.type): dict(e.attrs) for e in resolved})
    graph.catalog_dirty_nodes.clear()
    graph.catalog_dirty_origins.clear()
    return len(resolved)


def memory_inputs(builder: Any, state: Any, records: list[dict]) -> Any:
    from pheasant.memory.bridge import BridgeInputs, normalize_label

    graph = builder.graph
    memory_sources = {str(r.get("source_id") or "") for r in records}
    artifact_ids = {str(r.get("artifact_id") or "") for r in records}
    clause, params = state.dialect.in_clause("source_id", sorted(memory_sources))
    texts = {
        str(r["artifact_id"]): str(r["body"] or "")
        for r in state.rows(
            f"SELECT artifact_id,GROUP_CONCAT(text,' ') AS body FROM chunks WHERE {clause} "
            "GROUP BY artifact_id",
            params,
        )
    }
    symbols: dict[str, list[str]] = {}
    for row in state.rows(
        f"SELECT name,qualified_name,artifact_id FROM symbols WHERE NOT ({clause})", params
    ):
        for value in (row["name"], row["qualified_name"]):
            if value:
                symbols.setdefault(str(value).lower(), []).append(str(row["artifact_id"]))
    headings: dict[str, list[str]] = {}
    entities: dict[str, list[str]] = {}
    for key, attrs in graph.typed_nodes({"heading", "entity"}):
        if attrs.get("source_id") not in memory_sources:
            label = normalize_label(
                (attrs.get("title") if attrs["type"] == "heading" else None)
                or attrs.get("label")
                or ""
            )
            if label:
                target = headings if attrs["type"] == "heading" else entities
                target.setdefault(label, []).append(key)
    referenced: dict[str, list[str]] = {}
    graph._outgoing(artifact_ids)
    targets: dict[str, list[str]] = {}
    for origin in artifact_ids:
        for _, target, parallel in graph.out_edges(origin):
            if any(a.get("type") in {"references", "imports"} for a in parallel.values()):
                targets.setdefault(origin, []).append(target)
    found = graph.prefetch_nodes({target for group in targets.values() for target in group})
    for origin, candidates in targets.items():
        for target in candidates:
            if found.get(target, {}).get("type") in ARTIFACT_NODE_TYPES:
                referenced.setdefault(origin, []).append(target)
    return BridgeInputs(
        records=records,
        texts=texts,
        symbols={k: sorted(set(v)) for k, v in symbols.items()},
        headings={k: sorted(set(v)) for k, v in headings.items()},
        entities={k: sorted(set(v)) for k, v in entities.items()},
        referenced=referenced,
    )
