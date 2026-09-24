"""What the indexer's whole-graph passes are allowed to touch.

The graph moved into rows and every *serving* process stopped holding one, but
the indexer still keeps a `SimpleMultiDiGraph` working set — the builder
mutates it and the enrichment passes walk it. Those passes were written when
"walk the whole graph" was free, because the whole graph was already a dict in
front of them. It is not free: it is the reason the working set has to exist,
and three of them were doing it for data they never read.

These are efficiency assertions, so they are written the only way an
efficiency assertion survives: as a **bound on what the code touches**, never
as a stopwatch. A timing test measures the machine and goes flaky on CI; a
test that counts nodes visited fails exactly when someone puts the work back.

Measured at 100k files (630k nodes / 630k edges) with
`python -m pheasant.graph.capacity`'s corpus, which is what the numbers quoted
below refer to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.graph.builder import GraphBuilder
from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.ingestion.content_types import ARTIFACT_TYPES
from pheasant.sync.engine import SyncEngine

KB = "workingset"


def _config(tmp_path: Path, *, files: int = 6) -> PheasantConfig:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        (workspace / f"note-{index}.md").write_text(
            f"# Note {index}\n\n## Gateway\n\n"
            f"The gateway rotates credentials nightly. Marker{index} is unique.\n\n"
            f"See [next](note-{(index + 1) % files}.md).\n",
            encoding="utf-8",
        )
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": KB,
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {"graph_snapshots": False},
            "sources": [
                {
                    "name": "docs",
                    "type": "markdown_folder",
                    "path": str(workspace),
                    "include": ["**/*.md"],
                }
            ],
        }
    )


class _CountingGraph(SimpleMultiDiGraph):
    """A graph that records how much of itself each pass materializes.

    Subclassed rather than mocked because the assertions are about the real
    walk: a mock would let the pass keep asking for everything and simply not
    notice.
    """

    def __init__(self) -> None:
        super().__init__()
        self.snapshots = 0

    def nodes(self, data: bool = False):  # type: ignore[override]
        # `NodeView.__call__`, the whole-graph materialization. Counted rather
        # than forbidden, so a caller that genuinely wants one still can.
        self.snapshots += 1
        return super().nodes(data=data)


def _populate(graph: SimpleMultiDiGraph, artifacts: int = 40) -> None:
    """A graph with a realistic type mix: mostly chunks, a few artifacts."""

    for index in range(artifacts):
        path = f"src/file_{index}.md"
        artifact = f"file:docs:{path}:branch=none"
        graph.add_node(
            artifact,
            id=artifact,
            type="markdown_note",
            label=path,
            relative_path=path,
            source_id="docs",
        )
        for chunk in range(9):  # chunks dominate a real graph
            chunk_id = f"chunk:docs:{path}:{chunk}"
            graph.add_node(
                chunk_id, id=chunk_id, type="chunk", label=f"{path}#{chunk}", source_id="docs"
            )
            graph.add_edge(artifact, chunk_id, type="has_chunk")
        external = f"external:docs:{path}:ref"
        graph.add_node(
            external,
            id=external,
            type="external_reference",
            label="ref",
            source_id="docs",
            reference=f"src/file_{(index + 1) % artifacts}.md",
        )
        graph.add_edge(artifact, external, type="references", reference_type="document_link")


# --------------------------------------------------------------------------
# 1. The passes that were reading the whole graph for a slice of it
# --------------------------------------------------------------------------


def test_cross_source_resolution_only_materializes_what_it_reads() -> None:
    """It copied every node's attributes to use 15% of them.

    Both the pure-Python resolver and its WASM twin filter arrivals to
    artifacts carrying a path plus `external_reference` stubs — so handing
    them chunks and symbols was building a list whose first act was to discard
    three quarters of it. Measured 2.95s and +160MB at 100k files, against
    319ms and +24MB for the same answer.

    Asserted on the *list handed to the resolver*, because that is the thing
    that was oversized; the walk itself still visits every edge, which is
    genuinely what a cross-source pass has to do.
    """

    config = PheasantConfig.model_validate({"pheasant": {"name": KB}})
    builder = GraphBuilder(config)
    _populate(builder.graph)

    handed: list[list[tuple[str, dict[str, Any]]]] = []
    original = builder._resolve_cross_source_edges

    def spy(nodes, ref_edges):
        handed.append(list(nodes))
        return original(nodes, ref_edges)

    builder._resolve_cross_source_edges = spy  # type: ignore[method-assign]
    builder.add_cross_source_edges()

    assert handed, "the pass did not reach the resolver at all"
    passed = handed[0]
    total = builder.graph.number_of_nodes()
    kinds = {attrs.get("type") for _node_id, attrs in passed}
    assert kinds <= {"external_reference", *ARTIFACT_TYPES}, (
        f"the resolver was handed node types it does not read: {kinds}"
    )
    assert "chunk" not in kinds, "chunks are 55% of a real graph and are never read here"
    assert len(passed) < total / 2, (
        f"{len(passed)} of {total} nodes materialized; the projection is not narrowing"
    )


def test_the_memory_bridge_does_not_build_a_type_map_of_the_whole_graph(
    tmp_path: Path,
) -> None:
    """It did, to answer one question about a handful of edge targets.

    Rung 1 asks whether an edge out of a memory artifact points at an
    artifact node. The first version answered that by building `{node_id:
    type}` for **every node in the graph** — a whole-graph dict for a bounded
    lookup, and one of the structures that kept the indexer resident.

    Asserted through the bridge's real output rather than by inspecting the
    intermediate: what matters is that narrowing the lookup did not narrow the
    answer.
    """

    engine = SyncEngine(_config(tmp_path))
    try:
        engine.sync_source("docs", "full")
        report = engine.graph_builder.add_memory_edges(engine.state)
    finally:
        engine.close()

    # No memory records in this corpus, so the bridge has nothing to link —
    # and must say so rather than raising on the narrowed lookup.
    assert isinstance(report, dict)
    assert report.get("about", 0) == 0


def test_removal_does_not_snapshot_the_whole_graph() -> None:
    """`nodes(data=True)` materializes every node before the filter runs.

    Measured 1.16s at 100k files — an order of magnitude more than the removal
    it was preparing for, and pure waste: the filter wants ids, and the lock
    is held either way.
    """

    config = PheasantConfig.model_validate({"pheasant": {"name": KB}})
    builder = GraphBuilder(config)
    builder.graph = _CountingGraph()
    _populate(builder.graph)
    before = builder.graph.number_of_nodes()

    builder.remove_artifact_nodes(["file:docs:src/file_0.md:branch=none"])
    builder.remove_source_content("docs")

    assert builder.graph.snapshots == 0, "removal materialized the whole graph before filtering it"
    assert builder.graph.number_of_nodes() < before, "the removal removed nothing"


def test_removing_a_source_still_removes_all_of_it() -> None:
    """The efficiency changes above must not narrow what removal removes."""

    config = PheasantConfig.model_validate({"pheasant": {"name": KB}})
    builder = GraphBuilder(config)
    _populate(builder.graph)
    # Through the builder's own kb id, which is what `remove_source_content`
    # composes the source node from.
    source_node = f"source:{builder.kb_id}:docs"
    builder.graph.add_node(source_node, id=source_node, type="source", label="docs")

    builder.remove_source_content("docs")

    survivors = {node_id: attrs for node_id, attrs in builder.graph.iter_nodes()}
    assert not [
        node_id for node_id, attrs in survivors.items() if attrs.get("source_id") == "docs"
    ], "a node from the removed source survived"
    assert source_node not in survivors, "the source's own node survived"
    assert builder.graph.number_of_edges() == 0, "an edge on a removed node survived"
    # The knowledge-base node is seeded by the builder and belongs to no
    # source, so it is *supposed* to outlive one being removed.
    assert set(survivors) == {builder.kb_id}


def test_removing_the_last_source_prunes_its_type_hub() -> None:
    """A type hub is structural source state, not orphaned graph content."""

    config = PheasantConfig.model_validate(
        {
            "pheasant": {"name": KB},
            "sources": [
                {"name": "notes-a", "type": "markdown_folder", "path": "."},
                {"name": "notes-b", "type": "markdown_folder", "path": "."},
                {"name": "docs", "type": "document_folder", "path": "."},
            ],
        }
    )
    builder = GraphBuilder(config)
    for source in config.sources:
        builder.add_source(source)

    markdown_hub = f"source_type:{builder.kb_id}:markdown_folder"
    document_hub = f"source_type:{builder.kb_id}:document_folder"
    builder.remove_source_content("notes-a")
    assert builder.graph.has_node(markdown_hub), "the other Markdown source still needs its hub"

    builder.remove_source_content("notes-b")
    assert not builder.graph.has_node(markdown_hub), "the last source left an orphaned type hub"
    assert builder.graph.has_node(document_hub), "a different source type must not be pruned"

    builder.remove_source_content("docs")
    assert not builder.graph.has_node(document_hub)
    assert set(node_id for node_id, _attrs in builder.graph.iter_nodes()) == {builder.kb_id}


def test_removing_an_artifact_takes_its_derived_nodes_and_edges() -> None:
    """Both endpoints, as `remove_nodes_from` has always promised."""

    config = PheasantConfig.model_validate({"pheasant": {"name": KB}})
    builder = GraphBuilder(config)
    _populate(builder.graph, artifacts=3)
    artifact = "file:docs:src/file_1.md:branch=none"
    before_edges = builder.graph.number_of_edges()

    builder.remove_artifact_nodes([artifact])

    assert not builder.graph.has_node(artifact)
    assert builder.graph.number_of_edges() < before_edges
    for (source, target), _edge_map in builder.graph.iter_edges():
        assert source != artifact and target != artifact, "a dangling edge survived"


# --------------------------------------------------------------------------
# 2. The pass that was doing all of that to produce nothing
# --------------------------------------------------------------------------


def test_the_similarity_pass_is_retired_and_stays_retired() -> None:
    """It keyed off `concept_terms`, and concept extraction was retired.

    `_base_concepts` returns an empty enrichment, so no node has carried a
    concept term since — and the pass built an inverted index over an empty
    term set, walked every node and copied every artifact's attributes to emit
    zero edges. The concept retirement's own third justification was already
    "the live graph contained **zero** `similar_to` edges"; this is the walk
    that was still being paid for them.

    Asserted as "produces nothing from a graph that would have fed it", so if
    a real similarity pass ever returns it has to come back with a test that
    says what it emits.
    """

    config = PheasantConfig.model_validate({"pheasant": {"name": KB}})
    builder = GraphBuilder(config)
    _populate(builder.graph)
    before = builder.graph.number_of_edges()

    builder.add_similarity_edges()
    builder.add_similarity_edges("docs", changed_ids={"file:docs:src/file_0.md:branch=none"})

    assert builder.graph.number_of_edges() == before
    assert not any(
        attrs.get("type") == "similar_to"
        for _pair, edge_map in builder.graph.iter_edges()
        for attrs in edge_map.values()
    )


def test_no_node_carries_a_concept_term(tmp_path: Path) -> None:
    """The premise the retirement above rests on, checked against a real sync.

    If enrichment ever starts emitting `concept_terms` again, the similarity
    pass is no longer dead and this is where that gets noticed — rather than
    in a silent no-op that quietly stops linking things.
    """

    engine = SyncEngine(_config(tmp_path))
    try:
        engine.sync_source("docs", "full")
        graph = engine.graph_builder.graph
        assert graph.number_of_nodes() > 1, "the fixture indexed nothing"
        carriers = [node_id for node_id, attrs in graph.iter_nodes() if attrs.get("concept_terms")]
    finally:
        engine.close()

    assert not carriers, (
        f"{len(carriers)} nodes carry concept_terms; the similarity pass is no longer dead "
        "and its no-op is now dropping edges"
    )


# --------------------------------------------------------------------------
# 3. The index that replaced a maintained tally
# --------------------------------------------------------------------------


@pytest.mark.parametrize("index", ["idx_graph_nodes_type", "idx_graph_nodes_source"])
def test_the_graph_node_indexes_exist(tmp_path: Path, index: str) -> None:
    """Both have a reader, which is the bar an index has to clear here.

    `(kb_id, type)` covers the per-type tally `/overview`, `/graph/diagnostics`
    and the graph service's `stats` publish and the UI polls — measured 386ms
    to 61.5ms at 630k nodes, as a covering index, for 1% of the database.
    `(kb_id, source_id)` covers per-source deletion. Two others were written
    and removed for having no reader at all.
    """

    engine = SyncEngine(_config(tmp_path))
    try:
        rows = engine.state.rows(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index,)
        )
    finally:
        engine.close()

    assert rows, f"{index} is missing; the query it covers is a full scan without it"
