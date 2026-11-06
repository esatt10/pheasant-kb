"""Managed identity on the internal boundaries, and on the Foundry resource.

Offline, like `test_entra_identity.py`: tokens are signed with a local key and
the identity endpoint is a monkeypatched function. What is under test is the
one rule every boundary now follows (`security/service_auth.py`) and that a
configured token keeps working on both ends.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

pytest.importorskip("cryptography")

from pheasant import foundry  # noqa: E402
from pheasant.security import entra, service_auth  # noqa: E402

from .test_entra_identity import TENANT, Tenant  # noqa: E402

APP = "99999999-8888-7777-6666-555555555555"


@pytest.fixture
def tenant(monkeypatch: pytest.MonkeyPatch) -> Tenant:
    fake = Tenant()
    monkeypatch.setattr(entra, "_http_json", fake.http)
    service_auth._VERIFIERS.clear()
    entra.clear_token_cache()
    return fake


def _identity(enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=enabled,
        tenant_id=TENANT,
        audience=f"api://{APP}",
        app_id=APP,
        managed_identity_client_id="",
    )


def _app_token(tenant: Tenant, roles: list[str]) -> str:
    now = int(time.time())
    return tenant.sign(
        {
            "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
            "tid": TENANT,
            "aud": APP,
            "exp": now + 3600,
            "nbf": now - 10,
            "oid": "mi-object",
            "roles": roles,
        }
    )


def test_a_configured_token_is_sent_and_accepted_exactly_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOUNDARY_TOKEN", "t0ken")
    assert service_auth.outbound_token("BOUNDARY_TOKEN", _identity()) == "t0ken"
    assert service_auth.outbound_token("BOUNDARY_TOKEN") == "t0ken"
    assert (
        service_auth.inbound_method("Bearer t0ken", "BOUNDARY_TOKEN", service_auth.GRAPH_QUERY)
        == "service_token"
    )
    assert service_auth.inbound_method("Bearer nope", "BOUNDARY_TOKEN", "graph.query") is None


def test_with_no_token_a_sender_asks_its_managed_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOUNDARY_TOKEN", raising=False)
    asked: list[tuple[str, str]] = []

    def fake_mi(resource: str, client_id: str | None = None) -> str:
        asked.append((resource, client_id or ""))
        return "mi-token"

    monkeypatch.setattr(service_auth, "managed_identity_token", fake_mi)
    assert service_auth.outbound_token("BOUNDARY_TOKEN", _identity()) == "mi-token"
    assert asked == [(f"api://{APP}", "")]
    # Off, nothing changes: no token is no token.
    assert service_auth.outbound_token("BOUNDARY_TOKEN", _identity(enabled=False)) == ""


def test_a_receiver_admits_an_identity_only_with_the_boundary_role(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOUNDARY_TOKEN", raising=False)
    identity = _identity()
    assert service_auth.inbound_configured("BOUNDARY_TOKEN", identity)
    indexer = _app_token(tenant, ["worker.prepare"])
    assert (
        service_auth.inbound_method(
            f"Bearer {indexer}", "BOUNDARY_TOKEN", service_auth.WORKER_PREPARE, identity
        )
        == "managed_identity"
    )
    # The worker boundary's identity cannot read the graph: the role is what
    # keeps the boundaries apart under one app registration.
    assert (
        service_auth.inbound_method(
            f"Bearer {indexer}", "BOUNDARY_TOKEN", service_auth.GRAPH_QUERY, identity
        )
        is None
    )
    assert not service_auth.inbound_configured("BOUNDARY_TOKEN", _identity(enabled=False))
    assert (
        service_auth.inbound_method(
            f"Bearer {indexer}", "BOUNDARY_TOKEN", "worker.prepare", _identity(enabled=False)
        )
        is None
    )


def test_the_graph_service_admits_the_api_identity_over_http(
    tenant: Tenant, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app
    from pheasant.config.schema import PheasantConfig

    monkeypatch.delenv("PHEASANT_GRAPH_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("PHEASANT_API_TOKEN", "front-door")
    for name in ("ws", "state", "exports"):
        (tmp_path / name).mkdir()
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "kb",
                "defaults": "standalone",
                "workspace_root": str(tmp_path / "ws"),
                "state_path": str(tmp_path / "state"),
                "exports_path": str(tmp_path / "exports"),
            },
            "server": {"role": "graph"},
            "security": {
                "api_auth": {"token_env": "PHEASANT_API_TOKEN"},
                "service_identity": {
                    "enabled": True,
                    "tenant_id": TENANT,
                    "audience": f"api://{APP}",
                    "app_id": APP,
                },
            },
            "sources": [],
        }
    )
    app = create_app(config, config_path=str(tmp_path / "pheasant.yaml"))
    body = {"operation": "stats", "parameters": {}}
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        allowed = _app_token(tenant, ["graph.query"])
        response = client.post(
            "/internal/graph/query", json=body, headers={"Authorization": f"Bearer {allowed}"}
        )
        assert response.status_code == 200, response.text
        wrong_role = _app_token(tenant, ["worker.prepare"])
        refused = client.post(
            "/internal/graph/query", json=body, headers={"Authorization": f"Bearer {wrong_role}"}
        )
        assert refused.status_code == 401
    app.state.engine.close()


def test_foundry_uses_the_key_when_there_is_one_and_the_identity_when_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://acct.services.ai.azure.com/openai/v1"
    assert foundry.credential_headers(url, "k") == {"api-key": "k"}
    assert foundry.credential_headers("https://api.openai.com/v1", "k") == {
        "authorization": "Bearer k"
    }
    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    monkeypatch.delenv("IDENTITY_HEADER", raising=False)
    assert foundry.credential_headers(url, "") == {}

    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://identity.local/msi/token")
    monkeypatch.setenv("IDENTITY_HEADER", "header-secret")
    calls: list[tuple[str, dict]] = []

    def fake_http(target: str, *, headers=None, data=None, timeout=10.0) -> dict:
        calls.append((target, dict(headers or {})))
        return {"access_token": "entra-for-foundry", "expires_on": str(int(time.time()) + 3600)}

    monkeypatch.setattr(entra, "_http_json", fake_http)
    entra.clear_token_cache()
    assert foundry.credential_headers(url, "") == {"authorization": "Bearer entra-for-foundry"}
    assert foundry.credential_headers(url, "") == {"authorization": "Bearer entra-for-foundry"}
    assert len(calls) == 1, "a cached token is reused until it nears expiry"
    target, headers = calls[0]
    assert "resource=https%3A%2F%2Fcognitiveservices.azure.com" in target
    assert headers == {"X-IDENTITY-HEADER": "header-secret"}
    # Not a Foundry host: a missing key still sends nothing.
    assert foundry.credential_headers("https://api.openai.com/v1", "") == {}
    entra.clear_token_cache()
