"""Authorization across HTTP, MCP, indirect writes and credential lifetimes."""

from __future__ import annotations

import ast
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.mcp_server.server import _anticipated_failures
from pheasant.persistence import auth_tokens
from pheasant.security.authorization import (
    ROLE_PERMISSIONS,
    PermissionDenied,
    allowed,
    background_target,
    require_configuration_change,
    require_permission,
)
from pheasant.security.identity import Identity, bind_identity, reset_identity
from pheasant.security.permissions import HTTP_PERMISSIONS, MCP_PERMISSIONS
from pheasant.services import ServiceContext, archive, artifact_records, artifacts, ingestion
from tests.test_entra_identity import TENANT, Tenant, _config


@pytest.fixture
def region(tmp_path, monkeypatch):
    monkeypatch.setenv("PHEASANT_API_TOKEN", "admin-test-token")
    monkeypatch.setenv("READER_TEST_TOKEN", "reader-test-token")
    monkeypatch.setenv("CONTRIBUTOR_TEST_TOKEN", "contributor-test-token")
    config = _config(tmp_path)
    config.artifacts.index_on_save = False
    config.security.api_auth.role_tokens = {
        "READER_TEST_TOKEN": "Reader",
        "CONTRIBUTOR_TEST_TOKEN": "Contributor",
    }
    app = create_app(config, config_path=str(tmp_path / "pheasant.yaml"))
    with TestClient(app) as client:
        yield app, client, config
    app.state.engine.close()


def headers(role):
    return {"Authorization": f"Bearer {role.lower()}-test-token"}


@pytest.mark.parametrize("role", ["Admin", "Contributor", "Reader"])
def test_identity_exposes_effective_permissions(region, role):
    _, client, _ = region
    result = client.get("/auth/me", headers=headers(role))
    assert result.status_code == 200
    assert result.json()["role"] == role
    assert set(result.json()["permissions"]) == ROLE_PERMISSIONS[role]
    assert client.get("/sources", headers=headers(role)).status_code == 200
    assert client.post("/search", headers=headers(role), json={"query": "hello"}).status_code == 200
    assert client.get("/sources").status_code == 401


def test_every_forbidden_http_operation_refuses_before_body_or_side_effects(region):
    app, client, _ = region
    tested = 0
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or ():
            permission = HTTP_PERMISSIONS.get((method, route.path))
            if permission is None or permission in ROLE_PERMISSIONS["Reader"]:
                continue
            path = route.path
            for param in route.param_convertors:
                path = path.replace("{" + param + "}", "test")
            result = client.request(method, path, headers=headers("Reader"), json={})
            assert result.status_code == 403, (method, path, result.text)
            assert result.json()["code"] == "PERMISSION_DENIED"
            tested += 1
    assert tested >= 50


@pytest.mark.parametrize("role", ["Admin", "Contributor"])
def test_writers_retain_source_registration(region, role):
    _, client, _ = region
    result = client.post(
        "/sources",
        headers=headers(role),
        json={
            "name": "test",
            "type": "web_collection",
            "path": "",
            "urls": ["https://example.com/"],
            "sync_now": False,
        },
    )
    assert result.status_code < 400, result.text
    assert any(s["name"] == "test" for s in client.get("/sources", headers=headers(role)).json())


def test_contributor_cannot_change_security_or_infrastructure(region):
    _, client, _ = region
    for path, body in [
        (
            "/config/section/security",
            {"values": {"api_auth": {"token_env": "OTHER"}}, "persist": False},
        ),
        ("/config/section/storage", {"values": {"backend": "postgres"}, "persist": False}),
    ]:
        result = client.patch(path, headers=headers("Contributor"), json=body)
        assert result.status_code == 403, result.text


def test_manifest_covers_all_http_routes_and_mcp_operations(region):
    app, _, _ = region
    for route in app.routes:
        if (
            not isinstance(route, APIRoute)
            or route.path.startswith(("/auth/", "/internal/"))
            or route.path == "/mcp"
        ):
            continue
        for method in route.methods or ():
            assert (method, route.path) in HTTP_PERMISSIONS, (method, route.path)
    root = Path(__file__).parents[1] / "src/pheasant/mcp_server"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in {"tool", "resource"}
                ):
                    assert node.name in MCP_PERMISSIONS, (path, node.name)


def test_mcp_direct_invocation_and_unclassified_tools_fail_closed():
    from mcp.server.mcpserver.exceptions import ToolError

    called = []

    def register_source():
        called.append(True)

    def future_unclassified_tool():
        called.append(True)

    token = bind_identity(Identity("reader", "personal_key", roles=("Reader",)))
    try:
        for operation in (register_source, future_unclassified_tool):
            with pytest.raises(ToolError, match="permission required"):
                _anticipated_failures("tool")(operation)()
        assert called == []
    finally:
        reset_identity(token)


def test_reader_cannot_write_indirectly_and_autosave_is_suppressed(region):
    app, _, config = region
    context = ServiceContext(config, app.state.engine.state, None)
    token = bind_identity(Identity("reader", "personal_key", roles=("Reader",)))
    try:
        config.artifacts.auto_save = True
        result = {"route": {"intent": "compose"}, "files": [{"path": "test.py", "content": "x=1"}]}
        assert artifacts.keep_composed(context, "compose", "forged", result) is None
        with pytest.raises(PermissionDenied):
            artifacts.keep_composed(context, "compose", "forged", result, save=True)
        for operation in (
            lambda: artifacts.save(context, artifacts.SaveRequest(markdown="test")),
            lambda: artifact_records.write(context, None),
            lambda: archive.archive_documents(context, None),
            lambda: ingestion.submit(context, None),
        ):
            with pytest.raises(PermissionDenied):
                operation()
    finally:
        reset_identity(token)


def test_background_work_preserves_reader_restrictions():
    result = []
    token = bind_identity(Identity("reader", "personal_key", roles=("Reader",)))
    try:
        target = background_target(lambda: result.append(allowed("artifacts.manage")))
    finally:
        reset_identity(token)
    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    assert result == [False]


def test_key_narrowing_downgrade_removal_and_refresh(region):
    app, _, config = region
    service = app.state.identity
    identity = Identity(
        "alice@example.com", "entra_sso", subject="alice", roles=("Contributor",), tenant_id=TENANT
    )
    config.security.entra.role_assignments = {"alice": "Contributor"}
    key = service.create_personal_key(identity, name="reader only", days=1, role="Reader")["key"]
    assert service.authenticate(key).as_dict()["role"] == "Reader"
    assert service.authenticate(key).as_dict()["permissions"] == sorted(ROLE_PERMISSIONS["Reader"])
    with pytest.raises(PermissionDenied):
        service.create_personal_key(identity, name="escalate", days=1, role="Admin")
    full = service.create_personal_key(identity, name="full", days=1)["key"]
    assert "sources.manage" in service.authenticate(full).as_dict()["permissions"]
    config.security.entra.role_assignments["alice"] = "Reader"
    assert "sources.manage" not in service.authenticate(full).as_dict()["permissions"]
    config.security.entra.role_assignments = {}
    assert service.authenticate(full) is None
    assert service.authenticate(key) is None


def test_personal_key_ownership_survives_login_rename(region):
    app, _, _ = region
    service = app.state.identity
    owner = Identity(
        "old@example.com", "entra_sso", subject="stable-id", roles=("Reader",), tenant_id=TENANT
    )
    key = service.create_personal_key(owner, name="laptop", days=1)
    renamed = Identity(
        "new@example.com", "entra_sso", subject="stable-id", roles=("Reader",), tenant_id=TENANT
    )
    stranger = Identity(
        "old@example.com", "entra_sso", subject="different-id", roles=("Reader",), tenant_id=TENANT
    )
    assert {row["id"] for row in service.list_personal_keys(renamed)} == {key["id"]}
    assert not service.revoke_personal_key(stranger, key["id"])
    assert service.revoke_personal_key(renamed, key["id"])


def test_oauth_refresh_cannot_restore_removed_or_narrowed_grants(region):
    app, _, config = region
    service = app.state.identity
    config.security.entra.role_assignments = {"alice": "Contributor"}
    record = {
        "principal": "alice",
        "subject": "alice",
        "roles": ["Contributor"],
        "client_id": "test-client",
        "data": {"tenant_id": TENANT, "scopes": ["pheasant"]},
    }
    pair = service._token_pair(record)
    config.security.entra.role_assignments["alice"] = "Reader"
    narrowed = service.exchange_refresh(pair["refresh_token"], "test-client", ["pheasant"])
    assert service.authenticate(narrowed["access_token"]).as_dict()["role"] == "Reader"
    config.security.entra.role_assignments = {}
    from pheasant.services.errors import SignInFailed

    with pytest.raises(SignInFailed):
        service.exchange_refresh(narrowed["refresh_token"], "test-client", ["pheasant"])


def test_signed_entra_roles_and_immutable_subject(region, monkeypatch):
    app, client, config = region
    tenant = Tenant()
    monkeypatch.setattr("pheasant.security.entra._http_json", tenant.http)
    oid = tenant.claims()["oid"]
    config.security.entra.role_assignments = {oid: "Reader"}
    bearer = tenant.sign(tenant.claims(roles=["Admin"], preferred_username="renamed@example.com"))
    result = client.get("/auth/me", headers={"Authorization": f"Bearer {bearer}"})
    assert result.status_code == 200
    assert result.json()["role"] == "Reader"
    assert result.json()["subject"] == oid
    config.security.entra.role_assignments = {}
    assert app.state.identity.authenticate(bearer) is None


def test_token_expiration_and_revocation(region):
    app, _, _ = region
    service = app.state.identity
    identity = Identity("alice", "entra_sso", roles=("Reader",))
    key = service.create_personal_key(identity, name="key", days=1)
    assert service.authenticate(key["key"])
    assert service.revoke_personal_key(identity, key["id"])
    assert service.authenticate(key["key"]) is None
    expired, _ = auth_tokens.issue(
        service.state, kind="personal", lifetime=timedelta(seconds=-1), roles=["Admin"]
    )
    assert service.authenticate(expired) is None


def test_reader_can_generate_without_autosave(region, monkeypatch):
    from tests.test_artifacts import _composed

    app, client, config = region
    config.artifacts.auto_save = True
    monkeypatch.setattr(
        "pheasant.assistant.chat.answer_question", lambda question, **kw: _composed(question)
    )
    response = client.post(
        "/assistant/chat", headers=headers("Reader"), json={"question": "compose a draft"}
    )
    assert response.status_code == 200, response.text
    assert "account_counts.js" in response.json()["answer"]
    assert "artifact" not in response.json()
    assert app.state.state.rows("SELECT id FROM saved_artifacts") == []


def test_credential_cache_never_extends_expiry(region, monkeypatch):
    import time

    app, _, _ = region
    service = app.state.identity
    key, _ = auth_tokens.issue(
        service.state, kind="personal", lifetime=timedelta(seconds=5), roles=["Reader"]
    )
    assert service.authenticate(key)
    wall, monotonic = time.time(), time.monotonic()
    monkeypatch.setattr("pheasant.services.identity.time.time", lambda: wall + 10)
    monkeypatch.setattr("pheasant.services.identity.time.monotonic", lambda: monotonic + 10)
    monkeypatch.setattr(
        auth_tokens,
        "_now",
        lambda: __import__("datetime").datetime.fromtimestamp(
            wall + 10, __import__("datetime").UTC
        ),
    )
    assert service.authenticate(key) is None


def test_warm_credentials_do_not_add_persistence_reads(region, monkeypatch):
    app, _, _ = region
    service = app.state.identity
    key = service.create_personal_key(
        Identity("alice", "entra_sso", roles=("Reader",)), name="test", days=1
    )["key"]
    assert service.authenticate(key)
    monkeypatch.setattr(
        service, "_resolve", lambda value: pytest.fail("cached credential hit persistence")
    )
    for _ in range(1000):
        assert service.authenticate(key).roles == ("Reader",)


def test_contributor_configuration_scope():
    token = bind_identity(Identity("contributor", "personal_key", roles=("Contributor",)))
    try:
        require_configuration_change(
            {"assistant": {"retrieval": {"max_rounds": 1}}},
            {"assistant": {"retrieval": {"max_rounds": 2}}},
        )
        with pytest.raises(PermissionDenied):
            require_configuration_change(
                {"security": {}}, {"security": {"api_auth": {"token_env": "x"}}}
            )
        for change in (
            {"memory": {"synthesis": {"base_url": "https://attacker.test"}}},
            {"assistant": {"workflow_options": {"agentic": {"api_key_env": "PHEASANT_API_TOKEN"}}}},
        ):
            with pytest.raises(PermissionDenied):
                require_configuration_change({}, change)
        require_permission("sources.manage")
    finally:
        reset_identity(token)


def test_missing_role_token_values_fail_closed(region):
    app, _, config = region
    from pheasant.services.identity import IdentityService

    config.security.entra.enabled = False
    config.security.api_auth.token_env = ""
    config.security.api_auth.role_tokens = {"MISSING_ROLE_TOKEN": "Reader"}
    service = IdentityService(config, app.state.state)
    assert service.enforcing
    assert service.authenticate("anything") is None
