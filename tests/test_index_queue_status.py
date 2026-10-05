"""The pre-claim interval, named.

A sync requested on a replica that does not index is *published*, and until an
indexer claims it the only trace is a row in ``index_tasks``. Before
``services/index_queue.py`` every surface reported that interval the way it
reports "nothing happened": the route said ``queued`` once, and the jobs tray —
which reads the in-process registry — showed nothing at all. These tests hold
the five states the operation reports, that both transports report them
identically, and that a queue-less region (the default, rule 7) says
``enabled: false`` rather than an empty backlog.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.mcp_server.tools import PheasantTools
from pheasant.services import ServiceContext
from pheasant.services import index_queue as index_queue_service
from pheasant.services.errors import UnknownKnowledgeBase
from pheasant.sync.queue import IndexTask, LocalQueue, TaskQueue


def _config(tmp_path: Path, *, queue: bool) -> PheasantConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "a.md").write_text("# A\n\nThe gateway rotates nightly.\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "pheasant": {
            "name": "queue-status",
            "state_path": str(tmp_path / "state"),
            "workspace_root": str(workspace),
            "exports_path": str(tmp_path / "exports"),
        },
        "storage": {"graph_snapshots": False},
        "sources": [
            {"name": "docs", "type": "markdown_folder", "path": str(workspace)},
        ],
    }
    if queue:
        payload["sync"] = {"queue": {"enabled": True, "backend": "local"}}
    return PheasantConfig.model_validate(payload)


def _context(tools: PheasantTools) -> ServiceContext:
    return tools.services


def _seed(queue: LocalQueue) -> None:
    """One task in each state, published in a known order."""

    for name in ("waiting-1", "claimed", "lapsed", "retry", "dead", "waiting-2"):
        queue.publish(IndexTask(id=f"idx-{name}", source_id=name, max_attempts=1))
    # A real claim first, so the claimed row's shape is the one `claim` writes.
    claimed = queue.claim("indexer-0:41:abcd", visibility_seconds=600.0)
    assert claimed is not None and claimed.id == "idx-waiting-1"
    queue.state.execute(
        "UPDATE index_tasks SET status='pending', owner=NULL, visible_at=? WHERE id=?",
        (_iso(timedelta(seconds=-1)), "idx-waiting-1"),
    )
    queue.state.execute(
        "UPDATE index_tasks SET status='inflight', owner=?, visible_at=? WHERE id=?",
        ("indexer-0:41:abcd", _iso(timedelta(minutes=10)), "idx-claimed"),
    )
    queue.state.execute(
        "UPDATE index_tasks SET status='inflight', owner=?, visible_at=? WHERE id=?",
        ("indexer-1:7:ffff", _iso(timedelta(minutes=-1)), "idx-lapsed"),
    )
    queue.state.execute(
        "UPDATE index_tasks SET status='pending', visible_at=?, attempts=1, last_error=? "
        "WHERE id=?",
        (_iso(timedelta(minutes=5)), "parser crashed", "idx-retry"),
    )
    queue.state.execute(
        "UPDATE index_tasks SET status='dead', last_error=? WHERE id=?",
        ("out of attempts", "idx-dead"),
    )


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


@pytest.fixture
def queued(tmp_path: Path) -> PheasantTools:
    tools = PheasantTools(_config(tmp_path, queue=True))
    _seed(LocalQueue(tools.state))
    return tools


def test_each_outstanding_task_reports_its_pre_claim_state(queued: PheasantTools) -> None:
    status = index_queue_service.queue_status(_context(queued), None)

    assert status["enabled"] is True
    assert status["listing"] == "complete"
    states = {task["task_id"]: task["state"] for task in status["tasks"]}
    assert states == {
        "idx-waiting-1": "awaiting_claim",
        "idx-claimed": "claimed",
        "idx-lapsed": "claim_lapsed",
        "idx-retry": "retry_scheduled",
        "idx-dead": "dead",
        "idx-waiting-2": "awaiting_claim",
    }
    assert status["counts"] == {
        "awaiting_claim": 2,
        "retry_scheduled": 1,
        "claimed": 1,
        "claim_lapsed": 1,
        "dead": 1,
    }


def test_only_unclaimed_visible_tasks_carry_a_queue_position(queued: PheasantTools) -> None:
    tasks = {
        task["task_id"]: task
        for task in index_queue_service.queue_status(_context(queued), None)["tasks"]
    }

    assert tasks["idx-waiting-1"]["position"] == 1
    assert tasks["idx-waiting-2"]["position"] == 2
    assert all(
        task["position"] is None
        for key, task in tasks.items()
        if key not in {"idx-waiting-1", "idx-waiting-2"}
    )


def test_a_claim_names_its_host_and_nothing_else_does(queued: PheasantTools) -> None:
    tasks = {
        task["task_id"]: task
        for task in index_queue_service.queue_status(_context(queued), None)["tasks"]
    }

    assert tasks["idx-claimed"]["claimed_by"] == "indexer-0"
    # A lapsed claim's owner stopped heartbeating; naming it as the claimer
    # would tell a reader the work is in hand when it is about to be redelivered.
    assert tasks["idx-lapsed"]["claimed_by"] is None
    assert tasks["idx-retry"]["last_error"] == "parser crashed"


def test_waiting_time_is_measured_from_enqueue(queued: PheasantTools) -> None:
    later = datetime.now(UTC) + timedelta(seconds=90)

    status = index_queue_service.queue_status(_context(queued), None, now=later)

    waiting = next(t for t in status["tasks"] if t["task_id"] == "idx-waiting-2")
    assert 89.0 <= waiting["waiting_seconds"] <= 120.0


def test_a_region_with_no_queue_says_so_rather_than_reporting_an_empty_backlog(
    tmp_path: Path,
) -> None:
    tools = PheasantTools(_config(tmp_path, queue=False))

    status = index_queue_service.queue_status(_context(tools), None)

    assert status["enabled"] is False
    assert status["listing"] == "not_applicable"
    assert status["tasks"] == []


def test_a_backend_that_cannot_list_reports_unknown_not_empty(
    queued: PheasantTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LocalQueue, "outstanding", TaskQueue.outstanding)

    status = index_queue_service.queue_status(_context(queued), None)

    assert status["listing"] == "unavailable"
    assert status["tasks"] == []
    assert status["depth"]["pending"] == 3


def test_an_unknown_knowledge_base_is_refused(queued: PheasantTools) -> None:
    with pytest.raises(UnknownKnowledgeBase):
        index_queue_service.queue_status(_context(queued), "elsewhere")


def _stable(payload: dict[str, Any]) -> dict[str, Any]:
    # The one clock-dependent field; two calls a few milliseconds apart differ.
    return {
        **payload,
        "tasks": [{**task, "waiting_seconds": None} for task in payload["tasks"]],
    }


def test_both_surfaces_report_the_queue_identically(queued: PheasantTools, tmp_path: Path) -> None:
    client = TestClient(create_app(queued.config, config_path=str(tmp_path / "pheasant.yaml")))
    kb = queued.config.knowledge_base_id

    over_http = client.get("/queue", params={"knowledge_base": kb})
    over_mcp = queued.get_index_queue(kb)

    assert over_http.status_code == 200, over_http.text
    assert _stable(over_http.json()) == _stable(over_mcp)


def test_both_surfaces_refuse_an_unknown_knowledge_base_with_one_text(
    queued: PheasantTools, tmp_path: Path
) -> None:
    client = TestClient(create_app(queued.config, config_path=str(tmp_path / "pheasant.yaml")))

    over_http = client.get("/queue", params={"knowledge_base": "elsewhere"})
    with pytest.raises(UnknownKnowledgeBase) as refused:
        queued.get_index_queue("elsewhere")

    assert over_http.status_code == 404
    assert over_http.json()["detail"] == str(refused.value)
