"""Crash-safety acceptance tests (Synapse step 21.2).

Covers: WAL + pragmas on every StateStore connection, kill -9 mid-sync
recovery without repair mode, the single-writer engine lease (fail-fast,
heartbeat, stale takeover), the one-shot legacy-manifest migration, and
durable graph saves. Everything is offline and bounded — lease timings are
constructor-injected, never slept out.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pheasant.config.loader import load_config
from pheasant.persistence.state_store import StateStore
from pheasant.sync.engine import SyncEngine
from pheasant.sync.locks import EngineLease, EngineLeaseError, source_lock
from tests.conftest import CONFIG_TEMPLATE

POLL_TIMEOUT_S = 30.0

SYNC_DRIVER = """\
import sys
from pheasant.config.loader import load_config
from pheasant.sync.engine import SyncEngine

engine = SyncEngine(load_config(sys.argv[1]))
engine.sync_source("pheasant-repo", "full")
print("SYNC_COMPLETE", flush=True)
"""


def _render_config(tmp_path: Path, name: str, workspace: Path) -> Path:
    base = tmp_path / name
    for sub in ("state", "vault", "exports"):
        (base / sub).mkdir(parents=True)
    rendered = CONFIG_TEMPLATE.read_text(encoding="utf-8").format(
        workspace_path=workspace.as_posix(),
        state_path=(base / "state").as_posix(),
        exports_path=(base / "exports").as_posix(),
    )
    config_file = base / "pheasant.yaml"
    config_file.write_text(rendered, encoding="utf-8")
    return config_file


def _state_dir(config_file: Path) -> Path:
    return config_file.parent / "state"


def _artifact_count(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(db_path, timeout=1)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return 0


def _manifest_sha_map(engine: SyncEngine, source: str = "pheasant-repo") -> dict[str, str]:
    artifacts = engine.manifests.load(source)["artifacts"]
    return {relative: entry["sha256"] for relative, entry in artifacts.items()}


def _dead_pid() -> int:
    """Spawn and reap a short-lived process so its PID is provably dead."""
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return int(proc.stdout.strip())


def test_state_store_connections_use_wal_and_timeouts(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.migrate()
    try:
        from pheasant.persistence.state_store import BUSY_TIMEOUT_MS

        assert store.rows("PRAGMA journal_mode")[0][0] == "wal"
        # Reads the constant rather than repeating a literal: the value was
        # raised from 5 s after a real index died on "database is locked", and
        # a second copy of the number is how that silently reverts.
        assert int(store.rows("PRAGMA busy_timeout")[0][0]) == BUSY_TIMEOUT_MS
        assert int(store.rows("PRAGMA synchronous")[0][0]) == 1  # NORMAL
    finally:
        store.close()


# Needs SIGKILL and a POSIX process tree to simulate an unclean mid-sync kill.
@pytest.mark.skipif(os.name == "nt", reason="POSIX-only; see comment above.")
def test_kill9_mid_sync_then_restart_recovers_without_repair(
    tmp_path: Path,
    workspace_copy: Path,
) -> None:
    crashed_cfg = _render_config(tmp_path, "crashed", workspace_copy)
    control_cfg = _render_config(tmp_path, "control", workspace_copy)
    db_path = _state_dir(crashed_cfg) / "pheasant.db"

    env = {**os.environ, "PHEASANT_TEST_SLOW_SYNC_MS": "500"}
    proc = subprocess.Popen(
        [sys.executable, "-c", SYNC_DRIVER, str(crashed_cfg)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    killed = False
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        if _artifact_count(db_path) >= 1:
            proc.kill()  # SIGKILL mid-sync, between per-artifact writes
            killed = True
            break
        time.sleep(0.02)
    stdout, stderr = proc.communicate(timeout=30)
    assert killed, f"never observed mid-sync state; driver said: {stdout!r} / {stderr!r}"
    assert proc.returncode == -signal.SIGKILL
    assert "SYNC_COMPLETE" not in stdout, "driver finished before it was killed"

    # Restart on the same state dir: starts cleanly, no corruption, and an
    # incremental resync converges to the uninterrupted-run state.
    engine = SyncEngine(load_config(crashed_cfg))
    try:
        assert engine.state.rows("PRAGMA integrity_check")[0][0] == "ok"
        result = engine.sync_source("pheasant-repo", "incremental")
        assert result.status == "healthy"

        control = SyncEngine(load_config(control_cfg))
        try:
            control.sync_source("pheasant-repo", "full")
            assert _manifest_sha_map(engine) == _manifest_sha_map(control)
            assert engine.stats == control.stats
        finally:
            control.close()
    finally:
        engine.close()


def test_second_engine_process_fails_fast_with_lease_error(
    tmp_path: Path,
    workspace_copy: Path,
) -> None:
    config_file = _render_config(tmp_path, "primary", workspace_copy)
    engine = SyncEngine(load_config(config_file))
    engine.lease.acquire()  # process A holds the lease, heartbeating
    try:
        proc = subprocess.run(
            [sys.executable, "-c", SYNC_DRIVER, str(config_file)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode != 0
        assert "lease" in proc.stderr.lower()
        assert "SYNC_COMPLETE" not in proc.stdout
    finally:
        engine.close()
    assert not (_state_dir(config_file) / "engine.lease").exists()


def test_engine_lease_can_wait_for_current_writer(tmp_path: Path) -> None:
    lease_path = tmp_path / "engine.lease"
    lease_path.write_text(
        json.dumps(
            {
                "pid": os.getpid() + 1,
                "hostname": "another-host",
                "heartbeat_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    contender = EngineLease(
        tmp_path,
        heartbeat_interval_s=0.02,
        stale_after_s=1.0,
        wait_timeout_s=1.0,
        poll_interval_s=0.01,
    )
    timer = threading.Timer(0.05, lease_path.unlink)
    timer.start()
    try:
        contender.acquire()
        assert contender.held
    finally:
        contender.release()
        timer.join()


def test_stale_lease_is_taken_over_with_warning(
    tmp_path: Path,
    workspace_copy: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_file = _render_config(tmp_path, "stale", workspace_copy)
    lease_path = _state_dir(config_file) / "engine.lease"
    lease_path.write_text(
        json.dumps(
            {
                "pid": _dead_pid(),
                "hostname": "somewhere-else",  # forces the heartbeat-age path
                "heartbeat_at": "2020-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    engine = SyncEngine(load_config(config_file))
    try:
        with caplog.at_level(logging.WARNING, logger="pheasant.sync.locks"):
            engine.lease.acquire()
        assert any("stale" in record.message.lower() for record in caplog.records)
        assert json.loads(lease_path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    finally:
        engine.close()


# Lease takeover probes PID liveness with POSIX signal 0. Windows has no
# equivalent that distinguishes "dead" from "not permitted".
@pytest.mark.skipif(os.name == "nt", reason="POSIX-only; see comment above.")
def test_dead_pid_on_this_host_is_taken_over(
    tmp_path: Path,
    workspace_copy: Path,
) -> None:
    config_file = _render_config(tmp_path, "deadpid", workspace_copy)
    lease_path = _state_dir(config_file) / "engine.lease"
    lease_path.write_text(
        json.dumps(
            {
                "pid": _dead_pid(),
                "hostname": socket.gethostname(),
                # Fresh heartbeat: only the dead-PID check can permit takeover.
                "heartbeat_at": "2999-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    engine = SyncEngine(load_config(config_file))
    try:
        engine.lease.acquire()
        assert json.loads(lease_path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    finally:
        engine.close()


def test_lease_heartbeats_and_releases(tmp_path: Path) -> None:
    lease = EngineLease(tmp_path, heartbeat_interval_s=0.05, stale_after_s=1.0)
    lease.acquire()
    first = json.loads(lease.path.read_text(encoding="utf-8"))["heartbeat_at"]
    deadline = time.monotonic() + 5.0
    refreshed = False
    while time.monotonic() < deadline:
        if json.loads(lease.path.read_text(encoding="utf-8"))["heartbeat_at"] != first:
            refreshed = True
            break
        time.sleep(0.01)
    assert refreshed, "heartbeat thread never refreshed the lease"
    lease.release()
    assert not lease.path.exists()

    # A live, fresh lease held by another (simulated) process is respected.
    lease.path.write_text(
        json.dumps(
            {
                "pid": os.getpid() + 1,
                "hostname": "another-host",
                "heartbeat_at": "2999-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    contender = EngineLease(tmp_path, heartbeat_interval_s=0.05, stale_after_s=60.0)
    with pytest.raises(EngineLeaseError, match="lease"):
        contender.acquire()


def test_source_lock_from_a_dead_container_is_not_honoured(tmp_path: Path, caplog) -> None:
    """A lock left by a killed container must not brick the next container.

    The old container recorded ``pid: 1``; in the replacement container PID 1
    is the server itself, so a PID-only liveness check kept every restart
    failing with "Source is already locked". The recorded hostname (the
    container id) is what makes the PID meaningful.
    """

    locks = tmp_path / "locks"
    locks.mkdir()
    (locks / "pheasant-repo.lock").write_text(
        json.dumps(
            {
                "source_id": "pheasant-repo",
                "pid": 1,  # alive in this namespace, but not the same namespace
                "hostname": "a1b2c3d4e5f6",
                "created_at": "2026-05-29T02:41:08+00:00",
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="pheasant.sync.locks"):
        with source_lock(locks, "pheasant-repo"):
            payload = json.loads((locks / "pheasant-repo.lock").read_text(encoding="utf-8"))
            assert payload["pid"] == os.getpid()
            assert payload["hostname"] == socket.gethostname()
    assert any("stale source lock" in record.message.lower() for record in caplog.records)
    assert not (locks / "pheasant-repo.lock").exists()


def test_legacy_source_lock_without_hostname_is_cleared(tmp_path: Path) -> None:
    """Pre-fix locks carry no hostname, so their PID cannot be trusted."""

    locks = tmp_path / "locks"
    locks.mkdir()
    (locks / "notes.lock").write_text(
        json.dumps({"source_id": "notes", "pid": 1, "created_at": "2026-05-29T02:41:08+00:00"}),
        encoding="utf-8",
    )
    with source_lock(locks, "notes"):
        pass
    assert not (locks / "notes.lock").exists()


def test_live_source_lock_on_this_host_is_still_honoured(tmp_path: Path) -> None:
    """The lock must keep doing its job for a real concurrent sync."""

    locks = tmp_path / "locks"
    with source_lock(locks, "pheasant-repo"):
        with pytest.raises(RuntimeError, match="already locked"):
            with source_lock(locks, "pheasant-repo"):
                pass


def test_legacy_manifests_migrate_exactly_once(
    tmp_path: Path,
    workspace_copy: Path,
) -> None:
    config_file = _render_config(tmp_path, "legacy", workspace_copy)
    config = load_config(config_file)
    manifests_dir = _state_dir(config_file) / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    legacy_payload = {
        "source_id": "pheasant-repo",
        "artifacts": {
            "README.md": {
                "sha256": "a" * 64,
                "artifact_id": "file:pheasant-repo:README.md:branch=main",
                "last_indexed_at": "2026-06-01T00:00:00+00:00",
                "git_branch": "main",
                "git_commit": "deadbeef",
            }
        },
        "last_indexed_at": "2026-06-01T00:00:00+00:00",
        "connector": {"type": "filesystem"},
    }
    legacy_file = manifests_dir / "pheasant-repo.manifest.json"
    legacy_file.write_text(json.dumps(legacy_payload, indent=2, sort_keys=True), encoding="utf-8")

    first = SyncEngine(config)
    try:
        # Row present with equivalent content; original renamed, never deleted.
        assert first.manifests.load("pheasant-repo") == legacy_payload
        assert not legacy_file.exists()
        migrated_file = manifests_dir / "pheasant-repo.manifest.json.migrated"
        assert migrated_file.exists()
        assert json.loads(migrated_file.read_text(encoding="utf-8")) == legacy_payload
    finally:
        first.close()

    listing = sorted(path.name for path in manifests_dir.iterdir())
    mtimes = {path.name: path.stat().st_mtime_ns for path in manifests_dir.iterdir()}
    snapshot_store = StateStore(_state_dir(config_file) / "pheasant.db")
    rows = [
        (row["source_name"], row["payload_json"], row["updated_at"])
        for row in snapshot_store.rows(
            "SELECT source_name, payload_json, updated_at FROM manifests ORDER BY source_name"
        )
    ]
    snapshot_store.close()

    second = SyncEngine(config)  # second startup must be a byte-stable no-op
    try:
        assert sorted(path.name for path in manifests_dir.iterdir()) == listing
        assert {path.name: path.stat().st_mtime_ns for path in manifests_dir.iterdir()} == mtimes
        assert [
            (row["source_name"], row["payload_json"], row["updated_at"])
            for row in second.state.rows(
                "SELECT source_name, payload_json, updated_at FROM manifests ORDER BY source_name"
            )
        ] == rows
    finally:
        second.close()


def test_busy_timeout_is_long_enough_for_a_long_sync(tmp_path) -> None:
    """A 5-second budget lost a 12,667-file index to `database is locked`.

    The sync worker writes continuously in one process while the API server
    serves UI polls in another. WAL lets them coexist, but checkpoints and the
    enrichment pass's multi-statement transactions still take the write lock
    for long enough to blow a short budget — so the timeout has to be measured
    in tens of seconds, not units.
    """
    from pheasant.persistence.state_store import BUSY_TIMEOUT_MS, StateStore

    assert BUSY_TIMEOUT_MS >= 30_000, "too short to survive a real indexing run"

    store = StateStore(tmp_path / "state.db")
    try:
        store.migrate()
        applied = store.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert applied == BUSY_TIMEOUT_MS, "the pragma is not reaching the connection"
    finally:
        store.close()


def test_engines_sharing_a_lease_can_heartbeat_at_the_same_moment(tmp_path: Path) -> None:
    """Engines in one process share one lease file, and each heartbeats it.

    Every heartbeat wrote to the same fixed ``engine.lease.tmp`` and renamed it,
    so two heartbeats landing together had one rename take the other's temp
    file: ``FileNotFoundError``, logged from a daemon thread. Harmless to the
    lease -- and on Python 3.12 a daemon thread writing to stderr while the
    interpreter shuts down aborts the whole process, which is how it surfaced:
    a test run whose every test had passed, exiting 134.
    """

    path = tmp_path / "engine.lease"
    first = EngineLease(tmp_path)
    second = EngineLease(tmp_path)
    first.acquire()
    second.acquire()  # same process: ownership is shared, as two engines do
    try:
        start = threading.Barrier(2)
        errors: list[BaseException] = []

        def heartbeat(lease: EngineLease) -> None:
            start.wait()
            for _ in range(300):
                try:
                    lease._write()
                except OSError as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=heartbeat, args=(lease,)) for lease in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert errors == []
        assert json.loads(path.read_text())["pid"] == os.getpid()
        # No temp file is left behind by either writer.
        assert sorted(p.name for p in tmp_path.iterdir()) == ["engine.lease"]
    finally:
        second.release()
        first.release()
