"""Coherent history survives concurrent commits without blocking mutation completion."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from pheasant.persistence.graph_store import GraphStore
from pheasant.persistence.snapshot_stream import SnapshotCancelled, write_snapshot
from pheasant.persistence.state_store import StateStore
from pheasant.sync.engine import SyncEngine


def assert_snapshot_consistency(engine, monkeypatch):
    from pheasant.persistence import snapshot_stream

    kb, rows = engine.config.knowledge_base_id, engine.graph_store.rows
    rows.apply_delta(
        kb,
        node_upserts=[
            ("snapshot-x", {"id": "snapshot-x", "epoch": 1}),
            ("snapshot-y", {"id": "snapshot-y", "epoch": 1}),
        ],
        edge_upserts=[("snapshot-x", "snapshot-y", 0, {"type": "references", "epoch": 1})],
    )
    rows.publish(kb)
    entered, release = threading.Event(), threading.Event()
    original = snapshot_stream._rows

    def paused(*args):
        first = True
        for row in original(*args):
            if first:
                first = False
                entered.set()
                assert release.wait(10)
            yield row

    monkeypatch.setattr(snapshot_stream, "_rows", paused)

    def export():
        state = StateStore.from_config(engine.config, engine.paths.sqlite)
        try:
            store = GraphStore(engine.paths.graphs, state, "rows")
            return write_snapshot(store, kb, "2026-11-12T04:00:00Z", threading.Event())
        finally:
            state.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(export)
        try:
            assert entered.wait(5)
            rows.apply_delta(
                kb,
                node_upserts=[
                    ("snapshot-x", {"id": "snapshot-x", "epoch": 2}),
                    ("snapshot-y", {"id": "snapshot-y", "epoch": 2}),
                ],
                edge_upserts=[("snapshot-x", "snapshot-y", 0, {"type": "references", "epoch": 2})],
            )
            rows.publish(kb)
        finally:
            release.set()
        path = future.result(timeout=10)
    restored = engine.graph_store.read_snapshot(path)
    assert restored.nodes["snapshot-x"]["epoch"] == 1
    assert restored.nodes["snapshot-y"]["epoch"] == 1
    assert {a["epoch"] for a in restored.get_edge_data("snapshot-x", "snapshot-y").values()} == {1}
    assert rows.get_node(kb, "snapshot-x")["epoch"] == 2


def test_streamed_sqlite_snapshot_pins_one_generation(loaded_config, monkeypatch):
    engine = SyncEngine(loaded_config)
    try:
        assert_snapshot_consistency(engine, monkeypatch)
    finally:
        engine.close()


def test_cancelled_stream_preserves_history_and_removes_partial_file(loaded_config):
    engine = SyncEngine(loaded_config)
    cancelled = threading.Event()
    cancelled.set()
    try:
        engine.sync_source("architecture-notes", "full")
        before = engine.graph_store.list_snapshots(loaded_config.knowledge_base_id)
        with pytest.raises(SnapshotCancelled):
            write_snapshot(
                engine.graph_store,
                loaded_config.knowledge_base_id,
                "2026-11-12T05:00:00Z",
                cancelled,
            )
        assert engine.graph_store.list_snapshots(loaded_config.knowledge_base_id) == before
        assert not list(engine.paths.graphs.rglob("*.tmp"))
    finally:
        engine.close()


def test_history_publication_is_single_flight_and_off_the_mutation_path(loaded_config, monkeypatch):
    from pheasant.sync import graph_history, history_publisher

    loaded_config.storage.graph_snapshots = False
    engine = SyncEngine(loaded_config, defer_persisted_graph_load=True)
    engine.sync_source("architecture-notes", "incremental")
    engine.config.storage.graph_snapshots = True
    engine.config.storage.graph_snapshot_interval_seconds = 900
    entered, release = threading.Event(), threading.Event()
    original = history_publisher.write_snapshot
    calls = []

    def paused(*args):
        calls.append("write")
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(history_publisher, "write_snapshot", paused)
    try:
        graph_history.snapshot_after_sync(engine)
        assert entered.wait(5)
        graph_history.snapshot_after_sync(engine)
        # The live writer remains free to publish a different generation.
        engine.graph_store.rows.apply_delta(
            engine.config.knowledge_base_id,
            node_upserts=[("another-commit", {"id": "another-commit", "type": "entity"})],
        )
        engine.graph_store.rows.publish(engine.config.knowledge_base_id)
        release.set()
        engine._history_publisher._thread.join(timeout=5)
        assert not engine._history_publisher._thread.is_alive()
        assert calls == ["write"]
        assert engine.graph_store.list_snapshots(engine.config.knowledge_base_id)
    finally:
        release.set()
        engine.close()
