"""Phase 35.5c — the gRPC preparation transport.

Every test runs against a **real** gRPC server on a loopback port, not a
mock. The value of a second transport is entirely in whether the bytes
arrive; a mocked stub would test the mock.

The suite skips when the optional ``[grpc]`` extra is absent, which is also
the assertion that the extra is genuinely optional — the module is imported
inside the skip guard so a region without grpcio does not fail collection.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.sync.engine import SyncEngine
from pheasant.sync.remote_worker import ResultCache, task_payload
from pheasant.sync.worker_pool import AllWorkersFailed, TaskRejected, WorkerPool, build_transport

grpc = pytest.importorskip("grpc", reason="the [grpc] extra is optional")

from pheasant.sync.grpc_worker import (  # noqa: E402 - after the extra's guard
    DEFAULT_PORT,
    GrpcTransport,
    PreparationWorkerServicer,
    _target,
    load_protos,
    request_from_task,
    task_from_request,
)


def _write_docs(workspace: Path, count: int = 4) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (workspace / f"note-{index}.md").write_text(
            f"# Note {index}\n\nThe deployment gateway rotates credentials nightly.\n",
            encoding="utf-8",
        )


def _config(tmp_path: Path, workspace: Path, *, state_name: str = "state") -> PheasantConfig:
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "grpc-worker",
                "state_path": str(tmp_path / state_name),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / f"{state_name}-exports"),
            },
            "storage": {"graph_snapshots": False},
            "sync": {"concurrency": {"lock_timeout_seconds": 1}},
            "sources": [
                {
                    "name": "docs",
                    "type": "markdown_folder",
                    "path": str(workspace),
                    "include": ["**/*.md"],
                }
            ],
        }
    )


def _task(tmp_path: Path, name: str = "note-0.md") -> dict[str, Any]:
    workspace = tmp_path / "workspace"
    _write_docs(workspace, count=1)
    (workspace / name).write_text("# Title\n\nBody text for the worker.\n", encoding="utf-8")
    config = _config(tmp_path, workspace)
    source = config.sources[0]

    from pheasant.sync.connectors import connector_for_source

    connector = connector_for_source(source, config)
    item = next(item for item in connector.list_items() if item.relative_path == name)
    return task_payload(source, item, connector.read_item(item), None)


class _RunningWorker:
    def __init__(self, server: Any, port: int, servicer: PreparationWorkerServicer) -> None:
        self.server = server
        self.port = port
        self.servicer = servicer

    @property
    def url(self) -> str:
        return f"grpc://127.0.0.1:{self.port}"


@pytest.fixture
def grpc_worker(tmp_path: Path, monkeypatch: Any):  # type: ignore[no-untyped-def]
    from concurrent.futures import ThreadPoolExecutor

    workspace = tmp_path / "worker-workspace"
    _write_docs(workspace, count=1)
    config = _config(tmp_path, workspace, state_name="worker-state")
    config.sync.concurrency.remote_worker_enabled = True
    monkeypatch.setenv("PHEASANT_INDEX_WORKER_TOKEN", "grpc-token")

    _pb2, pb2_grpc = load_protos()
    servicer = PreparationWorkerServicer(config, ResultCache(maxsize=16), version="test")
    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    pb2_grpc.add_PreparationWorkerServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield _RunningWorker(server, port, servicer)
    finally:
        server.stop(grace=0).wait()


# --------------------------------------------------------------------------
# The wire format
# --------------------------------------------------------------------------


def test_file_content_crosses_as_raw_bytes_not_base64(tmp_path: Path) -> None:
    """The measurable reason this transport exists."""

    pb2, _ = load_protos()
    task = _task(tmp_path)
    request = request_from_task(pb2, task, "key")

    raw = request.content
    encoded = task["payload"]["content_base64"]
    assert raw == b"# Title\n\nBody text for the worker.\n"
    assert len(raw) < len(encoded), "base64 was not removed from the wire"
    assert "content_base64" not in request.payload_meta_json


def test_the_task_round_trips_through_the_proto(tmp_path: Path) -> None:
    """One preparation implementation serves both transports.

    A second parse path on the worker is exactly how two wire formats become
    two behaviours, so the gRPC servicer rebuilds the same task dict the JSON
    route hands to ``prepare_task``.
    """

    pb2, _ = load_protos()
    task = _task(tmp_path)
    assert task_from_request(request_from_task(pb2, task, "key")) == task


def test_binary_content_survives_the_round_trip(tmp_path: Path) -> None:
    pb2, _ = load_protos()
    task = _task(tmp_path)
    import base64

    payload = bytes(range(256))
    task["payload"]["content_base64"] = base64.b64encode(payload).decode("ascii")
    request = request_from_task(pb2, task, "key")
    assert request.content == payload
    assert (
        task_from_request(request)["payload"]["content_base64"] == task["payload"]["content_base64"]
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("grpc://host:9000", "host:9000"),
        ("grpcs://host:9000", "host:9000"),
        ("http://host:9000", "host:9000"),
        ("host:9000", "host:9000"),
        ("host", f"host:{DEFAULT_PORT}"),
        ("grpc://host/", f"host:{DEFAULT_PORT}"),
    ],
)
def test_endpoint_urls_are_normalised_to_grpc_targets(url: str, expected: str) -> None:
    assert _target(url) == expected


def test_dns_worker_service_uses_one_round_robin_channel(monkeypatch: Any) -> None:
    """A scaled Compose service must not pin every batch to one replica."""

    created: list[tuple[str, list[tuple[str, Any]]]] = []
    sentinel = object()

    def insecure_channel(target: str, *, options: list[tuple[str, Any]]) -> object:
        created.append((target, options))
        return sentinel

    monkeypatch.setattr(grpc, "insecure_channel", insecure_channel)
    transport = GrpcTransport()

    assert transport._channel("grpc://worker:8766") is sentinel
    assert transport._channel("grpc://worker:8766") is sentinel
    assert len(created) == 1
    assert created[0][0] == "worker:8766"
    assert ("grpc.lb_policy_name", "round_robin") in created[0][1]


# --------------------------------------------------------------------------
# Against a real server
# --------------------------------------------------------------------------


def test_a_batch_is_prepared_over_grpc(tmp_path: Path, grpc_worker: Any) -> None:
    pool = WorkerPool([grpc_worker.url], "grpc-token", timeout=15.0, transport=GrpcTransport())
    task = _task(tmp_path)
    try:
        results = pool.prepare_batch([task, task])
    finally:
        pool.close()

    assert len(results) == 2
    assert all(result is not None for result in results)
    assert results[0].relative_path == "note-0.md"
    assert results[0].chunks


def test_grpc_requires_the_bearer_token(tmp_path: Path, grpc_worker: Any) -> None:
    pool = WorkerPool(
        [grpc_worker.url],
        "wrong-token",
        timeout=15.0,
        transport=GrpcTransport(),
        sleep=lambda _s: None,
    )
    try:
        with pytest.raises(AllWorkersFailed) as caught:
            pool.prepare_batch([_task(tmp_path)])
    finally:
        pool.close()
    assert "UNAUTHENTICATED" in str(caught.value)


def test_a_disabled_grpc_worker_is_an_endpoint_problem_not_a_task_problem(
    tmp_path: Path, grpc_worker: Any
) -> None:
    """UNIMPLEMENTED must trip the breaker, not blame every file that lands."""

    grpc_worker.servicer.config.sync.concurrency.remote_worker_enabled = False
    pool = WorkerPool(
        [grpc_worker.url],
        "grpc-token",
        timeout=15.0,
        transport=GrpcTransport(),
        sleep=lambda _s: None,
    )
    try:
        with pytest.raises(AllWorkersFailed):
            pool.prepare_batch([_task(tmp_path)])
        assert pool.stats()[0]["failures"] > 0
    finally:
        pool.close()


def test_a_refused_file_does_not_fail_the_rest_of_the_stream(
    tmp_path: Path, grpc_worker: Any
) -> None:
    """Streaming is what makes per-item refusal possible.

    The JSON batch route fails the whole batch on one bad task because it
    answers with one body; a stream can refuse one file and keep going, and
    the coordinator prepares just that one locally.
    """

    good = _task(tmp_path)
    bad = {**good, "item": {**good["item"], "relative_path": "diagram.png"}}
    transport = GrpcTransport()
    body, channel = transport.post(
        None,
        grpc_worker.url,
        [good, bad, good],
        ["k1", "k2", "k3"],
        token="grpc-token",
        timeout=15.0,
        deadline_seconds=None,
    )
    transport.discard(channel)

    assert body["results"][0]["parsed"]["relative_path"] == "note-0.md"
    assert "only accepts ordinary text" in body["results"][1]["error"]
    assert body["results"][2]["parsed"] is not None


def test_the_pool_turns_a_refused_item_into_a_local_fallback(
    tmp_path: Path, grpc_worker: Any
) -> None:
    good = _task(tmp_path)
    bad = {**good, "item": {**good["item"], "relative_path": "diagram.png"}}
    pool = WorkerPool([grpc_worker.url], "grpc-token", timeout=15.0, transport=GrpcTransport())
    try:
        with pytest.raises(TaskRejected):
            pool.prepare_batch([good, bad])
    finally:
        pool.close()


def test_grpc_answers_a_retried_key_from_cache(tmp_path: Path, grpc_worker: Any) -> None:
    pool = WorkerPool([grpc_worker.url], "grpc-token", timeout=15.0, transport=GrpcTransport())
    task = _task(tmp_path)
    try:
        first = pool.prepare_batch([task])
        second = pool.prepare_batch([task])
    finally:
        pool.close()

    assert first[0].id == second[0].id
    assert grpc_worker.servicer.cache.hits >= 1


def test_the_grpc_channel_is_reused_across_batches(tmp_path: Path, grpc_worker: Any) -> None:
    channels: list[int] = []

    class Counting(GrpcTransport):
        def _new_channel(self, url: str) -> Any:
            channel = super()._new_channel(url)
            channels.append(id(channel))
            return channel

    pool = WorkerPool([grpc_worker.url], "grpc-token", timeout=15.0, transport=Counting())
    task = _task(tmp_path)
    try:
        for _ in range(4):
            pool.prepare_batch([task])
    finally:
        pool.close()

    assert len(channels) == 1, "a new HTTP/2 channel was opened per batch"


def test_a_dead_grpc_endpoint_fails_over_to_a_live_one(tmp_path: Path, grpc_worker: Any) -> None:
    dead = "grpc://127.0.0.1:1"  # nothing listens on port 1
    pool = WorkerPool(
        [dead, grpc_worker.url],
        "grpc-token",
        timeout=15.0,
        transport=GrpcTransport(),
        sleep=lambda _s: None,
    )
    try:
        results = pool.prepare_batch([_task(tmp_path)])
    finally:
        pool.close()
    assert results[0] is not None


def test_a_deadline_is_enforced_by_the_grpc_runtime(
    tmp_path: Path, monkeypatch: Any, grpc_worker: Any
) -> None:
    """A gRPC deadline is enforced on both sides, not merely advertised.

    The HTTP transport can only *send* its remaining budget and trust the
    peer to read it. Here the runtime cancels the call, which is the property
    that keeps a hung worker from holding a file worker for the full timeout.
    """

    from concurrent.futures import ThreadPoolExecutor

    from pheasant.sync.worker_pool import _Retryable

    released = threading.Event()

    class Stalling(PreparationWorkerServicer):
        def PrepareBatch(self, request_iterator: Any, context: Any):  # noqa: N802
            released.wait(timeout=5)
            yield from super().PrepareBatch(request_iterator, context)

    _pb2, pb2_grpc = load_protos()
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    pb2_grpc.add_PreparationWorkerServicer_to_server(
        Stalling(grpc_worker.servicer.config, ResultCache()), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        transport = GrpcTransport()
        with pytest.raises(_Retryable) as caught:
            transport.post(
                None,
                f"grpc://127.0.0.1:{port}",
                [_task(tmp_path)],
                ["k"],
                token="grpc-token",
                timeout=30.0,
                deadline_seconds=0.25,
            )
        assert "DEADLINE_EXCEEDED" in str(caught.value)
    finally:
        released.set()
        server.stop(grace=0).wait()


def test_a_full_sync_over_grpc_matches_a_local_sync(
    tmp_path: Path, monkeypatch: Any, grpc_worker: Any
) -> None:
    """Rule 4 again: the transport is a capacity control, never a semantic one."""

    def artifacts(engine: SyncEngine) -> list[tuple[Any, ...]]:
        return sorted(
            (row["relative_path"], row["sha256"], row["type"])
            for row in engine.state.rows(
                "SELECT relative_path, sha256, type FROM artifacts WHERE source_id=?", ("docs",)
            )
        )

    workspace = tmp_path / "grpc-corpus"
    _write_docs(workspace, count=5)
    config = _config(tmp_path, workspace, state_name="grpc-sync")
    config.sync.concurrency.file_executor = "remote"
    config.sync.concurrency.worker_transport = "grpc"
    config.sync.concurrency.remote_worker_urls = [grpc_worker.url]
    config.sync.concurrency.max_parallel_files = 2
    config.sync.concurrency.remote_worker_batch_size = 2
    monkeypatch.setenv("PHEASANT_INDEX_WORKER_TOKEN", "grpc-token")

    engine = SyncEngine(config)
    try:
        result = engine.sync_source("docs", "full")
        over_grpc = artifacts(engine)
    finally:
        engine.close()
    assert result.indexed_artifacts == 5

    local_workspace = tmp_path / "local-corpus"
    _write_docs(local_workspace, count=5)
    local_config = _config(tmp_path, local_workspace, state_name="local-sync")
    engine = SyncEngine(local_config)
    try:
        engine.sync_source("docs", "full")
        locally = artifacts(engine)
    finally:
        engine.close()

    assert over_grpc == locally


def test_build_transport_selects_by_name() -> None:
    from pheasant.sync.worker_transport import HttpTransport

    assert isinstance(build_transport("http"), HttpTransport)
    assert isinstance(build_transport("grpc"), GrpcTransport)
    # A typo is not worth failing a sync over; an unknown name logs and uses
    # the dependency-free default.
    assert isinstance(build_transport("gRPC-ish"), HttpTransport)


def test_the_worker_cli_refuses_to_start_when_the_role_is_not_enabled(
    tmp_path: Path, capsys: Any
) -> None:
    import yaml

    from pheasant.cli import main

    workspace = tmp_path / "cli-workspace"
    _write_docs(workspace, count=1)
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "pheasant": {
                    "name": "cli",
                    "state_path": str(tmp_path / "cli-state"),
                    "workspace_root": str(workspace),
                }
            }
        ),
        encoding="utf-8",
    )

    assert main(["worker", "--config", str(config_path)]) == 1
    assert "remote_worker_enabled is false" in capsys.readouterr().out


def test_a_taxonomy_source_is_prepared_by_the_fleet_and_matches_local(
    tmp_path: Path, monkeypatch: Any, grpc_worker: Any
) -> None:
    """Remote preparation used to refuse taxonomy-enabled sources outright, so
    a region with taxonomy on everywhere parsed every file on its indexer.
    Headings and the chunk plan now travel with the chunks they label."""

    def indexed(engine: SyncEngine) -> list[tuple[Any, ...]]:
        return [
            tuple(row)
            for row in engine.state.rows(
                "SELECT id, text, start_line, end_line, heading_path FROM chunks ORDER BY id", ()
            )
        ]

    def corpus(name: str) -> Path:
        workspace = tmp_path / name
        workspace.mkdir()
        for index in range(3):
            (workspace / f"guide-{index}.md").write_text(
                f"# Guide {index}\n\n## Install\n\n"
                + "Run the installer. " * 80
                + "\n\n## Configure\n\nSet the token.\n",
                encoding="utf-8",
            )
        return workspace

    results = []
    for name, remote in (("over-grpc", True), ("local", False)):
        config = _config(tmp_path, corpus(name), state_name=f"{name}-state")
        config.sources[0].taxonomy.enabled = True
        config.sources[0].chunking.strategy = "auto"
        if remote:
            config.sync.concurrency.file_executor = "remote"
            config.sync.concurrency.worker_transport = "grpc"
            config.sync.concurrency.remote_worker_urls = [grpc_worker.url]
            config.sync.concurrency.max_parallel_files = 2
            monkeypatch.setenv("PHEASANT_INDEX_WORKER_TOKEN", "grpc-token")
        engine = SyncEngine(config)
        try:
            assert engine.sync_source("docs", "full").indexed_artifacts == 3
            results.append(indexed(engine))
        finally:
            engine.close()

    assert len(grpc_worker.servicer.cache) >= 3, "the fleet answered nothing"
    over_grpc, locally = results
    assert over_grpc == locally
    assert any(row[4] for row in locally)
