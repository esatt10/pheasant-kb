"""Acceptance tests for the staged parallel indexing pipeline."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.search.vector_store import NumpyVectorStore, VectorIndexer
from pheasant.sync.engine import SyncEngine


def _config(
    tmp_path: Path,
    workspace: Path,
    *,
    state_name: str,
    file_workers: int = 1,
    source_workers: int = 1,
    file_executor: str = "thread",
    sources: list[dict[str, Any]] | None = None,
) -> PheasantConfig:
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "parallel-acceptance",
                "state_path": str(tmp_path / state_name),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / f"{state_name}-exports"),
            },
            "storage": {"graph_snapshots": False},
            "sync": {
                "concurrency": {
                    "max_parallel_sources": source_workers,
                    "max_parallel_files": file_workers,
                    "max_parallel_embeddings": 1,
                    "file_executor": file_executor,
                    "lock_timeout_seconds": 1,
                }
            },
            "sources": sources
            or [
                {
                    "name": "docs",
                    "type": "markdown_folder",
                    "path": str(workspace),
                    "include": ["**/*.md"],
                }
            ],
        }
    )


def _write_docs(root: Path, count: int = 4) -> None:
    nested = root / "docs"
    nested.mkdir(parents=True)
    for index in range(count):
        (nested / f"file-{index}.md").write_text(
            f"# File {index}\n\nDeterministic parallel content {index}.\n",
            encoding="utf-8",
        )


def test_file_preparation_uses_the_configured_parallel_workers(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    workspace = tmp_path / "workspace"
    _write_docs(workspace)
    config = _config(tmp_path, workspace, state_name="thread-state", file_workers=4)
    engine = SyncEngine(config)

    from pheasant.sync import engine as engine_module

    original = engine_module.parse_connector_payload
    rendezvous = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0
    worker_threads: set[str] = set()

    def observed_parse(*args: Any, **kwargs: Any) -> Any:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            worker_threads.add(threading.current_thread().name)
            if active >= 2:
                rendezvous.set()
        assert rendezvous.wait(5), "file preparation never overlapped"
        try:
            return original(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(engine_module, "parse_connector_payload", observed_parse)
    try:
        result = engine.sync_source("docs", "full")
    finally:
        engine.close()

    assert result.indexed_artifacts == 4
    assert max_active >= 2
    assert len(worker_threads) >= 2


def test_incremental_hash_check_skips_parse_after_one_parallel_read(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    workspace = tmp_path / "workspace"
    _write_docs(workspace)
    config = _config(tmp_path, workspace, state_name="incremental-state", file_workers=4)
    engine = SyncEngine(config)
    try:
        engine.sync_source("docs", "full")

        def parse_must_not_run(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("unchanged content reached parse/chunk work")

        monkeypatch.setattr(
            "pheasant.sync.engine.parse_connector_payload",
            parse_must_not_run,
        )
        result = engine.sync_source("docs", "incremental")
    finally:
        engine.close()

    assert result.indexed_artifacts == 0
    assert result.skipped_artifacts == 4
    assert result.details["fetched"] == 4


def test_parallel_sources_overlap_without_losing_state(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    workspace = tmp_path / "workspace"
    first = workspace / "first"
    second = workspace / "second"
    _write_docs(first, count=1)
    _write_docs(second, count=1)
    sources = [
        {
            "name": name,
            "type": "markdown_folder",
            "path": str(path),
            "include": ["**/*.md"],
        }
        for name, path in (("first", first), ("second", second))
    ]
    config = _config(
        tmp_path,
        workspace,
        state_name="source-state",
        source_workers=2,
        sources=sources,
    )
    engine = SyncEngine(config)

    from pheasant.sync.connectors import FilesystemConnector

    original = FilesystemConnector.read_item
    rendezvous = threading.Barrier(2)

    def observed_read(self: Any, item: Any) -> Any:
        rendezvous.wait(timeout=5)
        return original(self, item)

    monkeypatch.setattr(FilesystemConnector, "read_item", observed_read)
    try:
        results = engine.sync_all("full")
        artifact_count = engine.stats["artifact_count"]
    finally:
        engine.close()

    assert [result.source_id for result in results] == ["first", "second"]
    assert [result.indexed_artifacts for result in results] == [1, 1]
    assert artifact_count == 2


def test_process_workers_preserve_the_exact_published_generation(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Parallelism must not change what gets published (pillar 1).

    Asserted on the **generation id** rather than on the persisted bytes. That
    is not a weakening: the id is a digest of every row's own content digest,
    so two regions agreeing on it agree node-for-node and edge-for-edge — and
    unlike a zstd blob it says so whichever backend wrote it. Comparing bytes
    also quietly asserted things nobody meant, like compression level.
    """

    workspace = tmp_path / "workspace"
    _write_docs(workspace)
    monkeypatch.setattr(
        "pheasant.graph.builder.utc_now",
        lambda: "2026-08-12T12:00:00Z",
    )

    serial = SyncEngine(_config(tmp_path, workspace, state_name="serial-state", file_workers=1))
    try:
        serial.sync_source("docs", "full")
        serial_generation = serial.graph_store.published_generation(serial.config.knowledge_base_id)
    finally:
        serial.close()

    parallel = SyncEngine(
        _config(
            tmp_path,
            workspace,
            state_name="process-state",
            file_workers=2,
            file_executor="process",
        )
    )
    try:
        parallel.sync_source("docs", "full")
        parallel_generation = parallel.graph_store.published_generation(
            parallel.config.knowledge_base_id
        )
    finally:
        parallel.close()

    assert serial_generation is not None and parallel_generation is not None
    assert parallel_generation["generation_id"] == serial_generation["generation_id"]
    assert parallel_generation["nodes"] == serial_generation["nodes"]
    assert parallel_generation["edges"] == serial_generation["edges"]


def test_embedding_batches_run_concurrently_then_commit_once(tmp_path: Path) -> None:
    class ConcurrentEmbedder:
        model = "concurrency-probe"
        batch_size = 2

        def __init__(self) -> None:
            self.calls = 0
            self.texts_embedded = 0
            self._barrier = threading.Barrier(3)
            self._lock = threading.Lock()
            self.max_active = 0
            self.active = 0

        def embed(self, texts: list[str]) -> list[list[float]]:
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            self._barrier.wait(timeout=5)
            try:
                with self._lock:
                    self.calls += 1
                    self.texts_embedded += len(texts)
                return [[float(len(text)), 1.0] for text in texts]
            finally:
                with self._lock:
                    self.active -= 1

    embedder = ConcurrentEmbedder()
    store = NumpyVectorStore(tmp_path / "vectors")
    indexer = VectorIndexer(
        embedder,
        store,
        queue_size=6,
        max_parallel_embeddings=3,
    )
    rows = [
        {"id": f"chunk-{index}", "text": f"text {index}", "text_hash": f"hash-{index}"}
        for index in range(6)
    ]

    queued = indexer.index_artifact("docs", "artifact", rows)
    indexer.flush()

    assert queued == 6
    assert embedder.calls == 3
    assert embedder.max_active == 3
    assert store.count() == 6


def test_remote_executor_dispatches_immutable_tasks(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    workspace = tmp_path / "workspace"
    _write_docs(workspace)
    config = _config(tmp_path, workspace, state_name="remote-state", file_workers=4)
    config.sync.concurrency.file_executor = "remote"
    config.sync.concurrency.remote_worker_urls = ["https://worker-a", "https://worker-b"]
    config.sources[0].connector.headers = {"Authorization": "must-not-leak"}
    monkeypatch.setenv("PHEASANT_INDEX_WORKER_TOKEN", "test-token")

    config.sync.concurrency.remote_worker_batch_size = 1

    from pheasant.sync import remote_worker, worker_transport

    endpoints: list[str] = []

    class LocalProtocolProbe:
        """Speak the batch protocol without a socket, and check the boundary."""

        def post(
            self,
            connection: Any,
            url: str,
            tasks: list[dict[str, Any]],
            keys: list[str],
            *,
            token: str,
            timeout: float,
            deadline_seconds: float | None,
        ) -> Any:
            endpoints.append(url)
            assert token == "test-token"
            assert timeout <= 120
            assert deadline_seconds is not None and deadline_seconds <= 120
            assert len(keys) == len(tasks)
            for task in tasks:
                assert "connector" not in task["source"]
            return {"results": remote_worker.prepare_batch_tasks(tasks, keys)}, None

        def discard(self, connection: Any) -> None:  # pragma: no cover - no sockets here
            pass

    monkeypatch.setattr(worker_transport, "HttpTransport", LocalProtocolProbe)
    engine = SyncEngine(config)
    try:
        result = engine.sync_source("docs", "full")
    finally:
        engine.close()

    assert result.indexed_artifacts == 4
    assert set(endpoints) == {"https://worker-a", "https://worker-b"}


def test_remote_worker_endpoint_is_disabled_and_bearer_authenticated(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    workspace = tmp_path / "workspace"
    _write_docs(workspace, count=1)
    config = _config(tmp_path, workspace, state_name="worker-state")
    source = config.sources[0]

    from pheasant.sync.connectors import ConnectorItem, ConnectorPayload
    from pheasant.sync.remote_worker import task_payload

    path = next(workspace.rglob("*.md"))
    relative = path.relative_to(workspace).as_posix()
    item = ConnectorItem(
        identity="worker-test",
        relative_path=relative,
        uri=path.as_uri(),
        size_bytes=path.stat().st_size,
        metadata={"path": str(path)},
    )
    payload = ConnectorPayload(
        item=item,
        content=path.read_bytes(),
        size_bytes=path.stat().st_size,
        metadata={"path": str(path)},
    )
    task = task_payload(source, item, payload, None)

    with TestClient(create_app(config=config)) as client:
        assert client.post("/internal/indexing/prepare", json=task).status_code == 404

    config.sync.concurrency.remote_worker_enabled = True
    monkeypatch.setenv("PHEASANT_INDEX_WORKER_TOKEN", "worker-secret")
    with TestClient(create_app(config=config)) as client:
        assert client.post("/internal/indexing/prepare", json=task).status_code == 401
        response = client.post(
            "/internal/indexing/prepare",
            json=task,
            headers={"Authorization": "Bearer worker-secret"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["parsed"]["relative_path"] == relative

    from pheasant.sync import remote_worker

    captured: dict[str, Any] = {}

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: Any) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps({"parsed": remote_worker.prepare_task(task)}).encode()

    def fake_urlopen(request: Any, timeout: float) -> Response:
        captured.update(
            url=request.full_url,
            authorization=request.get_header("Authorization"),
            timeout=timeout,
        )
        return Response()

    monkeypatch.setattr(remote_worker, "urlopen", fake_urlopen)
    parsed = remote_worker.prepare_remote(
        "https://worker.example/",
        "worker-secret",
        task,
        timeout=3,
    )
    assert parsed is not None and parsed.relative_path == relative
    assert captured == {
        "url": "https://worker.example/internal/indexing/prepare",
        "authorization": "Bearer worker-secret",
        "timeout": 3,
    }


def test_remote_workers_accept_the_configured_repository_text_formats() -> None:
    from pheasant.sync.engine import _process_safe_text_path
    from pheasant.sync.remote_worker import _remote_preparation_path, _remote_text_path

    configured_text = [
        "component.jsx",
        "component.tsx",
        "guide.mdx",
        "reference.rst",
        "bootstrap.sh",
        "Dockerfile",
    ]
    assert all(_process_safe_text_path(path) for path in configured_text)
    assert all(_remote_text_path(path) for path in configured_text)
    assert not _remote_text_path("document.pdf")
    assert _remote_preparation_path("document.pdf")
    assert _remote_preparation_path("document.docx")
    assert not _remote_preparation_path("recording.mp3")


def test_failed_parallel_embedding_flush_restores_all_pending_work(tmp_path: Path) -> None:
    class FlakyEmbedder:
        model = "flaky"
        batch_size = 2

        def __init__(self) -> None:
            self.calls = 0
            self.texts_embedded = 0
            self._lock = threading.Lock()
            self._failed = False

        def embed(self, texts: list[str]) -> list[list[float]]:
            with self._lock:
                self.calls += 1
                if not self._failed:
                    self._failed = True
                    raise RuntimeError("transient batch failure")
                self.texts_embedded += len(texts)
            return [[float(len(text)), 1.0] for text in texts]

    embedder = FlakyEmbedder()
    store = NumpyVectorStore(tmp_path / "flaky-vectors")
    indexer = VectorIndexer(
        embedder,
        store,
        queue_size=4,
        max_parallel_embeddings=2,
    )
    rows = [
        {"id": f"chunk-{index}", "text": f"text {index}", "text_hash": f"hash-{index}"}
        for index in range(4)
    ]

    import pytest

    with pytest.raises(RuntimeError, match="transient batch failure"):
        indexer.index_artifact("docs", "artifact", rows)
    assert store.count() == 0

    indexer.flush()
    assert store.count() == 4


def test_sync_benchmark_emits_capacity_metrics() -> None:
    from pheasant.sync.benchmark import run_benchmark

    report = run_benchmark(files=2, lines=2, workers=(1,), repeats=1)

    assert report["fixture"] == {"files": 2, "lines_per_file": 2}
    assert report["cache"] == "warm"
    assert report["runs"][0]["indexed_artifacts"] == 2
    assert report["runs"][0]["files_per_second"] > 0
    assert report["runs"][0]["speedup"] == 1.0
    assert report["runs"][0]["unchanged_incremental_indexed_artifacts"] == 0
    assert report["runs"][0]["unchanged_incremental_skipped_artifacts"] == 2
    assert report["runs"][0]["unchanged_incremental_embedding_calls"] == 0


def test_source_lock_timeout_queues_a_second_same_source_writer(tmp_path: Path) -> None:
    from pheasant.sync.locks import source_lock

    entered = threading.Event()

    def holder() -> None:
        with source_lock(tmp_path, "docs"):
            entered.set()
            time.sleep(0.15)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(2)
    started = time.monotonic()
    with source_lock(tmp_path, "docs", timeout_s=1, poll_interval_s=0.01):
        pass
    elapsed = time.monotonic() - started
    thread.join(timeout=2)

    assert elapsed >= 0.1
    assert not thread.is_alive()
