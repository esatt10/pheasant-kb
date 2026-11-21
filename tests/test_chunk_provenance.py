"""Where a chunk sits: its neighbours, its section, its pages (`graph/provenance.py`).

Two halves, and the second is the one that matters most. The structure is
drawn (chunk order, section membership, section order, pages), and nothing
that ranks reads it: relationship search, node search, the node index, answer
facts and answer expansion answer exactly as they did before it existed.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.graph.provenance import (
    NEXT_CHUNK,
    NEXT_HEADING,
    PROVENANCE_ATTRIBUTES,
    PROVENANCE_EDGE_TYPES,
    SECTION_HAS_CHUNK,
    page_of,
    page_starts,
    section_chunks,
)
from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.graph.sql import SqlGraph
from pheasant.ingestion.extractor import PagedText, _tidy, paged_text
from pheasant.search.graph_search import search_graph
from pheasant.sync.engine import SyncEngine

QUERIES = [
    "guide",
    "first section",
    "falcons",
    "birds",
    "birds.pdf",
    "habitat hedgerows",
    "chunk",
    "next",
    "section",
    "2",
    "1 introduction",
    "deeper herons",
]


def _guide(edited: bool = False) -> str:
    second = "## Second Section\nA sentence about cranes.\n\n### Deeper\nHerons nest here.\n"
    if edited:
        second = (
            "## Renamed Section\nA sentence about cranes.\n\n## Inserted\nNew words.\n\n"
            "### Deeper\nHerons nest here, and egrets.\n"
        )
    return (
        "# Guide\n\nIntro line.\n\n## First Section\n"
        + ("A sentence about falcons. " * 120)
        + "\n\n"
        + second
    )


def _corpus(root: Path, *, edited: bool = False, pdf: bool = True) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "guide.md").write_text(_guide(edited), encoding="utf-8")
    if pdf:
        from pheasant.ingestion.pdf_write import text_pdf

        pages = [
            ["1 Introduction", "Pheasants live in fields."] + ["Filler about grass."] * 30,
            ["2 Habitat", "They prefer hedgerows."] + ["Filler about hedges."] * 30,
            [],
            ["3 Diet", "Seeds and insects."] + ["Filler about seeds."] * 30,
        ]
        (root / "birds.pdf").write_bytes(text_pdf(pages))


#: A throwaway Postgres (CLAUDE.md rule 10); unset, the Postgres cases skip.
DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()
BACKENDS = [
    "sqlite",
    pytest.param(
        "postgres",
        marks=pytest.mark.skipif(
            not DSN, reason="set PHEASANT_TEST_POSTGRES_DSN to run on Postgres"
        ),
    ),
]


def _engine(
    tmp_path: Path, name: str, workspace: Path, backend: str = "sqlite", **kwargs
) -> SyncEngine:
    storage: dict = {"graph_snapshots": False}
    if backend == "postgres":
        storage.update(backend="postgres", dsn_env="PHEASANT_TEST_POSTGRES_DSN")
    return SyncEngine(
        PheasantConfig.model_validate(
            {
                "pheasant": {
                    "name": "provenance",
                    "defaults": "standalone",
                    "state_path": str(tmp_path / name / "state"),
                    "workspace_root": str(workspace),
                    "exports_path": str(tmp_path / name / "exports"),
                },
                "storage": storage,
                "sync": {"source_processing": {"taxonomy_enabled": True, "chunk_max_chars": 800}},
                "sources": [
                    {
                        "name": "docs",
                        "type": "document_folder",
                        "path": str(workspace),
                        "include": ["**/*.md", "**/*.pdf"],
                    }
                ],
            }
        ),
        **kwargs,
    )


def _reset() -> None:
    from tests.test_backend_parity import _reset as reset

    reset(DSN)


@pytest.fixture(params=BACKENDS)
def indexed(request, tmp_path: Path):
    pytest.importorskip("pypdfium2")
    if request.param == "postgres":
        _reset()
    workspace = tmp_path / "ws"
    _corpus(workspace)
    engine = _engine(tmp_path, "main", workspace, request.param)
    engine.sync_source("docs", "full")
    yield engine
    engine.close()


def _out(graph, node_id: str, edge_type: str) -> list[str]:
    return [
        target
        for _s, target, edge_map in graph.out_edges(node_id)
        for data in edge_map.values()
        if data.get("type") == edge_type
    ]


# ------------------------------------------------------------------ pages


def test_a_paged_text_is_the_text_the_reader_always_produced():
    rng = random.Random(7)
    pieces = ["alpha beta", "  gamma\t\tdelta  ", "", "\n\n\nepsilon\n\n\n\nzeta ", "eta\r\ntheta"]
    for _ in range(200):
        pages = [rng.choice(pieces) + rng.choice(["", " ", "\n", "\n\n\n"]) for _ in range(6)]
        text = paged_text(pages)
        assert text == _tidy("\n\n".join(pages)), pages
        if isinstance(text, PagedText):
            lines = text.splitlines()
            for page, start in zip(pages, text.page_lines, strict=True):
                if start is None:
                    assert not _tidy(page)
                else:
                    assert lines[start - 1] == _tidy(page).splitlines()[0]


def test_lines_map_to_pages_and_an_empty_page_holds_none():
    starts = page_starts([1, None, 5, 8])
    assert [page_of(starts, line) for line in (1, 4, 5, 7, 8, 99)] == [1, 1, 3, 3, 4, 4]
    assert page_starts([3, 1]) == ([], [])  # not ascending: no page map at all
    assert page_of(page_starts(None), 3) is None


def test_pdf_chunks_and_sections_carry_their_pages(indexed):
    rows = indexed.state.rows(
        "SELECT chunk_index, page_start, page_end, heading_path FROM chunks "
        "WHERE artifact_id LIKE '%birds.pdf%' ORDER BY chunk_index",
        (),
    )
    assert [(r["page_start"], r["page_end"]) for r in rows][0] == (1, 1)
    assert {r["page_start"] for r in rows} == {1, 2, 4}, "page 3 is empty"
    graph = indexed.graph_builder.graph
    pages = {
        attrs["label"]: attrs.get("page")
        for _id, attrs in graph.iter_nodes()
        if attrs.get("type") == "heading" and attrs.get("relative_path") == "birds.pdf"
    }
    assert pages == {"1 Introduction": 1, "2 Habitat": 2, "3 Diet": 4}
    markdown = indexed.state.rows(
        "SELECT page_start FROM chunks WHERE artifact_id LIKE '%guide.md%'", ()
    )
    assert markdown and all(r["page_start"] is None for r in markdown)


# -------------------------------------------------------------- structure


def test_chunks_are_chained_in_order_and_sections_hold_their_chunks(indexed):
    graph = indexed.graph_builder.graph
    for artifact_id in ("file:docs:guide.md:branch=none", "file:docs:birds.pdf:branch=none"):
        chunks = sorted(
            _out(graph, artifact_id, "has_chunk"), key=lambda i: graph.nodes[i]["chunk_index"]
        )
        assert len(chunks) > 2
        for current, following in zip(chunks, chunks[1:], strict=False):
            assert _out(graph, current, NEXT_CHUNK) == [following]
        assert _out(graph, chunks[-1], NEXT_CHUNK) == []
        headings = sorted(
            _out(graph, artifact_id, "has_heading"), key=lambda i: graph.nodes[i]["order"]
        )
        assert [graph.nodes[h]["order"] for h in headings] == list(range(len(headings)))
        for current, following in zip(headings, headings[1:], strict=False):
            assert _out(graph, current, NEXT_HEADING) == [following]
        covered = set()
        for heading in headings:
            held = _out(graph, heading, SECTION_HAS_CHUNK)
            line = graph.nodes[heading]["start_line"]
            assert held, heading
            assert any(
                graph.nodes[c]["start_line"] <= line <= graph.nodes[c]["end_line"]
                or graph.nodes[c]["start_line"] >= line
                for c in held
            )
            covered.update(held)
        # Every chunk after the first heading belongs to some section.
        first = graph.nodes[headings[0]]["start_line"]
        assert {c for c in chunks if graph.nodes[c]["end_line"] >= first} <= covered


def test_a_section_owns_every_chunk_that_holds_its_own_lines():
    headings = [("h1", 1), ("h2", 10), ("h3", 10), ("h4", 30)]
    chunks = [("c0", 1, 9), ("c1", 9, 20), ("c2", 21, 35), ("c3", 36, 40)]
    assert section_chunks(headings, chunks) == {
        "h1": ["c0", "c1"],
        "h2": ["c1"],
        "h3": ["c1", "c2"],
        "h4": ["c2", "c3"],
    }


# ------------------------------------------------- idempotency and edits


def _shape(engine: SyncEngine):
    link = engine.graph_builder.graph.to_node_link()

    def stable(attrs):
        return json.dumps(
            {k: v for k, v in attrs.items() if k not in {"created_at", "updated_at"}},
            sort_keys=True,
            default=str,
        )

    return (
        sorted((node["id"], stable(node)) for node in link["nodes"]),
        sorted(
            (edge["source"], edge["target"], stable({k: v for k, v in edge.items() if k != "key"}))
            for edge in link["links"]
        ),
    )


def test_an_unchanged_resync_moves_nothing(indexed):
    published = indexed.graph_store.published_generation("provenance")["generation_id"]
    before = _shape(indexed)
    assert indexed.sync_source("docs", "incremental").indexed_artifacts == 0
    assert indexed.graph_store.published_generation("provenance")["generation_id"] == published
    # A full re-index rebuilds the same graph; its id may still move, because
    # `created_at` restarts (CLAUDE.md §6, "a `full` re-index still moves the
    # generation id"), so the content is what is compared.
    assert indexed.sync_source("docs", "full").indexed_artifacts == 2
    assert _shape(indexed) == before


def test_an_edited_outline_leaves_exactly_what_a_full_sync_draws(tmp_path: Path):
    """Sections renamed, inserted and reordered: the old section nodes, their
    order edges and their chunk edges must all go, as a full sync would."""

    workspace = tmp_path / "ws"
    _corpus(workspace, pdf=False)
    engine = _engine(tmp_path, "incremental", workspace)
    try:
        engine.sync_source("docs", "full")
        _corpus(workspace, edited=True, pdf=False)
        assert engine.sync_source("docs", "incremental").indexed_artifacts == 1
        incremental = _shape(engine)
    finally:
        engine.close()
    reference = _engine(tmp_path, "full", workspace)
    try:
        reference.sync_source("docs", "full")
        assert incremental == _shape(reference)
    finally:
        reference.close()
    labels = " ".join(node for node, _attrs in incremental[0])
    assert "Second Section" not in json.dumps(incremental[0])
    assert labels


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_fleet_writer_draws_what_the_resident_writer_draws(tmp_path: Path, backend: str):
    if backend == "postgres":
        _reset()
    workspace = tmp_path / "ws"
    _corpus(workspace, pdf=False)
    resident = _engine(tmp_path, "resident", workspace)
    rows = _engine(tmp_path, "rows", workspace, backend, defer_persisted_graph_load=True)

    def content(engine):
        store, kb = engine.graph_store.rows, engine.config.knowledge_base_id

        def stable(attrs):
            return json.dumps(
                {k: v for k, v in attrs.items() if k not in {"created_at", "updated_at"}},
                sort_keys=True,
            )

        return (
            sorted((key, stable(attrs)) for key, attrs in store.iter_nodes(kb)),
            sorted(
                (a, b, stable(attrs))
                for (a, b), parallel in store.iter_edges(kb)
                for attrs in parallel.values()
            ),
        )

    try:
        for engine in (resident, rows):
            engine.sync_source("docs", "incremental")
        assert content(rows) == content(resident)
        _corpus(workspace, edited=True, pdf=False)
        for engine in (resident, rows):
            engine.sync_source("docs", "incremental")
        assert content(rows) == content(resident)
    finally:
        resident.close()
        rows.close()


# ---------------------------------------------------- nothing that ranks


def _without_provenance(graph) -> SimpleMultiDiGraph:
    link = graph.to_node_link()
    link["nodes"] = [
        {k: v for k, v in node.items() if k not in PROVENANCE_ATTRIBUTES} for node in link["nodes"]
    ]
    link["links"] = [e for e in link["links"] if e.get("type") not in PROVENANCE_EDGE_TYPES]
    return SimpleMultiDiGraph.from_node_link(link)


def _hits(results):
    return [
        (r.get("kind"), r.get("node_id"), r.get("edge_type"), r.get("target"), r.get("score"))
        for r in results
    ]


def _reloaded(graph) -> SimpleMultiDiGraph:
    """The graph rebuilt the way :func:`_without_provenance` rebuilds it.

    Equal-scoring hits are ordered by graph iteration, which differs between a
    graph built in place and one reloaded from its node-link form (CLAUDE.md
    §6, "not every divergence a comparison finds"). Comparing two graphs
    built the same way compares what the structure changed, and nothing else.
    """

    return SimpleMultiDiGraph.from_node_link(graph.to_node_link())


@pytest.mark.parametrize("relationships", [True, False])
def test_graph_search_answers_as_it_did_before_the_structure(indexed, relationships):
    graph = indexed.graph_builder.graph
    with_it, before = _reloaded(graph), _without_provenance(graph)
    stored = SqlGraph(indexed.state, indexed.config.knowledge_base_id)
    for query in QUERIES:
        expected = _hits(search_graph(before, query, 20, include_relationships=relationships))
        assert _hits(search_graph(with_it, query, 20, include_relationships=relationships)) == (
            expected
        ), query
        # The row store orders ties its own way (key order); every hit and
        # every score must still be the same, untruncated.
        everything = sorted(
            _hits(search_graph(before, query, 500, include_relationships=relationships)),
            key=repr,
        )
        assert (
            sorted(
                _hits(search_graph(stored, query, 500, include_relationships=relationships)),
                key=repr,
            )
            == everything
        ), query


def test_the_row_store_never_offers_a_provenance_edge_to_score(indexed):
    stored = SqlGraph(indexed.state, indexed.config.knowledge_base_id)
    offered = {
        data.get("type")
        for tokens in (["guide"], ["birds"], ["chunk", "next", "section"])
        for _pair, edge_map in stored.candidate_edges(tokens)
        for data in edge_map.values()
    }
    assert offered and not offered & PROVENANCE_EDGE_TYPES


def test_the_node_index_holds_no_position_or_page(indexed):
    """The index only rejects nodes that cannot match; it never held the new
    attributes, so it rejects exactly what it rejected before them."""

    graph = indexed.graph_builder.graph
    assert any(attrs.get("page_start") for _id, attrs in graph.iter_nodes())
    assert sorted(graph.index_rows()) == sorted(_without_provenance(graph).index_rows())


def test_answer_facts_say_nothing_about_order_or_sections(indexed):
    from pheasant.assistant.chat import collect_facts

    graph = indexed.graph_builder.graph
    chunks = [n for n, a in graph.iter_nodes() if a.get("type") in {"chunk", "heading"}]
    artifacts = ["file:docs:guide.md:branch=none", "file:docs:birds.pdf:branch=none"]
    for node_ids in (chunks, artifacts, artifacts + chunks):
        assert collect_facts(_reloaded(graph), node_ids, 50) == collect_facts(
            _without_provenance(graph), node_ids, 50
        )


def test_answer_expansion_does_not_walk_from_a_chunk_or_a_section(indexed):
    from pheasant.assistant.retrieval import Passage, PheasantRetriever

    graph = indexed.graph_builder.graph
    retriever = PheasantRetriever.__new__(PheasantRetriever)
    retriever.graph = graph
    walked: list[str] = []
    retriever.neighbors = lambda node_id, *a, **k: walked.append(node_id) or []
    retriever._artifact_allowed = lambda *a, **k: True
    roots = [n for n, a in graph.iter_nodes() if a.get("type") in {"chunk", "heading"}][:4]
    passages = [
        Passage(
            node_id=n,
            chunk_id=None,
            title=n,
            relative_path=None,
            source_id="docs",
            type=graph.nodes[n]["type"],
            score=1.0,
            snippet="",
            mode="graph",
        )
        for n in roots
    ]
    assert retriever._expand(passages, depth=2, per_node=4) == []
    assert walked == []
    # A text hit is typed `chunk` but rooted at its document: that one walks.
    document = Passage(
        node_id="file:docs:guide.md:branch=none",
        chunk_id=roots[0],
        title="guide.md",
        relative_path="guide.md",
        source_id="docs",
        type="chunk",
        score=1.0,
        snippet="",
        mode="text",
    )
    retriever._expand([document], depth=2, per_node=4)
    assert walked == ["file:docs:guide.md:branch=none"]


def test_reordered_sections_leave_exactly_what_a_full_sync_draws(tmp_path: Path):
    """Two sections swapped: their headings keep their ids, so the old
    `next_heading` edge between them must be retracted, not kept beside the new."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    first = "# Guide\n\n## Alpha\nAlpha words.\n\n## Beta\nBeta words.\n\n## Gamma\nGamma words.\n"
    second = "# Guide\n\n## Gamma\nGamma words.\n\n## Alpha\nAlpha words.\n\n## Beta\nBeta words.\n"
    (workspace / "guide.md").write_text(first, encoding="utf-8")
    engine = _engine(tmp_path, "incremental", workspace)
    try:
        engine.sync_source("docs", "full")
        (workspace / "guide.md").write_text(second, encoding="utf-8")
        assert engine.sync_source("docs", "incremental").indexed_artifacts == 1
        incremental = _shape(engine)
    finally:
        engine.close()
    reference = _engine(tmp_path, "full", workspace)
    try:
        reference.sync_source("docs", "full")
        assert incremental == _shape(reference)
    finally:
        reference.close()


def test_a_section_that_stops_holding_a_chunk_lets_it_go(tmp_path: Path):
    """A chunk keeps its id while its lines move (its id is its text and its
    index), so a section can lose a chunk that is still there. Real edits
    rarely do it; the retraction is what makes "rarely" not a stale edge."""

    from pheasant.graph.builder import GraphBuilder, chunk_node_id, heading_node_id
    from pheasant.ingestion.chunking import TextChunk
    from pheasant.ingestion.pipeline import ParsedArtifact
    from pheasant.ingestion.taxonomy import SectionHeading

    workspace = tmp_path / "ws"
    workspace.mkdir()
    engine = _engine(tmp_path, "unit", workspace)
    try:
        source = engine.config.sources[0]
        builder = GraphBuilder(engine.config)

        def artifact(first_line: int, second_line: int, spans):
            headings = [
                SectionHeading(line, 1, None, title, "markdown", title)
                for title, line in sorted(
                    (("One", first_line), ("Two", second_line)), key=lambda item: item[1]
                )
            ]
            return ParsedArtifact(
                id="file:docs:a.md:branch=none",
                source_id="docs",
                path=str(workspace / "a.md"),
                relative_path="a.md",
                type="document",
                mime_type=None,
                size_bytes=1,
                sha256="x",
                mtime="2026-01-01T00:00:00Z",
                git_branch=None,
                git_commit=None,
                chunks=[
                    TextChunk(index, f"text {index}", start, end)
                    for index, (start, end) in enumerate(spans)
                ],
                headings=headings,
            )

        builder.add_artifact(source, artifact(1, 5, [(1, 4), (5, 8)]))
        builder.add_artifact(source, artifact(6, 1, [(1, 4), (5, 8)]))
        one = heading_node_id("docs", "a.md", "One")
        c0 = chunk_node_id("docs", "a.md", TextChunk(0, "text 0", 1, 4))
        held = _out(builder.graph, one, SECTION_HAS_CHUNK)
        assert c0 not in held and held, held
    finally:
        engine.close()


# ------------------------------------------------- the worker wire, the schema


def test_pages_cross_the_worker_wire_and_an_older_worker_is_refused(indexed):
    from pheasant.sync.remote_worker import IncompatibleResult, parsed_from_wire, parsed_to_wire

    pdf = next(
        row
        for row in indexed.state.rows("SELECT path FROM artifacts", ())
        if "birds" in row["path"]
    )
    from pheasant.ingestion.extractor import build_extractor
    from pheasant.ingestion.pipeline import parse_file

    source = indexed.config.sources[0]
    parsed = parse_file(
        source,
        Path(pdf["path"]),
        extractor=build_extractor(indexed.config.ingestion.extractor),
    )
    assert parsed is not None and parsed.page_lines and parsed.chunks[0].page_start == 1
    wire = json.loads(json.dumps(parsed_to_wire(parsed)))
    back = parsed_from_wire(wire)
    assert back.page_lines == parsed.page_lines
    assert [(c.page_start, c.page_end) for c in back.chunks] == [
        (c.page_start, c.page_end) for c in parsed.chunks
    ]
    wire["chunks"][0]["from_a_newer_worker"] = 1  # unknown fields are ignored
    assert parsed_from_wire(wire).chunks[0] == parsed.chunks[0]
    del wire["page_lines"]
    with pytest.raises(IncompatibleResult, match="page numbers"):
        parsed_from_wire(wire)


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_state_written_before_pages_gains_the_columns(tmp_path: Path, backend: str):
    """On Postgres the schema marker would skip the DDL for a region that
    already has `chunks`; the `required` map is what makes the column arrive."""

    from pheasant.persistence.state_store import StateStore

    if backend == "postgres":
        _reset()
        engine = _engine(tmp_path, "pg", tmp_path, backend)
        state = engine.state
    else:
        engine, state = None, StateStore(tmp_path / "old.db")
        state.migrate()
    try:
        state.conn.execute("ALTER TABLE chunks DROP COLUMN page_end")
        state.conn.execute("ALTER TABLE chunks DROP COLUMN page_start")
        state.conn.commit()
        assert "page_start" not in state.backend.table_columns("chunks")
        state.migrate()
        assert {"page_start", "page_end"} <= set(state.backend.table_columns("chunks"))
        state.migrate()  # idempotent
    finally:
        if engine is not None:
            engine.close()
        else:
            state.close()
