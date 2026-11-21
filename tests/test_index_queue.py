"""Phase 35.5d — the durable index work queue.

The claim being tested is narrow and checkable: with the queue off,
``sync_all`` does exactly what it always did; with it on, a backlog survives
the process that created it. Everything else — dead-lettering, visibility
timeouts, concurrent claims — exists to make that first claim safe.
"""

from __future__ import annotations

import json
import os
import time
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
    _heartbeat_interval,
    drain,
    owner_id,
    queue_from_config,
)
from tests.conftest import nats_url


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


def _queue_yaml(settings: dict[str, Any]) -> str:
    """``settings`` as the ``sync.queue`` block of a YAML config."""

    return (
        "sync:\n"
        "  queue:\n"
        "    enabled: true\n"
        "    backend: nats\n"
        "    nats_servers:\n"
        f"      - {settings['nats_servers'][0]}\n"
        f"    nats_stream: {settings['nats_stream']}\n"
        f"    nats_subject: {settings['nats_subject']}\n"
        f"    nats_durable: {settings['nats_durable']}\n"
    )


def _store(config: PheasantConfig) -> StateStore:
    paths = StatePaths.from_config(config)
    paths.ensure()
    store = StateStore()
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
    """Same results, indexed in-process: nothing to publish to."""

    config = _config(tmp_path, state_name="off")
    engine = SyncEngine(config)
    try:
        results = engine.sync_all("full")
    finally:
        engine.close()

    assert [result.source_id for result in results] == ["src0", "src1", "src2"]
    assert all(result.indexed_artifacts == 2 for result in results)


def test_queued_sync_all_matches_unqueued_sync_all(
    tmp_path: Path, nats_queue_settings: dict
) -> None:
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

    queued = SyncEngine(_config(tmp_path, state_name="queued", queue=nats_queue_settings))
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


def test_queued_sync_all_preserves_configured_source_order(
    tmp_path: Path, nats_queue_settings: dict
) -> None:
    config = _config(tmp_path, state_name="order", sources=4, queue=nats_queue_settings)
    engine = SyncEngine(config)
    try:
        results = engine.sync_all("full")
    finally:
        engine.close()
    assert [result.source_id for result in results] == ["src0", "src1", "src2", "src3"]


def test_control_tasks_use_the_same_idempotent_single_writer_path(
    tmp_path: Path, nats_queue_settings: dict
) -> None:
    config = _config(tmp_path, state_name="control", sources=1, queue=nats_queue_settings)
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


def test_redelivered_full_task_resumes_from_its_published_checkpoint(
    tmp_path: Path, nats_queue_settings: dict
) -> None:
    config = _config(tmp_path, state_name="resume-full", sources=1, queue=nats_queue_settings)
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


def test_queued_sync_all_works_with_several_source_workers(
    tmp_path: Path, nats_queue_settings: dict
) -> None:
    config = _config(
        tmp_path, state_name="parallel", sources=4, queue=nats_queue_settings, source_workers=3
    )
    engine = SyncEngine(config)
    try:
        results = engine.sync_all("full")
        queue = queue_from_config(config, engine.state)
        try:
            depth = queue.depth()
        finally:
            queue.close()
    finally:
        engine.close()

    assert [result.source_id for result in results] == ["src0", "src1", "src2", "src3"]
    assert all(result.indexed_artifacts == 2 for result in results)
    # Every task ran exactly once and none is left: four drain loops sharing
    # one queue must not double-claim.
    assert depth[PENDING] == 0 and depth[INFLIGHT] == 0


# --------------------------------------------------------------------------
# The reason the queue exists: a backlog that survives the process
# --------------------------------------------------------------------------


def test_a_backlog_survives_the_process_that_created_it(
    tmp_path: Path, nats_queue_settings: dict
) -> None:
    """The failure the in-memory list cannot survive, reproduced.

    A process is killed partway through ``sync_all``. With sources held in a
    Python list the remaining ones are simply gone; here they are rows, so a
    fresh engine over the same ``/state`` finishes the job.
    """

    config = _config(tmp_path, state_name="resume", sources=4, queue=nats_queue_settings)

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
            queue = queue_from_config(config, engine.state)
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

    # A new process over the same state. Nothing was handed to it.
    resumed = SyncEngine(config)
    try:
        queue = queue_from_config(config, resumed.state)
        # The claimed-but-unfinished task is invisible until its ack window
        # lapses; the rest are immediately claimable.
        results = drain(queue, lambda task: resumed.sync_source(task.source_id, "full"))
        depth = queue.depth()
        queue.close()
    finally:
        resumed.close()

    assert {result.source_id for result in results} == {"src3"}
    assert depth[PENDING] == 0
    assert depth[INFLIGHT] == 1, "the crashed task is still claimed until it times out"


def test_a_broker_that_refuses_a_task_is_reported_not_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    nats_queue_settings: dict,
) -> None:
    """A failed publish used to be logged at debug as "already queued".

    No backend raises for a duplicate, so the exception was always the broker
    refusing the task -- found with NATS's storage deleted under it, where the
    sync reported success having indexed nothing. The refused source is now a
    result with its own status and an error log; the others still run.
    """

    from pheasant.sync.queue import NatsQueue

    original = NatsQueue.publish

    def refuse_src1(self: NatsQueue, task: IndexTask) -> IndexTask:
        if task.source_id == "src1":
            raise ConnectionError("stream storage unavailable")
        return original(self, task)

    monkeypatch.setattr(NatsQueue, "publish", refuse_src1)
    config = _config(tmp_path, state_name="refused", sources=3, queue=nats_queue_settings)
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


def test_a_large_task_payload_reaches_the_child_by_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux refuses one argument over 128 KiB (Windows a whole command line
    over 32 KiB), and the payload used to travel base64-encoded on argv: a
    4,698-URL web collection failed every queued sync with ``[Errno 7]
    Argument list too long`` before the child started."""

    from pheasant.sync import worker

    payload = {"operation": "replace_source", "blob": "x" * 300_000}
    seen: dict[str, Any] = {}

    def fake_run(command, timeout, on_progress):
        path = command[command.index("--task-payload-file") + 1]
        seen["longest"] = max(len(part) for part in command)
        seen["path"] = Path(path)
        seen["payload"] = json.loads(Path(path).read_text(encoding="utf-8"))
        return 0, json.dumps({"results": []}), ""

    monkeypatch.setattr(worker, "_run_streaming", fake_run)
    worker.run_sync(tmp_path / "pheasant.yaml", "web", task_payload=payload)
    assert seen["longest"] < 4096, "the payload is still on the command line"
    assert seen["payload"] == payload
    assert not seen["path"].exists(), "the payload file outlived the child"


def test_a_child_installs_a_source_with_thousands_of_urls(tmp_path: Path) -> None:
    """The real child process, end to end, with the shape that failed live."""

    import yaml

    from pheasant.sync.worker import run_sync

    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "pheasant": {
                    "name": "bigpayload",
                    "state_path": str(tmp_path / "state"),
                    "workspace_root": str(tmp_path),
                    "exports_path": str(tmp_path / "exports"),
                }
            }
        ),
        encoding="utf-8",
    )
    urls = [f"https://example.test/page-{n:05d}-with-a-longish-slug" for n in range(5000)]
    source = {
        "name": "web",
        "type": "web_collection",
        "path": "/unused",
        "urls": urls,
        "connector": {"allow_experimental": True},
    }
    assert len(json.dumps(source)) > 128 * 1024
    report = run_sync(
        config_path,
        "web",
        task_payload={"operation": "replace_source", "source": source},
        timeout=300,
    )
    assert report.get("status") not in {"failed", "timeout"}, report
    store = StateStore()
    try:
        installed = json.loads(store.get_source("web")["config_json"])
        assert installed["urls"] == urls
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


def test_queue_depth_reaches_the_metrics_endpoint(
    tmp_path: Path, nats_queue_settings: dict
) -> None:
    """The autoscaling signal, which is most of why the queue is durable.

    The in-memory job registry only knows this process's work, so a restarted
    fleet with rows waiting would scrape zero. The queue's own depth wins.
    """

    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    config = _config(tmp_path, state_name="metrics", sources=2, queue=nats_queue_settings)
    client = TestClient(create_app(config))

    queue = queue_from_config(config, None)
    try:
        queue.publish(_task("src0", id="m1"))
        queue.publish(_task("src1", id="m2"))
        claimed = queue.claim("worker", visibility_seconds=300.0)
        assert claimed is not None
    finally:
        queue.close()

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


def test_the_queue_cli_reports_and_replays(
    tmp_path: Path, capsys: Any, nats_queue_settings: dict
) -> None:
    from pheasant.cli import main

    config = _config(tmp_path, state_name="cli", sources=1, queue=nats_queue_settings)
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        "pheasant:\n"
        f"  name: {config.pheasant.name}\n"
        f"  state_path: {config.pheasant.state_path}\n"
        f"  workspace_root: {config.pheasant.workspace_root}\n"
        f"  exports_path: {config.pheasant.exports_path}\n"
        "storage:\n"
        "  graph_snapshots: false\n" + _queue_yaml(nats_queue_settings) + "sources:\n"
        "  - name: src0\n"
        "    type: markdown_folder\n"
        f"    path: {config.sources[0].path}\n"
        "    include:\n"
        '      - "**/*.md"\n',
        encoding="utf-8",
    )

    queue = queue_from_config(config, None)
    try:
        queue.publish(_task("src0", id="cli-1", max_attempts=1))
        drain(queue, lambda task: (_ for _ in ()).throw(RuntimeError("bad source")))
    finally:
        queue.close()

    assert main(["queue", "status", "--config", str(config_path)]) == 0
    reported = capsys.readouterr().out
    assert "dead: 1" in reported
    assert "requeue-dead" in reported

    assert main(["queue", "requeue-dead", "--config", str(config_path)]) == 0
    assert "requeued 1" in capsys.readouterr().out

    assert main(["queue", "drain", "--config", str(config_path)]) == 0
    drained = capsys.readouterr().out
    assert "src0: healthy (2 indexed" in drained
    assert "drained 1 task(s)" in drained


def test_the_queue_cli_talks_to_the_configured_backend(
    tmp_path: Path, capsys: Any, monkeypatch: Any, nats_queue_settings: dict
) -> None:
    """`pheasant queue` once built its queue directly, ignoring the config,
    and reported an empty queue on a backlog of thousands. Asserted through
    `queue_from_config`, so it holds without a live broker."""

    from pheasant.cli import main
    from pheasant.sync.queue import DEAD, DONE, INFLIGHT, PENDING

    config = _config(tmp_path, state_name="cli-backend", sources=1, queue=nats_queue_settings)
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        "pheasant:\n"
        f"  name: {config.pheasant.name}\n"
        f"  state_path: {config.pheasant.state_path}\n"
        f"  workspace_root: {config.pheasant.workspace_root}\n"
        f"  exports_path: {config.pheasant.exports_path}\n"
        "storage:\n"
        "  graph_snapshots: false\n" + _queue_yaml(nats_queue_settings) + "sources:\n"
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

NATS_URL = nats_url()


@pytest.fixture
def nats_queue(request: Any):  # type: ignore[no-untyped-def]
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


def test_nats_round_trips_a_task(nats_queue: Any) -> None:
    nats_queue.publish(_task("docs", id="n1", mode="full"))
    claimed = nats_queue.claim("worker")

    assert claimed is not None
    assert claimed.source_id == "docs"
    assert claimed.mode == "full"
    assert claimed.attempts == 1
    nats_queue.ack(claimed)
    assert nats_queue.claim("worker") is None


def test_nats_publish_retry_is_deduplicated(nats_queue: Any) -> None:
    """Retrying one publish must not enqueue the same invocation twice."""

    task = _task("docs", id="same-id")
    for _ in range(3):
        nats_queue.publish(task)

    drained = drain(nats_queue, lambda task: task.source_id)
    assert drained == ["docs"]


def test_nats_accepts_a_new_run_with_the_same_logical_task_id(nats_queue: Any) -> None:
    for _ in range(2):
        nats_queue.publish(_task("docs", id="repeatable-id"))

    drained = drain(nats_queue, lambda task: task.source_id)
    assert drained == ["docs", "docs"]


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


def test_nats_an_acked_task_leaves_the_broker(nats_queue: Any) -> None:
    """Work-queue retention: the stream holds what is outstanding, not history.

    Created with the default ``limits`` retention and no limits, the stream
    kept every task ever published -- on the broker's disk, forever.
    """

    for index in range(5):
        nats_queue.publish(_task(f"s{index}", id=f"kept-{index}"))
    while (task := nats_queue.claim("worker")) is not None:
        nats_queue.ack(task)

    async def stored() -> Any:
        info = await nats_queue._js.stream_info(nats_queue.stream)
        return info.state.messages, str(info.config.retention)

    messages, retention = nats_queue._run(stored())
    assert "workqueue" in retention.lower()
    assert messages == 0
    assert nats_queue.depth()[DONE] == 5  # still counted, from the consumer


def test_nats_dead_letters_a_task_whose_workers_keep_dying(nats_queue: Any) -> None:
    """A claimer that crashes never nacks, so the cap must hold at claim time.

    The attempt cap lived only in `nack`, and the consumer's `max_deliver` is
    unlimited, so a task that killed its worker was redelivered forever.
    """

    js = nats_queue._connect()

    async def fast_redelivery() -> None:
        from nats.js.api import AckPolicy, ConsumerConfig

        await js.add_consumer(
            nats_queue.stream,
            ConsumerConfig(
                durable_name=nats_queue.durable,
                filter_subject=nats_queue.subject,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=1.0,
            ),
        )

    nats_queue._run(fast_redelivery())
    nats_queue.publish(_task("docs", id="poison", max_attempts=2))

    import time as _time

    handed_out: list[int] = []
    deadline = _time.monotonic() + 12
    while _time.monotonic() < deadline and nats_queue.depth()[DEAD] == 0:
        task = nats_queue.claim("worker")
        if task is not None:
            handed_out.append(task.attempts)  # the worker "dies": no ack, no nack
        _time.sleep(1.2)

    assert handed_out == [1, 2]
    assert nats_queue.depth()[DEAD] == 1
    assert nats_queue.requeue_dead() == 1


def test_nats_depth_refuses_to_report_an_unreachable_broker_as_empty(monkeypatch: Any) -> None:
    """An empty queue is a reading an autoscaler acts on; an outage is not one.

    It returned zeros -- and before it could, the initial connect sat in the
    client's own retry loop for minutes.
    """

    import socket as _socket
    import time as _time

    from pheasant.sync.queue import NatsQueue, QueueUnavailable

    with _socket.socket() as probe:  # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(NatsQueue, "CONNECT_DEADLINE_SECONDS", 1.0)
    queue = NatsQueue([f"nats://127.0.0.1:{port}"], connect_timeout=0.2)
    try:
        started = _time.monotonic()
        with pytest.raises(QueueUnavailable):
            queue.depth()
        assert _time.monotonic() - started < 10
    finally:
        queue.close()


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


def test_a_full_sync_all_runs_over_nats(tmp_path: Path) -> None:
    """End to end: the broker really does drive a real index."""

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
    tmp_path: Path, nats_queue_settings: dict
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
    config = _config(tmp_path, state_name="traced", sources=1, queue=nats_queue_settings)
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

    queue = queue_from_config(config, None)
    try:
        claimed = [queue.claim("reader"), queue.claim("reader")]
        tasks = [task for task in claimed if task is not None]
        assert tasks, "nothing was published"
        # The logical id is the digest, unmoved by the trace on the payload.
        assert len({task.id for task in tasks}) == 1
        assert tasks[0].payload["traceparent"].startswith("00-" + "a" * 32 + "-")
    finally:
        queue.close()


def test_an_untraced_sync_queues_a_task_with_no_traceparent(
    tmp_path: Path, nats_queue_settings: dict
) -> None:
    """Observation is off by default, so the ordinary case must be byte-for-byte
    what it always was -- no extra payload key, and therefore the same task id."""

    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    config = _config(tmp_path, state_name="untraced", sources=1, queue=nats_queue_settings)
    config.server.host = "127.0.0.1"
    app = create_app(config, role="api")
    with TestClient(app) as client:
        response = client.post("/sync", json={"wait": False})
        assert response.status_code < 400, response.text

    queue = queue_from_config(config, None)
    try:
        task = queue.claim("reader")
        assert task is not None, "nothing was published"
        assert "traceparent" not in task.payload
    finally:
        queue.close()
