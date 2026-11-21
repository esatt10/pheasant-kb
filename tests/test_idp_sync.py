"""Product Step 32.4 — external-IdP group sync with a staleness SLA.

The load-bearing rule: IdP-derived group grants apply only while the
synced mapping honors the SLA; a mapping older than
``security.idp.staleness_max_minutes`` grants NOTHING (fail closed),
while config-mapped groups and explicit caller groups keep working.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.loader import load_config
from pheasant.mcp_server.tools import PheasantTools
from pheasant.security import idp as idp_module
from pheasant.security.idp import (
    fetch_scim_groups,
    fresh_idp_groups,
    idp_status,
    run_idp_maintenance,
    run_idp_sync,
)
from pheasant.sync import connector_registry
from tests.test_acl_enforcement import AclTestConnector

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)

SCIM_PAGES = {
    1: {
        "totalResults": 3,
        "startIndex": 1,
        "Resources": [
            {"displayName": "eng", "members": [{"value": "alice"}, {"value": "erin"}]},
            {"displayName": "finance", "members": [{"value": "bob"}]},
        ],
    },
    3: {
        "totalResults": 3,
        "startIndex": 3,
        "Resources": [
            {"displayName": "eng", "members": [{"value": "alice"}]},
        ],
    },
}


def _fake_scim(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake(url: str, token: str | None) -> dict[str, Any]:
        calls.append(url)
        start = int(url.split("startIndex=")[1].split("&")[0])
        return SCIM_PAGES[start]

    monkeypatch.setattr(idp_module, "_http_get_json", fake)
    return calls


def _config(tmp_path: Path, *, idp_enabled: bool = True, staleness_minutes: int = 60) -> Any:
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: idp-test
  state_path: {tmp_path / "state"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
security:
  acl_enforced: true
  groups:
    carol:
      - eng
  idp:
    enabled: {str(idp_enabled).lower()}
    base_url: https://idp.test/scim/v2
    sync_interval_minutes: 30
    staleness_max_minutes: {staleness_minutes}
sources:
  - name: docs
    type: acltest
    path: /unused
    include: []
""",
        encoding="utf-8",
    )
    return load_config(config_path)


@pytest.fixture()
def synced_tools(tmp_path: Path):
    connector_registry.reset_connector_registry()
    connector_registry.register_connector_class("acltest", AclTestConnector)
    tools = PheasantTools(_config(tmp_path))
    tools.sync_source("idp-test", "docs", "incremental")
    yield tools
    tools.engine.close()
    connector_registry.reset_connector_registry()


def _titles(tools: PheasantTools, **kwargs: Any) -> set[str]:
    payload = tools.search_context(
        "idp-test", "project handbook", mode="text", max_results=10, **kwargs
    )
    return {str(r.get("relative_path")) for r in payload["results"]}


def test_scim_fetch_paginated_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_scim(monkeypatch)
    mapping = fetch_scim_groups("https://idp.test/scim/v2", "tok")
    assert mapping == {"alice": ["eng"], "bob": ["finance"], "erin": ["eng"]}
    assert len(calls) == 2  # paginated: 2 resources, then the final page


def test_sync_is_idempotent_but_heartbeat_bumps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_scim(monkeypatch)
    config = _config(tmp_path)
    from pheasant.persistence.state_store import StateStore

    state = StateStore()
    state.migrate()
    state.migrate()  # idempotent second migrate
    try:
        first = run_idp_sync(config, state, now=NOW)
        assert first["changed"] is True and first["principals"] == 3
        later = NOW + timedelta(minutes=5)
        second = run_idp_sync(config, state, now=later)
        assert second["changed"] is False  # unchanged directory = row-stable
        assert state.idp_synced_at() == later.isoformat()  # heartbeat still bumps
        assert state.idp_groups_for({"alice"}) == {"eng"}
    finally:
        state.close()


def test_idp_grant_works_fresh_and_fails_closed_when_stale(synced_tools: PheasantTools) -> None:
    state = synced_tools.engine.state
    # Fresh mapping: alice is in eng per the IdP -> she sees the eng runbook.
    state.replace_idp_groups({"alice": ["eng"]}, datetime.now(UTC).isoformat())
    seen = _titles(synced_tools, principal="user:alice")
    assert "eng-runbook.txt" in seen and "bob-notes.txt" not in seen

    # Stale mapping (older than the 60-minute SLA): the SAME grant is dropped.
    stale_at = (datetime.now(UTC) - timedelta(minutes=120)).isoformat()
    state.replace_idp_groups({"alice": ["eng"]}, stale_at)
    seen = _titles(synced_tools, principal="user:alice")
    assert "eng-runbook.txt" not in seen and "alice-notes.txt" in seen

    # Config-mapped groups are the deterministic core and survive staleness.
    seen = _titles(synced_tools, principal="user:carol")
    assert "eng-runbook.txt" in seen


def test_fresh_idp_groups_disabled_or_never_synced_grants_nothing(tmp_path: Path) -> None:
    from pheasant.persistence.state_store import StateStore

    config = _config(tmp_path, idp_enabled=False)
    state = StateStore()
    state.migrate()
    try:
        assert fresh_idp_groups(state, "user:alice", config.security.idp) == set()
        enabled = _config(tmp_path, idp_enabled=True)
        # Enabled but never synced: stale by definition -> nothing granted.
        assert fresh_idp_groups(state, "user:alice", enabled.security.idp) == set()
        with pytest.raises(ValueError, match="disabled"):
            run_idp_sync(config, state)
        assert run_idp_maintenance(config, state) is None
    finally:
        state.close()


def test_maintenance_due_logic_and_error_resilience(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_scim(monkeypatch)
    config = _config(tmp_path)
    from pheasant.persistence.state_store import StateStore

    state = StateStore()
    state.migrate()
    try:
        report = run_idp_maintenance(config, state, now=NOW)
        assert report is not None and report["principals"] == 3
        # Within sync_interval_minutes: not due, no fetch.
        assert run_idp_maintenance(config, state, now=NOW + timedelta(minutes=5)) is None

        # Due again, but the IdP is down: reported, never raised; the
        # heartbeat does NOT move, so the mapping ages toward the SLA.
        def boom(url: str, token: str | None) -> dict[str, Any]:
            raise OSError("idp unreachable")

        monkeypatch.setattr(idp_module, "_http_get_json", boom)
        failed = run_idp_maintenance(config, state, now=NOW + timedelta(minutes=45))
        assert failed is not None and "error" in failed
        assert failed["synced_at"] == NOW.isoformat()
    finally:
        state.close()


def test_http_idp_sync_and_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app

    _fake_scim(monkeypatch)
    connector_registry.reset_connector_registry()
    connector_registry.register_connector_class("acltest", AclTestConnector)
    config = _config(tmp_path)
    app = create_app(config, config_path=str(tmp_path / "pheasant.yaml"))
    client = fastapi_testclient.TestClient(app)
    try:
        before = client.get("/security/idp/status").json()
        assert before["enabled"] is True and before["stale"] is True
        report = client.post("/security/idp/sync").json()
        assert report["principals"] == 3 and report["changed"] is True
        after = client.get("/security/idp/status").json()
        assert after["stale"] is False and after["principals"] == 3
    finally:
        app.state.engine.close()
        connector_registry.reset_connector_registry()


def test_status_reports_disabled(tmp_path: Path) -> None:
    from pheasant.persistence.state_store import StateStore

    config = _config(tmp_path, idp_enabled=False)
    state = StateStore()
    state.migrate()
    try:
        status = idp_status(config, state)
        assert status["enabled"] is False and status["stale"] is None
        assert status["principals"] == 0
    finally:
        state.close()
