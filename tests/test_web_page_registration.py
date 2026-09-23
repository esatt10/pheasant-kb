"""A web page can be registered, fetched, cleaned and searched — every way in.

Each case here was a live failure found by registering a real page:

1. a ``web_collection`` in YAML with no ``path`` crashed the loader with a
   bare ``KeyError`` — ``path`` is ceremony for a source that fetches URLs;
2. the stock ``include`` globs (code/Markdown/config) silently dropped every
   ``.html`` URL, while an extensionless one slipped through as ``.txt``;
3. that ``.txt`` page was then indexed as raw markup, ``<script>`` and
   ``<style>`` bodies included, even with ``html_text: true`` and a server
   saying ``text/html``;
4. the UI form got a 400 — it sends the ``/unused`` placeholder the catalog
   tells it to, and ``POST /sources`` only honoured that for plugin types;
5. ``pheasant up <url>`` wrote a source without the experimental opt-in, so
   its own first sync raised.
"""

from __future__ import annotations

import logging
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
from pheasant.ingestion.extractor import source_includes_documents
from pheasant.sync.connectors import WebCollectionConnector
from pheasant.sync.engine import SyncEngine
from pheasant.targets import resolve_target

PAGE = (
    b"<!doctype html><html><head><title>Forward deployed engineering</title>"
    b"<style>.cssmarker{color:red}</style><script>var scriptmarker = 1;</script></head>"
    b"<body><h1>How our field engineers work</h1>"
    b"<p>The team embeds with customers and ships the quokkaplatform integration.</p>"
    b"</body></html>"
)


class _PageHandler(BaseHTTPRequestHandler):
    """Serves PAGE as text/html at any path, extension or not."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _config(tmp_path: Path, *sources: SourceConfig, html_text: bool = True) -> PheasantConfig:
    return PheasantConfig(
        pheasant=PheasantSettings(
            name="web-registration",
            state_path=tmp_path / "state",
            workspace_root=tmp_path,
            exports_path=tmp_path / "exports",
        ),
        ingestion=IngestionSettings(extractor=ExtractorSettings(html_text=html_text)),
        sources=list(sources),
    )


def _web_source(urls: list[str], **overrides: object) -> SourceConfig:
    return SourceConfig(
        name="web",
        type=SourceType.web_collection,
        path=Path(PLACEHOLDER_SOURCE_PATH),
        urls=urls,
        sync=SourceSyncSettings(on_startup=False),
        connector=SourceConnectorSettings(allow_experimental=True),
        **overrides,
    )


# ---------------------------------------------------------------------------
# 1. the config file
# ---------------------------------------------------------------------------


def test_a_web_source_needs_no_path_in_yaml() -> None:
    config = PheasantConfig.model_validate(
        {
            "sources": [
                {
                    "name": "web",
                    "type": "web_collection",
                    "urls": ["https://example.com/a"],
                    "connector": {"allow_experimental": True},
                }
            ]
        }
    )
    assert config.sources[0].path == Path(PLACEHOLDER_SOURCE_PATH)


def test_a_filesystem_source_without_a_path_says_so() -> None:
    with pytest.raises(ValueError, match="'docs'.*needs a path"):
        PheasantConfig.model_validate({"sources": [{"name": "docs", "type": "document_folder"}]})


# ---------------------------------------------------------------------------
# 2 + 3. fetch every listed URL, and index text rather than markup
# ---------------------------------------------------------------------------


def test_every_listed_url_is_indexed_as_text(tmp_path: Path, site: str) -> None:
    config = _config(tmp_path, _web_source([f"{site}/blog/post", f"{site}/about.html"]))
    engine = SyncEngine(config)

    result = engine.sync_source("web", "full")

    assert result.indexed_artifacts == 2
    rows = engine.state.rows(
        "SELECT a.relative_path, c.text FROM chunks c JOIN artifacts a ON a.id = c.artifact_id "
        "WHERE a.source_id = ?",
        ("web",),
    )
    paths = sorted(row["relative_path"] for row in rows)
    # The extensionless page keeps its `.txt` relative path: the artifact id is
    # built from it, and changing it would orphan every existing web artifact.
    assert paths[0].endswith("/about.html") and paths[1].endswith("/blog/post.txt")
    for row in rows:
        assert "quokkaplatform" in row["text"]
        assert "scriptmarker" not in row["text"]
        assert "cssmarker" not in row["text"]
        assert "<p>" not in row["text"]
    hits = engine.search_context("quokkaplatform integration", max_results=5)["results"]
    assert len(hits) == 2


def test_an_unchanged_page_is_not_reindexed(tmp_path: Path, site: str) -> None:
    config = _config(tmp_path, _web_source([f"{site}/blog/post", f"{site}/about.html"]))
    engine = SyncEngine(config)
    engine.sync_source("web", "full")

    again = engine.sync_source("web", "incremental")

    assert again.indexed_artifacts == 0
    assert again.skipped_artifacts == 2


def test_without_html_text_an_extensionless_page_keeps_its_old_behaviour(
    tmp_path: Path, site: str
) -> None:
    # html_text is an opt-in that changes indexed text; with it off (and no
    # document source to build an extractor) nothing about the default moves.
    config = _config(tmp_path, _web_source([f"{site}/blog/post"]), html_text=False)
    engine = SyncEngine(config)
    engine.sync_source("web", "full")
    text = engine.state.rows("SELECT text FROM chunks WHERE source_id = ?", ("web",))[0]["text"]
    assert "<p>" in text


def test_an_include_the_operator_chose_still_filters_and_says_so(
    tmp_path: Path, site: str, caplog: pytest.LogCaptureFixture
) -> None:
    source = _web_source([f"{site}/guide.md", f"{site}/about.html"], include=["**/*.md"])
    connector = WebCollectionConnector(source, SyncEngine(_config(tmp_path, source)).state)

    with caplog.at_level(logging.WARNING, logger="pheasant.sync.connectors"):
        items = connector.list_items()

    assert [item.uri for item in items] == [f"{site}/guide.md"]
    assert "about.html" in caplog.text


def test_excludes_still_apply_to_listed_urls(tmp_path: Path, site: str) -> None:
    # The secret-file excludes are unioned in by effective_source and apply
    # to listed URLs whatever `include` says.
    source = _web_source([f"{site}/about.html", f"{site}/keys/server.pem"])
    engine = SyncEngine(_config(tmp_path, source))
    connector = WebCollectionConnector(engine.config.effective_source(source), engine.state)
    assert [item.uri for item in connector.list_items()] == [f"{site}/about.html"]


def test_a_listed_pdf_makes_the_source_a_document_source() -> None:
    assert source_includes_documents(_web_source(["https://example.com/reports/2025.pdf"]))
    assert not source_includes_documents(_web_source(["https://example.com/blog/post"]))


# ---------------------------------------------------------------------------
# 4. the HTTP API, exactly as the UI form calls it
# ---------------------------------------------------------------------------


def test_the_ui_form_registers_a_web_page(tmp_path: Path, site: str) -> None:
    client = TestClient(create_app(config=_config(tmp_path)))
    catalog = client.get("/sources/types").json()
    web_type = next(t for t in catalog["types"] if t["id"] == "web_collection")
    assert web_type["path_role"] == "unused"

    response = client.post(
        "/sources",
        json={
            "name": "web",
            "type": "web_collection",
            "path": catalog["placeholder_path"],
            "urls": [f"{site}/blog/post"],
            "connector": {"allow_experimental": True},
            "sync_now": True,
            "wait": True,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["sync_result"]["indexed_artifacts"] == 1
    hits = client.post("/search", json={"query": "quokkaplatform", "max_results": 3}).json()
    assert hits["results"]


def test_the_placeholder_is_still_refused_for_a_filesystem_type(tmp_path: Path) -> None:
    client = TestClient(create_app(config=_config(tmp_path)))
    response = client.post(
        "/sources",
        json={"name": "docs", "type": "document_folder", "path": PLACEHOLDER_SOURCE_PATH},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 5. `pheasant up <url>`
# ---------------------------------------------------------------------------


def test_pheasant_up_writes_a_web_source_that_syncs(tmp_path: Path, site: str) -> None:
    target = resolve_target(
        f"{site}/reports/2025.pdf", clone_root=tmp_path / "clones", workspace=tmp_path
    )
    payload = target.to_source_dict()
    assert payload["connector"] == {"allow_experimental": True}
    # No include: a glob list here dropped the very URL being added.
    assert "include" not in payload

    page = resolve_target(f"{site}/blog/post", clone_root=tmp_path / "clones", workspace=tmp_path)
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "up",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
            },
            "ingestion": {"extractor": {"html_text": True}},
            "sources": [page.to_source_dict()],
        }
    )
    result = SyncEngine(config).sync_source(page.name, "full")
    assert result.indexed_artifacts == 1
