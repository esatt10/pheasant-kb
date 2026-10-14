"""Product Steps 32.1/32.2/32.6 — ACL persistence, principal filtering, leak gate.

The adversarial contract: with enforcement on, a caller can NEVER retrieve
an artifact their identity set does not admit — across text and hybrid
modes, the MCP facade, and HTTP /search. With enforcement off (default),
behavior is identical to pre-32.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.loader import load_config
from pheasant.mcp_server.tools import PheasantTools
from pheasant.security.acl import expand_principal, is_allowed, normalize_acl
from pheasant.sync import connector_registry
from pheasant.sync.connectors import ConnectorItem, ConnectorPayload, SourceConnector

DOCS = {
    "alice-notes.txt": ("Project mercury launch codes for alice.", {"allow": ["user:alice"]}),
    "bob-notes.txt": ("Project gemini payroll data for bob.", {"allow": ["user:bob"]}),
    "eng-runbook.txt": ("Project apollo runbook for engineering.", {"allow": ["group:eng"]}),
    "handbook.txt": ("Company handbook, public to all.", {"public": True}),
}


class AclTestConnector(SourceConnector):
    connector_type = "acltest"

    def list_items(self) -> list[ConnectorItem]:
        import hashlib

        items = []
        for name, (text, acl) in sorted(DOCS.items()):
            items.append(
                ConnectorItem(
                    identity=f"acltest:{self.source.name}:{name}",
                    relative_path=name,
                    uri=f"acltest://{name}",
                    mime_type="text/plain",
                    sha256=hashlib.sha256(text.encode()).hexdigest(),
                    metadata={"acl": dict(acl), "text": text},
                )
            )
        return items

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        text = str(item.metadata["text"])
        return ConnectorPayload(item=item, content=text.encode(), mime_type="text/plain")


def _config(tmp_path: Path, *, enforced: bool, storage: str = "") -> Any:
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: acl-test
  state_path: {tmp_path / "state"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
{storage}security:
  acl_enforced: {str(enforced).lower()}
  groups:
    carol:
      - eng
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
    tools = PheasantTools(_config(tmp_path, enforced=True))
    tools.sync_source("acl-test", "docs", "incremental")
    yield tools
    tools.engine.close()
    connector_registry.reset_connector_registry()


def _titles(tools: PheasantTools, query: str, **kwargs: Any) -> set[str]:
    payload = tools.search_context("acl-test", query, mode="text", max_results=10, **kwargs)
    return {str(r.get("relative_path")) for r in payload["results"]}


def test_leak_gate_principal_scoping(synced_tools: PheasantTools) -> None:
    # Alice: her doc + public. NEVER bob's.
    seen = _titles(synced_tools, "project handbook", principal="user:alice")
    assert "alice-notes.txt" in seen and "handbook.txt" in seen
    assert "bob-notes.txt" not in seen and "eng-runbook.txt" not in seen

    # Bob: symmetric.
    seen = _titles(synced_tools, "project handbook", principal="user:bob")
    assert "bob-notes.txt" in seen and "alice-notes.txt" not in seen

    # Anonymous: public only.
    seen = _titles(synced_tools, "project handbook")
    assert seen <= {"handbook.txt"}

    # Group via explicit groups param, and via config-mapped groups (carol→eng).
    seen = _titles(
        synced_tools, "project handbook", principal="user:dave", principal_groups=["group:eng"]
    )
    assert "eng-runbook.txt" in seen and "bob-notes.txt" not in seen
    seen = _titles(synced_tools, "project handbook", principal="user:carol")
    assert "eng-runbook.txt" in seen

    # Hybrid mode is equally scoped (graph hits judged by the file that owns them).
    payload = synced_tools.search_context(
        "acl-test", "project handbook", mode="hybrid", max_results=10, principal="user:alice"
    )
    flat = str(payload).lower()
    assert "gemini payroll" not in flat


def test_enforcement_off_is_pre32_behavior(tmp_path: Path) -> None:
    connector_registry.reset_connector_registry()
    connector_registry.register_connector_class("acltest", AclTestConnector)
    tools = PheasantTools(_config(tmp_path, enforced=False))
    try:
        tools.sync_source("acl-test", "docs", "incremental")
        seen = _titles(tools, "project handbook")
        assert {"alice-notes.txt", "bob-notes.txt", "eng-runbook.txt"} <= seen
    finally:
        tools.engine.close()
        connector_registry.reset_connector_registry()


def test_http_search_forwards_principal(tmp_path: Path) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app

    connector_registry.reset_connector_registry()
    connector_registry.register_connector_class("acltest", AclTestConnector)
    config = _config(tmp_path, enforced=True)
    app = create_app(config, config_path=str(tmp_path / "pheasant.yaml"))
    client = fastapi_testclient.TestClient(app)
    try:
        app.state.engine.sync_source("docs", "incremental")
        body = {"query": "project", "mode": "text", "principal": "user:alice"}
        hits = client.post("/search", json=body).json()["results"]
        paths = {h.get("relative_path") for h in hits}
        assert "alice-notes.txt" in paths and "bob-notes.txt" not in paths
        anon = client.post("/search", json={"query": "project", "mode": "text"}).json()["results"]
        assert {h.get("relative_path") for h in anon} <= {"handbook.txt"}
    finally:
        app.state.engine.close()
        connector_registry.reset_connector_registry()


def test_acl_normalization_rules() -> None:
    # A type with no rule of its own (a third-party plugin, or a connector
    # that was removed) is trusted only for an explicitly canonical doc.
    assert normalize_acl("notion", {"created_by": "u1"}) is None
    assert normalize_acl("myplugin", {"allow": ["user:u1"], "public": False}) == {
        "allow": ["user:u1"],
        "public": False,
    }
    assert normalize_acl("gdrive", {"owners": ["o@x.com"], "shared": True})["allow"] == [
        "user:o@x.com"
    ]
    assert normalize_acl("anything", None) is None


def test_is_allowed_semantics() -> None:
    ids = expand_principal("user:alice", None, {"user:alice": ["eng"]})
    assert ids == {"user:alice", "group:eng"}
    assert is_allowed('{"allow": ["group:eng"], "public": false}', ids, default_public=True)
    assert not is_allowed('{"allow": ["user:bob"], "public": false}', ids, default_public=True)
    assert is_allowed(None, None, default_public=True)  # un-ACL'd public default
    assert not is_allowed(None, None, default_public=False)  # private default, anonymous
    assert is_allowed(None, ids, default_public=False)  # private default, authenticated
    assert not is_allowed("not-json", ids, default_public=True)  # fails closed


# ---------------------------------------------------------------------------
# Step 33.11 — agent-memory isolation
# ---------------------------------------------------------------------------


def _memory_config(tmp_path, *, enforced: bool = True):
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "readme.md").write_text("# Shared\n\nA public note.\n", encoding="utf-8")
    (tmp_path / "memory").mkdir(exist_ok=True)
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: acl-memory
  state_path: {tmp_path / ".pheasant" / "state"}
  exports_path: {tmp_path / ".pheasant" / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
security:
  acl_enforced: {"true" if enforced else "false"}
sources:
  - name: docs
    type: document_folder
    path: docs
  - name: agent-memory
    type: memory
    path: memory
""",
        encoding="utf-8",
    )
    return config_path


def test_a_user_scope_memory_is_readable_only_by_its_writer(tmp_path) -> None:
    """The leak Step 33.11 closes.

    Before it, `acl_enforced` filtered every corpus document by principal while
    leaving one agent's private notes readable by every other agent sharing the
    region — memory was the one source that carried no ACL at all.
    """
    from pheasant.config.loader import load_config
    from pheasant.mcp_server.tools import PheasantTools

    config_path = _memory_config(tmp_path)
    tools = PheasantTools(load_config(config_path))
    try:
        tools.memory_write(
            "acl-memory",
            "Alice keeps the release calendar in her head.",
            scope="user",
            principal="user:alice",
        )
        tools.sync_source("acl-memory", "agent-memory", "full")

        def seen_by(principal: str) -> str:
            # The *results*, not the whole payload: that echoes the query back,
            # so stringifying it would match no matter what was filtered.
            payload = tools.search_context(
                "acl-memory", "release calendar", mode="text", principal=principal
            )
            return str(payload.get("results") or []).lower()

        assert "release calendar" in seen_by("user:alice")
        assert "release calendar" not in seen_by("user:bob")
    finally:
        tools.engine.close()


def test_an_org_scope_memory_stays_shared(tmp_path) -> None:
    """Scope is the whole rule: `org` was written for everyone."""
    from pheasant.config.loader import load_config
    from pheasant.mcp_server.tools import PheasantTools

    config_path = _memory_config(tmp_path)
    tools = PheasantTools(load_config(config_path))
    try:
        tools.memory_write(
            "acl-memory",
            "Deploy freezes start Friday at five.",
            scope="org",
            principal="user:alice",
        )
        tools.sync_source("acl-memory", "agent-memory", "full")
        payload = tools.search_context(
            "acl-memory", "deploy freezes", mode="text", principal="user:bob"
        )
        assert "deploy freezes" in str(payload.get("results") or []).lower()
    finally:
        tools.engine.close()


def test_memory_acl_normalization_rules() -> None:
    from pheasant.security.acl import normalize_acl

    assert normalize_acl("memory", {"scope": "org", "written_by": "user:alice"})["public"] is True
    private = normalize_acl("memory", {"scope": "user", "written_by": "user:alice"})
    assert private["allow"] == ["user:alice"]
    assert private["public"] is False
    # No recorded writer: nothing can be asserted, so fall through to the
    # region default rather than inventing an owner.
    assert normalize_acl("memory", {"scope": "session", "written_by": None}) is None


def test_enforcement_off_leaves_memory_visible_to_everyone(tmp_path) -> None:
    """Pre-33.11 behavior is preserved when ACLs are not enforced."""
    from pheasant.config.loader import load_config
    from pheasant.mcp_server.tools import PheasantTools

    config_path = _memory_config(tmp_path, enforced=False)
    tools = PheasantTools(load_config(config_path))
    try:
        tools.memory_write(
            "acl-memory", "Alice keeps the calendar.", scope="user", principal="user:alice"
        )
        tools.sync_source("acl-memory", "agent-memory", "full")
        payload = tools.search_context(
            "acl-memory", "keeps the calendar", mode="text", principal="user:bob"
        )
        assert "keeps the calendar" in str(payload.get("results") or []).lower()
    finally:
        tools.engine.close()


def test_acl_subject_judges_only_file_owned_nodes_by_their_owner() -> None:
    from pheasant.security.acl import acl_subject

    owner = "file:docs:alice-notes.txt:branch=none"
    for node_type in ("chunk", "heading", "symbol"):
        assert acl_subject(f"{node_type}:x", node_type, owner) == owner
    # Shared across every file that mentions them: the artifact_id they carry
    # is the last writer, not an owner, so they are judged by their own id
    # (which resolves to no artifact row, and is withheld).
    for node_type in ("entity", "external_reference", "relationship", "directory"):
        assert acl_subject(f"{node_type}:x", node_type, owner) == f"{node_type}:x"
    assert acl_subject("symbol:kb:docs:call:open", "symbol", None) == "symbol:kb:docs:call:open"
    assert acl_subject(owner, "file", None) == owner


def _graph_hits(tools: PheasantTools, query: str, principal: str | None) -> list[dict]:
    payload = tools.search_context(
        "acl-test", query, mode="graph", max_results=50, principal=principal
    )
    return payload["results"]


POSTGRES_DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()


@pytest.fixture(params=["sqlite", "postgres"])
def graph_tools(request, tmp_path: Path):
    """The enforcement corpus on each backend: the filter reads `artifacts` there."""

    storage = ""
    if request.param == "postgres":
        if not POSTGRES_DSN:
            pytest.skip("set PHEASANT_TEST_POSTGRES_DSN to run backend parity")
        storage = "storage:\n  backend: postgres\n  dsn_env: PHEASANT_TEST_POSTGRES_DSN\n"
    connector_registry.reset_connector_registry()
    connector_registry.register_connector_class("acltest", AclTestConnector)
    tools = PheasantTools(_config(tmp_path, enforced=True, storage=storage))
    tools.sync_source("acl-test", "docs", "full")
    yield tools
    tools.engine.close()
    connector_registry.reset_connector_registry()


def test_graph_hits_are_judged_by_the_file_that_owns_them(
    graph_tools: PheasantTools,
) -> None:
    """A chunk of a file the caller may read is a graph hit; one they may not is not.

    The search filter used to judge a graph hit by its own node id, which is an
    artifact row only for file nodes -- so under enforcement every chunk,
    heading and symbol hit was withheld, readable or not, and the graph arm
    contributed almost nothing (4.6% of its hits on a 60k-chunk region).
    """

    alice = _graph_hits(graph_tools, "mercury launch codes", "user:alice")
    owned = [hit for hit in alice if hit.get("type") in {"chunk", "heading", "symbol"}]
    assert owned, alice
    assert {hit["artifact_id"] for hit in owned} <= {
        "file:docs:alice-notes.txt:branch=none",
        "file:docs:handbook.txt:branch=none",
    }

    for principal in ("user:bob", None):
        leaked = [
            hit
            for hit in _graph_hits(graph_tools, "mercury launch codes", principal)
            if "alice-notes" in str(hit.get("artifact_id") or hit.get("node_id"))
            or "mercury" in str(hit).lower()
        ]
        assert not leaked, leaked

    # Shared stubs carry the artifact_id of whichever file wrote them last, so
    # they are never admitted on its strength.
    for principal in ("user:alice", "user:bob", None):
        shared = [
            hit
            for hit in _graph_hits(graph_tools, "project mercury gemini apollo", principal)
            if hit.get("type") in {"entity", "external_reference"}
        ]
        assert not shared, shared
