"""Acceptance tests for the web-UI-facing HTTP routes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app, graph_neighbors, graph_slice
from pheasant.graph.simple import SimpleMultiDiGraph


def _client(config) -> TestClient:
    # No `with` block: the lifespan startup sync is skipped, keeping tests fast.
    return TestClient(create_app(config=config))


def test_config_route_returns_effective_and_profiles(loaded_config) -> None:
    client = _client(loaded_config)
    response = client.get("/config")
    assert response.status_code == 200
    payload = response.json()
    assert "effective" in payload
    assert payload["effective"]["pheasant"]["name"] == "acceptance-knowledge"
    assert "quickstart" in payload["profiles"]


def test_graph_routes_have_stable_shape_when_empty(loaded_config) -> None:
    client = _client(loaded_config)
    neighbors = client.get("/graph/neighbors", params={"node_id": "missing"})
    assert neighbors.status_code == 200
    assert neighbors.json()["neighbors"] == []

    explain = client.get("/nodes/explain", params={"node_id": "missing"})
    assert explain.status_code == 200
    assert "not present" in explain.json()["explanation"]

    slice_resp = client.get("/graph/slice", params={"node_id": "missing"})
    assert slice_resp.status_code == 200
    assert slice_resp.json()["links"] == []


def test_graph_neighbors_defaults_to_two_hops(loaded_config) -> None:
    app = create_app(config=loaded_config)
    graph = app.state.engine.graph_builder.graph
    graph.add_node("neighbor-start", id="neighbor-start", type="source", label="Start")
    graph.add_node("neighbor-middle", id="neighbor-middle", type="file", label="Middle")
    graph.add_node("neighbor-end", id="neighbor-end", type="chunk", label="End")
    graph.add_edge("neighbor-start", "neighbor-middle", type="contains")
    graph.add_edge("neighbor-middle", "neighbor-end", type="has_chunk")

    response = TestClient(app).get("/graph/neighbors", params={"node_id": "neighbor-start"})
    assert response.status_code == 200
    assert {node["node_id"] for node in response.json()["neighbors"]} == {
        "neighbor-middle",
        "neighbor-end",
    }


def test_graph_route_can_return_bounded_preview(loaded_config) -> None:
    app = create_app(config=loaded_config)
    graph = app.state.engine.graph_builder.graph
    existing_nodes = graph.number_of_nodes()
    existing_links = graph.number_of_edges()
    graph.add_node("a", id="a", type="source", label="A")
    graph.add_node("b", id="b", type="file", label="B")
    graph.add_node("c", id="c", type="chunk", label="C")
    graph.add_edge("a", "b", type="contains")
    graph.add_edge("b", "c", type="has_chunk")

    response = TestClient(app).get("/graph", params={"limit": 2, "link_limit": 1})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_nodes"] == existing_nodes + 3
    assert payload["total_links"] == existing_links + 2
    assert payload["truncated"] is True
    assert len(payload["nodes"]) == 2
    assert len(payload["links"]) <= 1


def test_fs_list_lists_roots_and_rejects_escapes(loaded_config, workspace_copy: Path) -> None:
    loaded_config.pheasant.workspace_root = workspace_copy
    loaded_config.security.allow_user_selected_source_paths = False
    client = _client(loaded_config)

    roots = client.get("/fs/list")
    assert roots.status_code == 200
    assert any(entry["path"] == str(workspace_copy) for entry in roots.json()["entries"])

    listing = client.get("/fs/list", params={"path": str(workspace_copy)})
    assert listing.status_code == 200
    assert listing.json()["path"] == str(workspace_copy)

    escaped = client.get("/fs/list", params={"path": "/etc"})
    assert escaped.status_code == 403


def test_fs_list_and_register_accept_user_selected_path(loaded_config, tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "note.md").write_text("# External\n", encoding="utf-8")
    loaded_config.pheasant.workspace_root = tmp_path / "workspace"
    loaded_config.security.allow_user_selected_source_paths = True
    client = _client(loaded_config)

    listing = client.get("/fs/list", params={"path": str(external)})
    assert listing.status_code == 200
    assert listing.json()["path"] == str(external.resolve())

    response = client.post(
        "/sources",
        json={
            "name": "external-notes",
            "type": "markdown_folder",
            "path": str(external),
            "max_depth": 0,
            "include": ["**/*.md"],
            "sync": {"on_startup": False},
        },
    )
    assert response.status_code == 200
    source = response.json()["source"]
    assert source["path"] == str(external.resolve())
    assert source["max_depth"] == 0


def test_register_source_appears_in_listing(loaded_config, workspace_copy: Path) -> None:
    loaded_config.pheasant.workspace_root = workspace_copy
    client = _client(loaded_config)
    target = workspace_copy / "notes"

    response = client.post(
        "/sources",
        json={
            "name": "ui-added-notes",
            "type": "markdown_folder",
            "path": str(target),
            "description": "Added through the UI",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "registered"
    assert body["config_update_required"] is True

    listed = client.get("/sources").json()
    assert any(source["name"] == "ui-added-notes" for source in listed)


def test_register_source_rejects_path_outside_roots(loaded_config, workspace_copy: Path) -> None:
    loaded_config.pheasant.workspace_root = workspace_copy
    loaded_config.security.allow_user_selected_source_paths = False
    client = _client(loaded_config)
    response = client.post(
        "/sources",
        json={"name": "escape", "type": "markdown_folder", "path": "/etc"},
    )
    assert response.status_code == 400


def test_update_source_persists_custom_runtime_config(loaded_config, workspace_copy: Path) -> None:
    loaded_config.pheasant.workspace_root = workspace_copy
    client = _client(loaded_config)
    response = client.put(
        "/sources/pheasant-repo",
        json={
            "path": str(workspace_copy / "pheasant-repo"),
            "type": "repository",
            "max_depth": 1,
            "include": ["**/*.py"],
            "exclude": ["**/.git/**"],
            "chunking": {"strategy": "semantic", "max_chars": 1200, "overlap_chars": 120},
            "sync": {"on_startup": False, "on_file_change": False, "on_git_commit": False},
            "repo": {"branch_policy": "current", "include_uncommitted": False},
        },
    )

    assert response.status_code == 200
    source = response.json()["source"]
    assert source["max_depth"] == 1
    assert source["chunking"]["max_chars"] == 1200
    assert source["repo"]["include_uncommitted"] is False


def test_node_content_route_returns_full_indexed_text(loaded_config) -> None:
    app = create_app(config=loaded_config)
    app.state.engine.sync_source("architecture-notes", "full")
    rows = app.state.state.rows(
        "SELECT artifact_id, text FROM chunks WHERE source_id=? ORDER BY chunk_index",
        ("architecture-notes",),
    )
    assert rows

    response = TestClient(app).get("/nodes/content", params={"node_id": rows[0]["artifact_id"]})

    assert response.status_code == 200
    assert rows[0]["text"].replace("\r\n", "\n") in response.json()["content"].replace("\r\n", "\n")


def test_promote_source_generates_patch(loaded_config, workspace_copy: Path) -> None:
    loaded_config.pheasant.workspace_root = workspace_copy
    client = _client(loaded_config)
    response = client.post("/sources/pheasant-repo/promote", json={"write": False})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "patch_generated"
    assert "sources:" in body["yaml_patch"]


def test_put_config_validates_and_writes(loaded_config, tmp_path: Path) -> None:
    config_file = tmp_path / "written.yaml"
    client = TestClient(create_app(config=loaded_config, config_path=config_file))
    payload = {
        "config": {
            "pheasant": {"name": "rewritten"},
            "server": {"port": 9001},
        }
    }
    response = client.put("/config", json=payload)
    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    assert config_file.exists()
    assert "rewritten" in config_file.read_text(encoding="utf-8")


def test_put_config_rejects_non_mapping(loaded_config) -> None:
    client = _client(loaded_config)
    response = client.put("/config", json={"yaml_text": "- just\n- a\n- list\n"})
    assert response.status_code == 400


def test_pure_graph_helpers_traverse_typed_edges() -> None:
    graph = SimpleMultiDiGraph()
    graph.add_node("a", id="a", type="source", label="A")
    graph.add_node("b", id="b", type="file", label="B")
    graph.add_node("c", id="c", type="concept", label="C")
    graph.add_edge("a", "b", type="contains")
    graph.add_edge("b", "c", type="mentions")

    one_hop = graph_neighbors(graph, "a", depth=1)
    assert {n["node_id"] for n in one_hop["neighbors"]} == {"b"}

    two_hop = graph_neighbors(graph, "a", depth=2)
    assert {n["node_id"] for n in two_hop["neighbors"]} == {"b", "c"}

    filtered = graph_neighbors(graph, "a", depth=2, edge_types=["contains"])
    assert {n["node_id"] for n in filtered["neighbors"]} == {"b"}

    sliced = graph_slice(graph, "a", depth=2)
    assert {node["id"] for node in sliced["nodes"]} == {"a", "b", "c"}
    assert len(sliced["links"]) == 2


def test_bounded_traversal_matches_the_unbounded_prefix() -> None:
    """``max_nodes`` may only stop the walk early — never change what is kept.

    A hub three hops out reaches most of a real graph, so the traversal has to
    stop at the caller's budget instead of enumerating everything and slicing
    afterwards. The kept set must stay byte-identical to the old behaviour.
    """

    graph = SimpleMultiDiGraph()
    graph.add_node("hub", id="hub", type="source", label="hub")
    for i in range(60):
        graph.add_node(f"f{i}", id=f"f{i}", type="file", label=f"f{i}")
        graph.add_edge("hub", f"f{i}", type="contains")
        graph.add_node(f"s{i}", id=f"s{i}", type="symbol", label=f"s{i}")
        graph.add_edge(f"f{i}", f"s{i}", type="mentions")

    full = graph_neighbors(graph, "hub", depth=3)
    capped = graph_neighbors(graph, "hub", depth=3, max_nodes=25)
    assert len(capped["neighbors"]) == 25
    assert capped["neighbors"] == full["neighbors"][:25]


def test_adjacency_index_survives_removals() -> None:
    """out_edges/neighbors read an index, which must track every mutation."""

    graph = SimpleMultiDiGraph()
    for node_id in ("a", "b", "c"):
        graph.add_node(node_id, id=node_id, type="file", label=node_id)
    graph.add_edge("a", "b", type="contains")
    graph.add_edge("a", "c", type="references")

    assert graph.neighbors("a") == ["b", "c"]  # insertion order, deterministic
    assert {t for _s, t, _m in graph.out_edges("a")} == {"b", "c"}

    graph.remove_edges_from([("a", "b")])
    assert graph.neighbors("a") == ["c"]

    graph.remove_nodes_from(["c"])
    assert graph.neighbors("a") == []
    assert graph.out_edges("a") == []

    # Re-adding relinks: the index is rebuilt through the normal write path.
    graph.add_node("c", id="c", type="file", label="c")
    graph.add_edge("a", "c", type="references")
    assert graph.neighbors("a") == ["c"]


def test_graph_slice_reports_hop_distance_per_node() -> None:
    """The canvas rings nodes by distance, so a slice carries its own depths."""

    graph = SimpleMultiDiGraph()
    for node_id in ("center", "one", "two", "three"):
        graph.add_node(node_id, id=node_id, type="file", label=node_id)
    graph.add_edge("center", "one", type="contains")
    graph.add_edge("one", "two", type="references")
    graph.add_edge("two", "three", type="references")
    # A shortcut edge: "three" is reachable in one hop as well as three, and the
    # nearest sighting is the one that decides which ring it lands in.
    graph.add_edge("center", "three", type="references")

    sliced = graph_slice(graph, "center", depth=3)
    assert sliced["depths"] == {"center": 0, "one": 1, "two": 2, "three": 1}

    shallow = graph_slice(graph, "center", depth=1)
    assert shallow["depths"] == {"center": 0, "one": 1, "three": 1}
    assert "two" not in shallow["depths"]


def test_graph_slice_reports_when_neighbor_budget_omits_nodes() -> None:
    """A full slice budget must not silently look like the complete graph."""

    graph = SimpleMultiDiGraph()
    graph.add_node("document", id="document", type="document", label="document")
    for index in range(4):
        chunk_id = f"chunk-{index}"
        graph.add_node(chunk_id, id=chunk_id, type="chunk", label=chunk_id)
        graph.add_edge("document", chunk_id, type="has_chunk")

    capped = graph_slice(graph, "document", depth=1, limit=2)
    complete = graph_slice(graph, "document", depth=1, limit=4)

    assert len(capped["nodes"]) == 3  # center plus two neighbours
    assert capped["truncated"] is True
    assert len(complete["nodes"]) == 5
    assert complete["truncated"] is False


def test_node_content_concatenates_chunks_in_index_order(loaded_config) -> None:
    """GROUP_CONCAT must respect chunk_index even when rows were written out of order."""
    app = create_app(config=loaded_config)
    state = app.state.state
    artifact = {
        "id": "file:test:ordered.md:branch=main",
        "source_id": "test",
        "type": "document",
        "path": "/nonexistent/ordered.md",  # forces the chunk-concat fallback
        "relative_path": "ordered.md",
        "mime_type": "text/markdown",
        "size_bytes": 10,
        "sha256": "0" * 64,
        "mtime": None,
        "git_branch": None,
        "git_commit": None,
        "last_indexed_at": None,
        "status": "healthy",
    }
    chunks = [
        {
            "id": f"chunk:test:ordered.md:sha256=h{i}:chunk={i:04d}",
            "artifact_id": artifact["id"],
            "source_id": "test",
            "chunk_index": i,
            "heading_path": None,
            "start_line": 1,
            "end_line": 2,
            "text": f"part-{i}",
            "text_hash": f"h{i}",
            "summary": f"part-{i}",
            "token_estimate": 2,
        }
        # Insert in reverse so raw rowid order disagrees with chunk order.
        for i in (2, 1, 0)
    ]
    state.replace_artifact_chunks(artifact, chunks)
    graph = app.state.engine.graph_builder.graph
    graph.add_node(artifact["id"], id=artifact["id"], type="document", label="ordered.md")

    response = TestClient(app).get("/nodes/content", params={"node_id": artifact["id"]})

    assert response.status_code == 200
    assert response.json()["content"] == "part-0\n\npart-1\n\npart-2"

    summary = TestClient(app).get(
        "/files/summary", params={"path": "ordered.md", "source_name": "test"}
    )
    assert summary.status_code == 200
    assert summary.json()["content"] == "part-0\n\npart-1\n\npart-2"


def test_register_source_invalid_payload_returns_400(loaded_config, workspace_copy: Path) -> None:
    loaded_config.pheasant.workspace_root = workspace_copy
    client = _client(loaded_config)
    response = client.post(
        "/sources",
        json={
            "name": "broken",
            "type": "markdown_folder",
            "path": str(workspace_copy / "notes"),
            "chunking": {"max_chars": "not-a-number"},
        },
    )
    assert response.status_code == 400
    assert "Invalid source" in response.json()["detail"]


def test_sync_routes_map_domain_errors_to_4xx(loaded_config) -> None:
    client = _client(loaded_config)

    bad_mode = client.post("/sync/architecture-notes", json={"mode": "definitely-not-a-mode"})
    assert bad_mode.status_code == 400
    assert "Unsupported sync mode" in bad_mode.json()["detail"]

    unknown = client.post("/sync", json={"source_name": "no-such-source"})
    assert unknown.status_code == 404


def test_bundle_is_mounted_and_reported_only_when_it_exists(
    loaded_config, tmp_path: Path, monkeypatch, capsys
) -> None:
    """Serving the UI is silent-by-default in both directions; make it legible.

    A missing bundle is invisible — the API answers on the port either way —
    which is the step people get stuck on between "the CLI works" and "I can
    see the graph". `app.state.ui_dist` records the outcome and the CLI banner
    reads it.
    """
    from pheasant.cli import _report_ui

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>ui</title>", encoding="utf-8")
    monkeypatch.setenv("PHEASANT_UI_DIST", str(dist))

    app = create_app(config=loaded_config)
    assert app.state.ui_dist == str(dist)
    client = TestClient(app)
    assert "<title>ui</title>" in client.get("/").text
    # The mount is added last, so API routes still win.
    assert client.get("/health").status_code == 200
    _report_ui(app, loaded_config)
    assert f":{loaded_config.server.port}" in capsys.readouterr().out

    # Nothing built anywhere: the banner has to say so and name the fix. (A
    # stub app, because this checkout may well have a real ui/dist — the
    # fallback candidate — sitting next to it.)
    unmounted = SimpleNamespace(state=SimpleNamespace(ui_dist=None))
    _report_ui(unmounted, loaded_config)
    banner = capsys.readouterr().out
    assert "not served" in banner
    assert "npm --prefix ui run build" in banner

    # Explicitly disabled: no mount, and no unsolicited advice either.
    loaded_config.server.ui.enabled = False
    disabled = create_app(config=loaded_config)
    assert disabled.state.ui_dist is None
    _report_ui(disabled, loaded_config)
    assert capsys.readouterr().out == ""


def test_hierarchy_survives_a_bounded_horizon() -> None:
    """A shortcut edge must not crowd the directory tree out of the view.

    A source indexes every artifact directly, so a plain breadth-first walk
    with a budget spends all of it hopping source→file and never descends the
    directory chain — the parent/child structure is in the graph but absent
    from every bounded view of it.
    """

    graph = SimpleMultiDiGraph()
    graph.add_node("kb", id="kb", type="knowledge_base", label="kb")
    graph.add_node("src", id="src", type="source", label="src")
    graph.add_edge("kb", "src", type="contains")

    # One deep directory chain, plus a wide flat fan-out of indexed files.
    parent = "src"
    for level in range(1, 5):
        directory = f"dir:{level}"
        graph.add_node(directory, id=directory, type="directory", label=directory)
        graph.add_edge(parent, directory, type="contains")
        parent = directory
    deep_file = "file:deep.py"
    graph.add_node(deep_file, id=deep_file, type="file", label="deep.py")
    graph.add_edge(parent, deep_file, type="contains")

    for i in range(200):
        flat = f"file:flat{i}.py"
        graph.add_node(flat, id=flat, type="file", label=f"flat{i}.py")
        graph.add_edge("src", flat, type="indexes")

    budget = 20
    plain = graph_slice(graph, "kb", depth=6, limit=budget)
    plain_types = {node["id"]: node.get("type") for node in plain["nodes"]}
    assert sum(1 for t in plain_types.values() if t == "directory") >= 1

    without_shortcut = graph_slice(
        graph, "kb", depth=6, limit=budget, exclude_edge_types={"indexes"}
    )
    ids = {node["id"] for node in without_shortcut["nodes"]}
    # The whole chain, and the file at the bottom of it, fit in the budget once
    # the shortcut is out of the way.
    assert {"dir:1", "dir:2", "dir:3", "dir:4", deep_file} <= ids
    assert not any(node_id.startswith("file:flat") for node_id in ids)
    assert without_shortcut["depths"][deep_file] == 6


def test_the_advertised_mcp_endpoint_is_actually_mounted(tmp_path) -> None:
    """`/mcp/info` advertises a streamable-HTTP URL; it has to exist.

    A live container told an MCP client to connect to `<base>/mcp` and then
    answered 404/405, because `serve` built the FastAPI app and never mounted
    the MCP one. Advertising an endpoint that is not served is worse than not
    offering the transport.
    """
    pytest.importorskip("mcp")
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app
    from pheasant.config.loader import load_config

    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: mcp-mount
  state_path: {tmp_path / "state"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
sources: []
""",
        encoding="utf-8",
    )
    app = create_app(load_config(config_path), config_path=str(config_path))
    handshake = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pheasant-tests", "version": "1"},
        },
    }
    headers = {"accept": "application/json, text/event-stream"}
    try:
        # The `with` matters: the SDK's session manager starts in the app's
        # lifespan, and mounting a sub-app does not run its lifespan for it.
        with fastapi_testclient.TestClient(app, base_url="http://localhost:8765") as client:
            advertised = client.get("/mcp/info").json()
            assert advertised["transports"]["streamable_http"] is True
            assert advertised["streamable_http_url"].endswith("/mcp")

            # Assert a real protocol handshake, not merely "not 404". The
            # mount reached the session manager long before it could answer,
            # so a routing-only assertion would have called this fixed twice
            # over while every client still failed.
            for path in ("/mcp", "/mcp/"):
                response = client.post(path, json=handshake, headers=headers)
                assert response.status_code == 200, (
                    f"advertised MCP endpoint {path} is not usable "
                    f"(HTTP {response.status_code}): {response.text[:200]}"
                )
                assert response.json()["result"]["serverInfo"]["name"]

            # Mounting an ASGI catch-all at /mcp must not shadow the sibling
            # routes that share the prefix, nor anything else.
            assert client.get("/mcp/info").status_code == 200
            assert client.get("/health").status_code == 200
    finally:
        app.state.engine.close()


def test_mcp_transport_security_follows_the_configured_cors_origins(tmp_path) -> None:
    """A container is not reached at `localhost`, and MCP must survive that.

    The SDK's DNS-rebinding guard allows only 127.0.0.1/localhost/[::1] by
    default, so the same server reached as `http://pheasant:8765` answers 421
    Misdirected Request to every MCP call. pheasant already has an operator
    knob for who may reach this API — `server.api.cors_origins` — so the guard
    is driven from that rather than from a second, separate allow-list.

    The second assertion does more work than it looks like since SDK 2.x: the
    guard is no longer auto-enabled from a constructor default but from the
    *real* bind address handed to `streamable_http_app()`, and pheasant binds
    `0.0.0.0`. Leave transport security to the SDK's default there and DNS
    rebinding protection is off entirely — every host admitted, no error, and
    nothing else in the suite the wiser.
    """
    pytest.importorskip("mcp")
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app
    from pheasant.config.loader import load_config

    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: mcp-hosts
  state_path: {tmp_path / "state"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
server:
  api:
    cors_origins:
      - http://pheasant.internal:8765
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
sources: []
""",
        encoding="utf-8",
    )
    handshake = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pheasant-tests", "version": "1"},
        },
    }
    headers = {"accept": "application/json, text/event-stream"}

    def handshake_status(host: str) -> int:
        # A fresh app per call: the SDK's session manager refuses to `run()`
        # twice, so two TestClient contexts cannot share one app.
        app = create_app(load_config(config_path), config_path=str(config_path))
        try:
            with fastapi_testclient.TestClient(app, base_url=f"http://{host}") as client:
                return client.post("/mcp/", json=handshake, headers=headers).status_code
        finally:
            app.state.engine.close()

    assert handshake_status("pheasant.internal:8765") == 200, (
        "a host the operator allowed via cors_origins was refused by the MCP transport guard"
    )
    # Narrowed to the operator's list, not switched off.
    assert handshake_status("evil.example:8765") == 421, "an unlisted host should be refused"


@pytest.mark.asyncio
async def test_the_mounted_mcp_endpoint_speaks_the_modern_protocol_revision(tmp_path) -> None:
    """The point of the SDK 2.x upgrade, pinned so a slide back is loud.

    Everything else about the MCP surface is revision-agnostic — the same
    tools, resources and prompts answer on SDK 1.x, whose newest protocol is
    2025-11-25 — so a dependency that resolved back to 1.x would pass every
    other MCP test in this file while every 2026-era client renegotiated down.

    It has to be a real client, not a hand-rolled `initialize` POST: the
    2026-07-28 revision does not use `initialize` at all, so the raw
    handshake the sibling tests send is inherently a legacy one and is
    answered — correctly — with the newest *legacy* revision. Driving the
    mounted app through `httpx2.ASGITransport` connects the way a 2026-era
    agent will, without a socket.

    The serverInfo assertion pins a second thing the upgrade changed: 1.x
    reported the *SDK's* version as the server's, so `/mcp` announced itself
    to every agent as "1.29.1". 2.x reports an empty string unless the server
    names itself, which pheasant now does.
    """
    pytest.importorskip("mcp")
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import LATEST_PROTOCOL_VERSION

    from pheasant.api.app import create_app
    from pheasant.config.loader import load_config
    from pheasant.version import __version__

    assert LATEST_PROTOCOL_VERSION >= "2026-07-28", (
        "the installed MCP SDK predates the 2026-07-28 protocol revision; "
        "pheasant declares mcp>=2.1,<3"
    )

    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: mcp-protocol
  state_path: {tmp_path / "state"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
sources: []
""",
        encoding="utf-8",
    )
    app = create_app(load_config(config_path), config_path=str(config_path))
    base = "http://localhost:8765"
    try:
        # The lifespan is what starts the session manager; a mounted sub-app
        # never runs its own, which is why the API app enters it by hand.
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url=base
            ) as http_client:
                transport = streamable_http_client(f"{base}/mcp/", http_client=http_client)
                async with Client(transport) as client:
                    assert client.protocol_version == LATEST_PROTOCOL_VERSION
                    assert client.server_info is not None
                    assert client.server_info.name == "pheasant"
                    assert client.server_info.version == __version__
                    assert client.instructions, "the model-facing instructions must still be sent"
                    tools = await client.list_tools()
                    assert {"search_context", "memory_write"} <= {t.name for t in tools.tools}
    finally:
        app.state.engine.close()


def test_mcp_transport_security_honours_the_cors_escape_hatch(tmp_path) -> None:
    """`cors_allow_all_origins` opens MCP too, or it opens nothing.

    The knob means "my own authenticating ingress fronts this"; refusing MCP
    on host grounds while every other route on the same port answers would be
    an inconsistency an operator has no way to resolve.
    """
    pytest.importorskip("mcp")
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app
    from pheasant.config.loader import load_config

    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: mcp-open
  state_path: {tmp_path / "state"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
server:
  api:
    cors_allow_all_origins: true
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
sources: []
""",
        encoding="utf-8",
    )
    handshake = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pheasant-tests", "version": "1"},
        },
    }
    app = create_app(load_config(config_path), config_path=str(config_path))
    try:
        with fastapi_testclient.TestClient(app, base_url="http://anywhere.example:8765") as client:
            response = client.post(
                "/mcp/",
                json=handshake,
                headers={"accept": "application/json, text/event-stream"},
            )
            assert response.status_code == 200, (
                "an operator who allowed every origin should not be refused by the "
                f"MCP host guard (HTTP {response.status_code})"
            )
    finally:
        app.state.engine.close()


def test_a_refused_mcp_call_still_carries_its_reason(tmp_path) -> None:
    """An agent that mistypes a name has to be told which name was wrong.

    MCP SDK 2.x sorts handler exceptions into deliberate refusals
    (`ToolError`/`ResourceError`, whose text is forwarded) and crashes
    (everything else, reported as a bare "Error executing tool <name>" with
    the text kept server-side). `PheasantTools` refuses deliberately but by
    raising plain `ValueError`/`KeyError`, and 1.x appended every exception's
    text regardless — so the SDK upgrade silently blanked the reason on every
    refusal across all 27 tools and 11 resources, leaving a model no way to
    correct itself. `server.py` translates the facade's anticipated failures
    at the SDK boundary; this is what says so.
    """
    pytest.importorskip("mcp")
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app
    from pheasant.config.loader import load_config

    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: mcp-errors
  state_path: {tmp_path / "state"}
  exports_path: {tmp_path / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
sources: []
""",
        encoding="utf-8",
    )
    app = create_app(load_config(config_path), config_path=str(config_path))
    # Stateless mode answers a call that never handshook, so each of these is
    # one self-contained POST.
    headers = {
        "accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    try:
        with fastapi_testclient.TestClient(app, base_url="http://localhost:8765") as client:
            refused = client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "search_context",
                        "arguments": {"knowledge_base": "nope", "query": "anything"},
                    },
                },
                headers=headers,
            )
            assert refused.status_code == 200, refused.text[:200]
            result = refused.json()["result"]
            assert result["isError"] is True
            text = result["content"][0]["text"]
            assert "Unknown knowledge base: nope" in text, (
                f"the refusal reached the agent without its reason: {text!r}"
            )

            # Resources take the other half of the same fork: a `ToolError`
            # raised in a resource handler is stripped exactly like a crash,
            # so the wrapper has to raise `ResourceError` there instead.
            resource = client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "resources/read",
                    "params": {"uri": "pheasant://knowledge-bases/nope/graph"},
                },
                headers=headers,
            )
            assert resource.status_code == 200, resource.text[:200]
            message = resource.json()["error"]["message"]
            assert "Unknown knowledge base: nope" in message, (
                f"the resource refusal reached the agent without its reason: {message!r}"
            )
    finally:
        app.state.engine.close()


# --------------------------------------------------------------------------
# Deep links into the single-page app
# --------------------------------------------------------------------------


def _app_with_ui(tmp_path: Path, loaded_config, monkeypatch):
    """An app serving a stub UI bundle, so the fallback has a shell to return."""

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>ui</title>", encoding="utf-8")
    monkeypatch.setenv("PHEASANT_UI_DIST", str(dist))
    return create_app(config=loaded_config)


def test_a_client_side_route_serves_the_ui_shell(tmp_path, loaded_config, monkeypatch) -> None:
    """Refreshing any UI page must not return JSON.

    `StaticFiles(html=True)` serves index.html at `/` and 404s everything
    else, so every deep link into the SPA — /evaluation, /memory, /tuning —
    returned an error to a browser. That is not only a broken bookmark: it is
    what a *hard refresh* does, so anyone who reloaded the page they were
    looking at had to navigate back in from the root.
    """

    app = _app_with_ui(tmp_path, loaded_config, monkeypatch)
    client = TestClient(app)
    for route in ("/evaluation", "/tuning", "/some/deep/client/route"):
        response = client.get(route, headers={"accept": "text/html"})
        assert response.status_code == 200, route
        assert "text/html" in response.headers["content-type"], route


def test_the_fallback_does_not_swallow_api_or_asset_404s(
    tmp_path, loaded_config, monkeypatch
) -> None:
    """Three cases where HTML would be the wrong answer.

    A missing asset served as HTML is the worst of them: it becomes a MIME
    error in the console, which is far harder to diagnose than a 404.
    """

    app = _app_with_ui(tmp_path, loaded_config, monkeypatch)
    client = TestClient(app)

    # An API client gets JSON, so a missing endpoint still reads as missing.
    assert client.get("/nope", headers={"accept": "application/json"}).status_code == 404
    # A missing file stays a missing file.
    assert client.get("/assets/gone.js", headers={"accept": "text/html"}).status_code == 404
    # A mistyped method deserves its 404, not a page.
    assert client.post("/tuning", headers={"accept": "text/html"}).status_code in (404, 405)


def test_a_real_api_route_still_wins_over_the_fallback(
    tmp_path, loaded_config, monkeypatch
) -> None:
    """The fallback runs at 404, so routing is untouched."""

    app = _app_with_ui(tmp_path, loaded_config, monkeypatch)
    client = TestClient(app)
    response = client.get("/tuning/parameters", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]
