"""Regression gate for the security scan of 2026-07-31.

Every test here pins a vulnerability that was reachable on `main` and is
now closed. They are written adversarially — each one fails if the guard is
removed — and paired with a "still works" assertion so a future tightening
does not quietly amputate the feature it protects.

The findings, in the order they appear below:

1. ``config_path`` on the promote surfaces was written verbatim, turning
   source management into an arbitrary file write (HTTP + MCP).
2. The web/API connectors handed any URL to ``urlopen``, so a
   ``file://`` "web collection" read and indexed the host filesystem.
3. CORS was ``*`` on an unauthenticated API: any page in the user's
   browser could drive every route.
4. ``/relevant-files`` and the raw-content endpoints skipped the Step-32
   ACL filter that ``/search`` applies.
5. Clone URLs reached ``git clone`` argv unvalidated (option injection,
   transport helpers).
6. Backup extraction trusted symlink members.
7. ``max_results`` was unbounded.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.persistence.backup import _safe_extract
from pheasant.search.hybrid import MAX_RESULTS_CEILING
from pheasant.security.path_policy import PathPolicyError, resolve_config_write_target
from pheasant.sync.connectors import is_fetchable_url, require_fetchable_url
from pheasant.targets import TargetError, validate_clone_url

NOTE = "# Alpha\nthe quick brown widget service runs on port 9000\n"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "ws" / "notes").mkdir(parents=True)
    (tmp_path / "ws" / "notes" / "a.md").write_text(NOTE, encoding="utf-8")
    (tmp_path / "state").mkdir()
    (tmp_path / "pheasant.yaml").write_text("pheasant:\n  name: sec\n", encoding="utf-8")
    return tmp_path


def _build(workspace: Path, **security: object) -> tuple[PheasantConfig, TestClient]:
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "sec",
                "workspace_root": str(workspace / "ws"),
                "state_path": str(workspace / "state"),
                "exports_path": str(workspace / "exports"),
            },
            "security": security,
            "sources": [],
        }
    )
    client = TestClient(create_app(config, config_path=str(workspace / "pheasant.yaml")))
    return config, client


def _register_notes(client: TestClient, workspace: Path, name: str = "n1") -> None:
    response = client.post(
        "/sources",
        json={
            "name": name,
            "type": "document_folder",
            "path": str(workspace / "ws" / "notes"),
            "sync_now": True,
        },
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# 1. Arbitrary file write via promote
# ---------------------------------------------------------------------------


def test_promote_refuses_to_write_outside_allowed_roots(workspace: Path) -> None:
    """``config_path`` must not be able to drop a file anywhere on disk."""

    _config, client = _build(workspace)
    _register_notes(client, workspace)
    victim = workspace / "pwned.yaml"

    response = client.post(
        "/sources/n1/promote",
        json={"config_path": str(victim), "write": True},
    )

    assert response.status_code == 400
    assert not victim.exists()


def test_promote_write_target_ignores_the_source_path_escape_hatch(workspace: Path) -> None:
    """``allow_user_selected_source_paths`` widens *indexing*, not config writes.

    It defaults to true and widens the source-path allowlist to ``/``. If the
    promote guard consulted the same list, the guard would be a no-op on a
    default install — which is exactly how the original bug survived.
    """

    _config, client = _build(workspace, allow_user_selected_source_paths=True)
    _register_notes(client, workspace)
    victim = workspace / "pwned-despite-open-source-paths.yaml"

    response = client.post(
        "/sources/n1/promote",
        json={"config_path": str(victim), "write": True},
    )

    assert response.status_code == 400
    assert not victim.exists()


def test_promote_still_writes_the_servers_own_config(workspace: Path) -> None:
    """The feature itself is intact: no ``config_path`` promotes in place."""

    _config, client = _build(workspace)
    _register_notes(client, workspace)

    response = client.post("/sources/n1/promote", json={"write": True})

    assert response.status_code == 200
    assert response.json()["wrote_config"] is True
    assert "n1" in (workspace / "pheasant.yaml").read_text(encoding="utf-8")


def test_promote_still_writes_under_an_allowed_root(workspace: Path) -> None:
    _config, client = _build(workspace)
    _register_notes(client, workspace)
    target = workspace / "ws" / "alt.yaml"

    response = client.post(
        "/sources/n1/promote",
        json={"config_path": str(target), "write": True},
    )

    assert response.status_code == 200
    assert target.exists()


def test_resolve_config_write_target_defaults_to_the_server_config(tmp_path: Path) -> None:
    server = tmp_path / "pheasant.yaml"
    server.write_text("pheasant: {}\n", encoding="utf-8")

    assert resolve_config_write_target(None, server_config_path=server) == server.resolve()
    assert resolve_config_write_target("", server_config_path=server) == server.resolve()
    with pytest.raises(PathPolicyError):
        resolve_config_write_target(tmp_path / "elsewhere.yaml", server_config_path=server)


def test_mcp_promote_refuses_a_path_outside_allowed_roots(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP facade carries the same guard as HTTP — agents are callers too."""

    from pheasant.mcp_server.tools import PheasantTools

    monkeypatch.setenv("PHEASANT_CONFIG", str(workspace / "pheasant.yaml"))
    config, _client = _build(workspace)
    tools = PheasantTools(config)
    tools.register_source(
        config.knowledge_base_id,
        "n1",
        "document_folder",
        str(workspace / "ws" / "notes"),
    )
    victim = workspace / "mcp-pwned.yaml"

    with pytest.raises(PathPolicyError):
        tools.promote_runtime_source_to_config(
            config.knowledge_base_id, "n1", config_path=str(victim), write=True
        )
    assert not victim.exists()


# ---------------------------------------------------------------------------
# 2. file:// local-file read through the web connector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "file:///proc/self/environ",
        "ftp://example.com/secret",
        "/etc/passwd",
    ],
)
def test_connectors_refuse_non_http_urls(url: str) -> None:
    assert not is_fetchable_url(url)
    with pytest.raises(Exception, match="refusing to fetch"):
        require_fetchable_url(url)


@pytest.mark.parametrize("url", ["http://example.com/a", "https://example.com/a"])
def test_connectors_still_fetch_http(url: str) -> None:
    assert is_fetchable_url(url)
    assert require_fetchable_url(url) == url


def test_file_url_web_collection_indexes_nothing(workspace: Path) -> None:
    """End to end: a file:// URL must never reach the index or search."""

    secret = workspace / "secret.txt"
    secret.write_text("TOP-SECRET-API-KEY-abc123", encoding="utf-8")
    _config, client = _build(workspace)

    response = client.post(
        "/sources",
        json={
            "name": "exfil",
            "type": "web_collection",
            "path": str(workspace / "ws"),
            "urls": [f"file://{secret}"],
            "include": ["**/*"],
            "connector": {"allow_experimental": True},
            "sync_now": True,
        },
    )

    # The bad URL is skipped, not fatal: the rest of a collection still syncs.
    assert response.status_code == 200, response.text
    assert response.json()["sync_result"]["indexed_artifacts"] == 0

    hits = client.post("/search", json={"query": "TOP-SECRET-API-KEY-abc123"}).json()["results"]
    assert not any("TOP-SECRET" in str(hit) for hit in hits)


# ---------------------------------------------------------------------------
# 3. CORS on an unauthenticated API
# ---------------------------------------------------------------------------


def test_cors_does_not_admit_arbitrary_origins(workspace: Path) -> None:
    _config, client = _build(workspace)

    response = client.get("/overview", headers={"Origin": "https://evil.example"})

    assert response.headers.get("access-control-allow-origin") not in {"*", "https://evil.example"}


def test_cors_admits_the_configured_ui_origin(workspace: Path) -> None:
    _config, client = _build(workspace)

    response = client.get("/overview", headers={"Origin": "http://localhost:5173"})

    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_cors_wildcard_remains_available_as_an_explicit_opt_in(workspace: Path) -> None:
    """Deployments behind their own authenticating ingress can still opt in."""

    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "sec",
                "workspace_root": str(workspace / "ws"),
                "state_path": str(workspace / "state"),
                "exports_path": str(workspace / "exports"),
            },
            "server": {"api": {"cors_allow_all_origins": True}},
            "sources": [],
        }
    )
    client = TestClient(create_app(config, config_path=str(workspace / "pheasant.yaml")))

    response = client.get("/overview", headers={"Origin": "https://anywhere.example"})

    assert response.headers.get("access-control-allow-origin") == "*"


# ---------------------------------------------------------------------------
# 4. ACL enforcement parity across every retrieval surface
# ---------------------------------------------------------------------------


def test_relevant_files_enforces_acls_like_search(workspace: Path) -> None:
    """/relevant-files is /search with a different projection — same filter."""

    _config, client = _build(workspace, acl_enforced=True, default_visibility="private")
    _register_notes(client, workspace)

    anonymous = client.post("/relevant-files", json={"query": "widget service"}).json()["files"]
    identified = client.post(
        "/relevant-files", json={"query": "widget service", "principal": "user:alice"}
    ).json()["files"]

    assert anonymous == []
    assert identified, "an authorized principal must still get results"


def test_relevant_files_still_returns_files_under_a_small_limit(workspace: Path) -> None:
    """Threading ACLs through must not change *what* this route retrieves.

    /relevant-files projects results down to those carrying a path. Graph
    nodes (concepts, symbols) carry none, so letting graph hits into the
    merge crowds the file hits out and the route answers with an empty list
    — which is what happened the first time ACL enforcement was wired here.
    A workspace with wikilinks generates exactly those competing nodes.
    """

    notes = workspace / "ws" / "notes"
    (notes / "index.md").write_text("# Index\n[[Runbook]] covers deploys.\n", encoding="utf-8")
    (notes / "runbook.md").write_text(
        "# Runbook\nThe widget service listens on port 9000 and is deployed by CI.\n",
        encoding="utf-8",
    )
    _config, client = _build(workspace)
    _register_notes(client, workspace)

    response = client.post("/relevant-files", json={"query": "widget service", "max_results": 3})
    files = response.json()["files"]

    assert files, "a file-bearing query must return files"
    assert all(entry.get("relative_path") for entry in files)


def test_content_endpoints_enforce_acls(workspace: Path) -> None:
    """Filtering search while serving the same bytes elsewhere is not enforcement."""

    _config, client = _build(workspace, acl_enforced=True, default_visibility="private")
    _register_notes(client, workspace)
    files = client.post(
        "/relevant-files", json={"query": "widget service", "principal": "user:alice"}
    ).json()["files"]
    node_id = files[0]["node_id"]

    assert client.get("/files/summary", params={"path": "a.md"}).status_code == 403
    assert client.get("/nodes/content", params={"node_id": node_id}).status_code == 403

    allowed_summary = client.get(
        "/files/summary", params={"path": "a.md", "principal": "user:alice"}
    )
    allowed_content = client.get(
        "/nodes/content", params={"node_id": node_id, "principal": "user:alice"}
    )
    assert allowed_summary.status_code == 200
    assert allowed_content.status_code == 200


def test_content_endpoints_unchanged_when_enforcement_is_off(workspace: Path) -> None:
    """Default (acl_enforced: false) stays byte-identical to pre-32 behavior."""

    _config, client = _build(workspace)
    _register_notes(client, workspace)

    assert client.get("/files/summary", params={"path": "a.md"}).status_code == 200
    assert client.post("/relevant-files", json={"query": "widget service"}).json()["files"]


# ---------------------------------------------------------------------------
# 5. git clone argument / transport injection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ext::sh -c touch% /tmp/pwned% x.git",  # transport helper = RCE
        "--upload-pack=touch /tmp/pwned.git",  # leading dash = git option
        "-c core.pager=sh.git",
        "file:///etc/passwd.git",
        "ftp://example.com/x.git",
        "https://x-access-token:secret@github.com/me/private.git",
        "",
    ],
)
def test_clone_urls_that_are_not_clone_urls_are_refused(url: str) -> None:
    with pytest.raises(TargetError):
        validate_clone_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/me/proj",
        "https://github.com/me/proj.git",
        "http://git.internal/me/proj.git",
        "git@github.com:me/proj.git",
        "ssh://git@host/me/proj.git",
        "git://host/proj.git",
    ],
)
def test_real_clone_urls_still_pass(url: str) -> None:
    assert validate_clone_url(url) == url


def test_git_env_pins_transports_and_never_prompts() -> None:
    from pheasant.targets import _git_env

    env = _git_env()
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert pairs["protocol.allow"] == "never"
    assert pairs["protocol.https.allow"] == "always"


# ---------------------------------------------------------------------------
# 6. Backup archive extraction
# ---------------------------------------------------------------------------


def _tar_with(members: list[tarfile.TarInfo], payloads: dict[str, bytes]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for member in members:
            data = payloads.get(member.name)
            tar.addfile(member, io.BytesIO(data) if data is not None else None)
    buffer.seek(0)
    return buffer


def test_backup_extraction_rejects_escaping_symlinks(tmp_path: Path) -> None:
    """A symlink member passes a name-only check and escapes on the next write."""

    link = tarfile.TarInfo("escape")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc"
    buffer = _tar_with([link], {})

    with tarfile.open(fileobj=buffer, mode="r") as tar, pytest.raises(ValueError):
        _safe_extract(tar, tmp_path / "dest")


def test_backup_extraction_rejects_traversing_members(tmp_path: Path) -> None:
    member = tarfile.TarInfo("../escaped.txt")
    member.size = 3
    buffer = _tar_with([member], {"../escaped.txt": b"bad"})

    with tarfile.open(fileobj=buffer, mode="r") as tar, pytest.raises(ValueError):
        _safe_extract(tar, tmp_path / "dest")


def test_backup_extraction_still_restores_ordinary_members(tmp_path: Path) -> None:
    member = tarfile.TarInfo("graph.json")
    member.size = 2
    buffer = _tar_with([member], {"graph.json": b"{}"})
    dest = tmp_path / "dest"
    dest.mkdir()

    with tarfile.open(fileobj=buffer, mode="r") as tar:
        _safe_extract(tar, dest)

    assert (dest / "graph.json").read_bytes() == b"{}"


# ---------------------------------------------------------------------------
# 7. Unbounded result counts
# ---------------------------------------------------------------------------


def test_max_results_is_clamped(workspace: Path) -> None:
    _config, client = _build(workspace)
    _register_notes(client, workspace)

    response = client.post("/search", json={"query": "widget", "max_results": 10**9})

    assert response.status_code == 200
    assert len(response.json()["results"]) <= MAX_RESULTS_CEILING


# ---------------------------------------------------------------------------
# 8. An unauthenticated API with mount-anything power (Phase 35.8)
#
# The single-container posture — no authentication, published on loopback —
# shipped unchanged into the fleet profile, where the API is a multi-replica
# Service behind a port-publishing decision an operator can reasonably change.
# Startup now refuses that combination (tests/test_process_roles.py); these
# cover the other half, which is that a configured token is actually enforced
# on the request path and that the exemptions are the ones intended.
# ---------------------------------------------------------------------------


def _authenticated(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("PHEASANT_API_TOKEN", "s3cret-token")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "sec",
                "workspace_root": str(workspace / "ws"),
                "state_path": str(workspace / "state"),
                "exports_path": str(workspace / "exports"),
            },
            "security": {"api_auth": {"token_env": "PHEASANT_API_TOKEN"}},
            "sources": [],
        }
    )
    return TestClient(create_app(config, config_path=str(workspace / "pheasant.yaml")))


def test_a_configured_token_is_required_on_the_whole_api(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every route that can read or change the region, not a chosen subset."""

    client = _authenticated(workspace, monkeypatch)

    for method, path, payload in (
        ("post", "/search", {"query": "widget", "max_results": 3}),
        ("get", "/sources", None),
        ("post", "/sources", {"name": "x", "type": "document_folder", "path": "/tmp"}),
        ("get", "/config", None),
    ):
        response = getattr(client, method)(path, **({"json": payload} if payload else {}))
        assert response.status_code == 401, f"{method} {path} answered without a token"
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    ok = client.post(
        "/search",
        json={"query": "widget", "max_results": 3},
        headers={"authorization": "Bearer s3cret-token"},
    )
    assert ok.status_code == 200


def test_a_wrong_token_is_refused(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _authenticated(workspace, monkeypatch)
    for header in ("Bearer wrong", "Basic s3cret-token", "s3cret-token", ""):
        response = client.get("/sources", headers={"authorization": header})
        assert response.status_code == 401


def test_a_non_ascii_token_is_a_401_not_a_500(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starlette decodes headers as latin-1, so any caller can send one.

    `hmac.compare_digest` raises TypeError on str operands with a character
    above 127 — which would turn one byte in a header into a 500 from the
    guard itself, on unauthenticated input.
    """

    client = _authenticated(workspace, monkeypatch)
    # Sent as bytes: the HTTP client refuses to encode a non-ASCII header
    # value, but a raw socket has no such scruples and uvicorn hands the
    # latin-1 decoding straight to the handler.
    response = client.get("/sources", headers={"authorization": b"Bearer s3cret-tok\xe9n"})
    assert response.status_code == 401


def test_the_probes_and_metrics_stay_open(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe that needs a credential cannot tell a dead pod from an unauthorized one."""

    client = _authenticated(workspace, monkeypatch)
    for path in ("/health", "/ready", "/metrics"):
        assert client.get(path).status_code == 200


def test_the_ui_shell_is_public_but_its_api_routes_are_not(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser must be able to render the token-entry control first."""

    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>ui</title>", encoding="utf-8")
    (dist / "pheasant.png").write_bytes(b"png")
    (assets / "app.js").write_text("console.log('ui')", encoding="utf-8")
    monkeypatch.setenv("PHEASANT_UI_DIST", str(dist))
    client = _authenticated(workspace, monkeypatch)

    assert client.get("/").status_code == 200
    assert client.get("/pheasant.png").status_code == 200
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/sources").status_code == 401


def test_internal_routes_keep_their_own_boundary_token(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/internal/*` is exempt structurally, and still authenticated.

    Requiring the API token there too would mean handing the region's front-door
    credential to every preparation worker — the exact trust-boundary collapse
    this phase removed from the Compose file. Each internal route enforces the
    token for *its* boundary instead, so the exemption widens nothing.
    """

    monkeypatch.setenv("PHEASANT_INDEX_WORKER_TOKEN", "worker-token")
    client = _authenticated(workspace, monkeypatch)

    # No API token on the request, and the route still answers for itself:
    # 404 while remote preparation is off, 401 once it is on and the worker
    # token is wrong. Neither is the middleware's refusal.
    body = {"source": {}, "item": {}, "payload": {}}
    response = client.post("/internal/indexing/prepare", json=body)
    assert response.status_code in {401, 404}
    assert "this region requires a bearer token" not in response.text


def test_no_token_configured_leaves_the_api_exactly_as_it_was(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 7. A laptop and every existing single container set no token."""

    monkeypatch.delenv("PHEASANT_API_TOKEN", raising=False)
    _config, client = _build(workspace)
    assert client.get("/sources").status_code == 200


def test_a_process_with_the_api_off_serves_only_its_own_surface(workspace: Path) -> None:
    """`server.api.enabled: false` was documentation; now it is enforced.

    `deploy/compose/worker.yaml` has declared it since the worker tier
    existed, and a worker went on serving the whole knowledge-base API — on
    the tier designed to hold nothing, scale hardest, and run third-party
    parse code.
    """

    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "sec",
                "workspace_root": str(workspace / "ws"),
                "state_path": str(workspace / "state"),
                "exports_path": str(workspace / "exports"),
            },
            "server": {"api": {"enabled": False}},
            "sources": [],
        }
    )
    client = TestClient(create_app(config, config_path=str(workspace / "pheasant.yaml")))

    for path in ("/health", "/ready", "/metrics"):
        assert client.get(path).status_code == 200
    # 404 rather than 403: on this process the route does not exist.
    assert client.get("/sources").status_code == 404
    assert client.post("/search", json={"query": "widget"}).status_code == 404
    # The preparation endpoint is the reason such a process runs at all: the
    # surface guard must not be what answers it. (Remote preparation is off in
    # this config, so the route refuses for its own reason — with its own
    # message, which is how the two refusals are told apart.)
    prepared = client.post(
        "/internal/indexing/prepare",
        json={"source": {}, "item": {}, "payload": {}},
    )
    assert "serves no knowledge-base API" not in prepared.text
