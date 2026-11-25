"""File-owned provenance contributed by installed source extensions."""

from __future__ import annotations

from typing import Any

from pheasant.extensions import Provenance


def attach(builder: Any, artifact: Any, spec: Provenance) -> None:
    inputs = {
        f"file:{artifact.source_id}:{path}:branch=none": keys
        for path, keys in artifact.extension_inputs.items()
    }
    for target, keys in inputs.items():
        if target != artifact.id and builder.graph.has_node(target):
            builder.upsert_edge(
                artifact.id,
                target,
                "derived_from",
                {
                    "source_id": artifact.source_id,
                    "extension_input": True,
                    "role": "field_evidence" if keys else "research_input",
                    "field_keys": keys,
                },
            )
    for _, target, edges in list(builder.graph.out_edges(artifact.id)):
        if target not in inputs and any(edge.get("extension_input") for edge in edges.values()):
            builder.graph.remove_edges_where(
                artifact.id, target, lambda attrs: attrs.get("extension_input", False)
            )

    expected = set()
    for evidence in artifact.extension_provenance:
        attributes = {
            key: value for key, value in evidence.items() if key not in {"id", "label", "type"}
        }
        target = spec.node_id(artifact.id, evidence["id"])
        expected.add(target)
        builder.upsert_node(
            target,
            spec.node_type,
            evidence["label"],
            {
                **attributes,
                "source_id": artifact.source_id,
                "artifact_id": artifact.id,
                "evidence_id": evidence["id"],
            },
        )
        builder.upsert_edge(
            artifact.id,
            target,
            "derived_from",
            {
                "source_id": artifact.source_id,
                "field_keys": evidence["field_keys"],
            },
        )

    prefix = spec.node_id(artifact.id, "")
    stale = [
        target
        for _, target, _ in builder.graph.out_edges(artifact.id)
        if target.startswith(prefix) and target not in expected
    ]
    if stale:
        builder.graph.remove_nodes_from(stale)
