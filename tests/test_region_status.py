"""Whether the region is awake: `services.region_status` and the api's wake gate.

A region that scales to zero wakes on its first request. Until these, the
minute that took was a page that never loaded and an agent's call that hung
-- and once the api was up, a graph service still starting read as "region
not ready". Each part now says it is waking, warming or unavailable, over
HTTP, MCP and the web UI, and says nothing once everything is ready.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.deployment.wake_gate import WakeGate
from pheasant.mcp_server.tools import PheasantTools
from pheasant.services import ServiceContext
from pheasant.services import region_status as region


def _config(tmp_path: Path) -> PheasantConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "note.md").write_text("# Note\n\nAwake.\n", encoding="utf-8")
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "region-status-test",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "sources": [{"name": "docs", "type": "markdown_folder", "path": str(workspace)}],
        }
    )


def _settled(status: dict[str, Any]) -> dict[str, Any]:
    """The status without the clock, which moves between two calls."""

    return {key: value for key, value in status.items() if key != "elapsed_seconds"}


def test_an_awake_region_has_nothing_to_show_on_either_surface(tmp_path: Path) -> None:
    config = _config(tmp_path)
    over_http = TestClient(create_app(config)).get("/region/status").json()
    over_mcp = PheasantTools(config).get_region_status(config.knowledge_base_id)

    assert over_http["status"] == "ready"
    assert [part["state"] for part in over_http["components"]] == ["ready"]
    assert _settled(over_http) == _settled(over_mcp), "the two surfaces disagree"


class _Silent:
    """A store or graph service that is not answering yet."""

    dialect = SimpleNamespace(is_postgres=False)
    is_remote_graph = True

    def rows(self, *_args: Any) -> list:
        raise ConnectionError("connection refused")

    def ping(self) -> dict:
        raise ConnectionError("connection refused")


def test_a_part_not_answering_is_waking_soon_after_start_and_unavailable_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = ServiceContext(config=None, state=_Silent(), searcher=None, graph=_Silent())

    monkeypatch.setattr(region, "PROCESS_STARTED", time.monotonic() - 20)
    waking = region.region_status(context)
    assert waking["status"] == "waking"
    assert {part["name"]: part["state"] for part in waking["components"]} == {
        "database": "waking",
        "graph_service": "waking",
    }
    assert f"usually about {region.TYPICAL_WAKE_SECONDS} s" in waking["message"]
    assert 19 <= waking["elapsed_seconds"] <= 30

    monkeypatch.setattr(
        region, "PROCESS_STARTED", time.monotonic() - region.WAKE_WINDOW_SECONDS - 60
    )
    late = region.region_status(context)
    assert late["status"] == "unavailable", "a part still silent long after a wake is not waking"


def test_a_database_reloading_its_cache_is_warming_not_waking() -> None:
    """Postgres answers, and its autoprewarm worker is still loading pages."""

    class Warming:
        dialect = SimpleNamespace(is_postgres=True)

        def rows(self, sql: str, _params: Any = ()) -> list:
            if "autoprewarm" in sql:
                return [{"up": 41.0, "prewarming": 1}]
            return [{"ok": 1}]

    status = region.region_status(ServiceContext(config=None, state=Warming(), searcher=None))
    assert status["status"] == "warming"
    assert status["components"][0]["up_seconds"] == 41.0
    assert "first answers may be slower" in status["message"]


@pytest.mark.skipif(
    not os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip(),
    reason="set PHEASANT_TEST_POSTGRES_DSN to a throwaway database",
)
def test_the_cache_check_reads_a_real_postgres(tmp_path: Path) -> None:
    """`pg_stat_activity.backend_type` and `pg_postmaster_start_time()` exist there."""

    from pheasant.sync.engine import SyncEngine

    config = _config(tmp_path)
    config.storage.backend = "postgres"
    config.storage.dsn_env = "PHEASANT_TEST_POSTGRES_DSN"
    engine = SyncEngine(config)
    try:
        status = region.region_status(
            ServiceContext(config=config, state=engine.state, searcher=None)
        )
    finally:
        engine.close()
    database = status["components"][0]
    assert database["state"] in {"ready", "warming"}
    assert database["up_seconds"] > 0, "the uptime query did not run"


# -- the wake gate ------------------------------------------------------------


def _get(url: str, *, method: str = "GET", accept: str = "application/json") -> tuple:
    request = urllib.request.Request(url, method=method, headers={"Accept": accept})
    if method == "POST":
        request.data = b"{}"
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read().decode()


def test_the_wake_gate_holds_the_port_until_the_database_answers() -> None:
    awake = threading.Event()
    gate = WakeGate(awake.is_set, host="127.0.0.1", port=0, poll_seconds=0.05)
    base = f"http://127.0.0.1:{gate.port}"
    took: list[float] = []
    runner = threading.Thread(target=lambda: took.append(gate.run()), daemon=True)
    runner.start()
    try:
        # The platform's probe: answered at once, so traffic reaches the gate.
        status, _headers, body = _get(base + "/health")
        assert status == 200 and json.loads(body)["waking"] is True

        status, _headers, body = _get(base + "/region/status")
        report = json.loads(body)
        assert status == 200 and report["status"] == "waking"
        assert report["served_by"] == "wake_gate"
        assert {part["name"] for part in report["components"]} == {"database", "api"}

        # A person: a page that says so, and opens the app by itself.
        status, headers, body = _get(base + "/", accept="text/html,application/xhtml+xml")
        assert status == 503 and "waking up" in body
        assert headers["Content-Type"].startswith("text/html")

        # An agent, over the API or MCP: a refusal it can act on.
        status, headers, body = _get(base + "/mcp", method="POST")
        refusal = json.loads(body)
        assert status == 503 and headers["Retry-After"] == "5"
        assert refusal["code"] == "REGION_WAKING" and refusal["retryable"] is True
        assert "usually about" in refusal["detail"]
    finally:
        awake.set()
        runner.join(timeout=10)

    assert not runner.is_alive() and took, "the gate did not stop when the database answered"
    with pytest.raises(OSError):
        urllib.request.urlopen(base + "/health", timeout=2)
