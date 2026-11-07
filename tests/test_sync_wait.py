"""Waiting for a sync, and saying a failure is temporary when it is.

On a fleet a write only publishes a sync and its job reads ``succeeded`` at
once, so an agent searched and composed over content that was not indexed
yet. `wait_for_sync` is the wait; these tests hold its four states, its bound,
and that both transports answer it -- and that a dropped database connection
reaches a caller as a retryable refusal with a reason, not as a bare
"Error executing tool".
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest, SourceNotFound, transient_refusal
from pheasant.services.sync_wait import MAX_WAIT_SECONDS, WaitRequest, wait_for_sync
from pheasant.sync.engine import SyncEngine


def _config(root: Path, *, queue: bool = False) -> PheasantConfig:
    workspace = root / "notes"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "a.md").write_text("# Alpha\n\nThe alpha note.\n", encoding="utf-8")
    (workspace / "b.md").write_text("# Beta\n\nThe beta note.\n", encoding="utf-8")
    raw: dict[str, Any] = {
        "pheasant": {
            "name": "wait",
            "defaults": "standalone",
            "state_path": str(root / "state"),
            "exports_path": str(root / "exports"),
            "workspace_root": str(root),
        },
        "sources": [{"name": "notes", "type": "document_folder", "path": str(workspace)}],
    }
    if queue:
        raw["sync"] = {"queue": {"enabled": True, "backend": "local"}}
    return PheasantConfig.model_validate(raw)


class _Clock:
    """A clock the wait's sleeps advance, so a 55 s bound takes no time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _wait(engine: SyncEngine, clock: _Clock | None = None, **request: Any) -> dict[str, Any]:
    clock = clock or _Clock()
    context = ServiceContext(config=engine.config, state=engine.state, searcher=None)
    return wait_for_sync(
        context, WaitRequest(source_name="notes", **request), clock=clock, sleep=clock.sleep
    )


def test_a_finished_sync_is_finished_and_says_what_it_did(tmp_path: Path) -> None:
    engine = SyncEngine(_config(tmp_path))
    try:
        engine.sync_source("notes", "full")
        answer = _wait(engine, since="2000-01-01T00:00:00Z")
    finally:
        engine.close()
    assert answer["state"] == "finished"
    assert answer["timed_out"] is False
    assert answer["documents"] == 2
    assert answer["last_sync"]["status"] == "healthy"
    assert answer["last_sync"]["unreadable_items"] == 0


def test_nothing_since_the_write_is_not_started_after_a_grace(tmp_path: Path) -> None:
    """A sync that finished *before* the write must not count; with nothing
    running or queued either, the answer is to start one -- but only after a
    few seconds, because a sync started in-process writes its heartbeat a
    moment after it is asked for."""

    engine = SyncEngine(_config(tmp_path))
    clock = _Clock()
    try:
        engine.sync_source("notes", "full")
        answer = _wait(engine, clock, since="2999-01-01T00:00:00+00:00")
    finally:
        engine.close()
    assert answer["state"] == "not_started"
    assert answer["timed_out"] is False
    assert "sync_source" in answer["next"]
    assert 0 < clock.now < MAX_WAIT_SECONDS


def test_a_published_task_is_queued_and_the_wait_is_bounded(tmp_path: Path) -> None:
    from pheasant.sync.queue import IndexTask, queue_from_config

    config = _config(tmp_path, queue=True)
    engine = SyncEngine(config)
    clock = _Clock()
    try:
        queue = queue_from_config(config, engine.state)
        queue.publish(IndexTask(id="idx-notes", source_id="notes"))
        queue.close()
        answer = _wait(engine, clock, timeout_seconds=600)
    finally:
        engine.close()
    assert answer["state"] == "queued"
    assert answer["timed_out"] is True
    assert [task["task_id"] for task in answer["queued_tasks"]] == ["idx-notes"]
    assert clock.now == pytest.approx(MAX_WAIT_SECONDS), "a wait longer than its cap"


def test_a_heartbeating_sync_is_running(tmp_path: Path) -> None:
    from pheasant.sync.cancellation import SyncRun

    engine = SyncEngine(_config(tmp_path))
    try:
        with SyncRun(engine.state, "notes"):
            answer = _wait(engine, timeout_seconds=4)
    finally:
        engine.close()
    assert answer["state"] == "running"
    assert answer["timed_out"] is True
    assert answer["running_since"]


def test_a_bad_request_is_refused_with_its_reason(tmp_path: Path) -> None:
    engine = SyncEngine(_config(tmp_path))
    try:
        with pytest.raises(InvalidRequest, match="ISO timestamp"):
            _wait(engine, since="yesterday")
        context = ServiceContext(config=engine.config, state=engine.state, searcher=None)
        with pytest.raises(SourceNotFound):
            wait_for_sync(context, WaitRequest(source_name="nope"))
    finally:
        engine.close()


def test_both_transports_answer_the_wait(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app
    from pheasant.mcp_server.tools import PheasantTools

    config = _config(tmp_path)
    tools = PheasantTools(config)
    tools.engine.sync_source("notes", "full")
    client = TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))
    since = "2000-01-01T00:00:00Z"
    over_mcp = tools.wait_for_sync(config.knowledge_base_id, "notes", since, 5)
    over_http = client.get("/sources/notes/wait", params={"since": since}).json()
    for answer in (over_mcp, over_http):
        assert answer["state"] == "finished"
        assert answer["documents"] == 2
    assert over_mcp["last_sync"] == over_http["last_sync"]


# -- a database that did not answer ---------------------------------------------


def _dropped() -> Exception:
    psycopg = pytest.importorskip("psycopg")
    return psycopg.OperationalError(
        "consuming input failed: server closed the connection unexpectedly"
    )


def test_a_dropped_connection_is_a_retryable_refusal_with_its_reason() -> None:
    refusal = transient_refusal(_dropped())
    assert refusal is not None
    assert refusal.code == "STATE_STORE_UNAVAILABLE"
    assert refusal.retryable is True
    assert "server closed the connection" in str(refusal)
    assert "retry the call" in str(refusal)


def test_a_server_error_is_not_called_temporary() -> None:
    psycopg = pytest.importorskip("psycopg")
    assert transient_refusal(psycopg.errors.UniqueViolation("duplicate key")) is None
    assert transient_refusal(RuntimeError("boom")) is None


def test_mcp_forwards_the_refusal_where_it_used_to_blank_it() -> None:
    pytest.importorskip("mcp")
    from mcp.server.mcpserver.exceptions import ToolError

    from pheasant.mcp_server.server import _anticipated_failures

    exc = _dropped()

    @_anticipated_failures("tool")
    def edit_source_urls() -> dict:
        raise exc

    with pytest.raises(ToolError, match="retry the call"):
        edit_source_urls()


def test_http_answers_503_with_the_same_refusal(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app
    from pheasant.services import sync_wait

    exc = _dropped()

    def fail(*_args: Any, **_kwargs: Any) -> dict:
        raise exc

    monkeypatch.setattr(sync_wait, "wait_for_sync", fail)
    config = _config(tmp_path)
    client = TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))
    response = client.get("/sources/notes/wait")
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "STATE_STORE_UNAVAILABLE"
    assert body["retryable"] is True
    assert body["detail"] == str(transient_refusal(exc))


# -- a successful sync's warnings -----------------------------------------------


def test_a_successful_sync_worker_logs_what_it_warned(monkeypatch, caplog) -> None:
    """The child's stderr was read on failure alone, so a sync that could not
    fetch 79 of 80 pages logged nothing anywhere."""

    from pheasant.sync import worker

    monkeypatch.setattr(
        worker,
        "_run_streaming",
        lambda *_a, **_k: (0, '{"status": "ok", "results": []}', "site: could not read two"),
    )
    with caplog.at_level(logging.WARNING, logger="pheasant.sync.worker"):
        worker.run_sync("pheasant.yaml", "site")
    assert "sync worker: site: could not read two" in caplog.text


def test_a_killed_sync_worker_says_it_was_killed(monkeypatch) -> None:
    """An OOM-killed sync of 23,691 pages was reported as one of its 404
    warnings -- the last line it happened to print."""

    from pheasant.sync import worker

    monkeypatch.setattr(
        worker,
        "_run_streaming",
        lambda *_a, **_k: (-9, "", "site: could not read a.html (HTTP Error 404)"),
    )
    report = worker.run_sync("pheasant.yaml", "site")
    assert report["status"] == "failed"
    assert report["error"].startswith("the sync worker was killed (SIGKILL)")
    assert "memory limit" in report["error"]
    assert "last output:\nsite: could not read a.html" in report["error"]
