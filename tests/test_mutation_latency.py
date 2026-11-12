"""Mutations scale with the affected knowledge and survive writer failures."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pheasant.persistence import artifact_outbox
from pheasant.services import ServiceContext, artifacts
from pheasant.services.archive import Indexing
from pheasant.sync.engine import SyncEngine
from pheasant.sync.queue import IndexTask


def test_source_mutation_predicates_use_indexes(loaded_config):
    from pheasant.persistence.derived_indexes import migrate_derived_indexes

    engine = SyncEngine(loaded_config, defer_persisted_graph_load=True)
    try:
        migrate_derived_indexes(engine.state)  # repeat upgrades are harmless
        for table in ("chunks", "symbols", "artifact_terms", "memory_records"):
            if engine.state.dialect.is_postgres:
                indexes = engine.state.rows(
                    "SELECT indexname FROM pg_indexes WHERE tablename=?", (table,)
                )
                assert f"idx_{table}_source_id" in {r["indexname"] for r in indexes}
            else:
                plan = engine.state.rows(
                    f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE source_id=?", ("probe",)
                )
                assert any(f"idx_{table}_source_id" in r["detail"] for r in plan), plan
    finally:
        engine.close()


def test_source_removal_never_loads_the_corpus(loaded_config, monkeypatch):
    first = SyncEngine(loaded_config)
    first.sync_source("architecture-notes", "full")
    kb = loaded_config.knowledge_base_id
    before = first.graph_store.rows.get_nodes(kb, [kb])[kb]
    first.close()
    engine = SyncEngine(loaded_config, defer_persisted_graph_load=True)
    monkeypatch.setattr(engine.graph_store, "load", lambda *_: pytest.fail("whole graph loaded"))
    try:
        engine.remove_source("architecture-notes")
        engine.remove_source("architecture-notes")  # interrupted delivery is safe
        assert not engine.state.rows(
            "SELECT id FROM artifacts WHERE source_id=?", ("architecture-notes",)
        )
        assert engine.graph_store.rows.get_nodes(kb, [kb])[kb] == before
        assert engine.state.source_removed("architecture-notes")
        assert not engine.state.rows(
            "SELECT node_id FROM graph_nodes WHERE source_id=?", ("architecture-notes",)
        )
    finally:
        engine.close()


def test_cold_source_removal_reads_only_memory_provenance(loaded_config, tmp_path, monkeypatch):
    from pheasant.config.schema import PheasantConfig
    from pheasant.memory.source_removal import retire_source_memories
    from pheasant.memory.store import MemoryStore

    data = loaded_config.model_dump(mode="json")
    memory_path = tmp_path / "memory"
    data["sources"].append({"name": "memory", "type": "memory", "path": str(memory_path)})
    config = PheasantConfig.model_validate(data)
    record, _ = MemoryStore(memory_path).append("Supported by the notes.", scope="org")
    engine = SyncEngine(config)
    try:
        engine.sync_source("architecture-notes", "full")
        engine.sync_source("memory", "full")
        memory_id = engine.state.rows(
            "SELECT artifact_id FROM memory_records WHERE record_id=?", (record.record_id,)
        )[0]["artifact_id"]
        target = engine.state.rows(
            "SELECT id FROM artifacts WHERE source_id=? LIMIT 1", ("architecture-notes",)
        )[0]["id"]
        engine.graph_builder.upsert_edge(memory_id, target, "about", {"source_id": "memory"})
        engine.graph_store.save(config.knowledge_base_id, engine.graph_builder.graph)
        engine._graph_loaded = False
        monkeypatch.setattr(
            engine.graph_store, "load", lambda *_: pytest.fail("whole graph loaded")
        )
        retired = retire_source_memories(engine, "architecture-notes")
        assert retired["archived"] == 1
        assert not MemoryStore(memory_path).list_records()
    finally:
        engine.close()


def _fleet(engine: SyncEngine, monkeypatch) -> ServiceContext:
    monkeypatch.setattr(artifacts, "_wake_projection", lambda _: None)
    return ServiceContext(
        config=engine.config,
        state=engine.state,
        searcher=None,
        engine=engine,
        indexing=Indexing(local=False, start_sync=lambda _: {}),
    )


def _project(engine: SyncEngine):
    return engine.apply_index_task(
        IndexTask(
            id="projection",
            source_id="artifacts",
            mode="incremental",
            payload={"operation": "project_artifacts"},
        )
    )


def test_projection_notification_wakes_queue_without_resync(loaded_config, monkeypatch):
    from types import SimpleNamespace

    from pheasant.services.artifact_dispatch import _notify_projection
    from pheasant.sync import queue

    published = []
    closed = []
    broker = SimpleNamespace(publish=published.append, close=lambda: closed.append(True))
    monkeypatch.setattr(queue, "queue_from_config", lambda *_: broker)
    context = SimpleNamespace(config=loaded_config, state=None)
    _notify_projection(context)
    _notify_projection(context)
    assert len(closed) == 2
    assert published[0].id != published[1].id
    assert published[0].publish_id != published[1].publish_id
    assert all(t.payload == {"operation": "project_artifacts"} for t in published)
    assert all(t.source_id == "artifacts" for t in published)

    def failed(_):
        raise RuntimeError("broker unavailable")

    broker.publish = failed
    with pytest.raises(RuntimeError, match="broker unavailable"):
        _notify_projection(context)
    assert len(closed) == 3


def test_save_is_durable_before_landing_and_pending_delete_cannot_resurrect(
    loaded_config, monkeypatch
):
    engine = SyncEngine(loaded_config, defer_persisted_graph_load=True)
    context = _fleet(engine, monkeypatch)
    try:
        saved = artifacts.save(
            context, artifacts.SaveRequest(title="Pending", markdown="quartzsignal")
        )
        assert artifacts.get(context, saved["id"])["title"] == "Pending"
        assert artifact_outbox.pending(engine.state, loaded_config.knowledge_base_id)
        assert not (engine.paths.state / "uploads" / "artifacts").exists()
        artifacts.delete(context, saved["id"])
        assert (
            artifact_outbox.pending(engine.state, loaded_config.knowledge_base_id)[0]["operation"]
            == "remove"
        )
        monkeypatch.setattr(
            engine.graph_store, "load", lambda *_: pytest.fail("whole graph loaded")
        )
        _project(engine)
        assert not artifact_outbox.pending(engine.state, loaded_config.knowledge_base_id)
        assert not engine.state.rows("SELECT id FROM artifacts WHERE source_id='artifacts'")
        assert not list((engine.paths.state / "uploads" / "artifacts").rglob("README.md"))
    finally:
        engine.close()


def test_projection_failure_keeps_intent_and_retry_indexes_once(loaded_config, monkeypatch):
    from pheasant.ingestion.landing_service import LocalLandingZone

    loaded_config.artifacts.index_on_save = True
    engine = SyncEngine(loaded_config, defer_persisted_graph_load=True)
    context = _fleet(engine, monkeypatch)
    original = LocalLandingZone.write
    try:
        saved = artifacts.save(
            context, artifacts.SaveRequest(title="Retry", markdown="quartzsignal")
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                LocalLandingZone,
                "write",
                lambda *a, **kw: (_ for _ in ()).throw(OSError("disk offline")),
            )
            with pytest.raises(OSError, match="disk offline"):
                _project(engine)
        assert artifact_outbox.pending(engine.state, loaded_config.knowledge_base_id)[0][
            "last_error"
        ]
        assert LocalLandingZone.write is original
        result = _project(engine)
        assert artifacts.get(context, saved["id"])["index_status"] == "indexed", result
        count = len(engine.state.rows("SELECT id FROM artifacts WHERE source_id='artifacts'"))
        _project(engine)
        assert count == 1
        artifacts.delete(context, saved["id"])
        monkeypatch.setattr(
            engine.graph_store, "load", lambda *_: pytest.fail("whole graph loaded")
        )
        _project(engine)
        assert not engine.state.rows("SELECT id FROM artifacts WHERE source_id='artifacts'")
    finally:
        engine.close()


def test_tombstones_exclude_all_retrieval_arms_before_cleanup(loaded_config, monkeypatch):
    from pheasant.search.hybrid import HybridSearch
    from pheasant.search.sqlite_store import SearchStore

    engine = SyncEngine(loaded_config)
    context = ServiceContext(config=engine.config, state=engine.state, searcher=None, engine=engine)
    try:
        saved = artifacts.save(
            context, artifacts.SaveRequest(title="Hidden", markdown="quartzsignal")
        )
        engine.sync_source("artifacts", "incremental")
        fleet = _fleet(engine, monkeypatch)
        artifacts.delete(fleet, saved["id"])
        assert engine.state.rows("SELECT id FROM artifacts WHERE source_id='artifacts'")
        search = HybridSearch(SearchStore(engine.state))
        for mode in ("text", "hybrid", "graph"):
            result = search.search_context(
                engine.config.knowledge_base_id,
                "quartzsignal",
                graph=engine.graph_builder.graph,
                mode=mode,
            )
            assert not result["results"]
    finally:
        engine.close()


def test_writer_reuses_interpreter_and_recovers_from_process_death(loaded_config, tmp_path: Path):
    from pheasant.sync.writer import Session

    config = tmp_path / "persistent.yaml"
    config.write_text(yaml.safe_dump(loaded_config.model_dump(mode="json")), encoding="utf-8")
    session = Session(str(config))
    request = {
        "source": "architecture-notes",
        "mode": "full",
        "full_scan": False,
        "depth": None,
        "payload": None,
    }
    try:
        first = session.run(request, 60, None)
        assert first["status"] == "ok", first
        second = session.run({**request, "mode": "incremental"}, 60, None)
        assert second["status"] == "ok", second
        assert first["writer_pid"] == second["writer_pid"]
        assert second["results"][0]["indexed_artifacts"] == 0
        session.process.kill()
        session.process.wait(timeout=5)
        third = session.run({**request, "mode": "incremental"}, 60, None)
        assert third["status"] == "ok", third
        assert third["writer_pid"] != first["writer_pid"]
        assert third["results"][0]["indexed_artifacts"] == 0
        removed = session.run({**request, "payload": {"operation": "delete_source"}}, 60, None)
        assert removed["status"] == "ok", removed
        assert removed["results"][0]["status"] == "removed"
        assert removed["writer_pid"] == third["writer_pid"]
    finally:
        session.stop()
