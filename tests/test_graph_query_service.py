from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.graph.query_service import GraphQueryClient, GraphQueryError, RemoteGraph
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
    # The answerer's expansion bounds each walk; the service has to honour it.
    bounded = client.post(
        "/internal/graph/query",
        headers=headers,
        json={
            "operation": "neighbors_many",
            "parameters": {"node_ids": ["perf:a"], "depth": 2, "max_nodes": 1},
        },
    ).json()["result"]
    assert [len(item["neighbors"]) for item in bounded] == [1]
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


# --- the client's transport, against a real socket --------------------------
#
# Each case below scripts what the server does with one request: answer and
# keep the connection, answer and close it, or answer short. The handler gets
# the request's ordinal and its headers and returns the raw response bytes and
# whether to close afterwards.

Reply = tuple[bytes, bool]


def _response(status: str, payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode()
    head = f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
    return f"{head}Content-Length: {len(body)}\r\n\r\n".encode() + body


def _answer(result: Any) -> bytes:
    return _response("200 OK", {"result": result})


@contextmanager
def _scripted_server(
    handler: Callable[[int, dict[str, str]], Reply],
) -> Iterator[dict[str, Any]]:
    listener = socket.create_server(("127.0.0.1", 0))
    seen: dict[str, Any] = {"port": listener.getsockname()[1], "connections": 0, "requests": []}

    def serve(conn: socket.socket) -> None:
        with conn:
            buffer = b""
            while True:
                while b"\r\n\r\n" not in buffer:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
                head, _, buffer = buffer.partition(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                headers = {
                    k.strip().lower(): v.strip()
                    for k, _, v in (line.partition(":") for line in lines[1:])
                }
                length = int(headers.get("content-length", "0"))
                while len(buffer) < length:
                    buffer += conn.recv(65536)
                buffer = buffer[length:]
                seen["requests"].append(headers)
                reply, close = handler(len(seen["requests"]), headers)
                conn.sendall(reply)
                if close:
                    return

    def accept() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            seen["connections"] += 1
            threading.Thread(target=serve, args=(conn,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield seen
    finally:
        listener.close()


def _client(port: int, monkeypatch: Any, retries: int = 1) -> GraphQueryClient:
    monkeypatch.setenv("TEST_GRAPH_TOKEN", "graph-token")
    return GraphQueryClient(
        f"http://127.0.0.1:{port}", "TEST_GRAPH_TOKEN", timeout_seconds=5, retries=retries
    )


def test_graph_client_keeps_one_connection_open_across_queries(monkeypatch: Any) -> None:
    # urllib sent `Connection: close` on every call, and over a real network
    # hop the server's close then cut answers larger than its send buffer
    # short (65 KB of a 363 KB node_link on Azure Container Apps).
    with _scripted_server(lambda n, _h: (_answer({"call": n}), False)) as server:
        client = _client(server["port"], monkeypatch)
        assert client.query("stats") == {"call": 1}
        assert client.query("stats") == {"call": 2}

    assert server["connections"] == 1
    assert all(h.get("connection", "").lower() != "close" for h in server["requests"])
    assert all(h["authorization"] == "Bearer graph-token" for h in server["requests"])


def test_graph_client_reopens_a_connection_the_server_dropped_while_idle(
    monkeypatch: Any,
) -> None:
    # uvicorn closes an idle kept-alive connection after a few seconds. The
    # next call finds a dead socket before any answer exists; that is a
    # reconnect, not a failure, so it holds even with no retries to spend.
    with _scripted_server(lambda n, _h: (_answer({"call": n}), True)) as server:
        client = _client(server["port"], monkeypatch, retries=0)
        assert client.query("stats") == {"call": 1}
        assert client.query("stats") == {"call": 2}

    assert server["connections"] == 2


def test_graph_client_retries_a_truncated_answer(monkeypatch: Any) -> None:
    whole = _answer({"nodes": ["a" * 40]})
    short = whole[: len(whole) - 20]

    with _scripted_server(lambda n, _h: (short, True) if n == 1 else (whole, False)) as server:
        client = _client(server["port"], monkeypatch, retries=1)
        assert client.query("node_link") == {"nodes": ["a" * 40]}
    assert len(server["requests"]) == 2

    # With nothing left to retry it is the client's own error, not a raw
    # http.client.IncompleteRead escaping as a 500.
    with _scripted_server(lambda _n, _h: (short, True)) as server:
        client = _client(server["port"], monkeypatch, retries=0)
        with pytest.raises(GraphQueryError, match="could not answer 'node_link'"):
            client.query("node_link")


def test_graph_client_refuses_at_once_on_4xx_and_retries_5xx(monkeypatch: Any) -> None:
    forbidden = _response("403 Forbidden", {"detail": "bad token"})
    with _scripted_server(lambda _n, _h: (forbidden, False)) as server:
        client = _client(server["port"], monkeypatch, retries=3)
        with pytest.raises(GraphQueryError, match="rejected 'stats': bad token"):
            client.query("stats")
    assert len(server["requests"]) == 1

    def flaky(n: int, _h: dict[str, str]) -> Reply:
        if n == 1:
            return _response("503 Service Unavailable", {"detail": "draining"}), False
        return _answer({"call": n}), False

    with _scripted_server(flaky) as server:
        client = _client(server["port"], monkeypatch, retries=1)
        assert client.query("stats") == {"call": 2}
    assert len(server["requests"]) == 2
