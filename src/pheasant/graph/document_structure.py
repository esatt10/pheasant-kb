"""A document's outline in the graph: its sections, and where its chunks sit.

`GraphBuilder.add_artifact` writes a document's own node and chunks; this
writes the rest of its structure -- the `heading` nodes and their hierarchy
(`has_heading`, `contains`), their order, their pages, and the provenance
edges between sections and chunks (`graph/provenance.py`). Split out of
`graph/builder.py` when the provenance edges arrived, so the builder stays a
reviewable unit. Every function takes the builder for its graph and its
`upsert_node` / `upsert_edge`, which keep the generation id still when an
unchanged document is re-indexed.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from pheasant.graph.provenance import (
    NEXT_HEADING,
    SECTION_HAS_CHUNK,
    page_of,
    page_starts,
    retract_unwanted,
    section_chunks,
)

if TYPE_CHECKING:
    from pheasant.config.schema import SourceConfig
    from pheasant.ingestion.pipeline import ParsedArtifact


def chunk_node_id(source_name: str, relative_path: str, chunk: Any) -> str:
    """A chunk's node id (`docs/graph_model.md`): its index and hash in its document."""

    return f"chunk:{source_name}:{relative_path}:sha256={chunk.text_hash}:chunk={chunk.index:04d}"


def heading_node_id(source_name: str, relative_path: str, path: str) -> str:
    """A section's node id: its breadcrumb hashed, never its line (see `add_headings`)."""

    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    return f"heading:{source_name}:{relative_path}:sha256={digest}"


def prefetch(
    builder: Any, source: SourceConfig, artifact: ParsedArtifact, chunk_ids: list[str]
) -> None:
    """Read every node and pair the chunk and heading loops will touch, at once.

    On a resident graph these are dict lookups and there is nothing to
    do. On the fleet writer's row working set each `upsert_node` and
    `upsert_edge` is otherwise a point read of its own -- a 3,000-chunk
    PDF was 6,000 of them before the provenance edges, which would have
    made it 15,000. Batched, it is a handful of statements per document.
    """

    prefetch_pairs = getattr(builder.graph, "prefetch_pairs", None)
    prefetch_nodes = getattr(builder.graph, "prefetch_nodes", None)
    if not callable(prefetch_pairs) or not callable(prefetch_nodes):
        return
    headings = getattr(artifact, "headings", None) or ()
    placed = [
        (heading_node_id(source.name, artifact.relative_path, heading.path), heading.line)
        for heading in headings
    ]
    heading_ids = list(dict.fromkeys(heading_id for heading_id, _line in placed))
    prefetch_nodes([artifact.id, *chunk_ids, *heading_ids])
    pairs = [(artifact.id, chunk_id) for chunk_id in chunk_ids]
    pairs += list(zip(chunk_ids, chunk_ids[1:], strict=False))
    pairs += [(artifact.id, heading_id) for heading_id in heading_ids]
    pairs += list(zip(heading_ids, heading_ids[1:], strict=False))
    spans = [
        (chunk_id, chunk.start_line, chunk.end_line)
        for chunk_id, chunk in zip(chunk_ids, artifact.chunks, strict=True)
    ]
    for heading_id, targets in section_chunks(placed, spans).items():
        pairs += [(heading_id, chunk_id) for chunk_id in targets]
    prefetch_pairs(pairs)


def add_headings(
    builder: Any,
    source: SourceConfig,
    artifact: ParsedArtifact,
    chunk_ids: list[str] | None = None,
) -> int:
    """Emit the artifact's structural outline as `heading` nodes.

    `heading` and `has_heading` are both **documented** in
    `docs/graph_model.md` ("Retrieval and document structure units" /
    "Hierarchy and indexing relationships") and were never emitted
    anywhere — this connects them. Nothing happens unless the source
    enables `taxonomy` (and `taxonomy.graph_nodes`), so a graph without
    the feature is byte-identical to before.

    Two edge types, both existing: the artifact `has_heading` each of its
    sections, and a section `contains` its subsections — the same edge the
    directory/file hierarchy uses, so the taxonomy is walkable by the
    graph traversal that already knows how to follow `contains` (see
    `graph/traversal.py:HIERARCHY_EDGE_TYPES`).
    """
    headings = getattr(artifact, "headings", None) or []
    if not getattr(getattr(source, "taxonomy", None), "graph_nodes", True):
        headings = []
    current = {
        heading_node_id(source.name, artifact.relative_path, heading.path) for heading in headings
    }
    # Sections an earlier index of this document had and this one does not
    # (renamed, removed, or the source turned taxonomy off). Owned by the
    # document -- the id carries its path -- so removing them is safe, and
    # `remove_nodes_from` takes their edges with them. Before this they
    # stayed until a full sync, beside their replacements.
    if artifact.id in builder.graph:
        stale = [
            target
            for _, target, edge_map in builder.graph.out_edges(artifact.id)
            if target not in current
            and any(data.get("type") == "has_heading" for data in edge_map.values())
        ]
        if stale:
            builder.graph.remove_nodes_from(stale)
    if not headings:
        return 0
    pages = page_starts(getattr(artifact, "page_lines", None))
    order: dict[str, int] = {}
    placed: list[tuple[str, int]] = []

    # Stack of (level, node_id) so a subsection is parented to the section
    # that encloses it rather than to the artifact.
    stack: list[tuple[int, str]] = []
    emitted = 0
    for heading in headings:
        # Keyed on the section's *breadcrumb*, hashed — deliberately not on
        # its line number. A line-numbered ID churns the whole graph on any
        # edit: inserting one paragraph shifts every heading below it, so
        # every section downstream would be dropped and re-created despite
        # nothing about it changing. The breadcrumb is the section's
        # identity ("Article IV > 4.2 Termination" is that section wherever
        # it sits), and hashing keeps the ID bounded when a deep path runs
        # to hundreds of characters. Matches the `chunk:` ID's existing use
        # of a content hash in the context field.
        #
        # Trade-off, accepted: two sections with a byte-identical breadcrumb
        # in one document collapse to one node. That needs a document to
        # repeat the same caption under the same parent, and when it happens
        # the two really are the same section by name — whereas edit churn
        # would be constant.
        heading_id = heading_node_id(source.name, artifact.relative_path, heading.path)
        placed.append((heading_id, heading.line))
        first = heading_id not in order
        order.setdefault(heading_id, len(order))
        page = page_of(pages, heading.line)
        builder.upsert_node(
            heading_id,
            "heading",
            heading.label,
            {
                "source_id": source.name,
                "artifact_id": artifact.id,
                "level": heading.level,
                "pattern_level": heading.pattern_level,
                "number": heading.number,
                "title": heading.title,
                "kind": heading.kind,
                "heading_path": heading.path,
                "start_line": heading.line,
                # Position among the document's sections, from 0; a
                # repeated breadcrumb (one node, see above) keeps its first.
                "order": order[heading_id],
                **({"page": page} if page is not None and first else {}),
                "relative_path": artifact.relative_path,
                # The parsed ordinal, persisted so a reader can query by
                # citation ("§ 12.3" -> parts [12, 3]) and so the taxonomy
                # endpoint can re-derive sequence issues without reparsing
                # the document.
                "ordinal_parts": list(heading.ordinal.parts) if heading.ordinal else [],
                "ordinal_series": heading.ordinal.series if heading.ordinal else None,
                "ordinal_suffix": heading.ordinal.suffix if heading.ordinal else "",
                "ordinal_relative": bool(heading.ordinal.relative) if heading.ordinal else False,
            },
        )
        while stack and stack[-1][0] >= heading.level:
            stack.pop()
        parent = stack[-1][1] if stack else None
        if parent is None:
            builder.upsert_edge(artifact.id, heading_id, "has_heading", {"source_id": source.name})
        else:
            builder.upsert_edge(parent, heading_id, "contains", {"source_id": source.name})
            # Also link the artifact to every section, not just the roots,
            # so "which sections does this document have?" is one hop
            # rather than a full descent.
            builder.upsert_edge(artifact.id, heading_id, "has_heading", {"source_id": source.name})
        stack.append((heading.level, heading_id))
        emitted += 1
    _link_sections(builder, source, artifact, list(order), placed, chunk_ids)
    return emitted


def _link_sections(
    builder: Any,
    source: SourceConfig,
    artifact: ParsedArtifact,
    ordered: list[str],
    placed: list[tuple[str, int]],
    chunk_ids: list[str] | None,
) -> None:
    """Each section to the next, and to the chunks holding its own text.

    Derived again on every index of the document, and what it no longer
    implies is retracted (`provenance.retract_unwanted`), so an
    incremental re-index of an edited document equals a full one.
    """

    attrs = {"source_id": source.name}
    wanted_next = {
        current: {following} for current, following in zip(ordered, ordered[1:], strict=False)
    }
    if chunk_ids is None:
        chunk_ids = [
            chunk_node_id(source.name, artifact.relative_path, chunk) for chunk in artifact.chunks
        ]
    spans = [
        (chunk_id, chunk.start_line, chunk.end_line)
        for chunk_id, chunk in zip(chunk_ids, artifact.chunks, strict=True)
    ]
    sections = section_chunks(placed, spans)
    wanted_chunks = {heading_id: set(targets) for heading_id, targets in sections.items()}
    retract_unwanted(builder.graph, ordered, NEXT_HEADING, wanted_next)
    retract_unwanted(builder.graph, ordered, SECTION_HAS_CHUNK, wanted_chunks)
    for current, following in zip(ordered, ordered[1:], strict=False):
        builder.upsert_edge(current, following, NEXT_HEADING, dict(attrs))
    for heading_id in ordered:
        for chunk_id in sections.get(heading_id, ()):
            builder.upsert_edge(heading_id, chunk_id, SECTION_HAS_CHUNK, dict(attrs))
