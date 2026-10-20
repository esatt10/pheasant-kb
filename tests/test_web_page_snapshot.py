"""A fetched web page can be shown as the parser read it, beside its text.

A web collection's artifact path is a URL, so once a page's text was
extracted nothing held what the extractor had read, and the taxonomy view
could show only the text — a reader checking an extraction had nothing to
check it against. The indexer now keeps the page's HTML in the media store
under the artifact's own sha256, and ``/documents/page`` serves it.

The properties held here: the snapshot is the *indexed* page, not the live
one; it is served so that it can never run on pheasant's origin; and nothing
else changes — a local ``.html`` file has no snapshot, and the page is never
served as an image.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import (
    PLACEHOLDER_SOURCE_PATH,
    ExtractorSettings,
    IngestionSettings,
    PheasantConfig,
    PheasantSettings,
    SourceConfig,
    SourceConnectorSettings,
    SourceSyncSettings,
    SourceType,
)
from pheasant.ingestion.media import PAGE_SUFFIX, media_store_for_config
from pheasant.sync.engine import SyncEngine

FIRST = (
    b"<!doctype html><html><head><title>Sovereignty</title>"
    b'<link rel="stylesheet" href="/site.css"><script>var scriptmarker = 1;</script></head>'
    b"<body><h1>Greater control and choice</h1><p>The quokkaplatform first edition.</p>"
    b"</body></html>"
)
SECOND = FIRST.replace(b"first edition", b"second edition")


class _Site:
    def __init__(self) -> None:
        self.page = FIRST


@pytest.fixture()
def site() -> Iterator[tuple[str, _Site]]:
    state = _Site()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
            body = state.page
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _config(tmp_path: Path, *sources: SourceConfig) -> PheasantConfig:
    return PheasantConfig(
        pheasant=PheasantSettings(
            name="web-snapshot",
            state_path=tmp_path / "state",
            workspace_root=tmp_path,
            exports_path=tmp_path / "exports",
        ),
        ingestion=IngestionSettings(extractor=ExtractorSettings(html_text=True)),
        sources=list(sources),
    )


def _web(urls: list[str]) -> SourceConfig:
    return SourceConfig(
        name="web",
        type=SourceType.web_collection,
        path=Path(PLACEHOLDER_SOURCE_PATH),
        urls=urls,
        sync=SourceSyncSettings(on_startup=False),
        connector=SourceConnectorSettings(allow_experimental=True),
    )


def _relative(engine: SyncEngine, suffix: str) -> str:
    rows = engine.state.rows("SELECT relative_path FROM artifacts WHERE source_id='web'")
    return next(str(r["relative_path"]) for r in rows if str(r["relative_path"]).endswith(suffix))


def test_a_fetched_page_is_held_and_served_beside_its_text(
    tmp_path: Path, site: tuple[str, _Site]
) -> None:
    base, _ = site
    url = f"{base}/blog/2026/10/20/advancing-digital-sovereignty"
    config = _config(tmp_path, _web([url, f"{base}/about.html"]))
    engine = SyncEngine(config)
    engine.sync_source("web", "full")
    client = TestClient(create_app(config=config))

    for suffix in ("advancing-digital-sovereignty.txt", "about.html"):
        relative = _relative(engine, suffix)
        read = client.get("/documents/content", params={"path": relative, "source_name": "web"})
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["page"]["available"] is True
        assert body["page"]["url"].startswith(base)
        # The text is what was extracted, said as such, and never drawn as
        # markup: an `.html` URL's extracted text is not HTML.
        assert body["content_origin"] == "extracted"
        assert body["viewer"] == "text"
        assert "quokkaplatform" in body["content"] and "scriptmarker" not in body["content"]

        page = client.get("/documents/page", params={"path": relative, "source_name": "web"})
        assert page.status_code == 200, page.text
        assert page.content == FIRST
        assert page.headers["content-security-policy"].startswith("sandbox")
        assert page.headers["content-disposition"] == "attachment"
        assert page.headers["x-content-type-options"] == "nosniff"


def test_the_snapshot_is_the_page_that_was_indexed(tmp_path: Path, site: tuple[str, _Site]) -> None:
    base, state = site
    config = _config(tmp_path, _web([f"{base}/blog/post"]))
    engine = SyncEngine(config)
    engine.sync_source("web", "full")
    relative = _relative(engine, "post.txt")
    client = TestClient(create_app(config=config))

    # The site moves on; until the region re-reads it, the view shows what
    # the chunks were extracted from.
    state.page = SECOND
    params = {"path": relative, "source_name": "web"}
    assert client.get("/documents/page", params=params).content == FIRST

    engine.sync_source("web", "full")
    assert client.get("/documents/page", params=params).content == SECOND
    assert "second edition" in client.get("/documents/content", params=params).json()["content"]
    store = media_store_for_config(config)
    # Content-addressed, and user data: the earlier snapshot is not deleted.
    for body in (FIRST, SECOND):
        assert store.get(hashlib.sha256(body).hexdigest(), PAGE_SUFFIX) == body


def test_a_page_indexed_without_a_snapshot_says_so(tmp_path: Path, site: tuple[str, _Site]) -> None:
    base, _ = site
    config = _config(tmp_path, _web([f"{base}/blog/post"]))
    engine = SyncEngine(config)
    engine.sync_source("web", "full")
    relative = _relative(engine, "post.txt")
    # As a region upgraded from before snapshots were kept would hold it.
    for stored in (Path(config.pheasant.state_path) / "media").rglob(f"*{PAGE_SUFFIX}"):
        stored.unlink()
    client = TestClient(create_app(config=config))
    params = {"path": relative, "source_name": "web"}

    assert client.get("/documents/content", params=params).json()["page"]["available"] is False
    assert client.get("/documents/page", params=params).status_code == 404


def test_a_local_html_file_has_no_snapshot_and_is_previewed_as_before(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.html").write_bytes(FIRST)
    folder = SourceConfig(
        name="docs",
        type=SourceType.document_folder,
        path=docs,
        include=["**/*.html"],
        sync=SourceSyncSettings(on_startup=False),
    )
    config = _config(tmp_path, folder)
    SyncEngine(config).sync_source("docs", "full")
    client = TestClient(create_app(config=config))
    params = {"path": "guide.html", "source_name": "docs"}

    body = client.get("/documents/content", params=params).json()
    assert body["page"] is None
    assert body["viewer"] == "html" and body["content_origin"] == "file"
    assert client.get("/documents/page", params=params).status_code == 422
    assert not list((Path(config.pheasant.state_path) / "media").rglob(f"*{PAGE_SUFFIX}"))


def test_a_page_is_never_served_as_an_image(tmp_path: Path, site: tuple[str, _Site]) -> None:
    base, _ = site
    config = _config(tmp_path, _web([f"{base}/blog/post"]))
    engine = SyncEngine(config)
    engine.sync_source("web", "full")
    artifact_id = engine.state.rows("SELECT id FROM artifacts WHERE source_id='web'")[0]["id"]
    client = TestClient(create_app(config=config))

    assert client.get("/media", params={"node_id": artifact_id}).status_code == 404
