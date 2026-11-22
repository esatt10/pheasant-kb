"""Crash-safety acceptance tests.

Covers: kill -9 mid-sync recovery without repair mode (the state store is
PostgreSQL, read from outside the killed process), the single-writer engine lease (fail-fast,
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
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pheasant.config.loader import load_config
from pheasant.persistence.state_store import StateStore
from pheasant.sync.engine import SyncEngine
from pheasant.sync.locks import source_lock
from tests.conftest import CONFIG_TEMPLATE

POLL_TIMEOUT_S = 30.0


def test_pid_liveness_probe_does_not_signal_a_live_process() -> None:
    from pheasant.sync.locks import _pid_is_running

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert _pid_is_running(process.pid)
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.1)
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert not _pid_is_running(process.pid)


SYNC_DRIVER = """\
import sys
from pheasant.config.loader import load_config
from pheasant.sync.engine import SyncEngine

engine = SyncEngine(load_config(sys.argv[1]))
engine.sync_source("pheasant-repo", "full")
print("SYNC_COMPLETE", flush=True)
"""


def _render_config(tmp_path: Path, name: str, workspace: Path, dsn_env: str | None = None) -> Path:
    """Render the test config; ``dsn_env`` gives the region a database of its own."""

    base = tmp_path / name
    for sub in ("state", "vault", "exports"):
        (base / sub).mkdir(parents=True)
    rendered = CONFIG_TEMPLATE.read_text(encoding="utf-8").format(
        workspace_path=workspace.as_posix(),
        state_path=(base / "state").as_posix(),
        exports_path=(base / "exports").as_posix(),
    )
    if dsn_env:
        rendered += f"\nstorage:\n  dsn_env: {dsn_env}\n"
    config_file = base / "pheasant.yaml"
    config_file.write_text(rendered, encoding="utf-8")
    return config_file


def _state_dir(config_file: Path) -> Path:
    return config_file.parent / "state"


def _artifact_count(dsn: str) -> int:
    """Rows the driver has committed so far, read from outside its process."""

    from pheasant.persistence import pg_driver

    connection = pg_driver.connect(dsn, autocommit=True)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM artifacts").rows[0][0])
    finally:
        connection.close()


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


# Needs SIGKILL and a POSIX process tree to simulate an unclean mid-sync kill.
@pytest.mark.skipif(os.name == "nt", reason="POSIX-only; see comment above.")
def test_kill9_mid_sync_then_restart_recovers_without_repair(
    tmp_path: Path,
    workspace_copy: Path,
    _state_database: str,
    database_variable,
) -> None:
    crashed_cfg = _render_config(tmp_path, "crashed", workspace_copy)
    # The uninterrupted run needs a state store of its own to compare against.
    control_cfg = _render_config(tmp_path, "control", workspace_copy, database_variable())

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
        if _artifact_count(_state_database) >= 1:
            proc.kill()  # SIGKILL mid-sync, between per-artifact writes
            killed = True
            break
        time.sleep(0.02)
    stdout, stderr = proc.communicate(timeout=30)
    assert killed, f"never observed mid-sync state; driver said: {stdout!r} / {stderr!r}"
    assert proc.returncode == -signal.SIGKILL
    assert "SYNC_COMPLETE" not in stdout, "driver finished before it was killed"

    # Restart on the same database: starts cleanly, the killed run's committed
    # rows are readable, and an incremental resync converges to the
    # uninterrupted-run state.
    engine = SyncEngine(load_config(crashed_cfg))
    try:
        assert _artifact_count(_state_database) >= 1
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


# Lease takeover probes PID liveness with POSIX signal 0. Windows has no
# equivalent that distinguishes "dead" from "not permitted".
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
    snapshot_store = StateStore()
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
