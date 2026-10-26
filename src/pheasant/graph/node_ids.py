"""The ids the builder mints for one artifact, and what indexing it touches.

The grammar is `docs/graph_model.md`'s and a stable-ID contract (rule 3):
these functions are the one place a directory, chunk or heading id is
spelled, so the builder's writes and the read plan below cannot drift apart.
"""

from __future__ import annotations

import hashlib
from typing import Any


def directory_chain(
    source: Any, relative_path: str, branch: str | None
) -> list[tuple[str, str, str]]:
    """``(node_id, name, relative)`` for each directory above a file, root first."""

    parts = [part for part in relative_path.replace("\\", "/").split("/")[:-1] if part]
    chain = []
    for depth in range(1, len(parts) + 1):
        relative = "/".join(parts[:depth])
        node_id = f"directory:{source.name}:{relative}:branch={branch or 'none'}"
        chain.append((node_id, parts[depth - 1], relative))
    return chain


def chunk_id(source: Any, artifact: Any, chunk: Any) -> str:
    return (
        f"chunk:{source.name}:{artifact.relative_path}:"
        f"sha256={chunk.text_hash}:chunk={chunk.index:04d}"
    )


def heading_id(source: Any, artifact: Any, heading: Any) -> str:
    """Keyed on the section's breadcrumb, hashed -- see ``GraphBuilder.add_headings``."""

    digest = hashlib.sha256(heading.path.encode("utf-8")).hexdigest()[:16]
    return f"heading:{source.name}:{artifact.relative_path}:sha256={digest}"


def artifact_touches(
    kb_id: str, source: Any, artifact: Any, enrichment: Any
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """``(nodes, outs, pairs)`` that indexing this artifact reads.

    Handed to ``SimpleMultiDiGraph.prefetch`` -- a no-op on the resident
    graph, and on a store-backed one (`graph.store_backed`) the difference
    between a round trip per upsert and a few per file. A superset is harmless
    (an id nobody stored is learned absent) and a gap only costs the one round
    trip the read would make on its own, so this states what
    ``GraphBuilder.add_artifact`` derives rather than re-deriving each write.
    """

    source_node = f"source:{kb_id}:{source.name}"
    hub = f"source_type:{kb_id}:{source.type.value}"
    chain = [source_node] + [
        node_id
        for node_id, _name, _relative in directory_chain(
            source, artifact.relative_path, artifact.git_branch
        )
    ]
    headings = [
        heading_id(source, artifact, heading)
        for heading in getattr(artifact, "headings", None) or []
    ]
    outs = [artifact.id, *headings]
    nodes = [
        kb_id,
        hub,
        *chain,
        artifact.id,
        *(chunk_id(source, artifact, chunk) for chunk in artifact.chunks),
        *headings,
        *(node.id for node in enrichment.nodes),
    ]
    pairs = [
        (kb_id, source_node),
        (kb_id, hub),
        (hub, source_node),
        *zip(chain, chain[1:], strict=False),
        (source_node, artifact.id),
        (chain[-1], artifact.id),
        *((edge.source, edge.target) for edge in enrichment.edges if edge.source not in outs),
    ]
    return nodes, outs, pairs
