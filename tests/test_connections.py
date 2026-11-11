"""Connected accounts: sealed at rest, read by every secret consumer, one API.

A connection is a token, key or sign-in a person connects from the UI, an
agent over MCP, or a script over HTTP. These pin the properties that make that
safe and useful:

* sealed with a key that is not stored beside it, and refused under another;
* a secret never comes back from any surface, and one its service rejects is
  not stored at all;
* every consumer reads through it -- git clones, Drive, API sources, model
  keys -- with an operator's environment variable still winning over a
  connection that stands in for it, and a source's named connection winning
  over everything;
* both transports answer identically, refusals included;
* a Drive sign-in round trip completes once, and only once.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant import connections, git_auth
from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.connections import google, oauth, providers, resolve, vault
from pheasant.mcp_server.tools import PheasantTools
from pheasant.services import connections as service
from pheasant.services.errors import (
    ConnectionInUse,
    ConnectionNotFound,
    ConnectionRejected,
    ServiceError,
    SignInExpired,
)

GITHUB_TOKEN = "github_pat_0123456789abcdef"


def _config(root: Path, **sources: Any) -> PheasantConfig:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "readme.md").write_text("# Notes\n\nPlain words.\n", encoding="utf-8")
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "connections",
                "state_path": str(root / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(root / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {"graph_snapshots": False},
            "sources": [
                {"name": "notes", "type": "markdown_folder", "path": str(workspace)},
                *sources.get("extra", []),
            ],
        }
    )


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch):
    for name in ("PHEASANT_CREDENTIAL_KEY", "GITHUB_TOKEN", "GH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    # Every check that would reach a service answers from here instead.
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_request(url: str, headers: dict[str, str], *, data: bytes | None = None) -> Any:
        calls.append((url, headers))
        if "api.github.com/user" in url:
            if headers.get("Authorization") != f"Bearer {GITHUB_TOKEN}":
                raise _http_error(url, 401)
            return {"login": "octo"}
        if url.endswith("/models"):
            return {"data": [{"id": "a"}, {"id": "b"}]}
        if "drive/v3/about" in url:
            return {"user": {"emailAddress": "person@example.com"}}
        raise _http_error(url, 404)

    monkeypatch.setattr(providers, "_request", fake_request)

    def no_network(url: str, form: dict[str, str]) -> Any:
        raise AssertionError(f"a test reached a real token endpoint: {url}")

    # A test that forgets to stand in for a token endpoint fails here rather
    # than calling the real one.
    monkeypatch.setattr(oauth, "_post", no_network)
    google.forget_cached_tokens()
    yield calls
    resolve.uninstall()


def _http_error(url: str, code: int) -> Exception:
    from urllib.error import HTTPError

    return HTTPError(url, code, "nope", {}, None)  # type: ignore[arg-type]


@pytest.fixture
def tools(tmp_path: Path) -> PheasantTools:
    return PheasantTools(_config(tmp_path))


# ---------------------------------------------------------------------------
# Sealing
# ---------------------------------------------------------------------------


def test_a_sealed_secret_opens_only_with_its_key_and_its_row(tmp_path: Path) -> None:
    settings = PheasantConfig().security.connections
    key = vault.resolve_key(settings, tmp_path, create=True)
    assert key.source == "file"
    assert (tmp_path / vault.KEY_FILE).is_file()
    # The same file is the same key on the next read.
    assert vault.resolve_key(settings, tmp_path, create=False).key == key.key

    blob = vault.seal(key, "github", "github", {"token": "s3cret"})
    assert "s3cret" not in blob
    row = vault.Connection("github", "github", "token", "connected", sealed=blob, key_id=key.key_id)
    assert vault.unseal(key, row) == {"token": "s3cret"}

    other = vault.VaultKey(b"x" * 32, "environment")
    with pytest.raises(vault.SealedUnderAnotherKey, match="sealed under another key"):
        vault.unseal(other, row)
    # Copied onto another row, the blob does not open: the name is bound in.
    moved = vault.Connection("gitlab", "gitlab", "token", "connected", sealed=blob)
    with pytest.raises(vault.SealedUnderAnotherKey):
        vault.unseal(key, moved)


def test_the_environment_key_wins_and_no_key_says_what_to_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = PheasantConfig().security.connections
    monkeypatch.setenv("PHEASANT_CREDENTIAL_KEY", "a long shared value")
    assert vault.resolve_key(settings, tmp_path, create=True).source == "environment"
    assert not (tmp_path / vault.KEY_FILE).exists()
    monkeypatch.delenv("PHEASANT_CREDENTIAL_KEY")
    with pytest.raises(vault.VaultUnavailable, match="PHEASANT_CREDENTIAL_KEY"):
        vault.resolve_key(settings, tmp_path, create=False)


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------


def test_a_connected_token_never_comes_back(tools: PheasantTools) -> None:
    result = tools.connect_service("github", {"token": GITHUB_TOKEN})
    assert result["status"] == "connected"
    record = result["connection"]
    assert record["details"]["account"] == "octo"
    assert record["details"]["hint"] == "…cdef"
    assert record["details"]["added_via"] == "mcp"
    assert record["check"]["ok"] is True

    listed = tools.list_connections()
    assert [c["name"] for c in listed["connections"]] == ["github"]
    assert GITHUB_TOKEN not in json.dumps(listed)
    assert GITHUB_TOKEN not in json.dumps(result)
    # Nor is it in the row in plain text.
    stored = tools.state.rows("SELECT * FROM connections")[0]
    assert GITHUB_TOKEN not in json.dumps(dict(stored))


def test_a_token_the_service_rejects_is_not_stored(tools: PheasantTools) -> None:
    with pytest.raises(ConnectionRejected, match=r"GitHub rejected it \(401\); nothing was stored"):
        tools.connect_service("github", {"token": "wrong"})
    assert tools.list_connections()["connections"] == []


def test_unknown_providers_methods_and_fields_are_refused(tools: PheasantTools) -> None:
    with pytest.raises(ServiceError, match="unknown provider"):
        tools.connect_service("dropbox", {"token": "x"})
    with pytest.raises(ServiceError, match="unknown field"):
        tools.connect_service("github", {"password": "x"})
    with pytest.raises(ServiceError, match="needs Personal access token"):
        tools.connect_service("github", {})
    with pytest.raises(ServiceError, match="sign-in happens in a browser"):
        tools.connect_service("google_drive", {}, method="oauth")


# ---------------------------------------------------------------------------
# Reading: every consumer
# ---------------------------------------------------------------------------


def test_a_model_key_stands_in_for_its_variable_unless_the_operator_set_it(
    tools: PheasantTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert connections.secret("OPENAI_API_KEY") is None
    tools.connect_service("openai", {"api_key": "sk-connected-123456"})
    assert connections.secret("OPENAI_API_KEY") == "sk-connected-123456"
    assert connections.environment()["OPENAI_API_KEY"] == "sk-connected-123456"
    listed = tools.list_connections()["connections"][0]
    assert listed["stands_in_for"] == "OPENAI_API_KEY"
    assert listed["overridden_by_environment"] is False

    monkeypatch.setenv("OPENAI_API_KEY", "sk-operator")
    assert connections.secret("OPENAI_API_KEY") == "sk-operator"
    assert connections.environment()["OPENAI_API_KEY"] == "sk-operator"
    assert tools.list_connections()["connections"][0]["overridden_by_environment"] is True


def test_the_chat_provider_resolves_a_connected_key(tools: PheasantTools) -> None:
    from pheasant.assistant.catalog import resolve_auto_provider

    assert resolve_auto_provider(connections.environment()) is None
    tools.connect_service("openai", {"api_key": "sk-connected-123456"})
    assert resolve_auto_provider(connections.environment()) == "openai"


def test_an_environment_variable_connection_answers_for_any_named_variable(
    tools: PheasantTools,
) -> None:
    tools.connect_service("environment", {"variable": "MY_SERVICE_TOKEN", "value": "v-123456789"})
    assert tools.list_connections()["connections"][0]["name"] == "MY_SERVICE_TOKEN"
    assert connections.secret("MY_SERVICE_TOKEN") == "v-123456789"


def _git_header(env: dict[str, str], host: str) -> str | None:
    count = int(env["GIT_CONFIG_COUNT"])
    for index in range(count):
        if env[f"GIT_CONFIG_KEY_{index}"] == f"http.https://{host}/.extraheader":
            return env[f"GIT_CONFIG_VALUE_{index}"]
    return None


def _basic(user: str, token: str) -> str:
    return "AUTHORIZATION: basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


def test_a_git_clone_is_signed_with_the_connected_account(
    tools: PheasantTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://github.com/owner/private"
    assert _git_header(git_auth.git_env(url), "github.com") is None
    tools.connect_service("github", {"token": GITHUB_TOKEN})
    # One GitHub account connected: a pasted URL reads with it.
    assert _git_header(git_auth.git_env(url), "github.com") == _basic(
        "x-access-token", GITHUB_TOKEN
    )
    # ...and with no other host.
    assert _git_header(git_auth.git_env("https://gitlab.com/a/b"), "github.com") is None

    # An operator's GITHUB_TOKEN wins over the ambient connection...
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_operator")
    assert _git_header(git_auth.git_env(url), "github.com") == _basic(
        "x-access-token", "ghp_operator"
    )
    # ...and a connection the source names wins over that.
    with git_auth.using_connection("github"):
        assert _git_header(git_auth.git_env(url), "github.com") == _basic(
            "x-access-token", GITHUB_TOKEN
        )


def test_an_azure_devops_token_signs_its_own_host(tools: PheasantTools) -> None:
    tools.connect_service("azure_devops", {"token": "ado-pat-123456789"})
    env = git_auth.git_env("https://dev.azure.com/org/project/_git/repo")
    assert _git_header(env, "dev.azure.com") == _basic("pheasant", "ado-pat-123456789")


def test_a_named_connection_the_region_lacks_fails_the_clone_by_name(
    tools: PheasantTools,
) -> None:
    from pheasant.targets import ResolvedTarget, TargetError, fetch_target

    target = ResolvedTarget(
        name="private",
        type="repository",
        path=str(Path(tools.config.pheasant.state_path) / "sources" / "private"),
        description="",
        clone_url="https://github.com/owner/private",
        connector={"connection": "missing"},
    )
    with pytest.raises(TargetError, match="no connection named 'missing'"):
        fetch_target(target)


def test_an_api_source_sends_its_connections_token(tools: PheasantTools) -> None:
    from pheasant.sync.connectors import ConnectorUnavailable, source_request_headers

    tools.connect_service("api_token", {"token": "api-token-123456"}, name="internal-api")
    source = PheasantConfig.model_validate(
        {
            "sources": [
                {
                    "name": "api",
                    "type": "api",
                    "path": "/unused",
                    "connector": {"connection": "internal-api", "headers": {"X-A": "1"}},
                }
            ]
        }
    ).sources[0]
    assert source_request_headers(source) == {
        "X-A": "1",
        "Authorization": "Bearer api-token-123456",
    }
    source.connector.connection = "absent"
    with pytest.raises(ConnectorUnavailable, match="no connection named 'absent'"):
        source_request_headers(source)


def test_a_drive_source_reads_with_its_service_account(
    tools: PheasantTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from pheasant.connectors.gdrive import GDriveConnector

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    key = {
        "type": "service_account",
        "client_email": "indexer@project.iam.gserviceaccount.com",
        "private_key": pem,
    }
    posted: list[dict[str, str]] = []

    def fake_post(url: str, form: dict[str, str]) -> dict[str, Any]:
        posted.append(form)
        return {"access_token": "ya29.service", "expires_in": 3600}

    monkeypatch.setattr(google, "_post", fake_post)
    result = tools.connect_service(
        "google_drive", {"service_account_json": json.dumps(key)}, method="service_account"
    )
    assert result["connection"]["details"]["account"] == "person@example.com"
    source = PheasantConfig.model_validate(
        {
            "sources": [
                {
                    "name": "drive",
                    "type": "gdrive",
                    "path": "/unused",
                    "connector": {"connection": "google_drive"},
                }
            ]
        }
    ).sources[0]
    assert GDriveConnector(source, tools.state)._token() == "ya29.service"
    # Signed once, then reused until it nears expiry.
    assert len(posted) == 1
    assert posted[0]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"


# ---------------------------------------------------------------------------
# Removing, roles
# ---------------------------------------------------------------------------


def test_a_connection_a_source_reads_with_is_not_removed_by_accident(tmp_path: Path) -> None:
    extra = [
        {
            "name": "api",
            "type": "api",
            "path": "/unused",
            "connector": {"connection": "api_token"},
        }
    ]
    tools = PheasantTools(_config(tmp_path, extra=extra))
    tools.connect_service("api_token", {"token": "api-token-123456"})
    assert tools.list_connections()["connections"][0]["used_by"] == ["api"]
    with pytest.raises(ConnectionInUse, match="what api read with"):
        tools.remove_connection("api_token")
    assert tools.remove_connection("api_token", force=True)["sources_without_it"] == ["api"]
    with pytest.raises(ConnectionNotFound, match="Unknown connection: api_token"):
        tools.remove_connection("api_token")


def test_a_reader_may_not_connect_accounts(tools: PheasantTools) -> None:
    from pheasant.security.identity import Identity, bind_identity, reset_identity
    from pheasant.services.errors import ConnectionForbidden

    token = bind_identity(Identity("reader@example.com", "entra", roles=("Reader",)))
    try:
        with pytest.raises(ConnectionForbidden):
            tools.connect_service("github", {"token": GITHUB_TOKEN})
    finally:
        reset_identity(token)


def test_a_worker_refuses_the_connection_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from pheasant.deployment.roles import _credential_envs

    names = dict(_credential_envs(PheasantConfig()))
    assert "PHEASANT_CREDENTIAL_KEY" in names


# ---------------------------------------------------------------------------
# Both surfaces
# ---------------------------------------------------------------------------


def test_http_and_mcp_answer_identically(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tools = PheasantTools(config)
    client = TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))

    over_http = client.post(
        "/connections", json={"provider": "github", "fields": {"token": GITHUB_TOKEN}}
    )
    assert over_http.status_code == 200, over_http.text
    assert (
        tools.list_connections()["connections"] == client.get("/connections").json()["connections"]
    )
    assert tools.list_connections()["providers"] == client.get("/connections").json()["providers"]

    refused = client.post("/connections", json={"provider": "github", "fields": {"token": "bad"}})
    assert refused.status_code == 400
    with pytest.raises(ConnectionRejected) as over_mcp:
        tools.connect_service("github", {"token": "bad"})
    assert refused.json()["detail"] == str(over_mcp.value)

    missing = client.delete("/connections/nope")
    assert missing.status_code == 404
    with pytest.raises(ConnectionNotFound) as absent:
        tools.remove_connection("nope")
    assert missing.json()["detail"] == str(absent.value)

    tested = client.post("/connections/github/test").json()
    assert tested["check"]["ok"] is True
    assert tools.test_connection("github")["check"]["account"] == "octo"


def test_quick_add_records_the_connection_and_refuses_an_unknown_one(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))
    client.post(
        "/connections",
        json={"provider": "api_token", "fields": {"token": "tok-123456789"}, "name": "pages"},
    )
    refused = client.post(
        "/sources/quick-add",
        json={"target": "https://example.com/a", "connection": "nobody", "sync_now": False},
    )
    assert refused.status_code == 404
    assert refused.json()["detail"] == "Unknown connection: nobody"
    added = client.post(
        "/sources/quick-add",
        json={"target": "https://example.com/a", "connection": "pages", "sync_now": False},
    )
    assert added.status_code == 200, added.text
    assert added.json()["sources"][0]["connector"]["connection"] == "pages"


# ---------------------------------------------------------------------------
# Drive sign-in
# ---------------------------------------------------------------------------


def test_a_drive_sign_in_completes_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    client = TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))
    exchanged: list[dict[str, str]] = []

    def fake_post(url: str, form: dict[str, str]) -> dict[str, Any]:
        exchanged.append(form)
        if form["grant_type"] == "authorization_code":
            return {"refresh_token": "1//refresh", "access_token": "ya29.a", "expires_in": 3600}
        return {"access_token": "ya29.b", "expires_in": 3600}

    monkeypatch.setattr(oauth, "_post", fake_post)
    started = client.post(
        "/connections/oauth/start",
        json={"client_id": "cid.apps", "client_secret": "csecret", "return_to": "/sources"},
    )
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["redirect_uri"] == "http://testserver/connections/oauth/callback"
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(body["authorize_url"]).query)
    assert query["access_type"] == ["offline"]
    assert query["code_challenge_method"] == ["S256"]
    state = query["state"][0]
    # A pending sign-in is not a connection.
    assert client.get("/connections").json()["connections"] == []

    back = client.get(
        "/connections/oauth/callback",
        params={"state": state, "code": "4/code"},
        follow_redirects=False,
    )
    assert back.status_code == 303
    assert back.headers["location"] == "/sources?connected=google_drive"
    assert exchanged[0]["code_verifier"]
    listed = client.get("/connections").json()["connections"]
    assert [(c["name"], c["details"]["account"]) for c in listed] == [
        ("google_drive", "person@example.com")
    ]

    replay = client.get(
        "/connections/oauth/callback",
        params={"state": state, "code": "4/code"},
        follow_redirects=False,
    )
    assert replay.status_code == 303
    assert "connection_error=" in replay.headers["location"]
    tools = PheasantTools(config)
    with pytest.raises(SignInExpired):
        service.finish_sign_in(tools.services, state=state, code="4/code", error=None)


def test_a_sign_in_needs_a_client(tools: PheasantTools) -> None:
    with pytest.raises(ServiceError, match="needs an OAuth client"):
        tools.start_service_sign_in()


def test_only_the_api_and_the_indexer_hold_the_connection_key() -> None:
    """pheasant-kb generates the key once and gives it to the two tiers that
    read with connections; the worker, which runs other people's parsers, and
    the graph and logger tiers, which never need a secret, do not get it."""

    import yaml

    from tests.conftest import REPO_ROOT

    stack = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    holders = {
        name
        for name, service in stack["services"].items()
        if "PHEASANT_CREDENTIAL_KEY_FILE" in (service.get("environment") or {})
    }
    assert holders == {"api", "indexer"}
    init = (REPO_ROOT / "deploy" / "compose" / "init-secrets.sh").read_text(encoding="utf-8")
    assert "credential-key:PHEASANT_CREDENTIAL_KEY" in init
    # A fresh stack starts without an OpenAI key: it can be connected instead.
    assert "${OPENAI_API_KEY:-}" in (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Sign in with GitHub, Microsoft (Azure DevOps), GitLab
# ---------------------------------------------------------------------------


def _sign_in(client: TestClient, provider: str, **body: Any) -> str:
    from urllib.parse import parse_qs, urlsplit

    started = client.post("/connections/oauth/start", json={"provider": provider, **body})
    assert started.status_code == 200, started.text
    return parse_qs(urlsplit(started.json()["authorize_url"]).query)["state"][0]


def test_sign_in_with_github_signs_its_clones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "Iv1.client")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "client-secret")
    config = _config(tmp_path)
    client = TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))
    catalog = {p["id"]: p for p in client.get("/connections").json()["providers"]}
    sign_in = catalog["github"]["methods"][0]
    assert sign_in["label"] == "Sign in with GitHub"
    assert sign_in["oauth"]["client_configured"] is True
    assert catalog["gitlab"]["methods"][0]["oauth"]["client_configured"] is False

    forms: list[dict[str, str]] = []

    def token_endpoint(url: str, form: dict[str, str]) -> dict[str, Any]:
        forms.append(form)
        assert url == "https://github.com/login/oauth/access_token"
        return {"access_token": GITHUB_TOKEN, "token_type": "bearer", "scope": "repo"}

    monkeypatch.setattr(oauth, "_post", token_endpoint)
    state = _sign_in(client, "github")
    back = client.get(
        "/connections/oauth/callback",
        params={"state": state, "code": "gh-code"},
        follow_redirects=False,
    )
    assert back.headers["location"] == "/sources?connected=github"
    assert forms[0]["client_secret"] == "client-secret"
    listed = client.get("/connections").json()["connections"]
    assert [(c["name"], c["method"], c["details"]["account"]) for c in listed] == [
        ("github", "oauth", "octo")
    ]
    assert GITHUB_TOKEN not in json.dumps(listed)
    env = git_auth.git_env("https://github.com/owner/private")
    assert _git_header(env, "github.com") == _basic("x-access-token", GITHUB_TOKEN)


def test_sign_in_with_microsoft_signs_azure_devops_and_keeps_the_rotated_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time as clock

    monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "00000000-public-client")
    config = _config(tmp_path)
    client = TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))
    issued = iter(
        [
            # The code: a token that is about to expire.
            {"access_token": "eyJ.first", "refresh_token": "r1", "expires_in": 30},
            # The refresh: a new token and a new refresh token.
            {"access_token": "eyJ.second", "refresh_token": "r2", "expires_in": 3600},
        ]
    )
    forms: list[tuple[str, dict[str, str]]] = []

    def token_endpoint(url: str, form: dict[str, str]) -> dict[str, Any]:
        forms.append((url, form))
        return next(issued)

    def devops_profile(url: str, headers: dict[str, str], *, data: bytes | None = None) -> Any:
        assert url.startswith("https://app.vssps.visualstudio.com/_apis/profile")
        return {"emailAddress": "person@example.com"}

    monkeypatch.setattr(oauth, "_post", token_endpoint)
    monkeypatch.setattr(providers, "_request", devops_profile)
    state = _sign_in(client, "azure_devops")
    back = client.get(
        "/connections/oauth/callback",
        params={"state": state, "code": "ms-code"},
        follow_redirects=False,
    )
    assert back.headers["location"] == "/sources?connected=azure_devops", back.headers
    # A public client: no secret is sent, and the tenant is the configured one.
    url, form = forms[0]
    assert url == "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    assert "client_secret" not in form
    listed = client.get("/connections").json()["connections"]
    assert listed[0]["details"]["account"] == "person@example.com"

    # The first token is within its refresh margin: the clone refreshes it,
    # signs with a bearer header, and writes the rotated refresh token back.
    env = git_auth.git_env("https://dev.azure.com/org/project/_git/repo")
    assert _git_header(env, "dev.azure.com") == "AUTHORIZATION: bearer eyJ.second"
    assert forms[-1][1]["refresh_token"] == "r1"
    row = vault.get(PheasantTools(config).state, "azure_devops")
    assert row is not None
    kept = vault.unseal(resolve.key(), row)
    assert kept["refresh_token"] == "r2"
    assert kept["expires_at"] > clock.time() + 3000


def test_a_sign_in_without_a_client_names_what_to_register(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))
    refused = client.post("/connections/oauth/start", json={"provider": "gitlab"})
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert "Sign in with GitLab needs an OAuth client" in detail
    assert "http://testserver/connections/oauth/callback" in detail
    assert "GITLAB_OAUTH_CLIENT_ID and GITLAB_OAUTH_CLIENT_SECRET" in detail
    refused = client.post("/connections/oauth/start", json={"provider": "openai"})
    assert "has no sign-in" in refused.json()["detail"]
