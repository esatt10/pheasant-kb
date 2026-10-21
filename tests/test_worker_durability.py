"""Phase 35.5 — durable dispatch to remote preparation workers.

The property under test throughout is that remote preparation is an
*optimization*: no arrangement of worker failures may change what a sync
produces, only how long it takes. So most of these tests break something on
purpose and then assert the index is still correct.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.sync.engine import SyncEngine
from pheasant.sync.remote_worker import (
    DeadlineExceeded,
    ResultCache,
    prepare_batch_tasks,
    task_payload,
)
from pheasant.sync.worker_pool import (
    BREAKER_RESET_SECONDS,
    FAILURE_THRESHOLD,
    MAX_ATTEMPTS,
    AllWorkersFailed,
    TaskRejected,
    WorkerPool,
    _backoff,
    _idempotency_key,
    _parse_retry_after,
    _Retryable,
    redact_endpoint,
)
from pheasant.sync.worker_transport import HttpTransport

# --------------------------------------------------------------------------
# Fixtures and fakes
# --------------------------------------------------------------------------


def _write_docs(workspace: Path, count: int = 6) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (workspace / f"note-{index}.md").write_text(
            f"# Note {index}\n\nThe deployment gateway rotates credentials nightly.\n"
            f"Runbook step {index} describes the failover drill.\n",
            encoding="utf-8",
        )


def _config(tmp_path: Path, workspace: Path, *, state_name: str = "state") -> PheasantConfig:
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "durability",
                "state_path": str(tmp_path / state_name),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / f"{state_name}-exports"),
            },
            "storage": {"graph_snapshots": False},
            "sync": {"concurrency": {"lock_timeout_seconds": 1}},
            "sources": [
                {
                    "name": "docs",
                    "type": "markdown_folder",
                    "path": str(workspace),
                    "include": ["**/*.md"],
                }
            ],
        }
    )


class FakeTransport:
    """A transport whose behaviour per endpoint is scripted by the test."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        #: endpoint -> list of outcomes, consumed in order and then repeated
        #: from the last entry. An outcome is either an exception to raise or
        #: the literal string "ok".
        self.script = {url: list(outcomes) for url, outcomes in script.items()}
        self.calls: list[str] = []
        self.discarded = 0
        self.lock = threading.Lock()

    def post(
        self,
        connection: Any,
        url: str,
        tasks: list[dict[str, Any]],
        keys: list[str],
        *,
        token: str,
        timeout: float,
        deadline_seconds: float | None,
    ) -> Any:
        with self.lock:
            self.calls.append(url)
            outcomes = self.script.get(url) or ["ok"]
            outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return {"results": prepare_batch_tasks(tasks, keys)}, connection or object()

    def discard(self, connection: Any) -> None:
        self.discarded += 1


def _artifacts(engine: SyncEngine) -> list[dict[str, Any]]:
    """Every indexed artifact for the fixture source, in stable-ID order."""

    return [
        dict(row)
        for row in engine.state.rows(
            "SELECT a.id AS id, a.relative_path AS relative_path, a.sha256 AS sha256, "
            "a.type AS type, "
            "(SELECT COUNT(*) FROM chunks c WHERE c.artifact_id=a.id) AS chunk_count "
            "FROM artifacts a WHERE a.source_id=? ORDER BY a.id",
            ("docs",),
        )
    ]


def _task(tmp_path: Path, name: str = "note-0.md") -> dict[str, Any]:
    """One real preparation task, built the way the engine builds them."""

    workspace = tmp_path / "workspace"
    _write_docs(workspace, count=1)
    (workspace / name).write_text("# Title\n\nBody text for the worker.\n", encoding="utf-8")
    config = _config(tmp_path, workspace)
    source = config.sources[0]

    from pheasant.sync.connectors import connector_for_source

    connector = connector_for_source(source, config)
    item = next(item for item in connector.list_items() if item.relative_path == name)
    payload = connector.read_item(item)
    return task_payload(source, item, payload, None)


# --------------------------------------------------------------------------
# Retry, backoff, Retry-After
# --------------------------------------------------------------------------


def test_a_transient_failure_is_retried_and_then_succeeds(tmp_path: Path) -> None:
    transport = FakeTransport({"https://a": [_Retryable("boom"), "ok"]})
    slept: list[float] = []
    pool = WorkerPool(["https://a"], "token", transport=transport, sleep=slept.append)

    results = pool.prepare_batch([_task(tmp_path)])

    assert len(results) == 1 and results[0] is not None
    assert transport.calls == ["https://a", "https://a"]
    assert len(slept) == 1 and 0.0 <= slept[0] <= 1.0


def test_retry_stops_at_the_attempt_budget(tmp_path: Path) -> None:
    """Two budgets bound a retry, and the tighter one wins.

    ``MAX_ATTEMPTS`` bounds the whole task across endpoints; the breaker
    bounds one endpoint. With a single endpoint the breaker is tighter, so a
    dead lone worker costs three round trips per batch and not four.
    """

    transport = FakeTransport({"https://a": [_Retryable("always")]})
    pool = WorkerPool(["https://a"], "token", transport=transport, sleep=lambda _s: None)

    with pytest.raises(AllWorkersFailed) as caught:
        pool.prepare_batch([_task(tmp_path)])

    assert len(transport.calls) == FAILURE_THRESHOLD
    assert "always" in str(caught.value)

    # Across two endpoints the cross-task budget is what binds.
    transport = FakeTransport({"https://a": [_Retryable("x")], "https://b": [_Retryable("y")]})
    pool = WorkerPool(
        ["https://a", "https://b"], "token", transport=transport, sleep=lambda _s: None
    )
    with pytest.raises(AllWorkersFailed):
        pool.prepare_batch([_task(tmp_path)])
    assert len(transport.calls) == MAX_ATTEMPTS


def test_retry_after_overrides_the_jittered_backoff() -> None:
    assert _parse_retry_after("2.5") == 2.5
    assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") is None
    assert _parse_retry_after("-1") is None
    assert _parse_retry_after(None) is None
    # A worker asking for ten minutes is telling us to use a different one.
    assert _parse_retry_after("600") == 30.0
    assert _backoff(3, 4.0) == 4.0
    # Full jitter: the window grows, and the floor stays at zero so retries
    # from many files in flight decorrelate instead of arriving together.
    assert 0.0 <= _backoff(1, None) <= 0.5
    assert 0.0 <= _backoff(5, None) <= 8.0


def test_a_worker_asking_for_retry_after_is_obeyed(tmp_path: Path) -> None:
    transport = FakeTransport({"https://a": [_Retryable("busy", retry_after=1.25), "ok"]})
    slept: list[float] = []
    pool = WorkerPool(["https://a"], "token", transport=transport, sleep=slept.append)

    pool.prepare_batch([_task(tmp_path)])

    assert slept == [1.25]


# --------------------------------------------------------------------------
# Failover and the circuit breaker
# --------------------------------------------------------------------------


def test_a_dead_endpoint_fails_over_to_a_live_one(tmp_path: Path) -> None:
    transport = FakeTransport({"https://dead": [_Retryable("refused")], "https://live": ["ok"]})
    pool = WorkerPool(
        ["https://dead", "https://live"], "token", transport=transport, sleep=lambda _s: None
    )

    results = pool.prepare_batch([_task(tmp_path)])

    assert results[0] is not None
    assert "https://live" in transport.calls


def test_the_breaker_takes_a_dead_endpoint_out_of_rotation(tmp_path: Path) -> None:
    """The point of the breaker: stop re-proving the same thing per file."""

    transport = FakeTransport({"https://dead": [_Retryable("refused")], "https://live": ["ok"]})
    clock = iter(range(1, 10_000))
    pool = WorkerPool(
        ["https://dead", "https://live"],
        "token",
        transport=transport,
        sleep=lambda _s: None,
        clock=lambda: float(next(clock)),
    )

    task = _task(tmp_path)
    for _ in range(FAILURE_THRESHOLD + 3):
        pool.prepare_batch([task])

    dead_calls = transport.calls.count("https://dead")
    assert dead_calls == FAILURE_THRESHOLD, transport.calls
    assert pool.endpoint_health() == {"https://dead": False, "https://live": True}


def test_a_tripped_breaker_lets_one_probe_through_after_the_cooldown(tmp_path: Path) -> None:
    """A single failing endpoint: open, stay open for the cooldown, then probe."""

    transport = FakeTransport({"https://flaky": [_Retryable("down")]})
    now = [100.0]
    pool = WorkerPool(
        ["https://flaky"],
        "token",
        transport=transport,
        sleep=lambda _s: None,
        clock=lambda: now[0],
    )

    task = _task(tmp_path)
    with pytest.raises(AllWorkersFailed):
        pool.prepare_batch([task])
    tripped = transport.calls.count("https://flaky")
    assert tripped == FAILURE_THRESHOLD
    assert pool.endpoint_health()["https://flaky"] is False

    with pytest.raises(AllWorkersFailed) as caught:
        pool.prepare_batch([task])
    assert transport.calls.count("https://flaky") == tripped, "open breaker still dispatched"
    assert "circuit open" in str(caught.value)

    now[0] += BREAKER_RESET_SECONDS + 1
    with pytest.raises(AllWorkersFailed):
        pool.prepare_batch([task])
    assert transport.calls.count("https://flaky") > tripped, "cooldown did not probe"


def test_a_recovered_endpoint_closes_its_breaker(tmp_path: Path) -> None:
    transport = FakeTransport({"https://a": [_Retryable("down"), _Retryable("down"), "ok"]})
    pool = WorkerPool(["https://a"], "token", transport=transport, sleep=lambda _s: None)

    pool.prepare_batch([_task(tmp_path)])

    assert pool.endpoint_health() == {"https://a": True}
    assert pool.stats()[0]["circuit_open"] is False


def test_a_cooled_down_endpoint_that_was_not_dispatched_to_stays_reachable(
    tmp_path: Path,
) -> None:
    """The half-open flag must not survive the batch that set it.

    Marking every cooled-down candidate as `probing` up front leaked: only the
    endpoint actually dispatched to ever cleared it, so when an earlier one in
    the rotation answered, the rest kept `probing=True` — and `available()` is
    False while it is set, which excluded a perfectly healthy worker from the
    pool for the rest of the process's life and reported it down to
    `pheasant_worker_up`. The flag is held around the dispatch instead.
    """

    task = _task(tmp_path)
    transport = FakeTransport(
        {"https://a": [_Retryable("down")], "https://b": [_Retryable("down")]}
    )
    now = [100.0]
    pool = WorkerPool(
        ["https://a", "https://b"],
        "token",
        transport=transport,
        sleep=lambda _s: None,
        clock=lambda: now[0],
    )

    # Trip both, then let the cooldown expire so both are probe candidates.
    for _ in range(3):
        with pytest.raises(AllWorkersFailed):
            pool.prepare_batch([task])
    assert pool.endpoint_health() == {"https://a": False, "https://b": False}

    now[0] += BREAKER_RESET_SECONDS + 1
    transport.script = {"https://a": ["ok"], "https://b": ["ok"]}
    pool.prepare_batch([task])  # one of them answers; the other is untouched

    # Both must be reachable again. Before the fix exactly one was, and which
    # one depended on the rotation cursor.
    assert pool.endpoint_health() == {"https://a": True, "https://b": True}
    served = set(transport.calls)
    for _ in range(4):
        pool.prepare_batch([task])
        served |= set(transport.calls)
    assert served >= {"https://a", "https://b"}, "an endpoint was permanently excluded"


def test_a_transport_raising_something_other_than_a_worker_error_is_a_worker_failure(
    tmp_path: Path,
) -> None:
    """Not every transport failure is a `RemoteWorkerError`.

    A corrupt base64 payload raises `binascii.Error`, a truncated body raises
    `json.JSONDecodeError`, and a gRPC channel raises `grpc.RpcError` — none of
    them subclasses of the pool's own exception. Letting one out aborts the
    whole sync over a single sick worker, which is the single point of failure
    this pool exists to remove. It counts against the breaker instead, and the
    fleet moves on.
    """

    import binascii

    transport = FakeTransport(
        {"https://bad": [binascii.Error("invalid padding")], "https://good": ["ok"]}
    )
    pool = WorkerPool(
        ["https://bad", "https://good"], "token", transport=transport, sleep=lambda _s: None
    )

    # Several batches, because the rotation decides which endpoint is asked
    # first — the point is that reaching the broken one is survivable, not
    # which call reaches it.
    task = _task(tmp_path)
    for _ in range(3):
        results = pool.prepare_batch([task])
        assert len(results) == 1 and results[0] is not None
    assert "https://bad" in transport.calls, "the broken endpoint was never tried"

    # And when every endpoint is broken the caller gets the pool's own error —
    # so the engine falls back to local preparation instead of the sync dying.
    only_bad = FakeTransport({"https://bad": [ValueError("garbage on the wire")]})
    pool = WorkerPool(["https://bad"], "token", transport=only_bad, sleep=lambda _s: None)
    with pytest.raises(AllWorkersFailed) as caught:
        pool.prepare_batch([_task(tmp_path)])
    assert "ValueError" in str(caught.value)


def test_a_refused_task_does_not_fail_over(tmp_path: Path) -> None:
    """Every worker runs the same refusal logic, so failing over buys nothing."""

    rejection = TaskRejected("taxonomy not supported")
    transport = FakeTransport({"https://a": [rejection], "https://b": [rejection]})
    pool = WorkerPool(["https://a", "https://b"], "token", transport=transport)

    with pytest.raises(TaskRejected):
        pool.prepare_batch([_task(tmp_path)])

    assert len(transport.calls) == 1, "a refusal was retried at another worker"
    # The worker is healthy; it is the task that is not. A refusal must not
    # count towards the breaker or one bad file would take a worker offline.
    assert [entry["failures"] for entry in pool.stats()] == [0, 0]
    assert not any(entry["circuit_open"] for entry in pool.stats())


def test_no_endpoints_configured_is_reported_not_crashed() -> None:
    pool = WorkerPool([], "token", transport=FakeTransport({}))
    with pytest.raises(AllWorkersFailed):
        pool.prepare_batch([{"source": {}, "item": {}, "payload": {}}])
    assert pool.prepare_batch([]) == []


def test_endpoint_labels_drop_userinfo() -> None:
    """Endpoint labels are scraped and logged; credentials must not ride along."""

    assert redact_endpoint("https://user:pass@worker:8765/x") == "https://worker:8765/x"
    assert redact_endpoint("https://worker:8765") == "https://worker:8765"
    pool = WorkerPool(["https://u:p@w"], "token", transport=FakeTransport({}))
    assert list(pool.endpoint_health()) == ["https://w"]


def test_worker_health_reaches_the_metrics_registry(tmp_path: Path) -> None:
    from pheasant.telemetry import metrics

    metrics.register_default_metrics("test")
    transport = FakeTransport({"https://dead": [_Retryable("no")], "https://live": ["ok"]})
    clock = iter(range(1, 10_000))
    pool = WorkerPool(
        ["https://dead", "https://live"],
        "token",
        transport=transport,
        sleep=lambda _s: None,
        clock=lambda: float(next(clock)),
    )
    task = _task(tmp_path)
    # Rotation means the dead endpoint is only tried when it comes up first,
    # so the breaker trips after more passes than there are failures.
    for _ in range(4 * FAILURE_THRESHOLD):
        pool.prepare_batch([task])
    pool.publish_health()

    rendered = metrics.REGISTRY.render()
    assert 'pheasant_worker_up{endpoint="https://dead"} 0' in rendered
    assert 'pheasant_worker_up{endpoint="https://live"} 1' in rendered


# --------------------------------------------------------------------------
# Deadlines and idempotency
# --------------------------------------------------------------------------


def test_a_passed_deadline_is_not_dispatched(tmp_path: Path) -> None:
    transport = FakeTransport({"https://a": ["ok"]})
    pool = WorkerPool(["https://a"], "token", transport=transport, clock=lambda: 1000.0)

    with pytest.raises(AllWorkersFailed):
        pool.prepare_batch([_task(tmp_path)], deadline=999.0)

    assert transport.calls == []


def test_the_remaining_budget_travels_with_the_request(tmp_path: Path) -> None:
    seen: list[float | None] = []

    class Recorder(FakeTransport):
        def post(self, connection, url, tasks, keys, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(kwargs["deadline_seconds"])
            assert kwargs["timeout"] <= 60
            return super().post(connection, url, tasks, keys, **kwargs)

    pool = WorkerPool(
        ["https://a"],
        "token",
        timeout=60.0,
        transport=Recorder({"https://a": ["ok"]}),
        clock=lambda: 1000.0,
    )
    pool.prepare_batch([_task(tmp_path)], deadline=1010.0)

    assert seen == [10.0]


def test_the_worker_stops_a_batch_whose_caller_gave_up(tmp_path: Path) -> None:
    task = _task(tmp_path)
    budget = iter([5.0, -1.0, -1.0])

    with pytest.raises(DeadlineExceeded) as caught:
        prepare_batch_tasks([task, task, task], ["a", "b", "c"], deadline=lambda: next(budget))

    assert "after 2 of 3" in str(caught.value)


def test_work_done_before_the_deadline_survives_in_the_cache(tmp_path: Path) -> None:
    """The abort discards the *results*, not the *work*.

    The response contract is one result per task — a short list is rejected as
    malformed — so a partial batch cannot be returned. What must not happen is
    the finished parses being thrown away too, which would make the retry
    after a 408 cost exactly as much as the attempt that timed out.
    """

    task = _task(tmp_path)
    cache = ResultCache(maxsize=8)
    # Checked between tasks only, so the first call is the one before task 2.
    budget = iter([-1.0])

    with pytest.raises(DeadlineExceeded):
        prepare_batch_tasks(
            [task, task], ["first", "second"], cache=cache, deadline=lambda: next(budget)
        )

    found, cached = cache.get("first")
    assert found and cached is not None, "the completed parse was discarded"
    assert cache.get("second")[0] is False, "a task that never ran was cached anyway"

    # And the retry is a lookup: the second attempt reuses it rather than
    # re-parsing, which is the whole point of keeping it.
    before = cache.hits
    results = prepare_batch_tasks([task, task], ["first", "second"], cache=cache)
    assert len(results) == 2
    assert cache.hits == before + 1


def test_idempotency_keys_are_content_addressed(tmp_path: Path) -> None:
    task = _task(tmp_path)
    assert _idempotency_key(task) == _idempotency_key(json.loads(json.dumps(task)))

    other = json.loads(json.dumps(task))
    other["payload"]["sha256"] = "0" * 64
    assert _idempotency_key(other) != _idempotency_key(task)

    # Two identical files in one source are two artifacts, so the path is part
    # of the key even though the bytes are not.
    renamed = json.loads(json.dumps(task))
    renamed["item"]["relative_path"] = "elsewhere.md"
    assert _idempotency_key(renamed) != _idempotency_key(task)

    # The same bytes read under other parser settings are other text, so a
    # result cached before ``math: latex`` was switched on is not reused.
    latex = json.loads(json.dumps(task))
    latex["extractor"] = {"math": "latex"}
    plain = json.loads(json.dumps(task))
    plain["extractor"] = {"math": "off"}
    assert len({_idempotency_key(latex), _idempotency_key(plain), _idempotency_key(task)}) == 3


def test_a_retried_task_is_answered_from_cache_not_reparsed(tmp_path: Path) -> None:
    task = _task(tmp_path)
    key = _idempotency_key(task)
    cache = ResultCache(maxsize=4)

    first = prepare_batch_tasks([task], [key], cache=cache)
    second = prepare_batch_tasks([task], [key], cache=cache)

    assert first == second
    assert cache.hits == 1 and cache.misses == 1


def test_the_result_cache_is_bounded_and_lru(tmp_path: Path) -> None:
    cache = ResultCache(maxsize=2)
    cache.put("a", {"id": "a"})
    cache.put("b", {"id": "b"})
    cache.get("a")  # refresh a, so b is now the least recently used
    cache.put("c", {"id": "c"})

    assert len(cache) == 2
    assert cache.get("a")[0] is True
    assert cache.get("b")[0] is False


# --------------------------------------------------------------------------
# The HTTP transport itself, over a real socket
# --------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:  # pragma: no cover - quiet tests
        pass

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        server: Any = self.server
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        server.requests.append(
            {
                "path": self.path,
                "body": json.loads(raw.decode("utf-8")) if raw else None,
                "headers": dict(self.headers),
                "connection_id": id(self.connection),
            }
        )
        status, body = server.responder(self.path, server.requests[-1])
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        for name, value in (server.extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture
def worker_server():  # type: ignore[no-untyped-def]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []  # type: ignore[attr-defined]
    server.extra_headers = {}  # type: ignore[attr-defined]
    server.responder = lambda path, request: (  # type: ignore[attr-defined]
        200,
        {"results": [{"parsed": None} for _ in (request["body"] or {}).get("tasks", [{}])]},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _url(server: Any) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def test_the_http_transport_reuses_one_connection_across_a_batch_run(
    tmp_path: Path, worker_server: Any
) -> None:
    """Keep-alive is the point: 50k files used to mean 50k handshakes."""

    pool = WorkerPool([_url(worker_server)], "token", timeout=10.0)
    task = _task(tmp_path)
    try:
        for _ in range(5):
            pool.prepare_batch([task])
    finally:
        pool.close()

    assert len(worker_server.requests) == 5
    connection_ids = {request["connection_id"] for request in worker_server.requests}
    assert len(connection_ids) == 1, "each request opened a new socket"
    assert all(
        request["path"].endswith("/internal/indexing/prepare-batch")
        for request in worker_server.requests
    )


def test_the_http_transport_sends_auth_deadline_and_idempotency_headers(
    tmp_path: Path, worker_server: Any
) -> None:
    import time as _time

    task = _task(tmp_path)
    pool = WorkerPool([_url(worker_server)], "s3cret", timeout=10.0)
    try:
        pool.prepare_batch([task], deadline=_time.monotonic() + 30)
    finally:
        pool.close()

    headers = worker_server.requests[0]["headers"]
    assert headers["Authorization"] == "Bearer s3cret"
    assert 0 < float(headers["X-Pheasant-Deadline-Seconds"]) <= 30
    assert headers["X-Pheasant-Idempotency-Key"] == _idempotency_key(task)
    body = worker_server.requests[0]["body"]
    assert body["idempotency_keys"] == [_idempotency_key(task)]


def test_a_worker_without_the_batch_route_falls_back_to_the_single_task_path(
    tmp_path: Path, worker_server: Any
) -> None:
    """A coordinator upgraded ahead of its fleet must keep working."""

    def responder(path: str, request: dict[str, Any]) -> tuple[int, Any]:
        if path.endswith("prepare-batch"):
            return 404, {"detail": "Not Found"}
        return 200, {"parsed": None}

    worker_server.responder = responder
    pool = WorkerPool([_url(worker_server)], "token", timeout=10.0)
    task = _task(tmp_path)
    try:
        assert pool.prepare_batch([task, task]) == [None, None]
        assert pool.prepare_batch([task]) == [None]
    finally:
        pool.close()

    paths = [request["path"] for request in worker_server.requests]
    assert paths[0].endswith("prepare-batch")
    assert paths[1:] == ["/internal/indexing/prepare"] * 3
    # The 404 is learned once, not re-probed per batch.
    assert paths.count("/internal/indexing/prepare-batch") == 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, _Retryable),
        (503, _Retryable),
        (408, _Retryable),
        (401, RuntimeError),
        (422, TaskRejected),
        (413, TaskRejected),
    ],
)
def test_http_status_classification(
    tmp_path: Path, worker_server: Any, status: int, expected: type
) -> None:
    worker_server.responder = lambda path, request: (status, {"detail": "nope"})
    transport = HttpTransport()
    with pytest.raises(expected):
        transport.post(
            None,
            _url(worker_server),
            [_task(tmp_path)],
            ["k"],
            token="token",
            timeout=10.0,
            deadline_seconds=None,
        )


def test_a_server_that_closes_the_connection_is_not_counted_as_a_failure(
    tmp_path: Path, worker_server: Any
) -> None:
    """An idle-timeout close looks exactly like a dead worker if unhandled."""

    worker_server.extra_headers = {"Connection": "close"}
    pool = WorkerPool([_url(worker_server)], "token", timeout=10.0)
    task = _task(tmp_path)
    try:
        for _ in range(3):
            pool.prepare_batch([task])
    finally:
        pool.close()

    assert pool.stats()[0]["failures"] == 0
    assert len(worker_server.requests) == 3


def test_a_malformed_result_count_is_retryable(tmp_path: Path, worker_server: Any) -> None:
    worker_server.responder = lambda path, request: (200, {"results": []})
    pool = WorkerPool([_url(worker_server)], "token", timeout=10.0, sleep=lambda _s: None)
    try:
        with pytest.raises(AllWorkersFailed) as caught:
            pool.prepare_batch([_task(tmp_path)])
    finally:
        pool.close()
    assert "0 results for 1 tasks" in str(caught.value)


# --------------------------------------------------------------------------
# The worker endpoint
# --------------------------------------------------------------------------


def _worker_app(tmp_path: Path, monkeypatch: Any) -> TestClient:
    workspace = tmp_path / "workspace"
    _write_docs(workspace, count=1)
    config = _config(tmp_path, workspace, state_name="worker-state")
    config.sync.concurrency.remote_worker_enabled = True
    monkeypatch.setenv("PHEASANT_INDEX_WORKER_TOKEN", "worker-token")
    return TestClient(create_app(config))


def test_the_batch_endpoint_is_disabled_by_default(tmp_path: Path, monkeypatch: Any) -> None:
    workspace = tmp_path / "workspace"
    _write_docs(workspace, count=1)
    config = _config(tmp_path, workspace, state_name="off-state")
    monkeypatch.setenv("PHEASANT_INDEX_WORKER_TOKEN", "worker-token")
    client = TestClient(create_app(config))

    response = client.post(
        "/internal/indexing/prepare-batch",
        json={"tasks": []},
        headers={"Authorization": "Bearer worker-token"},
    )
    assert response.status_code == 404


def test_the_batch_endpoint_requires_the_bearer_token(tmp_path: Path, monkeypatch: Any) -> None:
    client = _worker_app(tmp_path, monkeypatch)
    payload = {"tasks": [_task(tmp_path / "task")]}

    assert client.post("/internal/indexing/prepare-batch", json=payload).status_code == 401
    assert (
        client.post(
            "/internal/indexing/prepare-batch",
            json=payload,
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )


def test_the_batch_endpoint_prepares_and_caches(tmp_path: Path, monkeypatch: Any) -> None:
    client = _worker_app(tmp_path, monkeypatch)
    task = _task(tmp_path / "task")
    key = _idempotency_key(task)
    headers = {"Authorization": "Bearer worker-token"}

    first = client.post(
        "/internal/indexing/prepare-batch",
        json={"tasks": [task], "idempotency_keys": [key]},
        headers=headers,
    )
    assert first.status_code == 200
    assert first.json()["results"][0]["parsed"]["relative_path"] == "note-0.md"
    assert first.json()["cache"]["misses"] == 1

    second = client.post(
        "/internal/indexing/prepare-batch",
        json={"tasks": [task], "idempotency_keys": [key]},
        headers=headers,
    )
    assert second.json()["results"] == first.json()["results"]
    assert second.json()["cache"]["hits"] == 1


def test_the_batch_endpoint_refuses_a_passed_deadline(tmp_path: Path, monkeypatch: Any) -> None:
    client = _worker_app(tmp_path, monkeypatch)
    response = client.post(
        "/internal/indexing/prepare-batch",
        json={"tasks": [_task(tmp_path / "task")], "deadline_seconds": -0.5},
        headers={"Authorization": "Bearer worker-token"},
    )
    assert response.status_code == 408


def test_the_batch_endpoint_caps_batch_size(tmp_path: Path, monkeypatch: Any) -> None:
    from pheasant.api.app import MAX_PREPARE_BATCH

    client = _worker_app(tmp_path, monkeypatch)
    task = _task(tmp_path / "task")
    response = client.post(
        "/internal/indexing/prepare-batch",
        json={"tasks": [task] * (MAX_PREPARE_BATCH + 1)},
        headers={"Authorization": "Bearer worker-token"},
    )
    assert response.status_code == 413


def test_the_batch_endpoint_refuses_a_task_it_cannot_prepare(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client = _worker_app(tmp_path, monkeypatch)
    task = _task(tmp_path / "task")
    task["item"]["relative_path"] = "diagram.png"
    response = client.post(
        "/internal/indexing/prepare-batch",
        json={"tasks": [task]},
        headers={"Authorization": "Bearer worker-token"},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# End to end: a failing fleet must not change the index
# --------------------------------------------------------------------------


def _sync_with_transport(
    tmp_path: Path,
    monkeypatch: Any,
    transport_factory: Any,
    name: str,
    *,
    on_progress: Any = None,
    files: int = 6,
):
    workspace = tmp_path / f"workspace-{name}"
    _write_docs(workspace, count=files)
    config = _config(tmp_path, workspace, state_name=f"state-{name}")
    config.sync.concurrency.file_executor = "remote"
    config.sync.concurrency.remote_worker_urls = ["https://a", "https://b"]
    config.sync.concurrency.max_parallel_files = 3
    config.sync.concurrency.remote_worker_batch_size = 2
    monkeypatch.setenv("PHEASANT_INDEX_WORKER_TOKEN", "token")

    from pheasant.sync import worker_transport

    monkeypatch.setattr(worker_transport, "HttpTransport", transport_factory)
    engine = SyncEngine(config)
    try:
        result = engine.sync_source("docs", "full", on_progress=on_progress)
        artifacts = _artifacts(engine)
    finally:
        engine.close()
    return result, artifacts


def test_batching_does_not_disturb_discovery_order(tmp_path: Path, monkeypatch: Any) -> None:
    """Rule 3/4: a batch is a transport grouping, never a reordering.

    Committing in stable discovery order is where deterministic graph bytes
    come from, so the property has to survive the change from one-file-per-
    request to several. Batches are contiguous runs of positions and the
    look-ahead window is FIFO, which is what makes that true — this pins it.
    """

    seen: list[tuple[int, str]] = []

    def record(phase: str, current: int, total: int | None, detail: str, **_meta: Any) -> None:
        if phase == "preparing" and detail.endswith(".md"):
            seen.append((current, detail))

    _sync_with_transport(
        tmp_path,
        monkeypatch,
        lambda: FakeTransport({"https://a": ["ok"], "https://b": ["ok"]}),
        "order",
        on_progress=record,
        files=9,
    )

    assert [position for position, _path in seen] == list(range(1, 10))
    assert [path for _position, path in seen] == sorted(path for _position, path in seen)


def test_an_answer_for_the_wrong_file_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """A worker is another process on a network; its answers are checked.

    A batch makes this sharper than it was: results are matched to tasks by
    position, so a worker that reorders or drops one would otherwise commit
    file A's chunks under file B's stable ID. The identity check turns that
    into a loud failure instead of a silently wrong index.
    """

    class Reversing(FakeTransport):
        def post(self, connection, url, tasks, keys, **kwargs):  # type: ignore[no-untyped-def]
            body, reuse = super().post(connection, url, tasks, keys, **kwargs)
            body["results"] = list(reversed(body["results"]))
            return body, reuse

    with pytest.raises(ValueError, match="different source/item"):
        _sync_with_transport(
            tmp_path,
            monkeypatch,
            lambda: Reversing({"https://a": ["ok"], "https://b": ["ok"]}),
            "reversed",
        )


def test_every_worker_dead_falls_back_to_local_preparation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The durability claim, stated as a test: a dead fleet costs time, not data."""

    healthy, healthy_artifacts = _sync_with_transport(
        tmp_path,
        monkeypatch,
        lambda: FakeTransport({"https://a": ["ok"], "https://b": ["ok"]}),
        "up",
    )
    dead, dead_artifacts = _sync_with_transport(
        tmp_path,
        monkeypatch,
        lambda: FakeTransport(
            {"https://a": [_Retryable("down")], "https://b": [_Retryable("down")]}
        ),
        "down",
    )

    assert healthy.indexed_artifacts == 6
    assert dead.indexed_artifacts == 6
    assert [row["sha256"] for row in dead_artifacts] == [row["sha256"] for row in healthy_artifacts]
    assert [row["id"] for row in dead_artifacts] == [row["id"] for row in healthy_artifacts]


def test_one_dead_worker_no_longer_fails_its_share_of_the_sync(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The regression this step exists for.

    The old dispatch picked a worker with ``position % len(urls)``, so half
    the files went to the dead endpoint and stayed there. Now the breaker
    takes it out of rotation after three attempts and the rest of the sync is
    served by the live worker.
    """

    made: list[FakeTransport] = []

    def factory() -> FakeTransport:
        transport = FakeTransport({"https://a": [_Retryable("refused")], "https://b": ["ok"]})
        made.append(transport)
        return transport

    result, _artifacts = _sync_with_transport(tmp_path, monkeypatch, factory, "half")

    assert result.indexed_artifacts == 6
    transport = made[0]
    assert transport.calls.count("https://a") <= FAILURE_THRESHOLD
    assert transport.calls.count("https://b") >= 1


def test_remote_and_thread_executors_produce_identical_state(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Rule 4: the executor is a capacity control, never a semantic one."""

    _remote, remote_artifacts = _sync_with_transport(
        tmp_path,
        monkeypatch,
        lambda: FakeTransport({"https://a": ["ok"], "https://b": [_Retryable("flap"), "ok"]}),
        "remote",
    )

    workspace = tmp_path / "workspace-thread"
    _write_docs(workspace)
    config = _config(tmp_path, workspace, state_name="state-thread")
    engine = SyncEngine(config)
    try:
        engine.sync_source("docs", "full")
        thread_artifacts = _artifacts(engine)
    finally:
        engine.close()

    def shape(rows: list[Any]) -> list[tuple[Any, ...]]:
        return sorted(
            (row["relative_path"], row["sha256"], row["type"], row["chunk_count"]) for row in rows
        )

    assert shape(remote_artifacts) == shape(thread_artifacts)


def test_the_worker_cli_reports_a_missing_grpc_extra_instead_of_a_traceback(
    tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker tier scales hardest and comes up last, so its startup path is
    the one an operator meets a broken image on.

    `GrpcUnavailable` exists to turn a missing extra into one actionable line,
    and `serve()` reached a bare `import grpc` before the call that raises it —
    so the CLI's handler never ran and the operator got a stack trace ending in
    `ModuleNotFoundError: No module named 'grpc'`.

    Driven through `main` with `load_protos` forced to fail, so it asserts the
    contract whether or not the extra happens to be installed here.
    """

    import yaml

    from pheasant.cli import main
    from pheasant.sync import grpc_worker

    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "pheasant": {
                    "name": "no-grpc",
                    "state_path": str(tmp_path / "state"),
                    "workspace_root": str(tmp_path / "ws"),
                },
                "sync": {"concurrency": {"remote_worker_enabled": True}},
            }
        ),
        encoding="utf-8",
    )

    def missing(*_args: Any, **_kwargs: Any) -> Any:
        raise grpc_worker.GrpcUnavailable(
            "the gRPC worker transport needs the [grpc] extra: pip install 'pheasant[grpc]'"
        )

    monkeypatch.setattr(grpc_worker, "load_protos", missing)
    monkeypatch.setenv("PHEASANT_INDEX_WORKER_TOKEN", "a-token")

    assert main(["worker", "--config", str(config_path)]) == 1
    assert "[grpc] extra" in capsys.readouterr().out
