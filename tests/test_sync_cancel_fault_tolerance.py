"""Ending a sync, whatever state it is in and whatever fails around it.

`tests/test_sync_cancellation.py` holds the between-items cancel. This module
holds the four layers `sync/cancellation.py` adds on top, each against the
real thing it depends on -- PostgreSQL for the ledger, a JetStream broker for
redelivery, a real child process for the supervisor:

* a sync **waiting in the queue** is reached through its ledger row and never
  runs, on any delivery -- including the one after an indexer died;
* a sync **inside a long item** stops within seconds, and nothing an item
  computed after the stop is committed, whatever the code in between did with
  the `Stopped` it was handed;
* a **child that cannot stop** is ended by its supervisor after the grace
  period, and the next sync of the source heals what that leaves behind;
* every one of them ends in the state a single full sync of the same corpus
  produces, once an incremental sync has run after it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from pheasant import cancel_scope
from pheasant.config.loader import load_config
from pheasant.config.schema import PheasantConfig
from pheasant.services import ServiceContext, sync_control
from pheasant.services.errors import SyncNotRunning
from pheasant.services.index_queue import queue_status
from pheasant.sync import cancellation, request_ledger, supervision
from pheasant.sync.cancellation import SyncRun, cancel_source, running
from pheasant.sync.engine import SyncEngine
from pheasant.sync.queue import IndexTask, queue_from_config
from pheasant.sync.worker import WorkerBackedEngine

FILES = 8

from pheasant.sync import engine as _engine_module  # noqa: E402

_REAL_PARSE = _engine_module.parse_connector_payload


@pytest.fixture(autouse=True)
def _fast_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    # The production throttles are a second; a test corpus syncs in less.
    monkeypatch.setattr(cancellation, "CHECK_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(cancellation, "WATCH_INTERVAL_SECONDS", 0.1)


def _corpus(root: Path, files: int = FILES) -> Path:
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        (corpus / f"note-{index:02d}.md").write_text(
            f"# Note {index}\n\nThe word marker{index} appears in note {index}.\n",
            encoding="utf-8",
        )
    return corpus


def _payload(root: Path, corpus: Path, *, queue: dict[str, Any] | None = None) -> dict:
    payload: dict[str, Any] = {
        "pheasant": {
            "name": "cancel-ft",
            "defaults": "standalone",
            "state_path": str(root / "state"),
            "workspace_root": str(root),
            "exports_path": str(root / "exports"),
        },
        "storage": {"graph_snapshots": False},
        # Vectors on (the offline stub, into LanceDB under the state path), so
        # every test also sees what a stop does to the embeddings.
        "search": {"embeddings": {"enabled": True, "provider": "stub"}},
        "sync": {"concurrency": {"max_parallel_files": 1, "lock_timeout_seconds": 5}},
        "sources": [
            {
                "name": "docs",
                "type": "document_folder",
                "path": str(corpus),
                "include": ["**/*.md"],
            }
        ],
    }
    if queue is not None:
        payload["sync"]["queue"] = queue
    return payload


def _config(root: Path, corpus: Path, **kwargs: Any) -> PheasantConfig:
    return PheasantConfig.model_validate(_payload(root, corpus, **kwargs))


def _artifacts(engine: SyncEngine) -> int:
    rows = engine.state.rows("SELECT COUNT(*) AS n FROM artifacts WHERE source_id='docs'")
    return int(rows[0]["n"])


def _events(engine: SyncEngine, kind: str = "sync.cancelled") -> list[dict]:
    return [e for e in engine.state.list_sync_events("docs", limit=20) if e["event_type"] == kind]


@dataclass
class _Task:
    """The parts of an `IndexTask` the ledger reads."""

    source_id: str = "docs"
    id: str = "idx-test"
    mode: str = "incremental"
    payload: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# The ledger: what a queued sync is, and what a cancel does to it
# --------------------------------------------------------------------------


def test_a_queued_request_cancelled_before_its_claim_is_never_run(tmp_path: Path) -> None:
    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        state = engine.state
        task = _Task()
        request_id = request_ledger.record(state, task)
        assert request_id and task.payload["request_id"] == request_id
        assert [r["id"] for r in request_ledger.open_requests(state, "docs")] == [request_id]

        reached = cancel_source(state, "docs", requested_by="tester", reason="wrong source")
        assert reached is not None and reached["run"] is None and reached["first_request"]
        assert [(r["id"], r["state"]) for r in reached["requests"]] == [(request_id, "cancelled")]
        # It leaves every listing at once, before any indexer claims it.
        assert request_ledger.open_requests(state, "docs") == []

        # Every delivery of the task meets the same answer.
        for _delivery in range(3):
            admission = request_ledger.admit(state, task, "indexer-a")
            assert admission.verdict == "cancelled"
            assert admission.requested_by == "tester" and admission.reason == "wrong source"
    finally:
        engine.close()


def test_a_sync_requested_after_a_cancel_still_runs(tmp_path: Path) -> None:
    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        state = engine.state
        first, second = _Task(id="a"), _Task(id="b")
        request_ledger.record(state, first)
        cancel_source(state, "docs", requested_by="tester")
        request_ledger.record(state, second)  # "Run now", pressed again afterwards
        assert request_ledger.admit(state, first, "indexer").verdict == "cancelled"
        assert request_ledger.admit(state, second, "indexer").runs
    finally:
        engine.close()


@pytest.mark.parametrize("nacked", [True, False], ids=["nacked", "died-holding-it"])
def test_a_redelivery_after_a_cancel_is_not_run_again(tmp_path: Path, nacked: bool) -> None:
    """The indexer claimed it, was cancelled, and died before acknowledging.

    Either it got as far as giving the task back (``nack``), or it died
    holding it and the broker redelivers on its own. Both reach the next
    indexer with the cancel already written down.
    """

    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        state = engine.state
        task = _Task()
        request_id = request_ledger.record(state, task)
        assert request_ledger.admit(state, task, "indexer-a").runs
        reached = cancel_source(state, "docs", requested_by="tester")
        # No live run row (the run never registered, or its heartbeat died),
        # but the claimed request carries the cancel.
        assert reached is not None and reached["run"] is None
        assert [(r["id"], r["state"]) for r in reached["requests"]] == [(request_id, "claimed")]
        assert request_ledger.cancel_of(state, request_id)
        if nacked:
            request_ledger.release(state, request_id, dead=False)
        assert request_ledger.admit(state, task, "indexer-b").verdict == "cancelled"
        assert request_ledger.open_requests(state, "docs") == []
    finally:
        engine.close()


def test_a_finished_request_whose_ack_was_lost_is_not_run_twice(tmp_path: Path) -> None:
    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        state = engine.state
        task = _Task()
        request_id = request_ledger.record(state, task)
        assert request_ledger.admit(state, task, "indexer").runs
        request_ledger.close(state, request_id, request_ledger.FINISHED)
        assert request_ledger.admit(state, task, "indexer").verdict == "finished"
        # And a finished request is out of a cancel's reach: nothing to stop.
        assert cancel_source(state, "docs", requested_by="tester") is None
    finally:
        engine.close()


def test_a_replayed_dead_letter_reopens_its_request(tmp_path: Path) -> None:
    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        state = engine.state
        task = _Task()
        request_id = request_ledger.record(state, task)
        assert request_ledger.admit(state, task, "indexer").runs
        request_ledger.release(state, request_id, dead=True)
        assert request_ledger.open_requests(state, "docs") == []
        # `pheasant queue requeue-dead` publishes the dead body, id included.
        assert request_ledger.record(state, task) == request_id
        assert [r["state"] for r in request_ledger.open_requests(state, "docs")] == ["queued"]
        assert request_ledger.admit(state, task, "indexer").runs
    finally:
        engine.close()


def test_control_tasks_are_not_syncs_a_cancel_can_stop(tmp_path: Path) -> None:
    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        prune = _Task(payload={"operation": "prune_documents"})
        assert request_ledger.record(engine.state, prune) is None
        assert request_ledger.admit(engine.state, prune, "indexer").runs
        with pytest.raises(SyncNotRunning):
            sync_control.cancel_sync(
                ServiceContext(engine.config, engine.state, None),
                sync_control.CancelSyncRequest(source_name="docs"),
            )
    finally:
        engine.close()


def test_rows_nobody_closed_expire_and_closed_rows_are_pruned(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        state = engine.state
        ghost, done = _Task(id="ghost"), _Task(id="done")
        ghost_id = request_ledger.record(state, ghost)
        done_id = request_ledger.record(state, done)
        request_ledger.close(state, done_id, request_ledger.FINISHED)
        old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        state.execute("UPDATE sync_requests SET requested_at=?, finished_at=?", (old, old))
        # A ghost past the horizon is neither listed nor reached by a cancel.
        assert request_ledger.open_requests(state) == []
        assert cancel_source(state, "docs", requested_by="tester") is None
        assert request_ledger.prune(state) == 2  # the ghost expired, the closed row deleted
        assert [r["id"] for r in state.rows("SELECT id FROM sync_requests")] == [ghost_id]
        # Expired now and old: the next pass deletes it too.
        state.execute("UPDATE sync_requests SET finished_at=?", (old,))
        assert request_ledger.prune(state) == 1
        assert state.rows("SELECT id FROM sync_requests") == []
    finally:
        engine.close()


def test_a_claim_and_a_cancel_racing_on_one_row_never_both_win(tmp_path: Path) -> None:
    """Under READ COMMITTED, one conditional UPDATE each, many times.

    Whichever lands first, the outcome is safe: either the claim lost (the
    task is acknowledged as cancelled), or it won and the cancel was written
    on the row it claimed (the run reads it at its first check). Never a run
    that nothing will stop.
    """

    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        state = engine.state
        outcomes: list[tuple[str, bool]] = []
        for trial in range(25):
            task = _Task(id=f"race-{trial}")
            request_id = request_ledger.record(state, task)
            start = threading.Barrier(2)
            verdict: list[str] = []

            def claim(
                task: _Task = task, start: threading.Barrier = start, verdict: list = verdict
            ) -> None:
                start.wait()
                verdict.append(request_ledger.admit(state, task, "indexer").verdict)

            def cancel(start: threading.Barrier = start) -> None:
                start.wait()
                cancel_source(state, "docs", requested_by="tester")

            threads = [threading.Thread(target=claim), threading.Thread(target=cancel)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            outcomes.append((verdict[0], bool(request_ledger.cancel_of(state, request_id))))
        assert all(cancelled for _verdict, cancelled in outcomes), outcomes
        assert {verdict for verdict, _ in outcomes} <= {"run", "cancelled"}
    finally:
        engine.close()


# --------------------------------------------------------------------------
# Through a real JetStream broker
# --------------------------------------------------------------------------


def _publish(config: PheasantConfig, state: Any, mode: str = "incremental") -> IndexTask:
    queue = queue_from_config(config, state)
    assert queue is not None
    try:
        return queue.publish(
            IndexTask(id=f"idx-{time.monotonic_ns()}", source_id="docs", mode=mode)
        )
    finally:
        queue.close()


def test_a_sync_cancelled_while_queued_never_runs_and_is_acknowledged(
    tmp_path: Path, nats_queue_settings: dict[str, Any]
) -> None:
    config = _config(tmp_path, _corpus(tmp_path), queue=nats_queue_settings)
    engine = SyncEngine(config)
    try:
        published = _publish(config, engine.state)
        context = ServiceContext(engine.config, engine.state, None)
        listed = queue_status(context, None)
        assert listed["listing"] == "ledger"
        assert [t["task_id"] for t in listed["tasks"]] == [published.id]
        assert listed["tasks"][0]["state"] == "awaiting_claim"

        answer = sync_control.cancel_sync(
            context, sync_control.CancelSyncRequest(source_name="docs", requested_by="tester")
        )
        assert answer["stage"] == "queued" and answer["run"] is None
        assert [q["state"] for q in answer["queued"]] == ["cancelled"]
        assert queue_status(context, None)["tasks"] == []

        # An indexer claims it later: acknowledged, nothing read.
        queue = queue_from_config(config, engine.state)
        try:
            task = queue.claim("indexer")
            assert (
                task is not None and task.payload["request_id"] == answer["queued"][0]["request_id"]
            )
            result = engine.apply_index_task(task)
            queue.ack(task)
            assert result.status == "cancelled"
            assert result.details["cancelled"]["stage"] == "queued"
            assert _artifacts(engine) == 0
            assert queue.claim("indexer") is None  # the broker forgot it
            assert queue.depth()["pending"] == 0
        finally:
            queue.close()
        assert _events(engine)[0]["details"]["cancelled"]["stage"] == "queued"
        # Asking again: nothing left to stop.
        with pytest.raises(SyncNotRunning):
            sync_control.cancel_sync(context, sync_control.CancelSyncRequest(source_name="docs"))
    finally:
        engine.close()


def test_a_redelivered_task_of_a_dead_indexer_is_acknowledged_not_rerun(
    tmp_path: Path, nats_queue_settings: dict[str, Any]
) -> None:
    """An indexer claims the sync, is cancelled, and dies holding it."""

    config = _config(tmp_path, _corpus(tmp_path), queue=nats_queue_settings)
    engine = SyncEngine(config)
    try:
        _publish(config, engine.state)
        first = queue_from_config(config, engine.state)
        task = first.claim("indexer-a")
        assert task is not None
        # It got as far as admitting the task; then the cancel; then it died.
        assert request_ledger.admit(engine.state, task, "indexer-a").runs
        cancel_source(engine.state, "docs", requested_by="tester")
        first.nack(task, "indexer-a was killed", retry_in_seconds=0)
        first.close()

        second = queue_from_config(config, engine.state)
        try:
            redelivered = None
            deadline = time.monotonic() + 10
            while redelivered is None and time.monotonic() < deadline:
                redelivered = second.claim("indexer-b")
            assert redelivered is not None and redelivered.attempts == 2
            result = engine.apply_index_task(redelivered)
            second.ack(redelivered)
            assert result.status == "cancelled" and _artifacts(engine) == 0
        finally:
            second.close()
        # The next sync somebody asks for is a new request, and runs in full.
        assert engine.sync_source("docs").indexed_artifacts == FILES
    finally:
        engine.close()


def test_a_cancel_reaches_a_run_through_its_request_before_it_registers(tmp_path: Path) -> None:
    """Claimed, then cancelled while it waited for the source lock.

    The run has no row yet, so the cancel lands on its ledger request; the
    run's first check reads that and it stops before listing anything.
    """

    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        task = _Task()
        request_id = request_ledger.record(engine.state, task)
        assert request_ledger.admit(engine.state, task, "indexer").runs
        cancel_source(engine.state, "docs", requested_by="tester")
        with cancellation.bound_request(request_id):
            result = engine.sync_source("docs")
        assert result.status == "cancelled"
        assert result.details["cancelled"]["stage"] == "starting"
        assert _artifacts(engine) == 0
    finally:
        engine.close()


# --------------------------------------------------------------------------
# Inside one long item
# --------------------------------------------------------------------------


def _slow_item(monkeypatch: pytest.MonkeyPatch, path: str, behaviour: str) -> threading.Event:
    """Make preparing ``path`` long; set the returned event when it starts.

    ``wait``: an interruptible wait, as a pacing slot or a backoff is.
    ``swallow``: code that catches the stop and carries on with a complete,
    plausible result -- the worst a broad ``except`` in between can do.
    """

    from pheasant.sync import engine as engine_module

    real = engine_module.parse_connector_payload
    started = threading.Event()

    def parse(source: Any, item: Any, *args: Any, **kwargs: Any) -> Any:
        if item.relative_path == path:
            started.set()
            if behaviour == "wait":
                cancel_scope.sleep(60)
            else:
                deadline = time.monotonic() + 20
                while not cancel_scope.stopping() and time.monotonic() < deadline:
                    try:
                        cancel_scope.sleep(0.05)
                    except cancel_scope.Stopped:
                        break
        return real(source, item, *args, **kwargs)

    monkeypatch.setattr(engine_module, "parse_connector_payload", parse)
    return started


def _cancel_when(started: threading.Event, engine: SyncEngine) -> threading.Thread:
    def cancel() -> None:
        assert started.wait(30)
        assert cancel_source(engine.state, "docs", requested_by="tester") is not None

    thread = threading.Thread(target=cancel)
    thread.start()
    return thread


def test_a_cancel_lands_inside_a_long_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        started = _slow_item(monkeypatch, "note-03.md", "wait")
        canceller = _cancel_when(started, engine)
        began = time.monotonic()
        result = engine.sync_source("docs")
        elapsed = time.monotonic() - began
        canceller.join(10)
        assert result.status == "cancelled", result
        assert elapsed < 15, f"the stop took {elapsed:.1f}s; the item waits 60s"
        indexed = {
            r["relative_path"]
            for r in engine.state.rows("SELECT relative_path FROM artifacts WHERE source_id='docs'")
        }
        assert "note-03.md" not in indexed
        # Stopped is not unreadable: the stopped item is nobody's read failure.
        assert "note-03.md" not in str(_events(engine)[0]["details"])

        from pheasant.sync import engine as engine_module

        monkeypatch.setattr(engine_module, "parse_connector_payload", _REAL_PARSE)
        assert engine.sync_source("docs").status == "healthy"
        assert _artifacts(engine) == FILES
    finally:
        engine.close()


def test_an_item_finished_after_the_stop_is_never_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit loop, not the item's own code, decides what lands.

    With the row check throttled to an hour, only the in-memory signal the
    watch thread set can stop the loop -- which is the production case, where
    the row is read once a second and an item can finish inside that second.
    """

    monkeypatch.setattr(cancellation, "CHECK_INTERVAL_SECONDS", 3600.0)
    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        started = _slow_item(monkeypatch, "note-02.md", "swallow")
        canceller = _cancel_when(started, engine)
        result = engine.sync_source("docs")
        canceller.join(10)
        assert result.status == "cancelled"
        indexed = {
            r["relative_path"]
            for r in engine.state.rows("SELECT relative_path FROM artifacts WHERE source_id='docs'")
        }
        assert "note-02.md" not in indexed, "an item prepared after the stop was committed"
        # The ones before it may or may not have landed before the signal did.
        assert indexed <= {"note-00.md", "note-01.md"}
    finally:
        engine.close()


def test_the_signal_reaches_pacing_backoff_and_page_ranges() -> None:
    from pheasant.sync.web_pacing import HostPacer

    class _Sync:
        min_request_interval_seconds = 30.0
        respect_robots = False
        max_items_per_run = None

    class _Source:
        sync = _Sync()

    signal = cancel_scope.StopSignal()
    pacer = HostPacer(_Source(), lambda _url: None)
    with cancel_scope.bound(signal):
        pacer.admit("https://example.test/a")  # the first request never waits
        threading.Timer(0.2, signal.set, kwargs={"requested_by": "tester"}).start()
        began = time.monotonic()
        with pytest.raises(cancel_scope.Stopped, match="tester"):
            pacer.admit("https://example.test/b")  # a 30 s slot
        assert time.monotonic() - began < 5

    # Outside a sync, the same call is the clock it always was.
    assert cancel_scope.current() is None
    began = time.monotonic()
    cancel_scope.sleep(0.05)
    assert time.monotonic() - began >= 0.04


def test_a_stopped_pdf_range_is_never_read_locally_instead(monkeypatch: pytest.MonkeyPatch) -> None:
    from pheasant.ingestion.extractor import AutoExtractor
    from pheasant.ingestion.pdf_write import text_pdf
    from pheasant.sync import pdf_split
    from pheasant.sync.worker_pool import AllWorkersFailed

    monkeypatch.setenv("PHEASANT_INDEX_WORKER_TOKEN", "t")
    content = text_pdf([[f"Page {number} text."] for number in range(30)])
    signal = cancel_scope.StopSignal()
    sent: list[int] = []
    local: list[Any] = []

    class _Pool:
        def extract_pages(self, content: bytes, first: int, stop: int, **_kw: Any):
            sent.append(first)
            signal.set("tester")  # the cancel lands while this range is out
            raise AllWorkersFailed({"grpc://worker": "abandoned"})

        def close(self) -> None:
            pass

    real_local = pdf_split.pdf_page_texts
    monkeypatch.setattr(
        pdf_split, "pdf_page_texts", lambda *a, **k: local.append(a[1:]) or real_local(*a, **k)
    )
    provider = pdf_split.RemotePdfPages(
        ["grpc://worker:8766"],
        "PHEASANT_INDEX_WORKER_TOKEN",
        pages_per_task=5,
        timeout=30,
        max_parallel_files=1,
        pool_factory=lambda _urls, _token: _Pool(),
    )
    with cancel_scope.bound(signal), pytest.raises(cancel_scope.Stopped):
        AutoExtractor(pdf_pages=provider).extract(content, "long.pdf")
    assert sent == [0], "ranges were sent after the stop"
    assert local == [], "a stopped range was read locally instead"


# --------------------------------------------------------------------------
# A child that cannot stop is ended by its supervisor
# --------------------------------------------------------------------------


def _config_file(root: Path, corpus: Path, **kwargs: Any) -> Path:
    import yaml

    payload = _payload(root, corpus, **kwargs)
    payload["sync"]["concurrency"]["cancel_grace_seconds"] = 3
    path = root / "pheasant.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def _await(condition: Any, timeout: float = 90.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def _ended_and_healed(engine: SyncEngine, result: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert result.status == "cancelled", result
    outcome = result.details["cancelled"]
    assert outcome["forced"] is True and outcome["requested_by"] == "tester"
    event = _events(engine)[0]
    assert event["details"]["cancelled"]["forced"] is True
    # The run's row stays (its successor must learn it died) but is not live.
    assert engine.state.rows("SELECT 1 FROM sync_runs WHERE source_id='docs'")
    assert running(engine.state, "docs") is None
    assert engine.state.rows("SELECT 1 FROM source_leases WHERE source_id='docs'") == []
    assert engine.state.get_source("docs")["last_status"] == "cancelled"
    committed = _artifacts(engine)
    assert 0 < committed < FILES

    monkeypatch.delenv("PHEASANT_TEST_SLOW_SYNC_MS")
    healed = engine.sync_source("docs")
    assert healed.status == "healthy" and _artifacts(engine) == FILES
    # Every committed chunk has its vector, whatever the ended worker had queued.
    assert engine.vectors is not None
    ids = [r["id"] for r in engine.state.rows("SELECT id FROM chunks WHERE source_id='docs'")]
    assert engine.vectors.store.existing_ids(ids) == set(ids)


def test_a_worker_that_does_not_stop_is_ended_and_the_next_sync_heals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _corpus(tmp_path)
    config_file = _config_file(tmp_path, corpus)
    # Each committed item is followed by an uninterruptible sleep: the child
    # sees the cancel and cannot act on it, which is the case being tested.
    monkeypatch.setenv("PHEASANT_TEST_SLOW_SYNC_MS", "600000")
    engine = SyncEngine(load_config(config_file))
    try:
        worker = WorkerBackedEngine(engine, config_file)
        outcome: list[Any] = []
        thread = threading.Thread(target=lambda: outcome.append(worker.sync_source("docs")))
        began = time.monotonic()
        thread.start()
        _await(lambda: _artifacts(engine) >= 1 and running(engine.state, "docs"), what="a commit")
        assert cancel_source(engine.state, "docs", requested_by="tester")
        thread.join(120)
        assert outcome, "the supervisor never returned"
        assert time.monotonic() - began < 110
        _ended_and_healed(engine, outcome[0], monkeypatch)
    finally:
        engine.close()


def test_the_fleet_writer_ends_its_interpreter_and_the_task_is_not_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nats_queue_settings: dict[str, Any]
) -> None:
    from pheasant.sync import writer

    corpus = _corpus(tmp_path)
    config_file = _config_file(tmp_path, corpus, queue=nats_queue_settings)
    monkeypatch.setenv("PHEASANT_TEST_SLOW_SYNC_MS", "600000")
    engine = SyncEngine(load_config(config_file))
    try:
        worker = WorkerBackedEngine(engine, config_file)
        _publish(engine.config, engine.state)
        queue = queue_from_config(engine.config, engine.state)
        task = queue.claim("indexer")
        assert task is not None
        outcome: list[Any] = []
        thread = threading.Thread(target=lambda: outcome.append(worker.apply_index_task(task)))
        thread.start()
        _await(lambda: _artifacts(engine) >= 1 and running(engine.state, "docs"), what="a commit")
        assert cancel_source(engine.state, "docs", requested_by="tester")
        thread.join(120)
        assert outcome, "the writer supervisor never returned"
        queue.ack(task)
        request_id = task.payload["request_id"]
        assert (
            engine.state.rows("SELECT state FROM sync_requests WHERE id=?", (request_id,))[0][
                "state"
            ]
            == "cancelled"
        )
        # A redelivery of the same task (an ack lost after all) is not rerun.
        assert request_ledger.admit(engine.state, task, "indexer-b").verdict == "cancelled"
        queue.close()
        writer.close()
        _ended_and_healed(engine, outcome[0], monkeypatch)
    finally:
        writer.close()
        engine.close()


def test_a_worker_that_is_stopping_is_not_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the child reports it is cancelling or saving, the clock stops."""

    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        now = [0.0]
        watch = supervision.CancelWatch(
            engine.state, "docs", None, grace_s=5, poll_interval_s=0, clock=lambda: now[0]
        )
        monkeypatch.setenv(cancellation.SUPERVISOR_ENV, watch.token)
        with SyncRun(engine.state, "docs") as run:
            assert run.run_id.startswith(f"{watch.token}.")
            assert not watch.overdue()
            assert watch.owner == run.owner
            cancel_source(engine.state, "docs", requested_by="tester")
            assert not watch.overdue()  # seen now; the grace period starts here
            now[0] = 4.9
            assert not watch.overdue()
            now[0] = 5.0
            assert watch.overdue()
            watch.note_phase("cancelling")
            assert not watch.overdue()
        assert not supervision.CancelWatch(engine.state, "docs", grace_s=0).overdue()
    finally:
        engine.close()


def test_a_child_waiting_for_another_runs_lock_is_not_ended_for_its_cancel(
    tmp_path: Path,
) -> None:
    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        watch = supervision.CancelWatch(engine.state, "docs", None, grace_s=0.01, poll_interval_s=0)
        # Our child has no run yet; this one is somebody else's (no token).
        with SyncRun(engine.state, "docs"):
            cancel_source(engine.state, "docs", requested_by="tester")
            time.sleep(0.05)
            assert not watch.overdue()
    finally:
        engine.close()


# --------------------------------------------------------------------------
# What a killed run leaves behind is healed by the next one
# --------------------------------------------------------------------------


def test_chunks_committed_without_vectors_are_embedded_by_the_next_run(tmp_path: Path) -> None:
    engine = SyncEngine(_config(tmp_path, _corpus(tmp_path)))
    try:
        assert engine.vectors is not None
        engine.sync_source("docs")
        ids = [r["id"] for r in engine.state.rows("SELECT id FROM chunks WHERE source_id='docs'")]
        assert engine.vectors.store.existing_ids(ids) == set(ids)
        engine.vectors.prune_source("docs", set(ids[2:]))  # two chunks lose their vectors
        assert engine.vectors.store.existing_ids(ids) == set(ids[2:])
        # The run that lost them never exited: its row is still there.
        engine.state.execute(
            "INSERT INTO sync_runs(source_id, run_id, owner, started_at, heartbeat_at) "
            "VALUES('docs','dead','gone:1',?,?)",
            (cancellation.EXPIRED_HEARTBEAT, cancellation.EXPIRED_HEARTBEAT),
        )
        result = engine.sync_source("docs")
        assert result.status == "healthy" and result.indexed_artifacts == 0
        assert engine.vectors.store.existing_ids(ids) == set(ids)
        # And a run with a clean predecessor makes no embedder call for it.
        assert engine.heal_vectors("docs") == 0
    finally:
        engine.close()
