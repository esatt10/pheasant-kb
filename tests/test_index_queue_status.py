"""The pre-claim interval, named.

A sync requested on a replica that does not index is *published*, and until an
indexer claims it the only trace is a row in ``index_tasks``. Before
``services/index_queue.py`` every surface reported that interval the way it
reports "nothing happened": the route said ``queued`` once, and the jobs tray —
which reads the in-process registry — showed nothing at all. These tests hold
the five states the operation reports, that both transports report them
identically, and that a queue-less region says ``enabled: false`` rather
than an empty backlog. JetStream counts its backlog but cannot list it without
consuming, so the listing is ``unavailable`` beside a real depth.
"""

from __future__ import annotations

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
from pheasant.sync.queue import IndexTask, queue_from_config
from tests.conftest import make_nats_queue_settings


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
        payload["sync"] = {"queue": make_nats_queue_settings()}
    return PheasantConfig.model_validate(payload)


def _context(tools: PheasantTools) -> ServiceContext:
    return tools.services


@pytest.fixture
def queued(tmp_path: Path) -> PheasantTools:
    tools = PheasantTools(_config(tmp_path, queue=True))
    queue = queue_from_config(tools.config, tools.state)
    try:
        for name in ("one", "two", "three"):
            queue.publish(IndexTask(id=f"idx-{name}", source_id=name, max_attempts=1))
    finally:
        queue.close()
    return tools


def test_a_region_with_no_queue_says_so_rather_than_reporting_an_empty_backlog(
    tmp_path: Path,
) -> None:
    tools = PheasantTools(_config(tmp_path, queue=False))

    status = index_queue_service.queue_status(_context(tools), None)

    assert status["enabled"] is False
    assert status["listing"] == "not_applicable"
    assert status["tasks"] == []


def test_a_jetstream_queue_is_listed_from_the_request_ledger(queued: PheasantTools) -> None:
    """JetStream counts its messages but cannot list them; the ledger can.

    Every sync published through the index queue has a row
    (`sync/request_ledger.py`), so the waiting syncs are named even though
    the broker cannot enumerate them, and the broker's depth still counts.
    """

    status = index_queue_service.queue_status(_context(queued), None)

    assert status["listing"] == "ledger"
    assert [task["source"] for task in status["tasks"]] == ["one", "two", "three"]
    assert {task["state"] for task in status["tasks"]} == {"awaiting_claim"}
    assert [task["position"] for task in status["tasks"]] == [1, 2, 3]
    assert all(task["request_id"] for task in status["tasks"])
    assert status["depth"]["pending"] == 3


def test_a_queue_whose_ledger_cannot_be_read_reports_unknown_not_empty(
    queued: PheasantTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pheasant.sync import request_ledger

    def unreadable(*_args: Any, **_kwargs: Any) -> list:
        raise RuntimeError("ledger unreachable")

    monkeypatch.setattr(request_ledger, "open_requests", unreadable)
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
