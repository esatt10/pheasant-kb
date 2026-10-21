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

import json
import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
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
from pheasant.sync.engine import SyncEngine
from pheasant.sync.web_connector import WebCollectionConnector
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
                "exports_path": str(tmp_path / "exports"),
            },
            "ingestion": {"extractor": {"html_text": True}},
            "sources": [page.to_source_dict()],
        }
    )
    result = SyncEngine(config).sync_source(page.name, "full")
    assert result.indexed_artifacts == 1


# ---------------------------------------------------------------------------
# 6. freshness: each page on its own schedule, and the beat costs nothing
#    for pages that are not due
# ---------------------------------------------------------------------------


class _Site:
    """A page whose body, headers and request count a test controls."""

    def __init__(self) -> None:
        self.body = PAGE
        self.cache_control: str | None = None
        self.hits = 0


@pytest.fixture()
def live_site() -> Iterator[tuple[str, _Site]]:
    state = _Site()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            state.hits += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            if state.cache_control:
                self.send_header("Cache-Control", state.cache_control)
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            self.wfile.write(state.body)

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


class _Clock:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1_000_000.0
        monkeypatch.setattr("pheasant.sync.web_connector._wall_clock", lambda: self.now)


def _schedule(engine: SyncEngine, url: str) -> dict:
    checkpoint = engine.state.get_source_checkpoint("web")
    return checkpoint["cursor"]["validators"][url]


def test_a_page_is_not_requested_until_it_is_due(
    tmp_path: Path, live_site: tuple[str, _Site], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, site = live_site
    clock = _Clock(monkeypatch)
    engine = SyncEngine(_config(tmp_path, _web_source([f"{base}/post"])))
    engine.sync_source("web", "incremental")
    assert site.hits == 1

    clock.now += 60 * 30  # half the default hour
    result = engine.sync_source("web", "incremental")
    assert site.hits == 1, "a page that is not due must cost no request"
    assert result.skipped_artifacts == 1
    assert engine.state.get_source_checkpoint("web")["high_watermark"]["deferred"] == 1

    clock.now += 60 * 31
    engine.sync_source("web", "incremental")
    assert site.hits == 2


def test_an_unchanged_page_backs_off_and_a_changed_one_is_watched_closely(
    tmp_path: Path, live_site: tuple[str, _Site], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, site = live_site
    clock = _Clock(monkeypatch)
    url = f"{base}/post"
    source = _web_source([url])
    source.connector.max_refresh_seconds = 4 * 3600
    engine = SyncEngine(_config(tmp_path, source))
    engine.sync_source("web", "incremental")
    assert _schedule(engine, url)["interval"] == 3600

    intervals = []
    for _ in range(4):
        clock.now += _schedule(engine, url)["interval"]
        engine.sync_source("web", "incremental")
        intervals.append(_schedule(engine, url)["interval"])
    assert intervals == [7200, 14400, 14400, 14400], "doubles while unchanged, capped"

    site.body = PAGE.replace(b"quokkaplatform", b"wallabyplatform")
    clock.now += _schedule(engine, url)["interval"]
    result = engine.sync_source("web", "incremental")
    assert result.indexed_artifacts == 1
    assert _schedule(engine, url)["interval"] == 3600, "a change resets to the minimum"
    assert engine.search_context("wallabyplatform", max_results=3)["results"]


def test_max_age_is_a_floor_and_the_source_interval_is_the_minimum(
    tmp_path: Path, live_site: tuple[str, _Site], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, site = live_site
    _Clock(monkeypatch)
    url = f"{base}/post"
    source = _web_source([url])
    source.sync.interval_seconds = 600
    site.cache_control = "public, max-age=7200"
    engine = SyncEngine(_config(tmp_path, source))
    engine.sync_source("web", "incremental")
    assert _schedule(engine, url)["interval"] == 7200

    site.cache_control = "no-cache"
    engine.sync_source("web", "full")
    assert _schedule(engine, url)["interval"] == 600


def test_full_ignores_the_schedule_and_zero_checks_every_beat(
    tmp_path: Path, live_site: tuple[str, _Site], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, site = live_site
    _Clock(monkeypatch)
    source = _web_source([f"{base}/post"])
    engine = SyncEngine(_config(tmp_path, source))
    engine.sync_source("web", "incremental")
    engine.sync_source("web", "full")
    assert site.hits == 2, "`full` is the 'check everything now' switch"

    source.sync.interval_seconds = 0
    every_beat = SyncEngine(_config(tmp_path / "zero", source))
    every_beat.sync_source("web", "incremental")
    every_beat.sync_source("web", "incremental")
    assert site.hits == 4


# ---------------------------------------------------------------------------
# 7. existing web sources re-index once after the upgrade, then never again
# ---------------------------------------------------------------------------


def test_a_web_fingerprint_carries_the_pipeline_and_html_text() -> None:
    from pheasant.sync.fingerprint import source_fingerprint

    web = _web_source(["https://example.com/a"])
    folder = SourceConfig(name="docs", type=SourceType.document_folder, path=Path("/workspace"))
    html_on, html_off = ExtractorSettings(html_text=True), ExtractorSettings(html_text=False)
    assert source_fingerprint(web, html_on) != source_fingerprint(web, html_off)
    # A folder's text does not change with html_text unless it holds HTML, and
    # re-reading every repository to find out is the re-index nobody asked for.
    assert source_fingerprint(folder, html_on) == source_fingerprint(folder)


def test_an_existing_web_source_is_reindexed_once(
    tmp_path: Path, live_site: tuple[str, _Site], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pheasant.sync.fingerprint import SOURCE_SCOPE

    base, site = live_site
    _Clock(monkeypatch)
    engine = SyncEngine(_config(tmp_path, _web_source([f"{base}/post"])))
    engine.sync_source("web", "incremental")
    # What a region indexed before this change holds: a fingerprint without
    # the web pipeline marker.
    engine.state.set_fingerprint(
        SOURCE_SCOPE.format(name="web"), "fingerprint-before-upgrade", "2026-01-01T00:00:00Z"
    )

    upgraded = engine.sync_source("web", "incremental")
    assert upgraded.indexed_artifacts == 1, "the stored text is re-derived"
    again = engine.sync_source("web", "incremental")
    assert again.indexed_artifacts == 0 and site.hits == 2, "and only once"


# ---------------------------------------------------------------------------
# 8. MCP: an agent can register web pages, but not internal ones by default
# ---------------------------------------------------------------------------


def _tools(tmp_path: Path, *, allow_private: bool = False):
    from pheasant.mcp_server.tools import PheasantTools

    config = _config(tmp_path)
    config.security.allow_agent_private_urls = allow_private
    return PheasantTools(config)


def test_an_agent_registers_web_pages_over_mcp(tmp_path: Path, site: str) -> None:
    tools = _tools(tmp_path, allow_private=True)
    response = tools.register_source(
        knowledge_base=tools.config.knowledge_base_id,
        name="agent-web",
        source_type="web_collection",
        urls=[f"{site}/blog/post"],
        sync_now=True,
        wait=True,
    )
    assert response["source"]["connector"]["allow_experimental"] is True
    assert response["sync_result"]["indexed_artifacts"] == 1
    hits = tools.search_context(tools.config.knowledge_base_id, "quokkaplatform")["results"]
    assert hits


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.1.2.3/wiki",
        "http://[::ffff:127.0.0.1]/",
    ],
)
def test_an_agent_cannot_point_the_region_at_an_internal_address(tmp_path: Path, url: str) -> None:
    tools = _tools(tmp_path)
    with pytest.raises(ValueError, match="non-public address"):
        tools.register_source(
            knowledge_base=tools.config.knowledge_base_id,
            name="internal",
            source_type="web_collection",
            urls=[url],
        )
    assert "internal" not in {s.name for s in tools.config.sources}


def test_a_hostname_that_resolves_privately_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, *a, **k: [(None, None, None, "", ("10.0.0.7", 0))]
    )
    from pheasant.security.url_policy import UrlPolicyError, require_public_urls

    with pytest.raises(UrlPolicyError, match="10.0.0.7"):
        require_public_urls(["https://wiki.corp.example/page"])
    # A literal public address needs no lookup at all.
    assert require_public_urls(["https://93.184.216.34/"]) == ["https://93.184.216.34/"]


def test_mcp_registration_refuses_what_it_cannot_honour(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    kb = tools.config.knowledge_base_id
    with pytest.raises(ValueError, match="at least one URL"):
        tools.register_source(knowledge_base=kb, name="w", source_type="web_collection")
    with pytest.raises(ValueError, match="only http"):
        tools.register_source(
            knowledge_base=kb, name="w", source_type="web_collection", urls=["file:///etc/passwd"]
        )
    with pytest.raises(ValueError, match="urls apply to web collections"):
        tools.register_source(
            knowledge_base=kb,
            name="d",
            source_type="document_folder",
            path=str(tmp_path),
            urls=["https://example.com"],
        )


def test_the_api_opts_a_web_registration_in(tmp_path: Path, site: str) -> None:
    client = TestClient(create_app(config=_config(tmp_path)))
    response = client.post(
        "/sources",
        json={
            "name": "web",
            "type": "web_collection",
            "path": PLACEHOLDER_SOURCE_PATH,
            "urls": [f"{site}/blog/post"],
            "sync_now": True,
            "wait": True,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["source"]["connector"]["allow_experimental"] is True
    assert response.json()["sync_result"]["indexed_artifacts"] == 1


# ---------------------------------------------------------------------------
# 7. one site, many pages: each URL added builds the collection up
# ---------------------------------------------------------------------------


class _PathPageHandler(BaseHTTPRequestHandler):
    """A page per path, naming its own path so each one is findable."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        marker = self.path.strip("/").replace("/", "zz").replace("-", "") or "rootpage"
        body = (
            f"<!doctype html><html><head><title>{self.path}</title></head>"
            f"<body><h1>{self.path}</h1><p>marker{marker} lives here.</p></body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def docs_site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PathPageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


DOCS_PAGES = ("/api/docs/model", "/chatgpt", "/api/docs/guides/realtime-transcription")


def test_quick_adding_pages_of_one_site_builds_one_collection(
    tmp_path: Path, docs_site: str
) -> None:
    """Regression: each URL of one host replaced the last one.

    Quick add names a web collection after its host, and registering under a
    name that existed replaced that source, so adding
    developers.openai.com/chatgpt after /api/docs/model left only /chatgpt
    and deleted the first page's index. Each URL now joins the collection,
    and the pages stay in folders that follow their URL paths.
    """

    client = TestClient(create_app(config=_config(tmp_path)))
    names = set()
    for page in DOCS_PAGES:
        response = client.post(
            "/sources/quick-add", json={"target": f"{docs_site}{page}", "wait": True}
        )
        assert response.status_code == 200, response.text
        names.update(source["name"] for source in response.json()["sources"])
    assert len(names) == 1, "one site is one collection"
    (name,) = names

    sources = {s["name"]: s for s in client.get("/sources").json()}
    assert set(sources) == {name}
    urls = json.loads(sources[name]["config_json"])["urls"]
    assert urls == [f"{docs_site}{page}" for page in DOCS_PAGES], "added in the order given"

    markers = (
        "markerapizzdocszzmodel",
        "markerchatgpt",
        "markerapizzdocszzguideszzrealtimetranscription",
    )
    for marker in markers:
        hits = client.post("/search", json={"query": marker, "max_results": 3}).json()
        assert hits["results"], f"{marker} was dropped from the collection"

    host = docs_site.removeprefix("http://").replace(":", "_")
    tree = client.get("/documents/tree", params={"source_name": name}).json()
    assert [folder["name"] for folder in tree["directories"]] == [host]
    docs = client.get(
        "/documents/tree", params={"source_name": name, "path": f"{host}/api/docs"}
    ).json()
    assert [folder["name"] for folder in docs["directories"]] == ["guides"]
    assert [file["name"] for file in docs["files"]] == ["model.txt"]
    content = client.get(
        "/documents/content",
        params={"path": f"{name}/{host}/api/docs/guides/realtime-transcription.txt"},
    ).json()
    assert "markerapizzdocszzguideszzrealtimetranscription" in content["content"]


def test_adding_a_page_twice_keeps_one_entry(tmp_path: Path, docs_site: str) -> None:
    client = TestClient(create_app(config=_config(tmp_path)))
    for _ in range(2):
        client.post("/sources/quick-add", json={"target": f"{docs_site}/chatgpt", "wait": True})
    (source,) = client.get("/sources").json()
    assert json.loads(source["config_json"])["urls"] == [f"{docs_site}/chatgpt"]


def test_pages_of_one_site_in_one_call_are_one_target(tmp_path: Path) -> None:
    """`pheasant up a b c` suffixed the second page's source as `<host>-2`."""

    from pheasant.targets import resolve_targets

    urls = [f"https://developers.example.com{page}" for page in DOCS_PAGES]
    targets = resolve_targets(
        [*urls, urls[0], "https://other.example.com/x"],
        clone_root=tmp_path / "clones",
        workspace=tmp_path,
    )
    assert [t.name for t in targets] == ["developers-example-com", "other-example-com"]
    assert targets[0].urls == urls


def test_pheasant_up_adds_a_page_to_the_sites_collection(tmp_path: Path) -> None:
    """A second `pheasant up <url>` of one site said "already configured" and
    dropped the page: matching by path found the first page's collection."""

    from pheasant.quickstart import added_sources, ensure_up_config
    from pheasant.targets import resolve_targets

    config_path = tmp_path / "pheasant.yaml"
    urls = [f"https://developers.example.com{page}" for page in DOCS_PAGES]
    for url in urls:
        targets = resolve_targets([url], clone_root=tmp_path / "clones", workspace=tmp_path)
        ensure_up_config(targets, config_path)
    assert added_sources() == ["developers-example-com"]
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    (source,) = [s for s in written["sources"] if s["type"] == "web_collection"]
    assert source["urls"] == urls

    unchanged = config_path.read_text(encoding="utf-8")
    ensure_up_config(
        resolve_targets([urls[1]], clone_root=tmp_path / "clones", workspace=tmp_path),
        config_path,
    )
    assert added_sources() == []
    assert config_path.read_text(encoding="utf-8") == unchanged, "a known page rewrites nothing"


def test_an_indexer_adopts_a_page_added_on_another_replica(tmp_path: Path, docs_site: str) -> None:
    """On a fleet an api replica registers the change and publishes the sync.
    An indexer that already read the collection held its old URL list in
    memory and synced that; it adopts the registered source per task now."""

    from pheasant.registry.source_registry import SourceRegistry
    from pheasant.sync.queue import IndexTask

    first = _web_source([f"{docs_site}{DOCS_PAGES[0]}"])
    engine = SyncEngine(_config(tmp_path, first))
    assert engine.sync_source("web", "incremental").indexed_artifacts == 1

    grown = first.model_dump(mode="json")
    grown["urls"] = [f"{docs_site}{page}" for page in DOCS_PAGES]
    replica = SourceRegistry(_config(tmp_path), engine.state)  # its own config object
    before = replica.config_digest("web")
    replica.install(grown)
    assert replica.config_digest("web") != before, "a changed source is a new task id"

    result = engine.apply_index_task(IndexTask(id="idx-grown", source_id="web"))
    assert result.indexed_artifacts == 2, "the two new pages, and not the one already held"
    held = {r["relative_path"] for r in engine.state.rows("SELECT relative_path FROM artifacts")}
    assert len(held) == 3

    engine.state.set_source_enabled("web", False, "disabled")
    SourceRegistry(engine.config, engine.state).adopt("web")
    assert next(s for s in engine.config.sources if s.name == "web").enabled is False
