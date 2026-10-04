"""What an incremental re-index of one artifact takes back.

Every per-artifact output is *upserted*, which is right for what the new text
still implies and wrong for what it stopped implying: nothing else ever
retracts it, because a full sync clears the source first and an incremental one
only adds. Three things are retracted here, each re-derived from the
artifact's current text every time it is indexed, each walking only the
artifact's own out-edges (O(artifact), not O(graph)):

* chunk nodes an edit replaced,
* `embeds` edges to images the document stopped showing,
* the code pass's imports, calls and symbols.
"""

from __future__ import annotations

from typing import Any

from pheasant.graph.enrichment import ArtifactEnrichment

#: Edge types an artifact's own text implies, emitted by the per-artifact
#: enrichment passes and re-derived each time the artifact is indexed.
TEXT_DERIVED_EDGE_TYPES = frozenset({"imports", "calls", "mentions"})
#: Enrichment nodes several artifacts can point at; removed only when nothing
#: does. Owned symbols (with an `artifact_id`) are not among them.
SHARED_ENRICHMENT_NODE_TYPES = frozenset({"external_reference", "symbol", "entity"})


def drop_stale_chunks(graph: Any, artifact_id: str, current: set[str]) -> int:
    """Remove chunk nodes an earlier index of this artifact left behind.

    Chunk ids embed the chunk's sha256, so an edit mints new nodes and the
    old ones — still reachable by a `has_chunk` edge from the artifact —
    were never retracted on the incremental path (a full sync cleared the
    source first, which hid it). Walks only the artifact's own out-edges,
    so the cost is the artifact's, not the graph's. A chunk id is unique
    to one artifact (source and path are in it), so nothing shared is
    removed; `remove_nodes_from` takes the incident edges with it.
    """

    if artifact_id not in graph:
        return 0
    stale = [
        target
        for _, target, edge_map in graph.out_edges(artifact_id)
        if target not in current
        and edge_map
        and all(data.get("type") == "has_chunk" for data in edge_map.values())
    ]
    if stale:
        graph.remove_nodes_from(stale)
    return len(stale)


def drop_embeds(graph: Any, artifact_id: str) -> None:
    """Forget which images a document showed before this index of it.

    Enrichment is upserted, so on an incremental re-index an edge the new
    text no longer implies would otherwise survive — the pre-existing
    behaviour for `references`, and a visible one for `embeds`: an answer
    would keep showing a figure the document stopped containing. The
    document's `embeds` edges are therefore re-derived from its current
    text every time it is indexed; the ones it still has come straight
    back from `apply_enrichment` and the global resolution pass.

    Only pairs whose every edge is `embeds` are dropped, so a parallel edge
    of another type between the same two nodes is never collateral. Scoped
    to `embeds` deliberately: doing the same for `references` changes what
    an incremental sync means for every corpus and needs its own evidence
    (CLAUDE.md §6, the chunk-node leak).
    """

    if artifact_id not in graph:
        return
    pairs = [
        (source, target)
        for source, target, edge_map in graph.out_edges(artifact_id)
        if edge_map and all(data.get("type") == "embeds" for data in edge_map.values())
    ]
    if pairs:
        graph.remove_edges_from(pairs)


def retract_stale_enrichment(
    graph: Any, artifact_id: str, enrichment: ArtifactEnrichment
) -> set[str]:
    """Retract the imports, calls and symbols an edit took out of a file.

    Enrichment is upserted, so on an incremental re-index everything the
    old text implied survived: a removed import kept its edge (and its
    resolved file -> file edge), a deleted call kept `calls`, and a renamed
    or moved function stayed a symbol beside its replacement. Python and
    every other language alike, because all three come from the per-
    artifact passes and nothing retracted any of them.

    Only what the new text no longer implies goes, so an unchanged edge is
    never removed and re-added (which would re-key its pair and move the
    generation id for nothing). A resolved import edge is kept while the
    file still makes the import it was resolved from; the cross-source
    pass re-asserts it either way. Edges are removed by type, never by
    pair, so a memory record's `about` beside a `mentions` is untouched.

    Symbol nodes belong to one artifact (their id carries its path) and go
    with it. Call targets, import stubs and entities are shared across a
    source, so they are only *detached* here: their ids are returned, and the
    cross-source pass removes the ones nothing else points at.

    `references` (document links) has the same shape and is still left
    alone, as `drop_embeds` explains.
    """

    if artifact_id not in graph:
        return set()
    derived = TEXT_DERIVED_EDGE_TYPES
    keep = {
        (edge.target, edge.type)
        for edge in enrichment.edges
        if edge.source == artifact_id and edge.type in derived
    }
    stub_ids = {target for target, edge_type in keep if edge_type == "imports"}
    imports_made = {
        (node.attrs.get("reference"), node.attrs.get("reference_type"))
        for node in enrichment.nodes
        if node.id in stub_ids
    }

    def stale(target: str, attrs: dict[str, Any]) -> bool:
        edge_type = attrs.get("type")
        if edge_type not in derived or (target, edge_type) in keep:
            return False
        # A resolved import (artifact -> file) carries the spec it came from.
        if edge_type == "imports" and "reference" in attrs:
            return (attrs.get("reference"), attrs.get("reference_type")) not in imports_made
        return True

    detached: list[str] = []
    for _, target, edge_map in graph.out_edges(artifact_id):
        if any(stale(target, attrs) for attrs in edge_map.values()):
            graph.remove_edges_where(
                artifact_id, target, lambda attrs, target=target: stale(target, attrs)
            )
            detached.append(target)
    if not detached:
        return set()
    current_symbols = {node.id for node in enrichment.nodes if node.type == "symbol"}
    nodes = graph.node_map()
    owned: list[str] = []
    shared: set[str] = set()
    for target in detached:
        attrs = nodes.get(target) or {}
        if attrs.get("type") == "symbol" and attrs.get("artifact_id") == artifact_id:
            if target not in current_symbols:
                owned.append(target)
        elif attrs.get("type") in SHARED_ENRICHMENT_NODE_TYPES:
            shared.add(target)
    if owned:
        graph.remove_nodes_from(owned)
    return shared


#: Node types an artifact owns outright: their ids carry its source and path,
#: so no other artifact can share one.
_OWNED_BY_ARTIFACT = frozenset({"chunk", "heading"})


def remove_vanished_artifacts(
    graph: Any, source_name: str, vanished: list[tuple[str, str, str | None]]
) -> set[str]:
    """Remove artifacts whose files are gone, with everything only they held.

    ``vanished`` is ``(artifact_id, relative_path, git_branch)``. Removed:
    each artifact node, the chunks, headings and symbols it owns, and every
    directory the removal leaves empty, deepest first. Shared nodes it pointed
    at (import stubs, call targets, entities) are returned as detached, for
    the cross-source pass to remove if nothing else points at them; the
    artifact's own edges go with its node.

    Walks each artifact's out-edges and its directory chain, never the graph:
    owned nodes are reachable from the artifact (`has_chunk`, `has_heading`,
    `mentions`), which is how `drop_stale_chunks` already finds chunks.
    """

    remove: set[str] = set()
    shared: set[str] = set()
    directories: set[str] = set()
    nodes = graph.node_map()
    for artifact_id, relative_path, branch in vanished:
        if artifact_id not in graph:
            continue
        remove.add(artifact_id)
        for _, target, _edges in graph.out_edges(artifact_id):
            attrs = nodes.get(target) or {}
            kind = attrs.get("type")
            if kind in _OWNED_BY_ARTIFACT or (
                kind == "symbol" and attrs.get("artifact_id") == artifact_id
            ):
                remove.add(target)
            elif kind in SHARED_ENRICHMENT_NODE_TYPES:
                shared.add(target)
        parts = [part for part in relative_path.replace("\\", "/").split("/")[:-1] if part]
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            directories.add(f"directory:{source_name}:{prefix}:branch={branch or 'none'}")
    # Deepest first, so a parent is judged after its emptied children are.
    for directory in sorted(directories, key=lambda d: d.count("/"), reverse=True):
        if directory not in graph:
            continue
        children = {target for _, target, _ in graph.out_edges(directory)}
        if children <= remove:
            remove.add(directory)
    if remove:
        graph.remove_nodes_from(remove)
    return shared - remove
