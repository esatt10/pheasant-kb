"""Acceptance tests for Synapse step 21.6 session A.

Compressed timestamped graph snapshots + retention honoring
``max_state_size_gb`` + ``pheasant backup`` / ``restore`` round-trip safety.
Everything here is offline and deterministic.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pheasant.config.loader import load_config
from pheasant.config.schema import PheasantConfig
from pheasant.graph.simple import SimpleMultiDiGraph
from pheasant.persistence.backup import create_backup, restore_backup
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


def test_current_generation_snapshot_copies_published_bytes(tmp_path: Path) -> None:
    store = GraphStore(tmp_path / "graphs")
    graph = _graph(6)
    current = store.save("kb", graph)

    snapshot = store.snapshot_current("kb", "2026-06-18T01:00:00Z")

    assert snapshot.read_bytes() == current.read_bytes()
    restored = store.read_snapshot(snapshot)
    assert (restored.number_of_nodes(), restored.number_of_edges()) == (6, 5)


def test_save_publishes_small_generation_metadata_for_count_only_readers(
    tmp_path: Path,
) -> None:
    store = GraphStore(tmp_path / "graphs")
    graph = _graph(7)

    store.save("kb", graph)

    assert store.metadata_path("kb").exists()
    assert store.counts("kb") == (7, 6)


def test_counting_a_legacy_graph_migrates_it_to_metadata_only_reads(tmp_path: Path) -> None:
    store = GraphStore(tmp_path / "graphs")
    store.save("kb", _graph(4))
    store.metadata_path("kb").unlink()

    assert store.counts("kb") == (4, 3)
    assert store.metadata_path("kb").exists()


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


def test_retention_never_touches_latest_db_or_contract(tmp_path: Path) -> None:
    graphs = tmp_path / "graphs"
    store = GraphStore(graphs)
    kb = "kb"
    store.save(kb, _graph(10))
    for hour in range(1, 4):
        store.write_snapshot(kb, _graph(50), f"2026-06-18T0{hour}:00:00Z")
    latest = store.graph_path(kb)
    assert latest.exists()
    store.enforce_retention(kb, max_bytes=1)
    # latest.json must survive any retention pass.
    assert latest.exists()


# --- engine integration: interval throttling ---------------------------------


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


def test_backup_restore_round_trip(config_path: Path, tmp_path: Path) -> None:
    cfg = _build_state(config_path)
    original_nodes, original_edges = _engine_counts(cfg)
    assert original_nodes > 0 and original_edges > 0

    query = "architecture"
    original_hits = _fts_hits(cfg, query)
    assert original_hits, "expected the fixture workspace to answer an FTS query"

    archive = tmp_path / "backup.tar.zst"
    create_backup(cfg.state_path, archive, sqlite_path=cfg.storage.sqlite_path)
    assert archive.exists()
    # Original state dir must be untouched by backup.
    assert (cfg.state_path / "pheasant.db").exists()

    # Restore into a fresh state dir and rebind config to it.
    fresh_state = tmp_path / "restored-state"
    restore_backup(archive, fresh_state)

    restored_cfg = load_config(config_path)
    restored_cfg.pheasant.state_path = fresh_state
    restored_cfg.storage.sqlite_path = fresh_state / "pheasant.db"
    restored_cfg.storage.graph_path = fresh_state / "graphs"
    restored_cfg.storage.manifest_path = fresh_state / "manifests"

    restored_nodes, restored_edges = _engine_counts(restored_cfg)
    assert (restored_nodes, restored_edges) == (original_nodes, original_edges)

    restored_hits = _fts_hits(restored_cfg, query)
    assert [h.get("chunk_id") for h in restored_hits] == [h.get("chunk_id") for h in original_hits]

    # integrity_check on the restored db.
    conn = sqlite3.connect(fresh_state / "pheasant.db")
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    assert row[0] == "ok"


def test_restore_refuses_nonempty_without_force(config_path: Path, tmp_path: Path) -> None:
    cfg = _build_state(config_path)
    archive = tmp_path / "backup.tar.zst"
    create_backup(cfg.state_path, archive, sqlite_path=cfg.storage.sqlite_path)

    target = tmp_path / "occupied-state"
    target.mkdir()
    sentinel = target / "do-not-touch.txt"
    sentinel.write_text("precious user data", encoding="utf-8")

    with pytest.raises(FileExistsError):
        restore_backup(archive, target)
    # Target left completely untouched.
    assert sentinel.read_text(encoding="utf-8") == "precious user data"
    assert list(target.iterdir()) == [sentinel]


def test_restore_force_swaps_cleanly_and_preserves_original(
    config_path: Path, tmp_path: Path
) -> None:
    cfg = _build_state(config_path)
    archive = tmp_path / "backup.tar.zst"
    create_backup(cfg.state_path, archive, sqlite_path=cfg.storage.sqlite_path)

    target = tmp_path / "occupied-state"
    target.mkdir()
    (target / "old-marker.txt").write_text("old", encoding="utf-8")

    restore_backup(archive, target, force=True, now="2026-06-18T05:00:00Z")
    assert (target / "pheasant.db").exists()
    assert not (target / "old-marker.txt").exists()
    # The old directory is preserved aside, never deleted.
    preserved = list(tmp_path.glob("occupied-state.replaced-*"))
    assert preserved, "expected the displaced state dir to be preserved"
    assert (preserved[0] / "old-marker.txt").read_text(encoding="utf-8") == "old"
