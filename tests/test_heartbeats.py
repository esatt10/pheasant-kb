"""A long-running process's heartbeat keeps beating while the work goes on.

On the Azure fleet a 23,691-page web sync committed for another hour after its
``sync_runs`` heartbeat stopped, so ``wait_for_sync`` said ``queued`` about a
sync that was visibly indexing and a cancel would have been refused. The beat
shared the sync's own pooled connections and failed at debug level. These
tests hold the two rules `persistence.heartbeat` makes structural: a heartbeat
writes on a connection of its own, and a failed beat is said and retried.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.persistence.heartbeat import Heartbeat
from pheasant.persistence.state_store import StateStore
from pheasant.sync import cancellation
from pheasant.sync.locks import SourceLease

DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()
postgres = pytest.mark.skipif(not DSN, reason="set PHEASANT_TEST_POSTGRES_DSN to run")


def _state(tmp_path: Path, backend: str = "sqlite", **storage: Any) -> StateStore:
    raw: dict[str, Any] = {
        "pheasant": {"name": "beats", "defaults": "standalone", "state_path": str(tmp_path)},
    }
    if backend == "postgres":
        raw["storage"] = {"backend": "postgres", "dsn_env": "PHEASANT_TEST_POSTGRES_DSN", **storage}
    state = StateStore.from_config(PheasantConfig.model_validate(raw), tmp_path / "state.db")
    state.migrate()
    return state


def _wait_until(condition: Any, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_a_failed_beat_is_said_retried_and_its_recovery_noted(caplog) -> None:
    calls: list[int] = []

    def beat(_writer: Any) -> bool:
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("server closed the connection unexpectedly")
        return len(calls) < 4

    heartbeat = Heartbeat(None, "probe", beat, 0.02)
    with caplog.at_level(logging.WARNING, logger="pheasant.persistence.heartbeat"):
        heartbeat.start()
        assert _wait_until(lambda: not heartbeat.alive, 5), "a beat returning False did not stop it"
        heartbeat.stop()
    assert len(calls) == 4, "a failed beat ended the heartbeat instead of retrying"
    text = caplog.text
    assert "probe heartbeat failed (1 in a row): server closed the connection" in text
    assert "probe heartbeat recovered after 2 failed beats" in text


def test_a_heartbeat_gives_up_when_its_caller_says_the_claim_lapsed() -> None:
    def beat(_writer: Any) -> None:
        raise RuntimeError("down")

    seen: list[int] = []
    heartbeat = Heartbeat(None, "lease", beat, 0.02, on_failure=lambda n: seen.append(n) or n < 3)
    heartbeat.start()
    assert _wait_until(lambda: not heartbeat.alive, 5)
    heartbeat.stop()
    assert seen == [1, 2, 3]


def test_a_sync_run_beats_on_sqlite(tmp_path: Path) -> None:
    state = _state(tmp_path)
    run = cancellation.SyncRun(state, "notes", heartbeat_interval_s=0.05)
    with run:
        first = state.rows("SELECT heartbeat_at FROM sync_runs WHERE source_id='notes'")[0][
            "heartbeat_at"
        ]
        assert _wait_until(
            lambda: (
                state.rows("SELECT heartbeat_at FROM sync_runs WHERE source_id='notes'")[0][
                    "heartbeat_at"
                ]
                != first
            ),
            5,
        )
        assert cancellation.running(state, "notes") is not None


@postgres
def test_a_sync_run_beats_while_its_work_holds_every_pooled_connection(tmp_path: Path) -> None:
    """The Azure failure, made deterministic: a pool of one, held by the work.

    The old heartbeat checked a connection out of the same pool, waited the
    pool's 30 s and failed at debug level, every beat, for as long as the work
    held it. On a connection of its own it lands on time.
    """

    import psycopg

    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute("DROP TABLE IF EXISTS sync_runs")
    state = _state(tmp_path, "postgres", pool_size=1)
    try:
        held = threading.Event()
        done = threading.Event()

        def work() -> None:
            # A transaction left open on this thread's pooled connection: the
            # pool's only one, for as long as the "sync" runs.
            state.backend.execute("SELECT 1")
            held.set()
            done.wait(10)
            state.backend.commit()

        run = cancellation.SyncRun(state, "site", heartbeat_interval_s=0.2)
        with run:
            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            assert held.wait(5)
            with psycopg.connect(DSN, autocommit=True) as reader:

                def beats() -> list[Any]:
                    return reader.execute(
                        "SELECT heartbeat_at FROM sync_runs WHERE source_id='site'"
                    ).fetchall()

                first = beats()[0][0]
                assert _wait_until(lambda: beats()[0][0] != first, 5), (
                    "the heartbeat waited on the pool the work was holding"
                )
            done.set()
            worker.join(5)
    finally:
        state.close()


@postgres
def test_a_source_lease_holds_while_its_work_holds_every_pooled_connection(
    tmp_path: Path,
) -> None:
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute("DROP TABLE IF EXISTS source_leases")
    state = _state(tmp_path, "postgres", pool_size=1)
    try:
        lease = SourceLease(state, "site", heartbeat_interval_s=0.1, stale_after_s=60)
        assert lease.try_acquire()
        state.backend.execute("SELECT 1")  # the work's open transaction holds the pool
        with psycopg.connect(DSN, autocommit=True) as reader:

            def beat() -> Any:
                return reader.execute(
                    "SELECT heartbeat_at FROM source_leases WHERE source_id='site'"
                ).fetchall()[0][0]

            first = beat()
            assert _wait_until(lambda: beat() != first, 5), (
                "the lease's heartbeat waited on the pool the work was holding"
            )
        assert lease.held, "the lease was relinquished while its writer was alive"
        state.backend.commit()
        lease.release()
    finally:
        state.close()
