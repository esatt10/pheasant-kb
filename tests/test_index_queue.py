"""Phase 35.5d — the durable index work queue.

The claim being tested is narrow and checkable: with the queue off,
``sync_all`` does exactly what it always did; with it on, a backlog survives
the process that created it. Everything else — dead-lettering, visibility
timeouts, concurrent claims — exists to make that first claim safe.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.persistence.paths import StatePaths
from pheasant.persistence.state_store import StateStore
from pheasant.sync.engine import FULL_RESUME_SCOPE, SyncEngine
from pheasant.sync.queue import (
    DEAD,
    DONE,
    INFLIGHT,
    PENDING,
    IndexTask,
    LocalQueue,
    _heartbeat_interval,
    drain,
    owner_id,
    queue_from_config,
)


def test_queue_heartbeat_precedes_jetstream_default_ack_wait() -> None:
    assert _heartbeat_interval(900.0) == 10.0
    assert _heartbeat_interval(3.0) == 1.0


def _write_docs(workspace: Path, prefix: str, count: int = 2) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (workspace / f"{prefix}-{index}.md").write_text(
            f"# {prefix} {index}\n\nThe deployment gateway rotates credentials nightly.\n",
            encoding="utf-8",
        )


def _config(
    tmp_path: Path,
    *,
    state_name: str = "state",
    sources: int = 3,
    queue: dict[str, Any] | None = None,
    source_workers: int = 1,
) -> PheasantConfig:
    workspace = tmp_path / f"workspace-{state_name}"
    entries = []
    for index in range(sources):
        folder = workspace / f"src{index}"
        _write_docs(folder, f"note{index}")
        entries.append(
            {
                "name": f"src{index}",
                "type": "markdown_folder",
                "path": str(folder),
                "include": ["**/*.md"],
            }
        )
    payload: dict[str, Any] = {
        "pheasant": {
            "name": "queue-acceptance",
            "state_path": str(tmp_path / state_name),
            "workspace_root": str(workspace),
            "exports_path": str(tmp_path / f"{state_name}-exports"),
        },
        "storage": {"graph_snapshots": False},
        "sync": {
            "concurrency": {
                "lock_timeout_seconds": 5,
                "max_parallel_sources": source_workers,
            }
        },
        "sources": entries,
    }
    if queue is not None:
        payload["sync"]["queue"] = queue
    return PheasantConfig.model_validate(payload)


def _store(config: PheasantConfig) -> StateStore:
    paths = StatePaths.from_config(config)
    paths.ensure()
    store = StateStore(paths.sqlite)
    store.migrate()
    return store


def _task(source_id: str = "src0", **kwargs: Any) -> IndexTask:
    return IndexTask(id=kwargs.pop("id", f"t-{source_id}"), source_id=source_id, **kwargs)


# --------------------------------------------------------------------------
# Rule 7: off by default, and off means unchanged
# --------------------------------------------------------------------------


def test_the_queue_is_off_by_default(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.sync.queue.enabled is False
    store = _store(config)
    try:
        assert queue_from_config(config, store) is None
    finally:
        store.close()


def test_sync_all_is_unchanged_with_the_queue_off(tmp_path: Path) -> None:
    """Same results, and not one row written to the queue table."""

    config = _config(tmp_path, state_name="off")
    engine = SyncEngine(config)
    try:
        results = engine.sync_all("full")
        queued = engine.state.rows("SELECT COUNT(*) AS c FROM index_tasks", ())
    finally:
        engine.close()

    assert [result.source_id for result in results] == ["src0", "src1", "src2"]
    assert all(result.indexed_artifacts == 2 for result in results)
    assert int(queued[0]["c"]) == 0


def test_queued_sync_all_matches_unqueued_sync_all(tmp_path: Path) -> None:
    """The queue is plumbing: it must not change what an index contains."""

    def artifacts(engine: SyncEngine) -> list[tuple[Any, ...]]:
        return sorted(
            (row["source_id"], row["relative_path"], row["sha256"])
            for row in engine.state.rows("SELECT source_id, relative_path, sha256 FROM artifacts")
        )

    plain = SyncEngine(_config(tmp_path, state_name="plain"))
    try:
        plain_results = plain.sync_all("full")
        plain_artifacts = artifacts(plain)
    finally:
        plain.close()

    queued = SyncEngine(_config(tmp_path, state_name="queued", queue={"enabled": True}))
    try:
        queued_results = queued.sync_all("full")
        queued_artifacts = artifacts(queued)
    finally:
        queued.close()

    assert [r.source_id for r in queued_results] == [r.source_id for r in plain_results]
    assert [r.indexed_artifacts for r in queued_results] == [
        r.indexed_artifacts for r in plain_results
    ]
    assert [path for _source, path, _sha in queued_artifacts] == [
        path for _source, path, _sha in plain_artifacts
    ]
    assert [sha for _source, _path, sha in queued_artifacts] == [
        sha for _source, _path, sha in plain_artifacts
    ]


def test_queued_sync_all_preserves_configured_source_order(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="order", sources=4, queue={"enabled": True})
    engine = SyncEngine(config)
    try:
        results = engine.sync_all("full")
    finally:
        engine.close()
    assert [result.source_id for result in results] == ["src0", "src1", "src2", "src3"]


def test_control_tasks_use_the_same_idempotent_single_writer_path(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="control", sources=1, queue={"enabled": True})
    engine = SyncEngine(config)
    try:
        engine.sync_source("src0", "full")
        task = _task(
            "src0",
            id="delete-src0",
            payload={"operation": "delete_source"},
        )

        first = engine.apply_index_task(task)
        second = engine.apply_index_task(task)

        assert first.status == second.status == "removed"
        assert engine.state.get_source("src0") is None
        assert not any(
            attrs.get("source_id") == "src0"
            for _node_id, attrs in engine.graph_builder.graph.iter_nodes()
        )
    finally:
        engine.close()


def test_redelivered_full_task_resumes_from_its_published_checkpoint(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="resume-full", sources=1, queue={"enabled": True})
    engine = SyncEngine(config)
    try:
        engine.sync_source("src0", "full")
        (Path(config.sources[0].path) / "new.md").write_text(
            "# New\n\nOnly this delta remains after the checkpoint.\n",
            encoding="utf-8",
        )
        engine.state.set_fingerprint(
            FULL_RESUME_SCOPE.format(name="src0"),
            "checkpointed",
            "2026-08-25T00:00:00Z",
        )
        task = _task("src0", id="retry-full", mode="full", attempts=2)

        result = engine.apply_index_task(task)

        assert result.indexed_artifacts == 1
        assert result.details["resumed_interrupted_full"] is True
        assert engine.state.get_fingerprint(FULL_RESUME_SCOPE.format(name="src0")) is None
    finally:
        engine.close()


def test_queued_sync_all_works_with_several_source_workers(tmp_path: Path) -> None:
    config = _config(
        tmp_path, state_name="parallel", sources=4, queue={"enabled": True}, source_workers=3
    )
    engine = SyncEngine(config)
    try:
        results = engine.sync_all("full")
        depth = LocalQueue(engine.state).depth()
    finally:
        engine.close()

    assert [result.source_id for result in results] == ["src0", "src1", "src2", "src3"]
    assert all(result.indexed_artifacts == 2 for result in results)
    # Every task ran exactly once: four drain loops sharing one queue must not
    # double-claim, which is what the visibility timeout is for.
    assert depth[DONE] == 4 and depth[PENDING] == 0 and depth[INFLIGHT] == 0


# --------------------------------------------------------------------------
# The reason the queue exists: a backlog that survives the process
# --------------------------------------------------------------------------


def test_a_backlog_survives_the_process_that_created_it(tmp_path: Path) -> None:
    """The failure the in-memory list cannot survive, reproduced.

    A process is killed partway through ``sync_all``. With sources held in a
    Python list the remaining ones are simply gone; here they are rows, so a
    fresh engine over the same ``/state`` finishes the job.
    """

    config = _config(tmp_path, state_name="resume", sources=4, queue={"enabled": True})

    class Crash(RuntimeError):
        pass

    engine = SyncEngine(config)
    indexed: list[str] = []
    real_sync_source = engine.sync_source

    def crashing(name: str, *args: Any, **kwargs: Any) -> Any:
        if len(indexed) >= 2:
            raise Crash("process died")
        indexed.append(name)
        return real_sync_source(name, *args, **kwargs)

    engine.sync_source = crashing  # type: ignore[method-assign]
    try:
        with pytest.raises(Crash):
            # drain() nacks a failing task and moves on, so force the crash
            # out through the publish/claim loop the way a SIGKILL would.
            queue = LocalQueue(engine.state)
            for source in config.sources:
                queue.publish(
                    IndexTask(id=f"idx-{source.name}", source_id=source.name, mode="full")
                )
            for _ in range(2):
                task = queue.claim(owner_id())
                assert task is not None
                crashing(task.source_id, "full")
                queue.ack(task)
            task = queue.claim(owner_id())
            assert task is not None
            crashing(task.source_id, "full")
    finally:
        engine.close()

    # A new process over the same state directory. Nothing was handed to it.
    resumed = SyncEngine(config)
    try:
        queue = LocalQueue(resumed.state)
        # The claimed-but-unfinished task is invisible until its visibility
        # window lapses; the rest are immediately claimable.
        results = drain(queue, lambda task: resumed.sync_source(task.source_id, "full"))
        depth = queue.depth()
    finally:
        resumed.close()

    assert {result.source_id for result in results} == {"src3"}
    assert depth[PENDING] == 0
    assert depth[INFLIGHT] == 1, "the crashed task is still claimed until it times out"


def test_a_task_claimed_by_a_dead_worker_is_redelivered(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="redeliver", sources=1)
    store = _store(config)
    try:
        queue = LocalQueue(store)
        queue.publish(_task())

        first = queue.claim("worker-a", visibility_seconds=0.0)
        assert first is not None
        # worker-a dies here: no ack, no heartbeat.
        second = queue.claim("worker-b", visibility_seconds=60.0)
        assert second is not None and second.id == first.id
        assert second.attempts == 2

        third = queue.claim("worker-c", visibility_seconds=60.0)
        assert third is None, "a live claim was stolen"
    finally:
        store.close()


def test_a_heartbeat_keeps_a_slow_task_from_being_stolen(tmp_path: Path) -> None:
    """The visibility timeout bounds silence, not work."""

    config = _config(tmp_path, state_name="heartbeat", sources=1)
    store = _store(config)
    try:
        queue = LocalQueue(store)
        queue.publish(_task())
        claimed = queue.claim("slow-worker", visibility_seconds=0.0)
        assert claimed is not None

        queue.heartbeat(claimed, visibility_seconds=300.0)
        assert queue.claim("other", visibility_seconds=60.0) is None
    finally:
        store.close()


def test_concurrent_claims_never_hand_out_the_same_task(tmp_path: Path) -> None:
    """The database arbitrates, not a read-then-write in Python."""

    config = _config(tmp_path, state_name="race", sources=1)
    store = _store(config)
    try:
        queue = LocalQueue(store)
        for index in range(12):
            queue.publish(_task(id=f"race-{index}"))

        claimed: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def worker(name: str) -> None:
            barrier.wait(timeout=10)
            while True:
                task = queue.claim(name, visibility_seconds=300.0)
                if task is None:
                    return
                with lock:
                    claimed.append(task.id)

        threads = [threading.Thread(target=worker, args=(f"w{index}",)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert sorted(claimed) == sorted(f"race-{index}" for index in range(12))
        assert len(claimed) == len(set(claimed)), "a task was claimed twice"
    finally:
        store.close()


# --------------------------------------------------------------------------
# Dead-lettering
# --------------------------------------------------------------------------


def test_a_failing_source_is_dead_lettered_not_retried_forever(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="dead", sources=1)
    store = _store(config)
    try:
        queue = LocalQueue(store)
        queue.publish(_task(max_attempts=2))

        attempts = 0

        def always_fails(task: IndexTask) -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("cannot index this")

        for _ in range(5):
            drain(queue, always_fails, visibility_seconds=300.0)
            # nack schedules a retry in the future; make it visible again.
            store.execute(
                "UPDATE index_tasks SET visible_at=? WHERE status=?",
                (datetime.now(UTC).isoformat(), PENDING),
            )

        assert attempts == 2, "a dead-lettered task kept consuming the fleet"
        assert queue.depth()[DEAD] == 1
        assert queue.claim("anyone") is None
    finally:
        store.close()


def test_a_broker_that_refuses_a_task_is_reported_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed publish used to be logged at debug as "already queued".

    No backend raises for a duplicate, so the exception was always the broker
    refusing the task -- found with NATS's storage deleted under it, where the
    sync reported success having indexed nothing. The refused source is now a
    result with its own status and an error log; the others still run.
    """

    original = LocalQueue.publish

    def refuse_src1(self: LocalQueue, task: IndexTask) -> IndexTask:
        if task.source_id == "src1":
            raise ConnectionError("stream storage unavailable")
        return original(self, task)

    monkeypatch.setattr(LocalQueue, "publish", refuse_src1)
    config = _config(tmp_path, state_name="refused", sources=3, queue={"enabled": True})
    engine = SyncEngine(config)
    try:
        with caplog.at_level("INFO", logger="pheasant.sync.engine"):
            results = engine.sync_all("full")
        status = engine.state.get_source("src1")
    finally:
        engine.close()

    assert [r.source_id for r in results] == ["src0", "src1", "src2"]
    refused = results[1]
    assert refused.status == "queue_unavailable"
    assert "stream storage unavailable" in refused.details["error"]
    assert [r.indexed_artifacts > 0 for r in (results[0], results[2])] == [True, True]
    assert status["last_status"] == "queue_unavailable"
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("src1" in r.getMessage() for r in errors)
    assert any(
        "Queued 2 of 3" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records
    )


def test_one_failing_source_does_not_stop_the_others(tmp_path: Path) -> None:
    """Exactly what the in-memory list could not do."""

    config = _config(tmp_path, state_name="partial", sources=1)
    store = _store(config)
    try:
        queue = LocalQueue(store)
        for name in ("good-a", "bad", "good-b"):
            queue.publish(_task(id=name, source_id=name))

        done: list[str] = []

        def handler(task: IndexTask) -> str:
            if task.source_id == "bad":
                raise RuntimeError("boom")
            done.append(task.source_id)
            return task.source_id

        drain(queue, handler, visibility_seconds=300.0)

        assert sorted(done) == ["good-a", "good-b"]
        assert queue.depth()[DONE] == 2
    finally:
        store.close()


def test_dead_tasks_are_kept_and_can_be_replayed(tmp_path: Path) -> None:
    """A dead letter is evidence, so it is never deleted."""

    config = _config(tmp_path, state_name="replay", sources=1)
    store = _store(config)
    try:
        queue = LocalQueue(store)
        queue.publish(_task(max_attempts=1))
        drain(queue, lambda task: (_ for _ in ()).throw(RuntimeError("nope")))
        assert queue.depth()[DEAD] == 1

        rows = store.rows("SELECT last_error FROM index_tasks WHERE status=?", (DEAD,))
        assert "nope" in str(rows[0]["last_error"])

        assert queue.requeue_dead() == 1
        assert queue.depth()[PENDING] == 1
        replayed = queue.claim("worker")
        assert replayed is not None and replayed.attempts == 1
    finally:
        store.close()


# --------------------------------------------------------------------------
# Housekeeping and shape
# --------------------------------------------------------------------------


def test_publishing_the_same_source_twice_does_not_duplicate_the_backlog(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, state_name="dedup", sources=2, queue={"enabled": True})
    engine = SyncEngine(config)
    try:
        engine.sync_all("full")
        engine.sync_all("full")
        rows = engine.state.rows("SELECT COUNT(*) AS c FROM index_tasks", ())
    finally:
        engine.close()
    # One row per (knowledge base, source, mode) — the id is derived from
    # exactly those, so a re-run replays rather than piling up.
    assert int(rows[0]["c"]) == 2


def test_a_long_running_handler_keeps_its_claim(tmp_path: Path) -> None:
    """`heartbeat` shipped on every backend with no caller.

    Without one, a task is claimed for exactly `visibility_seconds` however
    long the work takes — so a multi-minute source index gets redelivered to a
    second worker while the first is still running it. That is the dead-worker
    recovery path firing on a healthy worker.
    """

    config = _config(tmp_path, state_name="heartbeat", sources=1)
    store = _store(config)
    try:
        queue = LocalQueue(store)
        queue.publish(_task(id="slow"))

        def visible_at() -> str:
            return str(store.rows("SELECT visible_at FROM index_tasks WHERE id=?", ("slow",))[0][0])

        extended: list[bool] = []
        stolen: list[Any] = []

        def handler(task: Any) -> None:
            # Everything is asserted from *inside* the handler: once it
            # returns, `ack` and `nack` both move `visible_at` themselves, so
            # an assertion made afterwards passes whether or not a heartbeat
            # ever ran. (It did, on the first draft of this test.)
            first = visible_at()
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and visible_at() == first:
                time.sleep(0.05)
            extended.append(visible_at() > first)
            stolen.append(LocalQueue(store).claim("other-worker", visibility_seconds=3.0))

        drain(queue, handler, visibility_seconds=3.0)

        assert extended == [True], "the claim was never extended while the handler ran"
        assert stolen == [None], "a second worker stole a task that was still being worked"
    finally:
        store.close()


def test_completed_tasks_are_purged_but_failures_are_kept(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="purge", sources=1)
    store = _store(config)
    try:
        queue = LocalQueue(store)
        queue.publish(_task(id="old-done"))
        queue.publish(_task(id="dead-one", max_attempts=1))
        drain(queue, lambda task: None if task.id == "old-done" else 1 / 0)

        store.execute(
            "UPDATE index_tasks SET updated_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(days=3)).isoformat(), "old-done"),
        )
        assert queue.purge_completed(older_than_seconds=86_400) == 1
        assert queue.depth()[DEAD] == 1
    finally:
        store.close()


def test_the_queue_table_is_created_idempotently(tmp_path: Path) -> None:
    """Rule 2: /state is user data, so the table arrives without a migration."""

    config = _config(tmp_path, state_name="idempotent", sources=1)
    store = _store(config)
    try:
        queue = LocalQueue(store)
        queue.publish(_task())
    finally:
        store.close()

    reopened = _store(config)
    try:
        assert LocalQueue(reopened).depth()[PENDING] == 1
    finally:
        reopened.close()


def test_an_unknown_backend_falls_back_to_the_local_queue(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="unknown", queue={"enabled": True, "backend": "kafka"})
    store = _store(config)
    try:
        assert isinstance(queue_from_config(config, store), LocalQueue)
    finally:
        store.close()


def test_selecting_nats_without_the_extra_raises(tmp_path: Path, monkeypatch: Any) -> None:
    """Loudly, not by silently using a different backend.

    An operator who configured a broker and got a per-container SQLite table
    would have a fleet that looks coordinated and is not.
    """

    import builtins

    from pheasant.sync.queue import QueueUnavailable

    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "nats":
            raise ImportError("simulated: no [queue] extra")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    config = _config(tmp_path, state_name="nonats", queue={"enabled": True, "backend": "nats"})
    store = _store(config)
    try:
        with pytest.raises(QueueUnavailable, match=r"\[queue\] extra"):
            queue_from_config(config, store)
    finally:
        store.close()


def test_task_payload_round_trips_through_the_row(tmp_path: Path) -> None:
    config = _config(tmp_path, state_name="payload", sources=1)
    store = _store(config)
    try:
        queue = LocalQueue(store)
        queue.publish(_task(payload={"max_depth": 3, "full_scan": True}))
        claimed = queue.claim("worker")
        assert claimed is not None
        assert claimed.max_depth == 3
        assert claimed.full_scan is True
    finally:
        store.close()


def test_owner_ids_are_unique_per_claimant() -> None:
    """A recycled pid must not be mistaken for the process that died."""

    assert owner_id() != owner_id()


# --------------------------------------------------------------------------
# Postgres: where concurrent claiming is actually contended
# --------------------------------------------------------------------------

POSTGRES_DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()

postgres = pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="set PHEASANT_TEST_POSTGRES_DSN to a throwaway database to run the queue race test",
)


@postgres
def test_the_claim_statement_is_race_free_on_postgres(tmp_path: Path) -> None:
    """The one test that can distinguish the outer ``status`` guard.

    On SQLite this cannot fail: one writer at a time, so two claimants are
    serialized by the file lock and the guard is redundant. Postgres runs
    them concurrently, and under READ COMMITTED the loser's UPDATE
    re-evaluates its WHERE after the winner commits — which is exactly what
    the ``AND status IN (...)`` on the outer clause is for. Without it both
    transactions update the row the subquery picked and the same source is
    indexed twice, concurrently, by two workers.

    Mutation testing is what surfaced this: dropping the guard changed
    nothing in the SQLite suite.
    """

    pytest.importorskip("psycopg", reason="the [postgres] extra is optional")

    from pheasant.persistence.backends import PostgresBackend

    stores = []

    def new_store() -> StateStore:
        store = StateStore(backend=PostgresBackend(POSTGRES_DSN, pool_size=4))
        stores.append(store)
        return store

    seed = new_store()
    seed.migrate()
    seed.execute("DELETE FROM index_tasks", ())
    queue = LocalQueue(seed)
    for index in range(20):
        queue.publish(_task(f"src{index}", id=f"pg-race-{index}"))

    claimed: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(5)

    def worker(name: str) -> None:
        # A connection per thread, so these are genuinely concurrent
        # transactions and not one connection taking turns.
        own = LocalQueue(new_store())
        barrier.wait(timeout=15)
        while True:
            task = own.claim(name, visibility_seconds=300.0)
            if task is None:
                return
            with lock:
                claimed.append(task.id)

    threads = [threading.Thread(target=worker, args=(f"pg-w{index}",)) for index in range(5)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert sorted(claimed) == sorted(f"pg-race-{index}" for index in range(20))
        assert len(claimed) == len(set(claimed)), "two Postgres workers claimed the same task"
    finally:
        for store in stores:
            store.close()


# --------------------------------------------------------------------------
# The surfaces: metrics and the CLI
# --------------------------------------------------------------------------


def test_queue_depth_reaches_the_metrics_endpoint(tmp_path: Path) -> None:
    """The autoscaling signal, which is most of why the queue is durable.

    The in-memory job registry only knows this process's work, so a restarted
    fleet with rows waiting would scrape zero. The queue's own depth wins.
    """

    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    config = _config(tmp_path, state_name="metrics", sources=2, queue={"enabled": True})
    client = TestClient(create_app(config))

    store = _store(config)
    try:
        queue = LocalQueue(store)
        queue.publish(_task("src0", id="m1"))
        queue.publish(_task("src1", id="m2"))
        claimed = queue.claim("worker", visibility_seconds=300.0)
        assert claimed is not None
    finally:
        store.close()

    body = client.get("/metrics").text
    assert "pheasant_index_queue_depth 1" in body
    assert "pheasant_index_inflight 1" in body
    assert "pheasant_index_dead_letters 0" in body


def test_the_metrics_endpoint_ignores_the_queue_when_it_is_off(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    config = _config(tmp_path, state_name="metrics-off", sources=1)
    client = TestClient(create_app(config))
    body = client.get("/metrics").text
    assert "pheasant_index_queue_depth 0" in body


def test_the_queue_cli_reports_and_replays(tmp_path: Path, capsys: Any) -> None:
    from pheasant.cli import main

    config = _config(tmp_path, state_name="cli", sources=1, queue={"enabled": True})
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        "pheasant:\n"
        f"  name: {config.pheasant.name}\n"
        f"  state_path: {config.pheasant.state_path}\n"
        f"  workspace_root: {config.pheasant.workspace_root}\n"
        f"  exports_path: {config.pheasant.exports_path}\n"
        "storage:\n"
        "  graph_snapshots: false\n"
        "sync:\n"
        "  queue:\n"
        "    enabled: true\n"
        "sources:\n"
        "  - name: src0\n"
        "    type: markdown_folder\n"
        f"    path: {config.sources[0].path}\n"
        "    include:\n"
        '      - "**/*.md"\n',
        encoding="utf-8",
    )

    store = _store(config)
    try:
        queue = LocalQueue(store)
        queue.publish(_task("src0", id="cli-1", max_attempts=1))
        drain(queue, lambda task: (_ for _ in ()).throw(RuntimeError("bad source")))
    finally:
        store.close()

    assert main(["queue", "status", "--config", str(config_path)]) == 0
    reported = capsys.readouterr().out
    assert "dead: 1" in reported
    assert "bad source" in reported
    assert "requeue-dead" in reported

    assert main(["queue", "requeue-dead", "--config", str(config_path)]) == 0
    assert "requeued 1" in capsys.readouterr().out

    assert main(["queue", "drain", "--config", str(config_path)]) == 0
    drained = capsys.readouterr().out
    assert "src0: healthy (2 indexed" in drained
    assert "drained 1 task(s)" in drained


def test_the_queue_cli_talks_to_the_configured_backend(
    tmp_path: Path, capsys: Any, monkeypatch: Any
) -> None:
    """`pheasant queue` built a LocalQueue directly, ignoring the config.

    With `sync.queue.backend: nats` the tasks live in JetStream, so `status`
    reported an empty queue on a backlog of thousands, `drain` drained nothing,
    and `requeue-dead` silently did nothing to the queue actually holding the
    dead letters — all three exiting 0, which is the worst way to be wrong.
    Asserted through `queue_from_config`, so this holds for any backend without
    needing a live broker.
    """

    from pheasant.cli import main
    from pheasant.sync.queue import DEAD, DONE, INFLIGHT, PENDING

    config = _config(tmp_path, state_name="cli-backend", sources=1, queue={"enabled": True})
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        "pheasant:\n"
        f"  name: {config.pheasant.name}\n"
        f"  state_path: {config.pheasant.state_path}\n"
        f"  workspace_root: {config.pheasant.workspace_root}\n"
        f"  exports_path: {config.pheasant.exports_path}\n"
        "storage:\n"
        "  graph_snapshots: false\n"
        "sync:\n"
        "  queue:\n"
        "    enabled: true\n"
        "sources:\n"
        "  - name: src0\n"
        "    type: markdown_folder\n"
        f"    path: {config.sources[0].path}\n"
        "    include:\n"
        '      - "**/*.md"\n',
        encoding="utf-8",
    )

    class _Sentinel:
        closed = False

        def depth(self) -> dict[str, int]:
            return {PENDING: 7, INFLIGHT: 0, DONE: 0, DEAD: 0}

        def close(self) -> None:
            _Sentinel.closed = True

    import pheasant.sync.queue as queue_module

    monkeypatch.setattr(queue_module, "queue_from_config", lambda *_a, **_k: _Sentinel())

    assert main(["queue", "status", "--config", str(config_path)]) == 0
    assert "queued: 7" in capsys.readouterr().out, "the CLI read a queue the config did not name"
    assert _Sentinel.closed, "the queue handle was never closed"


# --------------------------------------------------------------------------
# NATS JetStream, against a real broker when one is reachable
# --------------------------------------------------------------------------

NATS_URL = "nats://127.0.0.1:14222"


def _broker_reachable() -> bool:
    import socket as _socket

    try:
        with _socket.create_connection(("127.0.0.1", 14222), timeout=0.5):
            return True
    except OSError:
        return False


nats_broker = pytest.mark.skipif(
    not _broker_reachable(),
    reason="no JetStream broker on 127.0.0.1:14222 (run: nats-server -js)",
)


@pytest.fixture
def nats_queue(request: Any):  # type: ignore[no-untyped-def]
    pytest.importorskip("nats", reason="the [queue] extra is optional")
    from pheasant.sync.queue import NatsQueue

    suffix = abs(hash(request.node.name)) % 100_000
    queue = NatsQueue(
        [NATS_URL],
        stream=f"TEST_{suffix}",
        subject=f"test.{suffix}.tasks",
        durable=f"test-{suffix}",
    )
    try:
        yield queue
    finally:
        queue.close()


@nats_broker
def test_nats_round_trips_a_task(nats_queue: Any) -> None:
    nats_queue.publish(_task("docs", id="n1", mode="full"))
    claimed = nats_queue.claim("worker")

    assert claimed is not None
    assert claimed.source_id == "docs"
    assert claimed.mode == "full"
    assert claimed.attempts == 1
    nats_queue.ack(claimed)
    assert nats_queue.claim("worker") is None


@nats_broker
def test_nats_publish_retry_is_deduplicated(nats_queue: Any) -> None:
    """Retrying one publish must not enqueue the same invocation twice."""

    task = _task("docs", id="same-id")
    for _ in range(3):
        nats_queue.publish(task)

    drained = drain(nats_queue, lambda task: task.source_id)
    assert drained == ["docs"]


@nats_broker
def test_nats_accepts_a_new_run_with_the_same_logical_task_id(nats_queue: Any) -> None:
    for _ in range(2):
        nats_queue.publish(_task("docs", id="repeatable-id"))

    drained = drain(nats_queue, lambda task: task.source_id)
    assert drained == ["docs", "docs"]


@nats_broker
def test_nats_depth_reports_unprocessed_work(nats_queue: Any) -> None:
    """The autoscaling signal must come down as work is done.

    A stream's message count does not drop on ack under the default
    retention, so reading it would give an HPA a number that only ever
    grows. This reads the durable consumer instead.
    """

    assert nats_queue.depth()[PENDING] == 0
    for index in range(3):
        nats_queue.publish(_task(f"src{index}", id=f"d{index}"))
    assert nats_queue.depth()[PENDING] == 3

    claimed = nats_queue.claim("worker")
    assert claimed is not None
    nats_queue.ack(claimed)
    assert nats_queue.depth()[PENDING] == 2

    drain(nats_queue, lambda task: task.source_id)
    assert nats_queue.depth()[PENDING] == 0


@nats_broker
def test_nats_redelivers_a_nacked_task(nats_queue: Any) -> None:
    nats_queue.publish(_task("docs", id="retry-me", max_attempts=5))

    first = nats_queue.claim("worker")
    assert first is not None and first.attempts == 1
    nats_queue.nack(first, "transient", retry_in_seconds=0.0)

    second = nats_queue.claim("worker")
    assert second is not None
    assert second.source_id == "docs"
    assert second.attempts == 2, "redelivery count did not advance"
    nats_queue.ack(second)


@nats_broker
def test_nats_terminates_a_task_that_exhausts_its_attempts(nats_queue: Any) -> None:
    """Exhaustion creates an observable, replayable DLQ record."""

    nats_queue.publish(_task("docs", id="doomed", max_attempts=2))

    for expected in (1, 2):
        task = nats_queue.claim("worker")
        assert task is not None and task.attempts == expected
        nats_queue.nack(task, "always fails", retry_in_seconds=0.0)

    assert nats_queue.claim("worker") is None
    assert nats_queue.depth()[PENDING] == 0
    assert nats_queue.depth()[DEAD] == 1

    assert nats_queue.requeue_dead() == 1
    assert nats_queue.depth()[DEAD] == 0
    replay = nats_queue.claim("worker")
    assert replay is not None
    assert replay.id == "doomed"
    assert replay.attempts == 1
    nats_queue.ack(replay)


def _nats_server_binary_available() -> bool:
    import shutil

    return shutil.which("nats-server") is not None


nats_server_binary = pytest.mark.skipif(
    not _nats_server_binary_available(),
    reason="the nats-server binary is not on PATH",
)


@nats_server_binary
def test_nats_survives_an_idle_period_between_calls(tmp_path: Path) -> None:
    """Regression: the loop must keep running between calls, not just for
    the duration of each one.

    Before the fix, ``NatsQueue._run`` spun its private event loop only via
    ``run_until_complete`` per call and let it sit idle in between. nats-py's
    own background tasks — the socket reader and its ping/keepalive
    coroutine, both created by ``nats.connect()`` — only run while something
    is actually driving the loop, so an indexer idle between ``claim()``
    polls never processed the server's own keepalive pings and the
    connection was silently broken server-side; the next call failed with a
    ``TimeoutError``.

    Runs its own broker (rather than the shared ``nats_broker`` fixture)
    with an aggressive ``ping_interval``/``ping_max`` — a scaled-down
    analogue of production's ``2min``/``2`` — so the idle window that would
    take minutes against a default broker takes seconds here. This is a
    regression guard for the mechanism, not a timing-exact reproduction of
    production's window.
    """

    pytest.importorskip("nats", reason="the [queue] extra is optional")
    import socket
    import subprocess

    from pheasant.sync.queue import NatsQueue

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    conf_path = tmp_path / "nats.conf"
    js_dir = tmp_path / "jsdata"
    js_dir.mkdir()
    conf_path.write_text(
        f'port: {port}\nping_interval: "1s"\nping_max: 2\njetstream {{ store_dir: "{js_dir}" }}\n'
    )
    server = subprocess.Popen(
        ["nats-server", "-c", str(conf_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("test-scoped nats-server never became reachable")

        queue = NatsQueue(
            [f"nats://127.0.0.1:{port}"],
            stream="IDLE_TEST",
            subject="idle.test.tasks",
            durable="idle-test",
        )
        try:
            queue.publish(_task("docs", id="before-idle"))
            # The loop must survive this unattended — nothing here drives it.
            time.sleep(8.0)
            queue.publish(_task("docs", id="after-idle"))
            claimed = drain(queue, lambda task: task.source_id)
            assert claimed == ["docs", "docs"]
        finally:
            queue.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=5.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            server.kill()


@nats_broker
def test_a_full_sync_all_runs_over_nats(tmp_path: Path) -> None:
    """End to end: the broker really does drive a real index."""

    pytest.importorskip("nats", reason="the [queue] extra is optional")
    suffix = abs(hash(tmp_path.name)) % 100_000
    config = _config(
        tmp_path,
        state_name="over-nats",
        sources=3,
        queue={
            "enabled": True,
            "backend": "nats",
            "nats_servers": [NATS_URL],
            "nats_stream": f"SYNC_{suffix}",
            "nats_subject": f"sync.{suffix}.tasks",
            "nats_durable": f"sync-{suffix}",
        },
    )
    engine = SyncEngine(config)
    try:
        results = engine.sync_all("full")
    finally:
        engine.close()

    assert [result.source_id for result in results] == ["src0", "src1", "src2"]
    assert all(result.indexed_artifacts == 2 for result in results)


def test_a_queued_sync_carries_the_trace_of_the_request_that_asked_for_it(
    tmp_path: Path,
) -> None:
    """`POST /sync` publishes; an indexer in another process runs it. Without a
    trace on the task those are two unrelated traces for one thing a person
    asked for.

    The trace is attached *after* the task id digest, never before: the id is
    content-addressed over the payload so two replicas answering one
    double-click enqueue one task, and a per-request value inside the digest
    would defeat that entirely.
    """

    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    # The `api` role, because that is the one that publishes: a role that
    # indexes locally runs the sync on a background thread instead, and a
    # contextvar does not cross a raw thread -- so the trace is carried where
    # the hand-off actually is, which is the fleet.
    config = _config(tmp_path, state_name="traced", sources=1, queue={"enabled": True})
    config.observability.interactions.enabled = True
    # In-process: this binds nothing, so loopback is what it actually is. A
    # fleet role on a routable bind refuses to start unauthenticated.
    config.server.host = "127.0.0.1"
    app = create_app(config, role="api")
    inbound = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"

    with TestClient(app) as client:
        # `wait: false` is the path that publishes instead of syncing inline.
        first = client.post("/sync", json={"wait": False}, headers={"traceparent": inbound})
        assert first.status_code < 400, first.text
        second = client.post("/sync", json={"wait": False}, headers={"traceparent": inbound})
        assert second.status_code < 400, second.text

    store = _store(config)
    try:
        rows = store.rows("SELECT id, payload FROM index_tasks", ())
        # Still one task: the digest did not move.
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload"] or "{}")
        assert payload["traceparent"].startswith("00-" + "a" * 32 + "-")
    finally:
        store.close()


def test_an_untraced_sync_queues_a_task_with_no_traceparent(tmp_path: Path) -> None:
    """Observation is off by default, so the ordinary case must be byte-for-byte
    what it always was -- no extra payload key, and therefore the same task id."""

    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    config = _config(tmp_path, state_name="untraced", sources=1, queue={"enabled": True})
    config.server.host = "127.0.0.1"
    app = create_app(config, role="api")
    with TestClient(app) as client:
        response = client.post("/sync", json={"wait": False})
        assert response.status_code < 400, response.text

    store = _store(config)
    try:
        payload = json.loads(
            store.rows("SELECT payload FROM index_tasks", ())[0]["payload"] or "{}"
        )
        assert "traceparent" not in payload
    finally:
        store.close()
