from __future__ import annotations

import hashlib
import io
import json
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import (
    ChunkingSettings,
    PheasantConfig,
    PheasantSettings,
    SourceConfig,
    SourceConnectorSettings,
    SourceSyncSettings,
    SourceType,
)
from pheasant.mcp_server.tools import PheasantTools
from pheasant.sync.connectors import ConnectorUnavailable
from pheasant.sync.engine import SyncEngine
from pheasant.sync.web_connector import WebCollectionConnector


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def web_server(tmp_path: Path) -> Iterator[str]:
    web_root = tmp_path / "web"
    web_root.mkdir()
    (web_root / "guide.md").write_text(
        "# Connector Guide\n\nWeb collection connector content for search.\n",
        encoding="utf-8",
    )
    handler = partial(QuietHandler, directory=str(web_root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _CountingHandler(BaseHTTPRequestHandler):
    """Mock transport for Synapse 21.3: serves bytes with optional ETag
    validators and counts conditional 304s vs. actual body downloads."""

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        server: Any = self.server
        content = server.files.get(self.path)
        if content is None:
            self.send_response(404)
            self.end_headers()
            return
        etag = '"' + hashlib.sha256(content).hexdigest()[:16] + '"'
        if server.send_validators and self.headers.get("If-None-Match") == etag:
            with server.lock:
                server.counters["not_modified"] += 1
            self.send_response(304)
            self.end_headers()
            return
        with server.lock:
            server.counters["body_downloads"] += 1
            per_path = server.counters["downloads_by_path"]
            per_path[self.path] = per_path.get(self.path, 0) + 1
        suffix = self.path.rsplit(".", 1)[-1]
        mime = {"md": "text/markdown", "json": "application/json"}.get(suffix, "text/plain")
        self.send_response(200)
        if server.send_validators:
            self.send_header("ETag", etag)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


@pytest.fixture()
def counting_server() -> Iterator[Callable[..., ThreadingHTTPServer]]:
    started: list[tuple[ThreadingHTTPServer, threading.Thread]] = []

    def _start(files: dict[str, bytes], send_validators: bool = True) -> ThreadingHTTPServer:
        server: Any = ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
        server.files = files
        server.send_validators = send_validators
        server.lock = threading.Lock()
        server.counters = {"body_downloads": 0, "not_modified": 0, "downloads_by_path": {}}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return server

    yield _start
    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _FakeS3Client:
    """Offline boto3 stand-in recording list_objects_v2/get_object calls."""

    def __init__(self, objects: dict[str, dict[str, Any]]):
        self.objects = objects
        self.list_calls = 0
        self.get_calls: list[str] = []

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.list_calls += 1
        contents = [
            {
                "Key": key,
                "Size": len(obj["content"]),
                "ETag": '"' + hashlib.sha256(obj["content"]).hexdigest()[:16] + '"',
                "LastModified": obj["last_modified"],
            }
            for key, obj in sorted(self.objects.items())
        ]
        return {"Contents": contents, "IsTruncated": False}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 (boto3 API)
        self.get_calls.append(Key)
        return {"Body": io.BytesIO(self.objects[Key]["content"]), "ContentType": "text/markdown"}


def _base_url(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def _web_config(tmp_path: Path, urls: list[str]) -> PheasantConfig:
    return _config(
        tmp_path,
        SourceConfig(
            name="web-docs",
            type=SourceType.web_collection,
            path=tmp_path,
            urls=urls,
            include=["**/*.md"],
            # Every page due on every sync: these tests are about what a
            # *check* costs (a 304, a hash comparison), not about when one is
            # made. The per-URL schedule has its own tests in
            # tests/test_web_page_registration.py.
            sync=SourceSyncSettings(on_startup=False, interval_seconds=0),
            connector=SourceConnectorSettings(allow_experimental=True),
        ),
    )


def test_misclassified_github_tree_url_has_actionable_error(tmp_path: Path) -> None:
    config = _web_config(tmp_path, ["https://github.com/apache/spark/tree/master/python"])
    engine = SyncEngine(config)
    connector = WebCollectionConnector(config.sources[0], engine.state)

    with pytest.raises(ConnectorUnavailable, match="clone the repository"):
        connector.list_items()


def _artifact_rows(engine: SyncEngine, source_id: str) -> list[tuple]:
    return [
        tuple(row)
        for row in engine.state.rows(
            "SELECT id, sha256, last_indexed_at FROM artifacts "
            "WHERE source_id=? ORDER BY relative_path",
            (source_id,),
        )
    ]


def _latest_event(engine: SyncEngine, source_id: str) -> dict:
    events = engine.state.list_sync_events(source_id)
    assert events, "expected at least one sync_events row"
    return events[0]


def test_web_second_sync_downloads_zero_bodies(
    tmp_path: Path,
    counting_server: Callable[..., ThreadingHTTPServer],
) -> None:
    server: Any = counting_server(
        {
            "/guide.md": b"# Guide\n\nWeb body one for incremental tests.\n",
            "/intro.md": b"# Intro\n\nWeb body two for incremental tests.\n",
        }
    )
    base = _base_url(server)
    config = _web_config(tmp_path, [f"{base}/guide.md", f"{base}/intro.md"])
    engine = SyncEngine(config)

    first = engine.sync_source("web-docs", "full")
    assert first.indexed_artifacts == 2
    assert server.counters["body_downloads"] == 2
    rows_before = _artifact_rows(engine, "web-docs")
    manifest_before = engine.manifests.load("web-docs")["artifacts"]

    second = engine.sync_source("web-docs", "incremental")

    assert second.indexed_artifacts == 0
    assert second.skipped_artifacts == 2
    assert server.counters["body_downloads"] == 2, "second sync must download zero bodies"
    assert server.counters["not_modified"] == 2, "both URLs must be answered 304"
    assert _artifact_rows(engine, "web-docs") == rows_before
    assert engine.manifests.load("web-docs")["artifacts"] == manifest_before
    event = _latest_event(engine, "web-docs")
    assert event["event_type"] == "sync.completed"
    assert event["details"]["fetched"] == 0
    assert event["details"]["skipped"] == 2


def test_web_changed_url_redownloads_only_that_url(
    tmp_path: Path,
    counting_server: Callable[..., ThreadingHTTPServer],
) -> None:
    server: Any = counting_server(
        {
            "/guide.md": b"# Guide\n\nOriginal guide body.\n",
            "/intro.md": b"# Intro\n\nOriginal intro body.\n",
        }
    )
    base = _base_url(server)
    config = _web_config(tmp_path, [f"{base}/guide.md", f"{base}/intro.md"])
    engine = SyncEngine(config)
    engine.sync_source("web-docs", "full")
    rows_before = dict((row[0], row[1]) for row in _artifact_rows(engine, "web-docs"))

    server.files["/guide.md"] = b"# Guide\n\nChanged guide body.\n"
    result = engine.sync_source("web-docs", "incremental")

    assert result.indexed_artifacts == 1
    assert result.skipped_artifacts == 1
    assert server.counters["downloads_by_path"]["/guide.md"] == 2
    assert server.counters["downloads_by_path"]["/intro.md"] == 1
    rows_after = dict((row[0], row[1]) for row in _artifact_rows(engine, "web-docs"))
    changed = [aid for aid, sha in rows_after.items() if rows_before[aid] != sha]
    assert len(changed) == 1
    assert "guide.md" in changed[0]
    event = _latest_event(engine, "web-docs")
    assert event["details"]["fetched"] == 1
    assert event["details"]["skipped"] == 1


def test_web_full_mode_bypasses_checkpoint_and_refetches(
    tmp_path: Path,
    counting_server: Callable[..., ThreadingHTTPServer],
) -> None:
    server: Any = counting_server(
        {
            "/guide.md": b"# Guide\n\nFull mode body one.\n",
            "/intro.md": b"# Intro\n\nFull mode body two.\n",
        }
    )
    base = _base_url(server)
    config = _web_config(tmp_path, [f"{base}/guide.md", f"{base}/intro.md"])
    engine = SyncEngine(config)
    engine.sync_source("web-docs", "full")
    assert server.counters["body_downloads"] == 2

    result = engine.sync_source("web-docs", "full")

    assert result.indexed_artifacts == 2
    assert server.counters["body_downloads"] == 4, "full mode must re-download every body"
    assert server.counters["not_modified"] == 0
    event = _latest_event(engine, "web-docs")
    assert event["details"]["fetched"] == 2
    assert event["details"]["skipped"] == 0


def test_web_server_without_validators_falls_back_to_hash_comparison(
    tmp_path: Path,
    counting_server: Callable[..., ThreadingHTTPServer],
) -> None:
    server: Any = counting_server(
        {"/guide.md": b"# Guide\n\nNo-validator body.\n"},
        send_validators=False,
    )
    base = _base_url(server)
    config = _web_config(tmp_path, [f"{base}/guide.md"])
    engine = SyncEngine(config)
    engine.sync_source("web-docs", "full")

    result = engine.sync_source("web-docs", "incremental")

    # No validators cached -> body is re-fetched (counts as fetched), then
    # the post-fetch hash comparison skips re-indexing.
    assert result.indexed_artifacts == 0
    assert result.skipped_artifacts == 1
    assert server.counters["body_downloads"] == 2
    event = _latest_event(engine, "web-docs")
    assert event["details"]["fetched"] == 1
    assert event["details"]["skipped"] == 0
    assert event["details"]["skipped_artifacts"] == 1


def test_api_connector_second_sync_fetches_zero_items(
    tmp_path: Path,
    counting_server: Callable[..., ThreadingHTTPServer],
) -> None:
    server: Any = counting_server(
        {
            "/doc-1.txt": b"API document one body.\n",
            "/doc-2.txt": b"API document two body.\n",
        }
    )
    base = _base_url(server)
    listing = {
        "items": [
            {
                "id": "doc-1",
                "path": "doc-1.txt",
                "url": f"{base}/doc-1.txt",
                "updated_at": "2026-06-01T00:00:00Z",
            },
            {
                "id": "doc-2",
                "path": "doc-2.txt",
                "url": f"{base}/doc-2.txt",
                "updated_at": "2026-06-02T00:00:00Z",
            },
        ]
    }
    server.files["/items.json"] = json.dumps(listing).encode("utf-8")
    config = _config(
        tmp_path,
        SourceConfig(
            name="api-docs",
            type=SourceType.api,
            path=tmp_path,
            include=["**/*.txt"],
            sync=SourceSyncSettings(on_startup=False),
            connector=SourceConnectorSettings(
                allow_experimental=True,
                api_endpoint=f"{base}/items.json",
            ),
        ),
    )
    engine = SyncEngine(config)

    first = engine.sync_source("api-docs", "full")
    assert first.indexed_artifacts == 2
    by_path = server.counters["downloads_by_path"]
    assert by_path["/doc-1.txt"] == 1
    assert by_path["/doc-2.txt"] == 1

    second = engine.sync_source("api-docs", "incremental")

    assert second.indexed_artifacts == 0
    assert second.skipped_artifacts == 2
    assert by_path["/doc-1.txt"] == 1, "unchanged API item must not be re-fetched"
    assert by_path["/doc-2.txt"] == 1, "unchanged API item must not be re-fetched"
    event = _latest_event(engine, "api-docs")
    assert event["details"]["fetched"] == 0
    assert event["details"]["skipped"] == 2

    server.files["/doc-1.txt"] = b"API document one changed body.\n"
    listing["items"][0]["updated_at"] = "2026-06-03T00:00:00Z"
    server.files["/items.json"] = json.dumps(listing).encode("utf-8")
    third = engine.sync_source("api-docs", "incremental")

    assert third.indexed_artifacts == 1
    assert by_path["/doc-1.txt"] == 2
    assert by_path["/doc-2.txt"] == 1


def test_s3_second_sync_lists_but_reads_zero_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeS3Client(
        {
            "a.md": {
                "content": b"# A\n\nS3 object one body.\n",
                "last_modified": datetime(2026, 6, 1, tzinfo=UTC),
            },
            "b.md": {
                "content": b"# B\n\nS3 object two body.\n",
                "last_modified": datetime(2026, 6, 2, tzinfo=UTC),
            },
        }
    )
    monkeypatch.setattr("pheasant.sync.connectors._boto3_client", lambda: client)
    config = _config(
        tmp_path,
        SourceConfig(
            name="s3-docs",
            type=SourceType.s3,
            path=tmp_path,
            include=["**/*.md"],
            sync=SourceSyncSettings(on_startup=False),
            connector=SourceConnectorSettings(allow_experimental=True, s3_bucket="kb-bucket"),
        ),
    )
    engine = SyncEngine(config)

    first = engine.sync_source("s3-docs", "full")
    assert first.indexed_artifacts == 2
    assert len(client.get_calls) == 2

    second = engine.sync_source("s3-docs", "incremental")

    assert second.indexed_artifacts == 0
    assert second.skipped_artifacts == 2
    assert client.list_calls == 2, "second sync must still list the bucket"
    assert len(client.get_calls) == 2, "second sync must not call get_object"
    event = _latest_event(engine, "s3-docs")
    assert event["details"]["fetched"] == 0
    assert event["details"]["skipped"] == 2

    client.objects["a.md"] = {
        "content": b"# A\n\nS3 object one changed.\n",
        "last_modified": datetime(2026, 6, 9, tzinfo=UTC),
    }
    client.objects["c.md"] = {
        "content": b"# C\n\nS3 object three is new.\n",
        "last_modified": datetime(2026, 6, 9, 1, tzinfo=UTC),
    }
    third = engine.sync_source("s3-docs", "incremental")

    assert third.indexed_artifacts == 2
    assert third.skipped_artifacts == 1
    assert set(client.get_calls[2:]) == {"a.md", "c.md"}, "only new/changed keys are read"
    assert len(client.get_calls) == 4


def test_validate_only_does_not_write_index_state(tmp_path: Path, workspace_copy: Path) -> None:
    config = _config(
        tmp_path,
        SourceConfig(
            name="sample-repo",
            type=SourceType.repository,
            path=workspace_copy / "pheasant-repo",
            sync=SourceSyncSettings(on_startup=False),
        ),
    )
    engine = SyncEngine(config)

    result = engine.sync_source("sample-repo", "validate_only")

    assert result.status == "validated"
    assert engine.stats["artifact_count"] == 0
    assert engine.stats["chunk_count"] == 0
    assert not engine.manifests.exists("sample-repo")


def test_filesystem_source_respects_max_depth(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    nested = root / "section" / "deep"
    nested.mkdir(parents=True)
    (root / "root.md").write_text("# Root\n", encoding="utf-8")
    (root / "section" / "child.md").write_text("# Child\n", encoding="utf-8")
    (nested / "deep.md").write_text("# Deep\n", encoding="utf-8")
    config = _config(
        tmp_path,
        SourceConfig(
            name="depth-limited",
            type=SourceType.document_folder,
            path=root,
            include=["**/*.md"],
            max_depth=1,
            sync=SourceSyncSettings(on_startup=False),
        ),
    )
    engine = SyncEngine(config)

    result = engine.sync_source("depth-limited", "full")

    assert result.indexed_artifacts == 2
    paths = {
        row["relative_path"]
        for row in engine.state.rows(
            "SELECT relative_path FROM artifacts WHERE source_id=?",
            ("depth-limited",),
        )
    }
    assert paths == {"root.md", "section/child.md"}


def test_filesystem_source_can_disable_chunk_splitting(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    text = "A" * 200 + "\n" + "B" * 200
    (root / "large.md").write_text(text, encoding="utf-8")
    config = _config(
        tmp_path,
        SourceConfig(
            name="unchunked",
            type=SourceType.document_folder,
            path=root,
            include=["**/*.md"],
            chunking=ChunkingSettings(enabled=False, max_chars=50, overlap_chars=0),
            sync=SourceSyncSettings(on_startup=False),
        ),
    )
    engine = SyncEngine(config)

    engine.sync_source("unchunked", "full")

    rows = engine.state.rows("SELECT text FROM chunks WHERE source_id=?", ("unchunked",))
    assert len(rows) == 1
    assert rows[0]["text"] == (root / "large.md").read_bytes().decode("utf-8")


def test_web_collection_connector_syncs_and_exposes_checkpoint(
    tmp_path: Path,
    web_server: str,
) -> None:
    config = _config(
        tmp_path,
        SourceConfig(
            name="web-docs",
            type=SourceType.web_collection,
            path=tmp_path,
            urls=[f"{web_server}/guide.md"],
            include=["**/*.md"],
            sync=SourceSyncSettings(on_startup=False),
            connector=SourceConnectorSettings(allow_experimental=True),
        ),
    )
    engine = SyncEngine(config)

    result = engine.sync_source("web-docs", "full")

    assert result.indexed_artifacts == 1
    assert result.details["connector_type"] == "web_collection"
    rows = engine.state.rows(
        "SELECT relative_path, path FROM artifacts WHERE source_id=?",
        ("web-docs",),
    )
    assert len(rows) == 1
    assert rows[0]["relative_path"].endswith("/guide.md")
    assert rows[0]["path"].startswith("http://127.0.0.1:")
    assert engine.search_context("connector", max_results=5)["results"]

    checkpoint = engine.state.get_source_checkpoint("web-docs")
    assert checkpoint is not None
    assert checkpoint["connector_type"] == "web_collection"
    assert checkpoint["high_watermark"]["item_count"] == 1

    api_status = TestClient(create_app(config)).get("/sync/status").json()
    mcp_status = PheasantTools(config).get_sync_status(config.knowledge_base_id)

    assert api_status["checkpoints"][0]["source_id"] == "web-docs"
    assert mcp_status["checkpoints"][0]["source_id"] == "web-docs"


def _config(tmp_path: Path, source: SourceConfig) -> PheasantConfig:
    return PheasantConfig(
        pheasant=PheasantSettings(
            name="connector-tests",
            state_path=tmp_path / "state",
            workspace_root=tmp_path,
            exports_path=tmp_path / "exports",
        ),
        sources=[source],
    )
