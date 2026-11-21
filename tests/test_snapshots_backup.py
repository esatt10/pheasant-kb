"""Acceptance tests for Synapse step 21.6 session A.

Compressed timestamped graph snapshots + retention honoring
``max_state_size_gb`` + ``pheasant backup`` / ``restore`` round-trip safety.
Everything here is offline and deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pheasant.config.loader import load_config
from pheasant.config.schema import PheasantConfig
from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.persistence.graph_store import GraphStore
from pheasant.sync.engine import SyncEngine
from tests.conftest import run_sync


def _build_state(config_path: Path) -> PheasantConfig:
    cfg = load_config(config_path)
    engine = SyncEngine(cfg)
    try:
        for source in cfg.sources:
            run_sync(engine, source_name=source.name, mode="full")
    finally:
        engine.close()
    return cfg


# --- GraphStore snapshot unit behavior ---------------------------------------


def _graph(n_nodes: int) -> SimpleMultiDiGraph:
    g = SimpleMultiDiGraph()
    for i in range(n_nodes):
        g.add_node(f"n{i}", id=f"n{i}", type="concept")
    for i in range(n_nodes - 1):
        g.add_edge(f"n{i}", f"n{i + 1}", type="references")
    return g


def test_write_and_read_snapshot_round_trips(tmp_path: Path) -> None:
    store = GraphStore(tmp_path / "graphs")
    graph = _graph(5)
    path = store.write_snapshot("kb", graph, "2026-06-18T01:00:00Z")
    assert path.name == "graph.2026-06-18T01-00-00Z.json.zst"
    assert path.exists()
    restored = store.read_snapshot(path)
    assert restored.number_of_nodes() == graph.number_of_nodes()
    assert restored.number_of_edges() == graph.number_of_edges()


def test_list_snapshots_is_oldest_first(tmp_path: Path) -> None:
    store = GraphStore(tmp_path / "graphs")
    for ts in ("2026-06-18T03:00:00Z", "2026-06-18T01:00:00Z", "2026-06-18T02:00:00Z"):
        store.write_snapshot("kb", _graph(3), ts)
    names = [p.name for p in store.list_snapshots("kb")]
    assert names == [
        "graph.2026-06-18T01-00-00Z.json.zst",
        "graph.2026-06-18T02-00-00Z.json.zst",
        "graph.2026-06-18T03-00-00Z.json.zst",
    ]


def test_retention_evicts_oldest_first_and_respects_cap(tmp_path: Path) -> None:
    store = GraphStore(tmp_path / "graphs")
    snaps = []
    for hour in range(1, 6):
        snaps.append(store.write_snapshot("kb", _graph(50), f"2026-06-18T0{hour}:00:00Z"))
    one_snapshot = snaps[0].stat().st_size
    # All snapshots have identical content -> identical size. Cap at 2.5x one
    # snapshot: retention keeps the newest 2 and evicts the oldest 3.
    cap = int(one_snapshot * 2.5)
    evicted = store.enforce_retention("kb", max_bytes=cap)
    remaining = [p.name for p in store.list_snapshots("kb")]
    total = sum(p.stat().st_size for p in store.list_snapshots("kb"))
    assert total <= cap
    # Oldest three evicted, in oldest-first order; newest two retained.
    assert [p.name for p in evicted] == [
        "graph.2026-06-18T01-00-00Z.json.zst",
        "graph.2026-06-18T02-00-00Z.json.zst",
        "graph.2026-06-18T03-00-00Z.json.zst",
    ]
    assert remaining == [
        "graph.2026-06-18T04-00-00Z.json.zst",
        "graph.2026-06-18T05-00-00Z.json.zst",
    ]


def test_retention_keeps_at_least_one_snapshot(tmp_path: Path) -> None:
    store = GraphStore(tmp_path / "graphs")
    store.write_snapshot("kb", _graph(100), "2026-06-18T01:00:00Z")
    # Cap below a single snapshot — newest is still kept.
    store.enforce_retention("kb", max_bytes=1)
    assert len(store.list_snapshots("kb")) == 1


def test_snapshot_interval_respected(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(config_path)
    cfg.storage.graph_snapshot_interval_seconds = 3600
    engine = SyncEngine(cfg)
    kb = cfg.knowledge_base_id

    # Drive snapshot timestamps deterministically.
    clock = {"now": "2026-06-18T01:00:00Z"}
    monkeypatch.setattr("pheasant.sync.engine.utc_now", lambda: clock["now"])
    # The snapshot names and gates itself by its own clock read.
    monkeypatch.setattr("pheasant.sync.graph_history.utc_now", lambda: clock["now"])

    try:
        run_sync(engine, source_name="architecture-notes", mode="full")
        first = engine.graph_store.list_snapshots(kb)
        assert len(first) == 1

        # Second sync within the interval -> no new snapshot.
        clock["now"] = "2026-06-18T01:30:00Z"
        run_sync(engine, source_name="architecture-notes", mode="incremental")
        assert len(engine.graph_store.list_snapshots(kb)) == 1

        # An unchanged generation is not snapshotted just because the clock
        # moved; immutable-generation history stores changes, not duplicates.
        clock["now"] = "2026-06-18T03:00:00Z"
        run_sync(engine, source_name="architecture-notes", mode="incremental")
        assert len(engine.graph_store.list_snapshots(kb)) == 1

        # A changed generation after the interval does publish new history.
        source = next(item for item in cfg.sources if item.name == "architecture-notes")
        (source.path / "generation.md").write_text("# New generation\n", encoding="utf-8")
        run_sync(engine, source_name="architecture-notes", mode="incremental")
        assert len(engine.graph_store.list_snapshots(kb)) == 2
    finally:
        engine.close()


def test_snapshots_disabled_writes_none(config_path: Path) -> None:
    cfg = load_config(config_path)
    cfg.storage.graph_snapshots = False
    engine = SyncEngine(cfg)
    try:
        run_sync(engine, source_name="architecture-notes", mode="full")
        assert engine.graph_store.list_snapshots(cfg.knowledge_base_id) == []
    finally:
        engine.close()


# --- backup / restore round-trip ---------------------------------------------


def _engine_counts(cfg: PheasantConfig) -> tuple[int, int]:
    engine = SyncEngine(cfg)
    try:
        return (
            engine.graph_builder.graph.number_of_nodes(),
            engine.graph_builder.graph.number_of_edges(),
        )
    finally:
        engine.close()


def _fts_hits(cfg: PheasantConfig, query: str) -> list:
    engine = SyncEngine(cfg)
    try:
        result = engine.search_context(query, max_results=10)
        return list(result.get("results", []))
    finally:
        engine.close()
