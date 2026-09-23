"""Acceptance tests for idempotent source synchronization."""

from __future__ import annotations

from pathlib import Path

import pytest

from pheasant.config.loader import load_config
from pheasant.persistence.graph_store import GraphStore
from pheasant.sync.engine import SyncEngine
from tests.conftest import make_vector_engine, run_sync, sync_result_counts


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
