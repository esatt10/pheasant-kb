"""Acceptance tests for idempotent source synchronization."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from pheasant.config.loader import load_config
from pheasant.config.schema import PheasantConfig
from pheasant.persistence.graph_store import GraphStore
from pheasant.registry.source_registry import SourceRegistry
from pheasant.search.hybrid import HybridSearch
from pheasant.search.sqlite_store import SearchStore
from pheasant.sync.engine import SyncEngine
from tests.conftest import make_vector_engine, run_sync, sync_result_counts


def test_configured_upload_source_keeps_zip_index_across_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "state"
    upload_dir = state_path / "uploads" / "uploads"
    upload_dir.mkdir(parents=True)
    with zipfile.ZipFile(upload_dir / "remarkable.zip", "w") as archive:
        archive.writestr("guide.txt", "A searchable test document inside the uploaded archive.")
    payload = {
        "pheasant": {
            "name": "upload-restart",
            "state_path": str(state_path),
            "workspace_root": str(tmp_path),
            "exports_path": str(tmp_path / "exports"),
        },
        "sources": [{"name": "uploads", "type": "document_folder", "path": str(upload_dir)}],
    }
    first = SyncEngine(PheasantConfig.model_validate(payload))
    try:
        assert first.config.sources[0].include == ["**/*"]
        assert first.sync_source("uploads", "full").indexed_artifacts == 1
    finally:
        first.close()

    reopened = SyncEngine(PheasantConfig.model_validate(payload))
    try:
        assert reopened.config.sources[0].include == ["**/*"]
        assert reopened.sync_source("uploads", "incremental").indexed_artifacts == 0
        assert len(reopened.state.rows("SELECT id FROM artifacts WHERE source_id='uploads'")) == 1
    finally:
        reopened.close()


def test_removed_configured_source_stays_absent_after_restart_until_reregistered(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "guide.md").write_text("# Guide\n\nRemoval must persist.\n", encoding="utf-8")
    payload = {
        "pheasant": {
            "name": "removal-lifecycle",
            "state_path": str(tmp_path / "state"),
            "workspace_root": str(tmp_path),
            "exports_path": str(tmp_path / "exports"),
        },
        "sources": [
            {
                "name": "guide",
                "type": "document_folder",
                "path": str(corpus),
                "include": ["*.md", "**/*.md"],
            }
        ],
    }
    first = SyncEngine(PheasantConfig.model_validate(payload))
    try:
        assert first.sync_source("guide", "full").indexed_artifacts == 1
        first.remove_source("guide")
        assert first.state.source_removed("guide")
        assert first.state.get_source("guide") is None
        assert first.state.rows("SELECT 1 FROM artifacts WHERE source_id=?", ("guide",)) == []
    finally:
        first.close()

    reopened = SyncEngine(PheasantConfig.model_validate(payload))
    try:
        assert reopened.state.get_source("guide") is None
        assert SourceRegistry(reopened.config, reopened.state).list_sources() == []
        assert reopened.enabled_sources() == []
        with pytest.raises(KeyError, match="Removed source"):
            reopened.sync_source("guide", "full")
        SourceRegistry(reopened.config, reopened.state).register_source(reopened.config.sources[0])
        assert not reopened.state.source_removed("guide")
        assert reopened.sync_source("guide", "full").indexed_artifacts == 1
    finally:
        reopened.close()


def test_fleet_processing_policy_reindexes_once_when_enabled(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "guide.md").write_text(
        "# Guide\n\n## First Section\n"
        + ("A sentence about falcons. " * 200)
        + "\n## Second Section\nA sentence about cranes.\n",
        encoding="utf-8",
    )
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "processing-policy",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [{"name": "guide", "type": "document_folder", "path": str(corpus)}],
        }
    )
    engine = SyncEngine(config)
    try:
        assert engine.sync_source("guide", "full").indexed_artifacts == 1
        assert engine.sync_source("guide", "incremental").indexed_artifacts == 0

        config.sync.source_processing.chunk_max_chars = 2000
        config.sync.source_processing.taxonomy_enabled = True
        assert engine.sync_source("guide", "incremental").indexed_artifacts == 1
        chunks = engine.state.rows("SELECT text, heading_path FROM chunks ORDER BY chunk_index", ())
        assert chunks
        assert all(len(row["text"]) <= 2000 for row in chunks)
        assert any(row["heading_path"] for row in chunks)
        assert engine.sync_source("guide", "incremental").indexed_artifacts == 0

        config.sync.source_processing.taxonomy_enabled = False
        assert engine.sync_source("guide", "incremental").indexed_artifacts == 1
        assert engine.sync_source("guide", "incremental").indexed_artifacts == 0
    finally:
        engine.close()


def test_mixed_zip_members_keep_stable_ids_and_searchable_content(tmp_path: Path) -> None:
    """A direct ZIP source exposes nested mixed files, then resyncs each independently."""

    from tests.test_document_extraction import HANDBOOK

    archive_path = tmp_path / "bundle.zip"

    def write_archive(note: str) -> None:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("notes/start.md", f"# Start\n\n{note}\n")
            archive.writestr("docs/handbook.pdf", HANDBOOK.read_bytes())
            archive.writestr("images/diagram.png", b"image-fixture")
            archive.writestr("audio/meeting.mp3", b"audio-fixture")
            archive.writestr("misc/unsupported.dat", b"ignored")
            archive.writestr("../escape.md", b"unsafe")
            archive.writestr("private/answer.md", b"Must not enter the index")

    write_archive("The orchid service is running.")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "zip-acceptance",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "ingestion": {"extractor": {"provider": "builtin"}},
            "sync": {"concurrency": {"file_executor": "process", "max_parallel_files": 2}},
            "readiness": {"corpus_denylist": ["private/*"]},
            "sources": [
                {
                    "name": "bundle",
                    "type": "single_file",
                    "path": str(archive_path),
                    "include": ["bundle.zip"],
                }
            ],
        }
    )
    engine = SyncEngine(config)
    try:
        first = engine.sync_source("bundle", "full")
        assert first.indexed_artifacts == 4
        assert first.details["refused"][0]["relative_path"] == "bundle.zip/private/answer.md"
        assert engine.extractor is not None
        assert engine.captioner is not None
        assert engine.transcriber is not None
        expected = {
            "bundle.zip/notes/start.md",
            "bundle.zip/docs/handbook.pdf",
            "bundle.zip/images/diagram.png",
            "bundle.zip/audio/meeting.mp3",
        }
        artifact_rows = engine.state.rows(
            "SELECT id, relative_path FROM artifacts WHERE source_id=?", ("bundle",)
        )
        assert {row["relative_path"] for row in artifact_rows} == expected
        ids = {row["relative_path"]: row["id"] for row in artifact_rows}
        assert ids["bundle.zip/notes/start.md"] == (
            "file:bundle:bundle.zip/notes/start.md:branch=none"
        )
        search = HybridSearch(SearchStore(engine.state), vector=None)
        hits = search.search_context(
            config.knowledge_base_id, "ZEBRAFISH", mode="text", max_results=10
        )["results"]
        assert any(hit.get("relative_path") == "bundle.zip/docs/handbook.pdf" for hit in hits)

        again = engine.sync_source("bundle", "incremental")
        assert again.indexed_artifacts == 0
        write_archive("The orchid service was updated.")
        changed = engine.sync_source("bundle", "incremental")
        assert changed.indexed_artifacts == 1
        assert {
            row["relative_path"]: row["id"]
            for row in engine.state.rows(
                "SELECT id, relative_path FROM artifacts WHERE source_id=?", ("bundle",)
            )
        } == ids
    finally:
        engine.close()


def test_full_sync_is_idempotent_for_sample_repository(sync_engine: object) -> None:
    """Running a full sync twice should not duplicate graph/search artifacts."""

    first_result = run_sync(sync_engine, source_name="pheasant-repo", mode="full")
    first_counts = sync_result_counts(first_result, sync_engine)

    second_result = run_sync(sync_engine, source_name="pheasant-repo", mode="full")
    second_counts = sync_result_counts(second_result, sync_engine)

    assert second_counts == first_counts
    assert all(value > 0 for value in second_counts.values())


def test_resync_of_unchanged_content_performs_zero_embedder_calls(tmp_path: Path) -> None:
    """Synapse 21.4: embed-on-sync is keyed on text_hash via content-addressed
    chunk ids, so re-syncing unchanged content never re-embeds — in full or
    incremental mode."""

    engine = make_vector_engine(tmp_path)
    run_sync(engine, source_name="notes", mode="full")
    embedder = engine.vectors.embedder
    store = engine.vectors.store
    assert embedder.texts_embedded > 0
    baseline_calls = embedder.calls
    baseline_count = store.count()

    run_sync(engine, source_name="notes", mode="full")
    assert embedder.calls == baseline_calls
    assert store.count() == baseline_count

    run_sync(engine, source_name="notes", mode="incremental")
    assert embedder.calls == baseline_calls
    assert store.count() == baseline_count


def test_background_embedding_resyncs_to_the_same_vector_state(tmp_path: Path) -> None:
    """The engine embeds off the commit loop; the store must not notice.

    A queue of one chunk sends every file's batch to the background thread,
    which the default queue never does on a corpus this small. After each
    sync the store holds exactly the chunks the state store holds -- nothing
    missing, nothing stale -- and an unchanged re-sync embeds nothing.
    """

    engine = make_vector_engine(tmp_path)
    engine.vectors.queue_size = 1
    assert engine.vectors.background is True

    def stored_equals_live() -> set[str]:
        live = {str(row["id"]) for row in engine.state.rows("SELECT id FROM chunks")}
        assert engine.vectors.store.existing_ids(sorted(live)) == live
        assert engine.vectors.store.count() == len(live)
        return live

    run_sync(engine, source_name="notes", mode="full")
    first = stored_equals_live()
    calls = engine.vectors.embedder.calls
    run_sync(engine, source_name="notes", mode="full")
    run_sync(engine, source_name="notes", mode="incremental")
    assert stored_equals_live() == first
    assert engine.vectors.embedder.calls == calls

    notes = Path(engine.config.sources[0].path)
    (notes / "kitchen.md").write_text("# Kitchen\n\nRestock the pantry weekly.\n")
    run_sync(engine, source_name="notes", mode="incremental")
    edited = stored_equals_live()
    assert edited != first  # the edited file's chunk was replaced, not added beside


def test_thread_and_process_executors_index_the_same_state(tmp_path: Path) -> None:
    """Which executor prepared a file is invisible in what was committed."""

    docs = tmp_path / "workspace" / "docs"
    docs.mkdir(parents=True)
    for index in range(12):
        (docs / f"note{index:02d}.md").write_text(
            f"# Note {index}\n\nSee [next](note{(index + 1) % 12:02d}.md). text {index}\n"
        )
    (docs / "answer_key.md").write_text("# Answers\n\nnever indexed\n")

    def index_with(executor: str) -> dict[str, object]:
        root = tmp_path / executor
        config = PheasantConfig.model_validate(
            {
                "pheasant": {
                    "name": "executors",
                    "state_path": str(root / "state"),
                    "workspace_root": str(tmp_path / "workspace"),
                    "exports_path": str(root / "exports"),
                },
                "readiness": {"corpus_denylist": ["answer_key.md"]},
                "sync": {"concurrency": {"file_executor": executor, "max_parallel_files": 3}},
                "sources": [{"name": "docs", "type": "markdown_folder", "path": str(docs)}],
            }
        )
        engine = SyncEngine(config)
        try:
            engine.sync_source("docs", "full")
            second = engine.sync_source("docs", "incremental")
            return {
                "artifacts": sorted(
                    str(row["id"]) for row in engine.state.rows("SELECT id FROM artifacts")
                ),
                "chunks": sorted(
                    str(row["id"]) for row in engine.state.rows("SELECT id FROM chunks")
                ),
                "resync_indexed": second.indexed_artifacts,
            }
        finally:
            engine.close()

    thread, process = index_with("thread"), index_with("process")
    assert thread == process
    assert len(thread["artifacts"]) == 12  # the denylisted file under neither
    assert thread["resync_indexed"] == 0


def test_incremental_noop_does_not_materialize_the_persisted_graph(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config(config_path)
    source = cfg.sources[0]
    writer = SyncEngine(cfg)
    try:
        first = writer.sync_source(source.name, "full")
        expected_counts = (first.graph_nodes, first.graph_edges)
    finally:
        writer.close()

    def unexpected_load(_self: GraphStore, _kb_id: str):
        raise AssertionError("unchanged incremental sync loaded the whole graph")

    monkeypatch.setattr(GraphStore, "load", unexpected_load)
    reader = SyncEngine(cfg, defer_persisted_graph_load=True)
    try:
        reader.ensure_node_index()
        result = reader.sync_source(source.name, "incremental")
    finally:
        reader.close()

    assert result.indexed_artifacts == 0
    assert (result.graph_nodes, result.graph_edges) == expected_counts


# ---------------------------------------------------------------------------
# Deferred enrichment (agent-speed memory compaction, Phase 0)
# ---------------------------------------------------------------------------
#
# `memory_write`'s default `sync=True` calls `sync_source(..., enrich="deferred")`
# so a single interactive write never pays the whole-graph walk
# (`add_similarity_edges` + `add_cross_source_edges` + `_bridge_memory`,
# `sync/engine.py:_finalize_index_state`). CLAUDE.md rule 4: any sync change
# owes idempotency cases here.


def _about_edge_count(tools) -> int:
    graph = tools.engine.graph_builder.graph
    n = 0
    with graph.reading():
        for _endpoints, edge_map in graph.iter_edges():
            n += sum(1 for data in edge_map.values() if data.get("type") == "about")
    return n


def _deferred_enrichment_tools(tmp_path: Path):
    from pheasant.config.loader import load_config
    from pheasant.mcp_server.tools import PheasantTools

    (tmp_path / "corpus").mkdir()
    (tmp_path / "corpus" / "runbook.md").write_text(
        "# Runbook\n\nThe Kestrel Gateway is operated by the Platform Team.\n",
        encoding="utf-8",
    )
    (tmp_path / "memory").mkdir()
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: deferred-enrich
  state_path: {tmp_path / "state"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
sources:
  - name: corpus
    type: document_folder
    path: corpus
    include: ["**/*.md"]
  - name: agent-memory
    type: memory
    path: memory
""",
        encoding="utf-8",
    )
    tools = PheasantTools(load_config(config_path))
    tools.sync_source("deferred-enrich", "corpus", "full")
    return tools


def test_a_deferred_memory_write_skips_the_whole_graph_walk(tmp_path: Path) -> None:
    """The write itself must not draw the `about` edge that only
    `_bridge_memory` (the whole-graph walk) draws, and must mark the source
    enrichment-dirty so the next pass knows there is work to do."""

    tools = _deferred_enrichment_tools(tmp_path)
    try:
        dirty_scope = tools.engine._ENRICH_DIRTY_SCOPE.format(name="agent-memory")
        assert tools.engine.state.get_fingerprint(dirty_scope) is None

        tools.memory_write(
            "deferred-enrich",
            "See the runbook for how the Kestrel Gateway is operated.",
            scope="org",
        )

        assert _about_edge_count(tools) == 0, "a deferred write ran the whole-graph walk"
        assert tools.engine.state.get_fingerprint(dirty_scope) is not None
    finally:
        tools.engine.close()


def test_the_next_beat_enriches_once_and_a_second_beat_does_nothing(tmp_path: Path) -> None:
    """`enrich="deferred"` defers, it does not skip: the next `enrich="now"`
    pass (an ordinary incremental sync — the shape the scheduler beat takes)
    must produce the same graph a non-deferred write would have, and clear
    the dirty flag so a second beat with nothing new runs none of the three
    enrichment steps again."""

    tools = _deferred_enrichment_tools(tmp_path)
    try:
        dirty_scope = tools.engine._ENRICH_DIRTY_SCOPE.format(name="agent-memory")
        tools.memory_write(
            "deferred-enrich",
            "See the runbook for how the Kestrel Gateway is operated.",
            scope="org",
        )
        assert _about_edge_count(tools) == 0

        tools.sync_source("deferred-enrich", "agent-memory", "incremental")
        assert tools.engine.state.get_fingerprint(dirty_scope) is None
        first_pass_edges = _about_edge_count(tools)
        assert first_pass_edges > 0, "the deferred enrichment never ran"

        calls = {"similarity": 0, "cross_source": 0, "bridge": 0}
        builder = tools.engine.graph_builder
        original_similarity = builder.add_similarity_edges
        original_cross_source = builder.add_cross_source_edges
        original_bridge = tools.engine._bridge_memory

        def spy_similarity(*args, **kwargs):
            calls["similarity"] += 1
            return original_similarity(*args, **kwargs)

        def spy_cross_source(*args, **kwargs):
            calls["cross_source"] += 1
            return original_cross_source(*args, **kwargs)

        def spy_bridge(*args, **kwargs):
            calls["bridge"] += 1
            return original_bridge(*args, **kwargs)

        builder.add_similarity_edges = spy_similarity
        builder.add_cross_source_edges = spy_cross_source
        tools.engine._bridge_memory = spy_bridge
        try:
            tools.sync_source("deferred-enrich", "agent-memory", "incremental")
        finally:
            builder.add_similarity_edges = original_similarity
            builder.add_cross_source_edges = original_cross_source
            tools.engine._bridge_memory = original_bridge

        assert calls == {"similarity": 0, "cross_source": 0, "bridge": 0}, calls
        assert _about_edge_count(tools) == first_pass_edges
    finally:
        tools.engine.close()


def test_a_no_op_memory_write_never_marks_the_source_dirty(tmp_path: Path) -> None:
    """An exact-duplicate write (`created=False`) indexes nothing new, so it
    must not mark the source enrichment-dirty — there is nothing for the
    next pass to pick up."""

    tools = _deferred_enrichment_tools(tmp_path)
    try:
        dirty_scope = tools.engine._ENRICH_DIRTY_SCOPE.format(name="agent-memory")
        tools.memory_write("deferred-enrich", "A note nobody references.", scope="org")
        tools.sync_source("deferred-enrich", "agent-memory", "incremental")
        assert tools.engine.state.get_fingerprint(dirty_scope) is None

        result = tools.memory_write("deferred-enrich", "A note nobody references.", scope="org")
        assert not result["created"]
        assert tools.engine.state.get_fingerprint(dirty_scope) is None
    finally:
        tools.engine.close()


def test_observing_a_call_never_touches_the_index(tmp_path: Path) -> None:
    """The boundary, asserted from the sync side.

    An observation is a row with a retention policy. It must not create an
    artifact, a chunk, a memory record or an enrichment-dirty marker -- if it
    did, a busy region would be re-indexing itself once per request, and the
    "unchanged re-sync does no work" pillar would be false for any region with
    observation on.
    """

    from pheasant.sync.log_queue import hot_row_count, write_events
    from pheasant.telemetry.interactions import InteractionEvent

    tools = _deferred_enrichment_tools(tmp_path)
    try:
        engine = tools.engine
        dirty_scope = engine._ENRICH_DIRTY_SCOPE.format(name="agent-memory")
        tools.sync_source("deferred-enrich", "agent-memory", "incremental")

        def counts() -> tuple[int, int, int]:
            return (
                engine.state.rows("SELECT COUNT(*) AS c FROM artifacts", ())[0]["c"],
                engine.state.rows("SELECT COUNT(*) AS c FROM chunks", ())[0]["c"],
                engine.state.rows("SELECT COUNT(*) AS c FROM memory_records", ())[0]["c"],
            )

        before = counts()
        write_events(
            engine.state,
            [
                InteractionEvent(
                    kb_id="deferred-enrich",
                    operation="search_context",
                    trace_id=f"{index:032x}",
                    span_id=f"{index:016x}",
                    started_at="2026-01-01T00:00:00.000000Z",
                    query_text="where is the watcher",
                )
                for index in range(25)
            ],
        )

        assert hot_row_count(engine.state) == 25
        assert counts() == before
        assert engine.state.get_fingerprint(dirty_scope) is None

        # And the next incremental sync still finds nothing to do.
        result = tools.sync_source("deferred-enrich", "agent-memory", "incremental")
        assert result["indexed_artifacts"] == 0
        assert counts() == before
    finally:
        tools.engine.close()


def test_a_sync_is_unchanged_when_observation_is_off(sync_engine: object) -> None:
    """Rule 7, from the other direction: the default config must reach the
    same state a pre-observation build did, and no ledger table is touched."""

    first = sync_result_counts(run_sync(sync_engine, mode="full"), sync_engine)
    second = sync_result_counts(run_sync(sync_engine, mode="full"), sync_engine)

    assert second == first
    assert all(value > 0 for value in second.values())
    # The tables exist (the schema is replayed on every start) and stay empty:
    # observation is off, so nothing anywhere writes to them.
    assert sync_engine.state.rows("SELECT COUNT(*) AS c FROM interaction_events", ())[0]["c"] == 0
    assert sync_engine.state.rows("SELECT COUNT(*) AS c FROM log_tasks", ())[0]["c"] == 0


def test_a_web_collection_resync_is_free_and_state_is_unchanged(tmp_path: Path) -> None:
    """A listed web page, fetched twice, indexes once and leaves state identical.

    Covers the web connector admitting every listed URL (not just the ones the
    stock include globs happen to match) and HTML served from an extensionless
    URL being extracted: neither may make a second sync do work.
    """

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from pheasant.config.schema import PheasantConfig

    page = b"<html><body><p>idempotent web page body</p></body></html>"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        config = PheasantConfig.model_validate(
            {
                "pheasant": {
                    "name": "web-idempotency",
                    "state_path": str(tmp_path / "state"),
                    "workspace_root": str(tmp_path),
                    "exports_path": str(tmp_path / "exports"),
                },
                "ingestion": {"extractor": {"html_text": True}},
                "sources": [
                    {
                        "name": "web",
                        "type": "web_collection",
                        "urls": [f"{base}/post", f"{base}/about.html"],
                        "connector": {"allow_experimental": True},
                        "sync": {"on_startup": False},
                    }
                ],
            }
        )
        engine = SyncEngine(config)
        first = engine.sync_source("web", "incremental")
        snapshot = engine.state.rows(
            "SELECT id, sha256 FROM artifacts WHERE source_id = ? ORDER BY id", ("web",)
        )
        second = engine.sync_source("web", "incremental")
        after = engine.state.rows(
            "SELECT id, sha256 FROM artifacts WHERE source_id = ? ORDER BY id", ("web",)
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert first.indexed_artifacts == 2
    assert second.indexed_artifacts == 0
    assert second.skipped_artifacts == 2
    assert [dict(r) for r in snapshot] == [dict(r) for r in after]


def test_a_document_embedding_an_image_resyncs_to_the_same_state(tmp_path: Path) -> None:
    """The image path adds three things a re-sync must not disturb: an
    `embeds` edge (resolved in the global post-pass, which re-runs every sync),
    a stored copy of the image's bytes, and — through both — the published
    graph generation. An unchanged corpus keeps all three exactly."""

    from pheasant.config.schema import PheasantConfig
    from pheasant.ingestion.media import media_store_for_config
    from pheasant.sync.engine import SyncEngine

    fixture = Path(__file__).parent / "fixtures" / "sample_workspace" / "images" / "diagram.png"
    workspace = tmp_path / "workspace"
    (workspace / "img").mkdir(parents=True)
    (workspace / "img" / "arch.png").write_bytes(fixture.read_bytes())
    (workspace / "design.md").write_text("# Design\n\n![Arch](img/arch.png)\n", encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "embeds-idempotency",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "sources": [
                {
                    "name": "docs",
                    "type": "document_folder",
                    "path": str(workspace),
                    "include": ["**/*.md", "**/*.png"],
                }
            ],
        }
    )
    engine = SyncEngine(config)
    store = media_store_for_config(config)

    def embeds() -> int:
        graph = engine.graph_builder.graph
        return sum(
            1
            for _s, _t, edges in graph.out_edges("file:docs:design.md:branch=none")
            for data in edges.values()
            if data.get("type") == "embeds"
        )

    def stored() -> dict[str, int]:
        return {str(p): p.stat().st_mtime_ns for p in store.root.rglob("*.png")}

    engine.sync_source("docs", "full")
    first = (embeds(), stored(), engine.loaded_graph_generation)
    assert first[0] == 2, "one edge to the link stub, one to the resolved image"
    assert len(first[1]) == 1

    incremental = engine.sync_source("docs", "incremental")
    assert incremental.indexed_artifacts == 0
    assert (embeds(), stored(), engine.loaded_graph_generation) == first

    engine.sync_source("docs", "full")
    assert embeds() == first[0], "a full re-sync must not duplicate the resolved edge"
    assert stored() == first[1], "unchanged bytes are never rewritten"
    engine.close()


def test_an_edit_that_drops_an_image_link_drops_the_figure(tmp_path: Path) -> None:
    """Enrichment is upserted, so an `embeds` edge the new text no longer
    implies would survive an incremental re-index — and an answer would keep
    showing a figure the document stopped containing. Found on the fleet: edit
    a page, sync through the queue, and the old image is still a figure.

    Covers the three edits that matter: the link removed, the link pointed at
    a different image, and an unchanged re-sync (which must keep the edge)."""

    from pheasant.config.schema import PheasantConfig
    from pheasant.graph.figures import collect_figures
    from pheasant.sync.engine import SyncEngine

    fixture = Path(__file__).parent / "fixtures" / "sample_workspace" / "images" / "diagram.png"
    workspace = tmp_path / "workspace"
    (workspace / "img").mkdir(parents=True)
    (workspace / "img" / "old.png").write_bytes(fixture.read_bytes())
    (workspace / "img" / "new.png").write_bytes(fixture.read_bytes() + b"\x02")
    page = workspace / "design.md"
    page.write_text("# Design\n\n![Old](img/old.png)\n", encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "embeds-edits",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "sources": [
                {
                    "name": "docs",
                    "type": "document_folder",
                    "path": str(workspace),
                    "include": ["**/*.md", "**/*.png"],
                }
            ],
        }
    )
    engine = SyncEngine(config)
    doc = "file:docs:design.md:branch=none"

    def shown() -> list[str]:
        figures = collect_figures(engine.graph_builder.graph, [doc])
        return [figure["relative_path"] for figure in figures]

    engine.sync_source("docs", "full")
    assert shown() == ["img/old.png"]

    engine.sync_source("docs", "incremental")
    assert shown() == ["img/old.png"], "an unchanged page keeps its figure"

    page.write_text("# Design\n\n![New](img/new.png)\n", encoding="utf-8")
    engine.sync_source("docs", "incremental")
    assert shown() == ["img/new.png"], "a re-pointed link moves the figure"

    page.write_text("# Design\n\nThe diagram was removed.\n", encoding="utf-8")
    engine.sync_source("docs", "incremental")
    assert shown() == [], "a removed link removes the figure"

    # And the persisted graph agrees with the working set: a serving replica
    # reads the rows, not the indexer's copy.
    engine.reload_graph()
    assert collect_figures(engine.serving_graph(), [doc]) == []
    engine.close()


def test_editing_a_file_incrementally_retracts_its_old_chunk_nodes(tmp_path: Path) -> None:
    """Chunk ids embed the chunk's sha256, so each edit mints new chunk nodes.
    The incremental path used to leave the old ones (and their `has_chunk`
    edges) behind; a full sync cleared the source first and hid it. After any
    number of edits the artifact must own exactly its current chunks, in the
    working set and in the persisted rows, and an unchanged re-sync must not
    move the graph generation."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    page = workspace / "notes.md"
    page.write_text("# Notes\n\nversion zero\n", encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "chunk-leak",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "sources": [
                {
                    "name": "docs",
                    "type": "document_folder",
                    "path": str(workspace),
                    "include": ["**/*.md"],
                }
            ],
        }
    )
    engine = SyncEngine(config)

    def chunk_nodes(graph) -> list[str]:
        return sorted(
            node_id for node_id, attrs in graph.iter_nodes() if attrs.get("type") == "chunk"
        )

    engine.sync_source("docs", "full")
    baseline = len(chunk_nodes(engine.graph_builder.graph))
    assert baseline >= 1

    for edit in range(1, 4):
        page.write_text(f"# Notes\n\nversion {edit}\n", encoding="utf-8")
        engine.sync_source("docs", "incremental")
        assert len(chunk_nodes(engine.graph_builder.graph)) == baseline

    before = engine.loaded_graph_generation
    engine.sync_source("docs", "incremental")
    assert engine.loaded_graph_generation == before

    engine.reload_graph()
    assert len(chunk_nodes(engine.serving_graph())) == baseline
    engine.close()


def test_auto_chunking_resyncs_to_the_same_state_and_switching_reindexes_once(
    tmp_path: Path,
) -> None:
    """`chunking.strategy: auto` plans per file, so pillar 1 has to hold per
    planner version: an unchanged re-sync re-reads nothing and moves no graph
    generation, turning it on re-indexes exactly once, and the default spelled
    `fixed` re-indexes nothing at all."""

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "tariff.txt").write_text(
        "ARTICLE I DEFINITIONS\n\n"
        + "".join(
            f"1.{n} Defined Term {n}\n\nThe term {n} means the obligations of the provider.\n\n"
            for n in range(1, 10)
        )
        + "ARTICLE II SERVICE\n\n2.1 Network Service\n\n"
        + "Service shall be provided pursuant to this section. " * 120,
        encoding="utf-8",
    )
    (corpus / "notes.md").write_text("# Notes\n\n## One\n\nalpha\n\n## Two\n\nbeta\n", "utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "auto-chunking",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "sources": [
                {
                    "name": "docs",
                    "type": "document_folder",
                    "path": str(corpus),
                    "include": ["**/*.txt", "**/*.md"],
                }
            ],
        }
    )
    engine = SyncEngine(config)
    try:
        assert engine.sync_source("docs", "full").indexed_artifacts == 2
        config.sources[0].chunking.strategy = "fixed"
        assert engine.sync_source("docs", "incremental").indexed_artifacts == 0

        config.sync.source_processing.chunk_strategy = "auto"
        assert engine.sync_source("docs", "incremental").indexed_artifacts == 2
        before = engine.loaded_graph_generation
        chunks_before = engine.state.rows(
            "SELECT id, text, heading_path FROM chunks ORDER BY id", ()
        )
        assert engine.sync_source("docs", "incremental").indexed_artifacts == 0
        assert engine.loaded_graph_generation == before

        graph = engine.graph_builder.graph
        artifact = dict(graph.nodes["file:docs:tariff.txt:branch=none"])
        assert artifact["chunk_plan"]["profile"] == "structured"
        headings = [n for n, attrs in graph.iter_nodes() if attrs.get("type") == "heading"]
        assert headings, "auto should emit the outline it chunked by"

        # A full re-index under the same planner reproduces every chunk.
        engine.sync_source("docs", "full")
        assert (
            engine.state.rows("SELECT id, text, heading_path FROM chunks ORDER BY id", ())
            == chunks_before
        )
    finally:
        engine.close()


def test_okf_bundle_graph_is_stable_across_resync_and_restart(tmp_path: Path) -> None:
    """An OKF bundle's relationships are derived from what each concept
    recorded about itself, so an incremental sync re-reads nothing and still
    re-plans the bundle from the persisted artifact rows. Re-planning an
    unchanged bundle -- in the same process, or after a restart that loads the
    graph back from `/state` -- must upsert identical edges and move neither
    the edge set nor the published graph generation."""

    from tests.test_okf_bundles import write_bundle

    corpus = write_bundle(tmp_path / "bundle")
    payload = {
        "pheasant": {
            "name": "okf-idempotency",
            "state_path": str(tmp_path / "state"),
            "workspace_root": str(tmp_path),
            "exports_path": str(tmp_path / "exports"),
        },
        "storage": {"graph_snapshots": False},
        "sources": [{"name": "kb", "type": "document_folder", "path": str(corpus)}],
    }

    def okf_edges(graph) -> set[tuple[str, str, str]]:
        return {
            (source, target, data["type"])
            for (source, target), edge_map in graph.iter_edges()
            for data in edge_map.values()
            if data.get("enrichment_pass") == "okf"
        }

    first = SyncEngine(PheasantConfig.model_validate(payload))
    try:
        first.sync_source("kb", "full")
        edges = okf_edges(first.graph_builder.graph)
        generation = first.loaded_graph_generation
        assert edges and "okf_bundle:kb:." in first.graph_builder.graph
        assert first.sync_source("kb", "incremental").indexed_artifacts == 0
        assert okf_edges(first.graph_builder.graph) == edges
        assert first.loaded_graph_generation == generation
    finally:
        first.close()

    reopened = SyncEngine(PheasantConfig.model_validate(payload))
    try:
        assert okf_edges(reopened.serving_graph()) == edges
        # Touch one file so the finalize pass (and with it the OKF re-plan)
        # runs over a graph that came back from rows, not from this process.
        readme = corpus / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nMore.\n", encoding="utf-8")
        assert reopened.sync_source("kb", "incremental").indexed_artifacts == 1
        assert okf_edges(reopened.graph_builder.graph) == edges
    finally:
        reopened.close()


def test_a_full_resync_does_not_leave_a_dropped_edge_in_the_rows(tmp_path: Path) -> None:
    """A full sync drops the source from the working set and rebuilds it, so
    each artifact is removed and re-added inside one delta. `add_node` used to
    take a re-added node back out of the pending removals, which skipped the
    row writer's cascade -- and an edge the rebuild no longer emitted (here a
    deleted link's `references`) stayed in `/state`, gone from the working
    set and back again after the next restart. Found by the OKF pass, whose
    `graph.okf_bundles: false` retraction came back the same way."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.md").write_text("# A\n\nSee [b](b.md).\n", encoding="utf-8")
    (workspace / "b.md").write_text("# B\n\nTarget.\n", encoding="utf-8")
    payload = {
        "pheasant": {
            "name": "resync-cascade",
            "state_path": str(tmp_path / "state"),
            "workspace_root": str(tmp_path),
            "exports_path": str(tmp_path / "exports"),
        },
        "storage": {"graph_snapshots": False},
        "sources": [{"name": "docs", "type": "markdown_folder", "path": str(workspace)}],
    }
    a, b = "file:docs:a.md:branch=none", "file:docs:b.md:branch=none"

    def link(graph) -> list[str]:
        return [data["type"] for data in (graph.get_edge_data(a, b) or {}).values()]

    engine = SyncEngine(PheasantConfig.model_validate(payload))
    try:
        engine.sync_source("docs", "full")
        assert link(engine.graph_builder.graph) == ["references"]
        (workspace / "a.md").write_text("# A\n\nNo link any more.\n", encoding="utf-8")
        engine.sync_source("docs", "full")
        assert link(engine.graph_builder.graph) == []
        rows = engine.graph_store.rows
        maintained = engine.state.rows(
            "SELECT nodes, edges, node_fold, edge_fold FROM graph_generations WHERE kb_id=?",
            ("resync-cascade",),
        )[0]
        recomputed = rows.recompute_folds("resync-cascade")
        assert str(maintained["edge_fold"]) == recomputed["edge_fold"]
        assert str(maintained["node_fold"]) == recomputed["node_fold"]
        assert (int(maintained["nodes"]), int(maintained["edges"])) == rows.recount(
            "resync-cascade"
        )
    finally:
        engine.close()

    reopened = SyncEngine(PheasantConfig.model_validate(payload))
    try:
        assert link(reopened.graph_builder.graph) == []
        assert link(reopened.serving_graph()) == []
    finally:
        reopened.close()
