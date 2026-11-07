"""Each file's health and data quality, with a reason, over both surfaces.

A source's score says how much of it reads; a cleanup needs to know which
files, and why. Every file is ``ok``, ``warning`` or ``error``, and each flag
carries a code and a reason: a read that failed (``unreadable`` / ``stale``),
a JavaScript shell, no text or almost none. ``describe_document`` carries one
file's answer and ``list_quality_issues`` / ``GET /quality`` lists the flagged
ones; dropping an item takes it off the list before any sync.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.services import ServiceContext
from pheasant.services.archive import ArchiveRequest, archive_documents
from pheasant.services.inventory_detail import document as describe_document
from pheasant.services.inventory_detail import source as describe_source
from pheasant.services.quality import QualityRequest, quality_issues
from pheasant.services.source_urls import UrlEditRequest, edit_urls
from pheasant.sync.engine import SyncEngine
from pheasant.sync.health import LOW_TEXT_TOKENS, file_quality
from tests.test_source_health import backend  # noqa: F401 - the backend fixture

LONG = " ".join(
    f"The rotation runbook step {n} names the vault key and its owner." for n in range(40)
)
SHELL = (
    "<html><head><title>App</title><script src='/app.js'></script></head>"
    "<body><div id='root'></div><noscript>Please enable JavaScript</noscript></body></html>"
)


class _Site:
    """A local site: each page's body and type, and which pages answer 500."""

    def __init__(self) -> None:
        site = self
        self.pages: dict[str, tuple[str, str]] = {}
        self.broken: set[str] = set()

        class Pages(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
                name = self.path.strip("/")
                if name in site.broken or name not in site.pages:
                    self.send_response(500)
                    self.end_headers()
                    return
                body, kind = site.pages[name]
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", f"{kind}; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Pages)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def url(self, name: str) -> str:
        return f"{self.base}/{name}"


@pytest.fixture
def site():
    served = _Site()
    try:
        yield served
    finally:
        served.server.shutdown()
        served.server.server_close()


def _engine(
    root: Path, site: _Site, names: list[str], store: str = "sqlite", **extra: Any
) -> SyncEngine:
    storage = (
        {"storage": {"backend": "postgres", "dsn_env": "PHEASANT_TEST_POSTGRES_DSN"}}
        if store == "postgres"
        else {}
    )
    storage.update(extra)
    return SyncEngine(
        PheasantConfig.model_validate(
            {
                **storage,
                "pheasant": {
                    "name": "quality",
                    "defaults": "standalone",
                    "state_path": str(root / "state"),
                    "workspace_root": str(root),
                    "exports_path": str(root / "exports"),
                },
                "sources": [
                    {
                        "name": "site",
                        "type": "web_collection",
                        "path": "/unused",
                        "urls": [site.url(name) for name in names],
                        "connector": {"allow_experimental": True},
                        "sync": {"interval_seconds": 0},
                    }
                ],
            }
        )
    )


def _context(engine: SyncEngine) -> ServiceContext:
    return ServiceContext(config=engine.config, state=engine.state, searcher=None, engine=engine)


def _by_path(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {issue["path"].rsplit("/", 1)[-1].split(".")[0]: issue for issue in report["issues"]}


def _codes(issue: dict[str, Any]) -> list[str]:
    return [flag["code"] for flag in issue["flags"]]


def test_each_rule_says_what_it_found_and_why() -> None:
    assert file_quality(held=True, chunks=3, tokens=400) == {"status": "ok", "flags": []}
    gone = file_quality(held=False, read_error="HTTPError: HTTP Error 404: Not Found")
    assert gone["status"] == "error"
    assert gone["flags"][0]["code"] == "unreadable"
    assert "404" in gone["flags"][0]["reason"]
    stale = file_quality(held=True, chunks=3, tokens=400, read_error="timed out")
    assert (stale["status"], _codes(stale)) == ("warning", ["stale"])
    assert "earlier read" in stale["flags"][0]["reason"]
    assert _codes(file_quality(held=True, chunks=0)) == ["no_text"]
    thin = file_quality(held=True, chunks=1, tokens=LOW_TEXT_TOKENS - 1)
    assert _codes(thin) == ["little_text"]
    assert str(LOW_TEXT_TOKENS - 1) in thin["flags"][0]["reason"]
    shell = file_quality(held=True, chunks=1, tokens=400, script_shell=True)
    assert _codes(shell) == ["script_shell"]
    assert all(flag["reason"] for flag in gone["flags"] + stale["flags"] + shell["flags"])


def test_every_flagged_file_is_listed_with_its_reason(
    tmp_path: Path,
    site: _Site,
    backend: str,  # noqa: F811 - the imported fixture
) -> None:
    site.pages = {
        "good": (LONG, "text/plain"),
        "thin": ("Contact us.", "text/plain"),
        "shell": (SHELL, "text/html"),
        "once": (LONG, "text/plain"),
    }
    engine = _engine(tmp_path, site, ["good", "thin", "shell", "once", "gone"], backend)
    context = _context(engine)
    try:
        engine.sync_source("site", "incremental")  # "gone" never answers
        site.broken = {"once"}  # read before, fails now
        engine.sync_source("site", "incremental")
        report = quality_issues(context, QualityRequest(source_name="site"))
        errors_only = quality_issues(context, QualityRequest(severity="error"))
        described = describe_document(context, "good", source_name="site")
        flagged = describe_document(context, "once", source_name="site")
        overview = describe_source(context, "site")["source"]["quality"]
    finally:
        engine.close()

    issues = _by_path(report)
    assert set(issues) == {"thin", "shell", "once", "gone"}, "a good file was listed"
    assert report["issues"][0]["status"] == "error", "errors come first"
    assert (issues["gone"]["status"], _codes(issues["gone"])) == ("error", ["unreadable"])
    assert issues["gone"]["id"] is None and issues["gone"]["url"] == site.url("gone")
    assert "HTTP Error 500" in issues["gone"]["flags"][0]["reason"]
    assert _codes(issues["once"]) == ["stale"]
    assert _codes(issues["thin"]) == ["little_text"]
    assert "script_shell" in _codes(issues["shell"])
    assert report["totals"] | {"by_code": None} == {
        "files": 5,
        "ok": 1,
        "warning": 3,
        "error": 1,
        "by_code": None,
        "matching": 4,
    }
    assert [i["path"] for i in errors_only["issues"]] == [issues["gone"]["path"]]
    # The file's own metadata says the same, reason included.
    assert described["document"]["quality"] == {"status": "ok", "flags": []}
    assert flagged["document"]["quality"]["flags"][0]["code"] == "stale"
    assert "HTTP Error 500" in flagged["document"]["quality"]["flags"][0]["reason"]
    assert overview == {"ok": 1, "warning": 3, "error": 1, "by_code": report["totals"]["by_code"]}


def test_dropping_a_flagged_file_takes_it_off_the_list(
    tmp_path: Path,
    site: _Site,
    backend: str,  # noqa: F811 - the imported fixture
) -> None:
    site.pages = {"good": (LONG, "text/plain"), "once": (LONG, "text/plain")}
    engine = _engine(tmp_path, site, ["good", "once", "gone"], backend)
    context = _context(engine)
    try:
        engine.sync_source("site", "incremental")
        site.broken = {"once"}
        engine.sync_source("site", "incremental")
        assert set(_by_path(quality_issues(context, QualityRequest()))) == {"once", "gone"}

        edit_urls(
            context, UrlEditRequest(source_name="site", remove=[site.url("gone")], sync=False)
        )
        archive_documents(context, ArchiveRequest(documents=["once.txt"], source_name="site"))
        report = quality_issues(context, QualityRequest())
    finally:
        engine.close()
    assert report["issues"] == []
    assert report["totals"]["ok"] == 1


def test_both_transports_list_the_same_issues(tmp_path: Path, site: _Site) -> None:
    pytest.importorskip("mcp")
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app
    from pheasant.mcp_server.tools import PheasantTools

    site.pages = {"good": (LONG, "text/plain"), "thin": ("Contact us.", "text/plain")}
    config = _engine(tmp_path, site, ["good", "thin", "gone"]).config
    tools = PheasantTools(config)
    tools.engine.sync_source("site", "incremental")
    client = TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))
    over_mcp = tools.list_quality_issues(config.knowledge_base_id, "site")
    over_http = client.get("/quality", params={"source_name": "site"}).json()
    assert over_mcp["issues"] == over_http["issues"]
    assert over_mcp["totals"] == over_http["totals"]
    assert {i["status"] for i in over_mcp["issues"]} == {"warning", "error"}
    refused = client.get("/quality", params={"severity": "bad"})
    assert refused.status_code == 422 and "severity" in refused.json()["detail"]


def test_access_control_hides_held_files_and_lists_never_read_ones(
    tmp_path: Path, site: _Site
) -> None:
    """A failing file the index holds is judged by its own ACL; one it holds
    nothing for is the source's own URL and error, listed for any caller."""

    site.pages = {"good": (LONG, "text/plain"), "once": (LONG, "text/plain")}
    private = {"acl_enforced": True, "default_visibility": "private"}
    engine = _engine(tmp_path, site, ["good", "once", "gone"], security=private)
    context = _context(engine)
    try:
        engine.sync_source("site", "incremental")
        site.broken = {"once"}
        engine.sync_source("site", "incremental")
        report = quality_issues(context, QualityRequest(source_name="site"))
    finally:
        engine.close()
    assert set(_by_path(report)) == {"gone"}, "a file the caller may not read was listed"
    assert report["totals"]["files"] == 1


def test_the_taxonomy_shows_each_folder_and_files_health(tmp_path: Path, site: _Site) -> None:
    """The tree carries what the cleanup list does: each file's state and
    flags, each folder's health counted over everything beneath it, and a
    file never read in the folder its path names."""

    from pheasant.services.source_browser import BrowseRequest, browse

    site.pages = {"good": (LONG, "text/plain"), "thin": ("Contact us.", "text/plain")}
    engine = _engine(tmp_path, site, ["good", "thin", "gone", "dead"])
    context = _context(engine)
    try:
        engine.sync_source("site", "incremental")
        top = browse(context, BrowseRequest())
        root = browse(context, BrowseRequest(source_name="site"))
        host = root["directories"][0]
        inside = browse(context, BrowseRequest(source_name="site", path=host["path"]))
    finally:
        engine.close()

    listed = {d["name"]: d for d in top["directories"]}
    assert listed["site"]["health"] == {"percent": 50.0, "status": "degraded"}
    assert host["health"] == {
        "percent": 50.0,
        "status": "degraded",
        "ok": 1,
        "warning": 1,
        "error": 2,
    }
    assert host["documents"] == 2, "documents still counts what the index holds"
    assert root["health"] == host["health"]
    files = {f["name"].split(".")[0]: f for f in inside["files"]}
    assert set(files) == {"good", "thin", "gone", "dead"}
    assert files["good"]["quality"] == {"status": "ok", "flags": []}
    assert [f["code"] for f in files["thin"]["quality"]["flags"]] == ["little_text"]
    assert files["gone"]["id"] is None and files["gone"]["viewer"] is None
    assert files["gone"]["url"] == site.url("gone")
    assert "HTTP Error 500" in files["gone"]["quality"]["flags"][0]["reason"]
    assert inside["totals"]["files"] == 4
