"""Sign-in with Microsoft Entra ID, beside the shared token.

Offline: tokens are signed here with a local RSA key, and the only network hop
(`security.entra._http_json`) answers with that key's JWKS and with the token
endpoint's reply. Everything between -- the verifier, the credential store,
the guard, the sign-in round trip, the MCP OAuth server and its consent page,
the access log and the interaction ledger -- is the real code.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pheasant.api.app import create_app  # noqa: E402
from pheasant.config.schema import PheasantConfig  # noqa: E402
from pheasant.security import entra  # noqa: E402

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
BASE = "http://localhost:8765"
SHARED = "shared-s3cret"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Tenant:
    """A signing key, its JWKS, and a fake token endpoint."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "kid-1"
        self.jwks_fetches = 0
        self.next_id_token: str | None = None
        self.token_requests: list[dict[str, str]] = []

    def jwks(self) -> dict[str, Any]:
        numbers = self.key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": self.kid,
                    "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                    "e": _b64(numbers.e.to_bytes(3, "big")),
                }
            ]
        }

    def sign(self, claims: dict[str, Any], *, kid: str | None = None, alg: str = "RS256") -> str:
        header = {"alg": alg, "typ": "JWT", "kid": kid or self.kid}
        signed = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
        signature = self.key.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signed}.{_b64(signature)}"

    def claims(self, **overrides: Any) -> dict[str, Any]:
        now = int(time.time())
        base = {
            "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
            "tid": TENANT,
            "aud": CLIENT,
            "exp": now + 3600,
            "nbf": now - 10,
            "oid": "00000000-0000-0000-0000-00000000a11c",
            "preferred_username": "Alice@Example.com",
            "name": "Alice Example",
            "roles": ["Reader"],
        }
        base.update(overrides)
        return base

    def http(self, url: str, *, headers=None, data=None, timeout=10.0) -> dict[str, Any]:
        if url.endswith("/discovery/v2.0/keys"):
            self.jwks_fetches += 1
            return self.jwks()
        if url.endswith("/oauth2/v2.0/token"):
            self.token_requests.append(dict(data or {}))
            return {"id_token": self.next_id_token, "token_type": "Bearer"}
        raise AssertionError(f"unexpected network call: {url}")


@pytest.fixture
def tenant(monkeypatch: pytest.MonkeyPatch) -> Tenant:
    fake = Tenant()
    monkeypatch.setattr(entra, "_http_json", fake.http)
    return fake


def _config(tmp_path: Path, **entra_settings: Any) -> PheasantConfig:
    for name in ("ws", "state", "exports"):
        (tmp_path / name).mkdir(exist_ok=True)
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "kb",
                "defaults": "standalone",
                "workspace_root": str(tmp_path / "ws"),
                "state_path": str(tmp_path / "state"),
                "exports_path": str(tmp_path / "exports"),
            },
            "observability": {"interactions": {"enabled": True, "flush_batch_size": 1}},
            "security": {
                "api_auth": {"token_env": "PHEASANT_API_TOKEN"},
                "entra": {
                    "enabled": True,
                    "tenant_id": TENANT,
                    "client_id": CLIENT,
                    "public_url": BASE,
                    **entra_settings,
                },
            },
            "sources": [],
        }
    )


@pytest.fixture
def client(tmp_path: Path, tenant: Tenant, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PHEASANT_API_TOKEN", SHARED)
    monkeypatch.setenv("PHEASANT_ENTRA_CLIENT_SECRET", "client-secret")
    app = create_app(_config(tmp_path), config_path=str(tmp_path / "pheasant.yaml"))
    with TestClient(app, base_url=BASE, follow_redirects=False) as test_client:
        yield test_client
    app.state.engine.close()


def _bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


# -- the verifier -----------------------------------------------------------


def test_the_verifier_accepts_a_good_token_and_nothing_else(tenant: Tenant) -> None:
    verifier = entra.EntraVerifier(TENANT, [CLIENT])
    assert verifier.verify(tenant.sign(tenant.claims()))["oid"].endswith("a11c")
    # v1 issuer for the same tenant counts: the resource app picks the version.
    v1 = tenant.claims(iss=f"https://sts.windows.net/{TENANT}/", aud=f"api://{CLIENT}")
    assert entra.EntraVerifier(TENANT, [f"api://{CLIENT}"]).verify(tenant.sign(v1))

    refused = {
        "another audience": tenant.sign(tenant.claims(aud="someone-else")),
        "another tenant": tenant.sign(
            tenant.claims(tid="x", iss="https://login.microsoftonline.com/x/v2.0")
        ),
        "expired": tenant.sign(tenant.claims(exp=int(time.time()) - 3600)),
        "not yet valid": tenant.sign(tenant.claims(nbf=int(time.time()) + 3600)),
        "alg none": tenant.sign(tenant.claims(), alg="none"),
    }
    for reason, token in refused.items():
        assert verifier.verify(token) is None, reason

    good = tenant.sign(tenant.claims())
    tampered = good.rsplit(".", 1)[0][:-4] + "AAAA." + good.rsplit(".", 1)[1]
    assert verifier.verify(tampered) is None
    other_key = Tenant()
    assert verifier.verify(other_key.sign(tenant.claims())) is None


def test_an_unknown_key_id_refetches_once_and_no_more(tenant: Tenant) -> None:
    verifier = entra.EntraVerifier(TENANT, [CLIENT])
    assert verifier.verify(tenant.sign(tenant.claims()))
    fetched = tenant.jwks_fetches
    for _ in range(5):
        assert verifier.verify(tenant.sign(tenant.claims(), kid="invented")) is None
    # One forced refetch, then the floor holds: invented key ids cannot make
    # every request a call to Entra.
    assert tenant.jwks_fetches == fetched + 1


# -- the guard --------------------------------------------------------------


def test_the_shared_token_still_works_exactly_as_before(client: TestClient) -> None:
    assert client.get("/sources").status_code == 401
    me = client.get("/auth/me", headers=_bearer(SHARED)).json()
    assert me["principal"] == "shared-token"
    assert me["auth_method"] == "api_token"
    assert me["person"] is False
    assert client.get("/sources", headers=_bearer(SHARED)).status_code == 200


def test_a_refusal_tells_an_mcp_client_where_to_sign_in(client: TestClient) -> None:
    response = client.post("/mcp/", json={})
    assert response.status_code == 401
    challenge = response.headers["WWW-Authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in challenge


def test_an_entra_token_needs_a_granted_role(client: TestClient, tenant: Tenant) -> None:
    good = tenant.sign(tenant.claims())
    me = client.get("/auth/me", headers=_bearer(good)).json()
    assert me == {
        "principal": "alice@example.com",
        "auth_method": "entra_token",
        "display_name": "Alice Example",
        "roles": ["Reader"],
        "person": True,
    }
    no_role = tenant.sign(tenant.claims(roles=[]))
    assert client.get("/auth/me", headers=_bearer(no_role)).status_code == 401
    wrong_app = tenant.sign(tenant.claims(aud="another-app"))
    assert client.get("/sources", headers=_bearer(wrong_app)).status_code == 401


def test_every_authenticated_request_is_logged_with_who_and_how(
    client: TestClient, tenant: Tenant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="pheasant.access")
    client.get("/sources", headers=_bearer(tenant.sign(tenant.claims())))
    client.get("/sources", headers=_bearer("wrong"))
    lines = [r.getMessage() for r in caplog.records if r.name == "pheasant.access"]
    assert any(
        "GET /sources 200 principal=alice@example.com auth=entra_token" in line for line in lines
    )
    assert any("GET /sources 401 principal=- auth=rejected" in line for line in lines)


def test_the_ledger_records_the_verified_principal_and_method(
    client: TestClient, tenant: Tenant
) -> None:
    token = tenant.sign(tenant.claims())
    # An asserted principal is overruled by the verified one.
    client.get("/sources", headers={**_bearer(token), "X-Pheasant-Principal": "mallory"})
    app = client.app
    app.state.interaction_buffer.flush()
    row = app.state.state.rows(
        "SELECT principal, attributes_json FROM interaction_events WHERE operation='/sources'",
        (),
    )[0]
    assert row["principal"] == "alice@example.com"
    assert json.loads(row["attributes_json"])["auth_method"] == "entra_token"


# -- personal keys ------------------------------------------------------------


def test_a_signed_in_person_mints_uses_and_revokes_a_key(
    client: TestClient, tenant: Tenant
) -> None:
    person = _bearer(tenant.sign(tenant.claims()))
    created = client.post("/auth/keys", json={"name": "laptop", "days": 30}, headers=person)
    assert created.status_code == 200, created.text
    key = created.json()["key"]
    assert key.startswith("pht_")

    me = client.get("/auth/me", headers=_bearer(key)).json()
    assert (me["principal"], me["auth_method"]) == ("alice@example.com", "personal_key")

    listed = client.get("/auth/keys", headers=person).json()["keys"]
    assert [k["name"] for k in listed] == ["laptop"]
    assert "key" not in listed[0] and "secret_hash" not in listed[0]

    # Another person cannot revoke it.
    bob = _bearer(tenant.sign(tenant.claims(preferred_username="bob@example.com", oid="b0b")))
    assert client.delete(f"/auth/keys/{listed[0]['id']}", headers=bob).json() == {"revoked": False}
    assert client.delete(f"/auth/keys/{listed[0]['id']}", headers=person).json() == {
        "revoked": True
    }
    assert client.get("/auth/me", headers=_bearer(key)).status_code == 401


def test_the_shared_token_cannot_mint_a_personal_key(client: TestClient) -> None:
    response = client.post("/auth/keys", json={"name": "x"}, headers=_bearer(SHARED))
    assert response.status_code == 403
    assert "names no one" in response.json()["detail"]


def test_a_key_longer_than_the_limit_is_refused(client: TestClient, tenant: Tenant) -> None:
    person = _bearer(tenant.sign(tenant.claims()))
    response = client.post("/auth/keys", json={"name": "x", "days": 4000}, headers=person)
    assert response.status_code == 422


# -- the browser sign-in ------------------------------------------------------


def _complete_entra(client: TestClient, tenant: Tenant, authorize_url: str, **claims: Any):
    """Play Entra: sign the id token with the request's nonce, call back."""

    query = parse_qs(urlsplit(authorize_url).query)
    assert query["client_id"] == [CLIENT]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [f"{BASE}/auth/callback"]
    tenant.next_id_token = tenant.sign(tenant.claims(nonce=query["nonce"][0], **claims))
    return client.get("/auth/callback", params={"code": "entra-code", "state": query["state"][0]})


def test_a_browser_signs_in_and_gets_a_session(client: TestClient, tenant: Tenant) -> None:
    started = client.get("/auth/login", params={"next": "/sources"})
    assert started.status_code == 302
    assert started.headers["location"].startswith(
        f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?"
    )
    done = _complete_entra(client, tenant, started.headers["location"])
    assert done.status_code == 302
    target = done.headers["location"]
    assert target.startswith("/sources#pheasant_session=pht_")
    redeemed = tenant.token_requests[-1]
    assert redeemed["client_secret"] == "client-secret" and redeemed["code_verifier"]

    session = target.split("pheasant_session=", 1)[1]
    me = client.get("/auth/me", headers=_bearer(session)).json()
    assert (me["principal"], me["auth_method"]) == ("alice@example.com", "entra_sso")

    # The state is one use: a replayed callback is refused.
    replay = client.get("/auth/callback", params={"code": "c", "state": "pht_x_y"})
    assert replay.status_code == 401

    client.post("/auth/logout", headers=_bearer(session))
    assert client.get("/auth/me", headers=_bearer(session)).status_code == 401


def test_an_unassigned_account_is_refused_at_sign_in(client: TestClient, tenant: Tenant) -> None:
    started = client.get("/auth/login")
    done = _complete_entra(client, tenant, started.headers["location"], roles=[])
    assert done.status_code == 401
    assert "not been granted access" in done.text


def test_next_cannot_send_a_browser_to_another_origin(client: TestClient, tenant: Tenant) -> None:
    started = client.get("/auth/login", params={"next": "//evil.example/x"})
    done = _complete_entra(client, tenant, started.headers["location"])
    assert done.headers["location"].startswith("/#pheasant_session=")


def test_a_federated_identity_signs_the_code_redemption(
    tmp_path: Path, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No secret anywhere: the region's managed identity is the client assertion."""

    monkeypatch.setenv("PHEASANT_API_TOKEN", SHARED)
    monkeypatch.delenv("PHEASANT_ENTRA_CLIENT_SECRET", raising=False)
    asked: list[tuple[str, str | None]] = []

    def fake_mi(resource: str, client_id: str | None = None) -> str:
        asked.append((resource, client_id))
        return "mi-assertion"

    monkeypatch.setattr(entra, "managed_identity_token", fake_mi)
    app = create_app(
        _config(tmp_path, federated_identity_client_id="mi-client"),
        config_path=str(tmp_path / "pheasant.yaml"),
    )
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        started = client.get("/auth/login")
        done = _complete_entra(client, tenant, started.headers["location"])
        assert done.status_code == 302
    app.state.engine.close()
    assert asked == [("api://AzureADTokenExchange", "mi-client")]
    redeemed = tenant.token_requests[-1]
    assert redeemed["client_assertion"] == "mi-assertion"
    assert "client_secret" not in redeemed


# -- the MCP authorization server -------------------------------------------


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _register(client: TestClient, redirect: str = "http://127.0.0.1:33418/callback") -> str:
    response = client.post(
        "/register",
        json={
            "client_name": "Claude Code",
            "redirect_uris": [redirect],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def _authorize(client: TestClient, tenant: Tenant, client_id: str, challenge: str, **claims):
    started = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:33418/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "client-state",
            "resource": f"{BASE}/mcp",
        },
    )
    assert started.status_code == 302, started.text
    return _complete_entra(client, tenant, started.headers["location"], **claims)


def test_the_metadata_points_mcp_clients_at_this_region(client: TestClient) -> None:
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == f"{BASE}/mcp"
    assert resource["authorization_servers"] == [f"{BASE}/"]
    server = client.get("/.well-known/oauth-authorization-server").json()
    assert server["authorization_endpoint"] == f"{BASE}/authorize"
    assert server["registration_endpoint"] == f"{BASE}/register"
    assert "S256" in server["code_challenge_methods_supported"]


def test_an_mcp_client_signs_in_through_entra_and_consent(
    client: TestClient, tenant: Tenant
) -> None:
    client_id = _register(client)
    verifier, challenge = _pkce()
    consent = _authorize(client, tenant, client_id, challenge)
    assert consent.status_code == 302
    assert consent.headers["location"].startswith("/auth/consent?request=")

    page = client.get(consent.headers["location"])
    assert page.status_code == 200
    assert "Claude Code" in page.text and "alice@example.com" in page.text
    assert "127.0.0.1:33418" in page.text
    assert page.headers["X-Frame-Options"] == "DENY"

    request = parse_qs(urlsplit(consent.headers["location"]).query)["request"][0]
    approved = client.post("/auth/consent", data={"request": request, "decision": "allow"})
    assert approved.status_code == 302
    back = urlsplit(approved.headers["location"])
    assert (back.netloc, back.path) == ("127.0.0.1:33418", "/callback")
    code = parse_qs(back.query)["code"][0]
    assert parse_qs(back.query)["state"] == ["client-state"]

    tokens = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "code_verifier": verifier,
            "redirect_uri": "http://127.0.0.1:33418/callback",
        },
    )
    assert tokens.status_code == 200, tokens.text
    pair = tokens.json()
    me = client.get("/auth/me", headers=_bearer(pair["access_token"])).json()
    assert (me["principal"], me["auth_method"]) == ("alice@example.com", "mcp_oauth")

    # The code is one use.
    again = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "code_verifier": verifier,
            "redirect_uri": "http://127.0.0.1:33418/callback",
        },
    )
    assert again.status_code == 400

    refreshed = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": pair["refresh_token"],
            "client_id": client_id,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    assert (
        client.get("/auth/me", headers=_bearer(refreshed.json()["access_token"])).status_code == 200
    )


def test_a_wrong_pkce_verifier_gets_no_token(client: TestClient, tenant: Tenant) -> None:
    client_id = _register(client)
    _verifier, challenge = _pkce()
    consent = _authorize(client, tenant, client_id, challenge)
    request = parse_qs(urlsplit(consent.headers["location"]).query)["request"][0]
    approved = client.post("/auth/consent", data={"request": request, "decision": "allow"})
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    tokens = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "code_verifier": "not-the-verifier-" + "x" * 40,
            "redirect_uri": "http://127.0.0.1:33418/callback",
        },
    )
    assert tokens.status_code == 400


def test_denying_consent_sends_the_client_an_error_and_no_code(
    client: TestClient, tenant: Tenant
) -> None:
    client_id = _register(client)
    _verifier, challenge = _pkce()
    consent = _authorize(client, tenant, client_id, challenge)
    request = parse_qs(urlsplit(consent.headers["location"]).query)["request"][0]
    denied = client.post("/auth/consent", data={"request": request, "decision": "deny"})
    query = parse_qs(urlsplit(denied.headers["location"]).query)
    assert query["error"] == ["access_denied"] and "code" not in query
    # And the approval cannot be replayed into an allow afterwards.
    replay = client.post("/auth/consent", data={"request": request, "decision": "allow"})
    assert replay.status_code == 401


def test_an_mcp_tool_call_is_recorded_under_the_signed_in_person(
    client: TestClient, tenant: Tenant
) -> None:
    pytest.importorskip("mcp")
    token = tenant.sign(tenant.claims())
    headers = {**_bearer(token), "accept": "application/json, text/event-stream"}
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "list_sources", "arguments": {"knowledge_base": "kb"}},
    }
    response = client.post("/mcp/", json=call, headers=headers)
    assert response.status_code == 200, response.text[:300]
    app = client.app
    app.state.interaction_buffer.flush()
    rows = app.state.state.rows(
        "SELECT principal, attributes_json FROM interaction_events WHERE modality='mcp'", ()
    )
    assert rows, "the tool call was not observed"
    assert rows[0]["principal"] == "alice@example.com"
    assert json.loads(rows[0]["attributes_json"])["auth_method"] == "entra_token"


def test_the_public_url_is_an_mcp_host_the_guard_admits(tmp_path: Path) -> None:
    """A client that followed the region's own OAuth metadata to `public_url`
    must not then be refused by the DNS-rebinding guard (421)."""

    pytest.importorskip("mcp")
    from pheasant.mcp_server.server import _transport_security

    config = _config(tmp_path, public_url="https://kb.example.azurecontainerapps.io")
    settings = _transport_security(config)
    assert settings.enable_dns_rebinding_protection is True
    assert "kb.example.azurecontainerapps.io" in settings.allowed_hosts
    assert "https://kb.example.azurecontainerapps.io" in settings.allowed_origins
    assert "evil.example" not in settings.allowed_hosts


# -- standalone stays as it was ----------------------------------------------


def test_with_nothing_configured_the_api_needs_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 7: a laptop sets neither the token nor Entra."""

    monkeypatch.delenv("PHEASANT_API_TOKEN", raising=False)
    config = _config(tmp_path, enabled=False)
    app = create_app(config, config_path=str(tmp_path / "pheasant.yaml"))
    with TestClient(app, base_url=BASE) as client:
        assert client.get("/sources").status_code == 200
        assert client.get("/auth/config").json()["entra"] is False
        assert client.get("/.well-known/oauth-authorization-server").status_code == 404
    app.state.engine.close()


def test_entra_alone_satisfies_the_routable_bind_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    from pheasant.deployment.roles import (
        RoleConfigurationError,
        _validate_serving_exposure,
        resolve_role,
    )

    monkeypatch.delenv("PHEASANT_API_TOKEN", raising=False)

    def build(entra_on: bool) -> PheasantConfig:
        return PheasantConfig.model_validate(
            {
                "pheasant": {"name": "kb", "defaults": "standalone"},
                "server": {"host": "0.0.0.0", "role": "api"},
                "security": {
                    "entra": {"enabled": entra_on, "tenant_id": TENANT, "client_id": CLIENT}
                },
            }
        )

    with pytest.raises(RoleConfigurationError):
        _validate_serving_exposure(resolve_role(build(False)), build(False))
    _validate_serving_exposure(resolve_role(build(True)), build(True))
