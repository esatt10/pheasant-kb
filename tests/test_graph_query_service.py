from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.graph.query_service import GraphQueryClient, RemoteGraph
from pheasant.sync.engine import SyncEngine


def _config(tmp_path: Path, *, remote: bool = False) -> PheasantConfig:
    workspace = tmp_path / "workspace"
    source = workspace / "docs"
    source.mkdir(parents=True, exist_ok=True)
    (source / "runbook.md").write_text(
        "# Deployment runbook\n\nThe gateway rotates credentials nightly.\n",
        encoding="utf-8",
    )
    payload: dict[str, Any] = {
        "pheasant": {
            "name": "graph-service-test",
            "state_path": str(tmp_path / "state"),
            "workspace_root": str(workspace),
            "exports_path": str(tmp_path / "exports"),
        },
        "storage": {"graph_snapshots": False},
        # Loopback: these tests run the app in-process and bind nothing. A
        # non-loopback bind with no token is what a fleet role refuses at
        # startup (tests/test_process_roles.py), and claiming that posture
        # here would be describing a deployment none of this performs.
        "server": {"host": "127.0.0.1"},
        "sync": {"queue": {"enabled": True}},
        "sources": [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(source),
                "include": ["*.md", "**/*.md"],
            }
        ],
    }
    if remote:
        payload["graph"] = {
            "query_service_url": "http://graph.internal:8765",
            "query_service_token_env": "TEST_GRAPH_TOKEN",
        }
    return PheasantConfig.model_validate(payload)


def test_graph_role_exposes_only_authenticated_query_operations(
    tmp_path: Path, monkeypatch: Any
) -> None:
    config = _config(tmp_path)
    writer = SyncEngine(config)
    try:
        writer.sync_source("docs", "full")
    finally:
        writer.close()

    config.graph.query_service_token_env = "TEST_GRAPH_TOKEN"
    monkeypatch.setenv("TEST_GRAPH_TOKEN", "secret")
    app = create_app(config, role="graph")
    client = TestClient(app)

    assert client.get("/health").json()["role"] == "graph"
    assert (
        client.post(
            "/internal/graph/query", json={"operation": "stats", "parameters": {}}
        ).status_code
        == 401
    )

    headers = {"Authorization": "Bearer secret"}
    stats = client.post(
        "/internal/graph/query",
        headers=headers,
        json={"operation": "stats", "parameters": {}},
    ).json()["result"]
    assert stats["total_nodes"] > 1
    assert stats["total_links"] > 0

    results = client.post(
        "/internal/graph/query",
        headers=headers,
        json={
            "operation": "search",
            "parameters": {"query": "deployment", "max_results": 5},
        },
    ).json()["result"]
    assert results
    node_id = str(results[0]["node_id"])
    node = client.post(
        "/internal/graph/query",
        headers=headers,
        json={"operation": "node", "parameters": {"node_id": node_id}},
    ).json()["result"]
    assert node is not None


def test_graph_service_expands_multiple_roots_in_one_bounded_operation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    config = _config(tmp_path)
    writer = SyncEngine(config)
    try:
        graph = writer.graph_builder.graph
        graph.add_node("perf:a", type="file", label="a")
        graph.add_node("perf:b", type="file", label="b")
        graph.add_node("perf:c", type="file", label="c")
        graph.add_edge("perf:a", "perf:c", type="references")
        graph.add_edge("perf:b", "perf:c", type="references")
        writer.graph_store.save("graph-service-test", graph)
    finally:
        writer.close()

    config.graph.query_service_token_env = "TEST_GRAPH_TOKEN"
    monkeypatch.setenv("TEST_GRAPH_TOKEN", "secret")
    client = TestClient(create_app(config, role="graph"))
    headers = {"Authorization": "Bearer secret"}
    roots = ["perf:a", "perf:b", "perf:missing"]
    singles = [
        client.post(
            "/internal/graph/query",
            headers=headers,
            json={"operation": "neighbors", "parameters": {"node_id": root, "depth": 1}},
        ).json()["result"]
        for root in roots
    ]
    many = client.post(
        "/internal/graph/query",
        headers=headers,
        json={"operation": "neighbors_many", "parameters": {"node_ids": roots, "depth": 1}},
    ).json()["result"]
    assert many == singles
    assert (
        client.post(
            "/internal/graph/query",
            headers=headers,
            json={"operation": "neighbors_many", "parameters": {"node_ids": ["perf:a"] * 17}},
        ).status_code
        == 400
    )


def test_remote_api_does_not_load_the_persisted_graph(tmp_path: Path, monkeypatch: Any) -> None:
    config = _config(tmp_path, remote=True)
    writer = SyncEngine(config)
    try:
        writer.sync_source("docs", "full")
    finally:
        writer.close()

    def fail_load(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an API replica tried to materialize the persisted graph")

    monkeypatch.setattr("pheasant.persistence.graph_store.GraphStore.load", fail_load)
    app = create_app(config, role="api")

    assert app.state.engine._loads_persisted_graph is False
    assert app.state.engine.graph_builder.graph.number_of_nodes() == 1
    assert isinstance(app.state.serving_graph, RemoteGraph)
    monkeypatch.setattr(app.state.serving_graph, "ping", lambda: {"total_nodes": 1})
    ready = TestClient(app).get("/ready")
    assert ready.status_code == 200
    assert ready.json()["refreshes_graph"] is False


def test_graph_role_is_not_ready_without_its_internal_token(
    tmp_path: Path, monkeypatch: Any
) -> None:
    config = _config(tmp_path)
    config.graph.query_service_token_env = "TEST_GRAPH_TOKEN"
    monkeypatch.delenv("TEST_GRAPH_TOKEN", raising=False)

    client = TestClient(create_app(config, role="graph"))
    ready = client.get("/ready")

    assert ready.status_code == 503
    assert ready.json()["reason"] == "graph query service token is not configured"


def test_remote_graph_keeps_only_bounded_stats_and_nodes() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def query(self, operation: str, **parameters: Any) -> Any:
            self.calls.append((operation, parameters))
            if operation == "stats":
                return {"total_nodes": 12, "total_links": 18, "node_types": {"file": 4}}
            if operation == "node":
                return {"id": parameters["node_id"], "type": "file"}
            if operation == "neighbors":
                return {"node_id": parameters["node_id"], "neighbors": []}
            raise AssertionError(operation)

    client = Client()
    graph = RemoteGraph(client)  # type: ignore[arg-type]

    assert graph.number_of_nodes() == 12
    assert graph.number_of_edges() == 18
    assert graph.type_counts() == {"file": 4}
    # One stats request is cached across all three aggregate reads.
    assert [name for name, _ in client.calls].count("stats") == 1
    assert graph.nodes["file:1"]["type"] == "file"
    assert graph.remote_neighbors(node_id="file:1", depth=1)["neighbors"] == []


def test_remote_neighbor_batch_uses_one_call_and_supports_older_graph_services() -> None:
    from pheasant.graph.query_service import GraphQueryError

    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.old = False
            self.misordered = False

        def query(self, operation: str, **parameters: Any) -> Any:
            self.calls.append(operation)
            if operation == "neighbors_many":
                if self.old:
                    raise GraphQueryError(
                        "graph service rejected 'neighbors_many': "
                        "unknown graph operation: neighbors_many"
                    )
                if self.misordered:
                    return [
                        {"node_id": item, "neighbors": []}
                        for item in reversed(parameters["node_ids"])
                    ]
                return [{"node_id": item, "neighbors": []} for item in parameters["node_ids"]]
            return {"node_id": parameters["node_id"], "neighbors": []}

    client = Client()
    graph = RemoteGraph(client)  # type: ignore[arg-type]
    assert [item["node_id"] for item in graph.remote_neighbors_many(["a", "b"])] == ["a", "b"]
    assert client.calls == ["neighbors_many"]
    client.old = True
    client.calls.clear()
    assert [item["node_id"] for item in graph.remote_neighbors_many(["a", "b"])] == ["a", "b"]
    assert client.calls == ["neighbors_many", "neighbors", "neighbors"]
    client.old = False
    client.misordered = True
    with pytest.raises(GraphQueryError, match="invalid neighbors_many response"):
        graph.remote_neighbors_many(["a", "b"])


def test_graph_client_round_robins_cached_service_replicas(monkeypatch: Any) -> None:
    calls = 0

    def addresses(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        nonlocal calls
        calls += 1
        return [
            (2, 1, 6, "", ("10.0.0.11", 8765)),
            (2, 1, 6, "", ("10.0.0.12", 8765)),
        ]

    monkeypatch.setattr("pheasant.graph.query_service.socket.getaddrinfo", addresses)
    client = GraphQueryClient("http://graph:8765", "TEST_GRAPH_TOKEN")

    first, host1 = client._target()
    second, host2 = client._target()
    third, _host3 = client._target()

    assert first.startswith("http://10.0.0.11:8765/")
    assert second.startswith("http://10.0.0.12:8765/")
    assert third.startswith("http://10.0.0.11:8765/")
    assert host1 == host2 == "graph:8765"
    assert calls == 1
