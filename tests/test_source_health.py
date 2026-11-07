"""A source's health is the share of it the index can read, and dropping an item moves it.

``degraded`` used to mean "any item could not be read": 4 dead links among
17,059 pages read exactly like 77 refusals among 80. The score is a
percentage, the status follows the majority, and a removed URL or an archived
document stops counting against the source at once, before the sync that
removes it has run.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.registry.source_registry import SourceRegistry
from pheasant.services import ServiceContext
from pheasant.services.archive import ArchiveRequest, archive_documents
from pheasant.services.inventory import overview
from pheasant.services.inventory_detail import source as describe_source
from pheasant.services.source_urls import UrlEditRequest, edit_urls
from pheasant.services.sync_wait import WaitRequest, wait_for_sync
from pheasant.sync.engine import SyncEngine
from pheasant.sync.health import percent, status_for
from tests.test_sync_idempotency import _FlakySite

DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres",
            marks=pytest.mark.skipif(not DSN, reason="set PHEASANT_TEST_POSTGRES_DSN to run"),
        ),
    ]
)
def backend(request) -> str:
    """The score's queries on both backends: SQLite here, Postgres on the fleet."""

    if request.param == "postgres":
        import psycopg

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
    return request.param


def _web_engine(root: Path, base: str, pages: list[str], backend: str = "sqlite") -> SyncEngine:
    raw = {
        "pheasant": {
            "name": "health",
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
                "urls": [f"{base}/{page}" for page in pages],
                "connector": {"allow_experimental": True},
                "sync": {"interval_seconds": 0},
            }
        ],
    }
    if backend == "postgres":
        raw["storage"] = {"backend": "postgres", "dsn_env": "PHEASANT_TEST_POSTGRES_DSN"}
    return SyncEngine(PheasantConfig.model_validate(raw))


@pytest.fixture
def site():
    served = _FlakySite()
    try:
        yield served
    finally:
        served.close()


def _context(engine: SyncEngine) -> ServiceContext:
    return ServiceContext(config=engine.config, state=engine.state, searcher=None, engine=engine)


def _listed(engine: SyncEngine) -> dict:
    return {s["name"]: s for s in SourceRegistry(engine.config, engine.state).list_sources()}


def test_the_score_is_the_readable_share_rounded_down() -> None:
    assert percent(17_055, 4) == 99.9, "never rounded up to look whole"
    assert percent(3, 77) == 3.7
    assert percent(80, 0) == 100.0
    assert percent(0, 0) is None
    assert status_for(99.9) == "healthy"
    assert status_for(50.1) == "healthy"
    assert status_for(50.0) == "degraded", "half is not a majority"
    assert status_for(3.7) == "degraded"


def test_a_source_that_mostly_fails_is_degraded(
    tmp_path: Path, site: _FlakySite, backend: str
) -> None:
    engine = _web_engine(tmp_path, site.base, ["a", "b", "c", "d", "e"], backend)
    try:
        site.broken = {"b", "c", "d"}
        result = engine.sync_source("site", "incremental")
        described = describe_source(_context(engine), "site")
    finally:
        engine.close()
    assert result.status == "degraded"
    assert described["source"]["status"] == "degraded"
    assert described["source"]["health"]["percent"] == 40.0
    assert described["source"]["health"]["unreadable"] == 3


def test_removing_a_failing_url_raises_the_score_before_any_sync(
    tmp_path: Path, site: _FlakySite, backend: str
) -> None:
    pages = ["one", "two", "three", "four"]
    engine = _web_engine(tmp_path, site.base, pages, backend)
    context = _context(engine)
    try:
        site.broken = {"two"}
        engine.sync_source("site", "incremental")
        assert _listed(engine)["site"]["health"]["percent"] == 75.0
        before = engine.state.list_sync_events("site", limit=1)[0]["id"]

        removal = UrlEditRequest(source_name="site", remove=[f"{site.base}/two"], sync=False)
        edit_urls(context, removal)
        assert engine.state.list_sync_events("site", limit=1)[0]["id"] == before, "no sync ran"
        listed = _listed(engine)["site"]
        described = describe_source(context, "site")
        waited = wait_for_sync(context, WaitRequest(source_name="site", timeout_seconds=0))
        summary = {s["name"]: s for s in overview(context)["sources"]}["site"]
    finally:
        engine.close()
    for health in (listed["health"], described["source"]["health"], waited["health"]):
        assert health["percent"] == 100.0
        assert health["unreadable"] == 0
        assert health["dropped"] == 1
    assert summary["health_percent"] == 100.0
    assert described["last_sync"]["unreadable"] == [], "a removed page is still listed as failing"


def test_archiving_a_page_that_now_fails_takes_it_out(
    tmp_path: Path, site: _FlakySite, backend: str
) -> None:
    """A page that read once and fails now keeps its old text: held, not
    readable. Archiving it is the other way to drop it."""

    engine = _web_engine(tmp_path, site.base, ["one", "two", "three"], backend)
    context = _context(engine)
    try:
        engine.sync_source("site", "incremental")
        site.broken = {"one"}
        engine.sync_source("site", "incremental")
        health = describe_source(context, "site")["source"]["health"]
        assert (health["readable"], health["unreadable"], health["percent"]) == (2, 1, 66.6)

        archive_documents(context, ArchiveRequest(documents=["one.txt"], source_name="site"))
        health = describe_source(context, "site")["source"]["health"]
    finally:
        engine.close()
    assert (health["readable"], health["unreadable"], health["percent"]) == (2, 0, 100.0)
    assert health["dropped"] == 1


def test_a_failed_sync_is_not_scored(tmp_path: Path, site: _FlakySite, backend: str) -> None:
    """Nothing read, so the pass fails; the score says nothing rather than 0%."""

    engine = _web_engine(tmp_path, site.base, ["one"], backend)
    try:
        site.broken = {"one"}
        try:
            engine.sync_source("site", "incremental")
        except RuntimeError:
            pass
        listed = _listed(engine)["site"]
    finally:
        engine.close()
    assert listed["health"] is None


def test_the_source_listing_does_not_carry_every_page_of_a_checkpoint(
    tmp_path: Path, site: _FlakySite
) -> None:
    """GET /sources answered 8.9 MB for a 17,000-page collection, 6.9 MB of it
    one validator per page, and the UI polls it every second while a sync
    runs. A collection past the limit is its length in the listing."""

    from pheasant.registry.source_registry import CHECKPOINT_LIST_LIMIT

    pages = [f"p{n}" for n in range(CHECKPOINT_LIST_LIMIT + 5)]
    engine = _web_engine(tmp_path, site.base, pages)
    try:
        engine.sync_source("site", "incremental")
        listed = _listed(engine)["site"]["checkpoint"]
        whole = engine.state.get_source_checkpoint("site")
    finally:
        engine.close()
    assert len(whole["high_watermark"]["urls"]) == len(pages), "the stored checkpoint is whole"
    assert listed["high_watermark"]["urls"] == len(pages)
    assert "high_watermark.urls" in listed["summarized"]
    assert listed["high_watermark"]["item_count"] == whole["high_watermark"]["item_count"]


def test_the_source_listing_reads_the_job_snapshots_once(tmp_path: Path) -> None:
    """Each source's syncing/job/progress lookup read every shared job
    snapshot afresh -- three per source, on /state, which on the Azure fleet
    is an NFS share -- and GET /sources took 5.8 s with eight sources."""

    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    sources = []
    for name in ("one", "two", "three"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "a.md").write_text("# A\n\nText.\n", encoding="utf-8")
        sources.append({"name": name, "type": "document_folder", "path": str(folder)})
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "listing",
                "defaults": "standalone",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": sources,
        }
    )
    app = create_app(config, config_path=str(tmp_path / "pheasant.yaml"))
    shared = app.state.jobs._shared
    reads = []
    original = shared.list

    def counted(*args, **kwargs):
        reads.append(1)
        return original(*args, **kwargs)

    shared.list = counted
    response = TestClient(app).get("/sources")
    assert response.status_code == 200
    assert len(response.json()) == 3
    assert len(reads) == 1, f"{len(reads)} reads of the job snapshots for one listing"
