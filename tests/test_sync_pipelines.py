"""Every source's sync pipeline: pacing, schedules, the ledger, both surfaces, the trace.

The Syncs page is A (pipelines with an inspector) and C (the document ledger)
of the design: what each source's pipeline does and when, how it went, and
per document when it was read, changed and is checked next and in what state.
These tests hold the mechanisms under it: a site's crawl delay and a run's
budget (`sync/web_pacing.py`), the schedule switches every automatic trigger
asks (`sync/pipeline.py`), the service both surfaces call
(`services/sync_pipelines.py`) and the OpenTelemetry-shaped trace each run
writes (`telemetry/sync_trace.py`).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.services import ServiceContext
from pheasant.services import sync_ledger as ledger_service
from pheasant.services import sync_pipelines as pipes
from pheasant.services.errors import InvalidRequest
from pheasant.sync.engine import SyncEngine
from pheasant.sync.pipeline import runs_automatically
from pheasant.sync.web_pacing import HostPacer, crawl_delay

ROBOTS = "User-agent: *\nCrawl-delay: 0.05\n"


class _Site:
    def __init__(self, robots: str = ROBOTS) -> None:
        site = self
        self.robots = robots
        self.pages: list[str] = []
        self.broken: set[str] = set()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                name = self.path.strip("/")
                if name == "robots.txt":
                    body = site.robots.encode()
                    kind = "text/plain"
                elif name in site.broken:
                    self.send_response(404)
                    self.end_headers()
                    return
                else:
                    site.pages.append(name)
                    body = (
                        " ".join(f"The {name} page explains field {n}." for n in range(30))
                    ).encode()
                    kind = "text/plain"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def site():
    served = _Site()
    try:
        yield served
    finally:
        served.server.shutdown()
        served.server.server_close()


def _engine(root: Path, site: _Site, names: list[str], **sync: Any) -> SyncEngine:
    sources = [
        {
            "name": "web",
            "type": "web_collection",
            "path": "/unused",
            "urls": [f"{site.base}/{n}" for n in names],
            "connector": {"allow_experimental": True},
            "sync": {"interval_seconds": 0, **sync},
        }
    ]
    notes = root / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "a.md").write_text("# A\n\nA note about fields and pages.\n", encoding="utf-8")
    sources.append({"name": "notes", "type": "document_folder", "path": str(notes)})
    return SyncEngine(
        PheasantConfig.model_validate(
            {
                "pheasant": {
                    "name": "pipes",
                    "defaults": "standalone",
                    "state_path": str(root / "state"),
                    "workspace_root": str(root),
                    "exports_path": str(root / "exports"),
                },
                "sources": sources,
            }
        )
    )


def _context(engine: SyncEngine) -> ServiceContext:
    return ServiceContext(config=engine.config, state=engine.state, searcher=None, engine=engine)


# -- pacing ---------------------------------------------------------------------


def test_robots_txt_crawl_delay_is_read_for_our_agent_then_for_everyone() -> None:
    text = (
        "User-agent: Googlebot\nAllow: /\n\n"
        "User-agent: *\nCrawl-Delay: 20\nDisallow: /search/\n\n"
        "User-agent: pheasant\nCrawl-delay: 5\n"
    )
    assert crawl_delay(text) == 5.0
    assert crawl_delay(text, agent="other") == 20.0
    assert crawl_delay("User-agent: *\nDisallow:\n") == 0.0
    assert crawl_delay("") == 0.0


def test_a_paced_host_is_asked_one_request_per_delay_within_a_budget() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    source = SimpleNamespace(
        sync=SimpleNamespace(
            min_request_interval_seconds=None, respect_robots=True, max_items_per_run=None
        )
    )
    fetched: list[str] = []
    pacer = HostPacer(
        source,
        lambda url: fetched.append(url) or "User-agent: *\nCrawl-delay: 1100\n",
        clock=lambda: now[0],
        sleep=sleep,
    )
    urls = [f"https://docs.example.com/{n}" for n in range(4)]
    assert pacer.plan(urls) == {urls[3]}, "the budget is ~55 minutes of the delay: 3 pages"
    for url in urls[:3]:
        pacer.admit(url)
    assert slept == [1100.0, 1100.0], "each request waited its turn"
    assert fetched == ["https://docs.example.com/robots.txt"], "robots.txt read once"
    assert pacer.report()["budget_deferred"] == 1
    assert pacer.report()["crawl_delays"] == {"docs.example.com": 1100.0}
    remembered = HostPacer(
        source, lambda url: pytest.fail("read again"), known=pacer.robots_state()
    )
    assert remembered.delay_for("https://docs.example.com/x") == 1100.0


def test_an_override_and_an_explicit_budget_win_over_robots() -> None:
    source = SimpleNamespace(
        sync=SimpleNamespace(
            min_request_interval_seconds=0, respect_robots=True, max_items_per_run=2
        )
    )
    pacer = HostPacer(
        source, lambda url: pytest.fail("robots.txt not needed"), sleep=lambda s: None
    )
    urls = [f"https://a.example/{n}" for n in range(3)]
    assert pacer.plan(urls) == {urls[2]}


def test_a_paced_sync_takes_its_budget_and_the_rest_on_the_next_run(
    tmp_path: Path, site: _Site
) -> None:
    names = [f"p{n}" for n in range(5)]
    engine = _engine(tmp_path, site, names, max_items_per_run=2)
    try:
        first = engine.sync_source("web", "incremental")
        assert len(site.pages) == 2, "a run took more than its budget"
        assert first.status == "healthy"
        engine.sync_source("web", "incremental")
        engine.sync_source("web", "incremental")
        assert set(site.pages) == set(names), "a deferred page was never read"
        assert site.pages[2:4] == ["p2", "p3"], "the second run re-read instead of moving on"
        watermark = engine.state.get_source_checkpoint("web")["high_watermark"]
    finally:
        engine.close()
    assert watermark["crawl_delays"], "the site's crawl delay was not recorded"


# -- schedules ----------------------------------------------------------------------


def test_only_an_automatic_pipeline_runs_by_itself(tmp_path: Path, site: _Site) -> None:
    engine = _engine(tmp_path, site, ["p0"], paused=True)
    try:
        sources = {s.name: s for s in engine.enabled_sources()}
        assert not runs_automatically(sources["web"])
        assert runs_automatically(sources["notes"])
        ran = {r.source_id for r in engine.sync_all("incremental", scheduled=True)}
        assert ran == {"notes"}, "a beat synced a paused pipeline"
        asked = {r.source_id for r in engine.sync_all("incremental")}
        assert asked == {"notes", "web"}, "asking for every source skipped a paused one"
    finally:
        engine.close()


def test_a_deleted_pipeline_syncs_nothing_even_when_asked(tmp_path: Path, site: _Site) -> None:
    engine = _engine(tmp_path, site, ["p0"], pipeline=False)
    try:
        result = engine.sync_source("web", "incremental")
    finally:
        engine.close()
    assert result.status == "skipped" and result.details["reason"] == "pipeline deleted"
    assert site.pages == []


# -- the service ----------------------------------------------------------------------


def test_pipelines_say_how_each_source_syncs_and_how_it_went(tmp_path: Path, site: _Site) -> None:
    engine = _engine(tmp_path, site, ["p0", "p1"])
    try:
        engine.sync_source("web", "incremental")
        engine.sync_source("notes", "incremental")
        listing = pipes.pipelines(_context(engine))
        detail = pipes.pipeline(_context(engine), "web")
    finally:
        engine.close()
    rows = {row["name"]: row for row in listing["pipelines"]}
    web = rows["web"]
    assert web["state"] == "scheduled"
    assert web["schedule"]["mode"] == "adaptive"
    assert web["last_run"]["status"] == "healthy" and web["last_run"]["trace_id"]
    assert web["health"]["percent"] == 100.0
    assert web["quality"]["ok"] == 2
    assert web["pacing"]["source"] == "robots.txt" and web["pacing"]["delay_seconds"] == 0.05
    assert rows["notes"]["schedule"]["mode"] == "triggers"
    assert "on change" in rows["notes"]["schedule"]["label"].lower()
    assert detail["runs"] and detail["settings"]["schedule"] == "auto"
    assert [s["phase"] for s in detail["stages"]], "a run with no job keeps its trace's stages"
    assert {d["url"] for d in detail["next_due"]} == {f"{site.base}/p0", f"{site.base}/p1"}


def test_a_pipeline_is_paused_deleted_and_restored(tmp_path: Path, site: _Site) -> None:
    engine = _engine(tmp_path, site, ["p0"])
    context = _context(engine)
    try:
        paused = pipes.configure(context, pipes.PipelineChange("web", settings={"paused": True}))
        manual = pipes.delete(context, "web", "schedule")
        deleted = pipes.delete(context, "web", "pipeline")
        assert engine.sync_source("web", "incremental").status == "skipped"
        restored = pipes.restore(context, "web")
        with pytest.raises(InvalidRequest, match="schedule must be"):
            pipes.configure(context, pipes.PipelineChange("web", settings={"schedule": "often"}))
        with pytest.raises(InvalidRequest, match="unknown pipeline setting"):
            pipes.configure(context, pipes.PipelineChange("web", settings={"speed": 3}))
        with pytest.raises(InvalidRequest, match="what must be"):
            pipes.delete(context, "web", "everything")
    finally:
        engine.close()
    assert (paused["state"], manual["state"], deleted["state"], restored["state"]) == (
        "paused",
        "manual",
        "deleted",
        "scheduled",
    )
    assert (paused["schedule"]["label"], manual["schedule"]["label"]) == (
        "Paused",
        "Only when asked",
    ), "a web collection that is not scheduled does not read as adaptive"


def test_the_ledger_says_when_each_document_was_read_changed_and_is_due(
    tmp_path: Path, site: _Site
) -> None:
    site.broken = {"gone"}
    engine = _engine(tmp_path, site, ["p0", "p1", "gone"], interval_seconds=3600)
    context = _context(engine)
    try:
        engine.sync_source("web", "incremental")
        ledger = ledger_service.documents(context, ledger_service.DocumentsRequest("web"))
        attention = ledger_service.documents(
            context, ledger_service.DocumentsRequest("web", state="attention")
        )
        one = ledger_service.document(context, "web", f"{site.base}/p0")
        marked = ledger_service.recheck(context, "web", [f"{site.base}/p0"])
        due = ledger_service.documents(
            context, ledger_service.DocumentsRequest("web", state="due_soon")
        )
        with pytest.raises(InvalidRequest, match="state must be"):
            ledger_service.documents(
                context, ledger_service.DocumentsRequest("web", state="sometimes")
            )
    finally:
        engine.close()
    rows = {row["path"].rsplit("/", 1)[-1].split(".")[0]: row for row in ledger["documents"]}
    assert ledger["documents"][0]["status"] == "error", "the failing page is listed first"
    assert rows["p0"]["last_read_at"] and rows["p0"]["last_changed_at"]
    assert rows["p0"]["next_check_at"] and rows["p0"]["interval_seconds"] == 3600
    assert rows["p0"]["server_said"] == "200"
    assert rows["gone"]["server_said"] == "404" and rows["gone"]["status"] == "error"
    assert [r["path"] for r in attention["documents"]] == [rows["gone"]["path"]]
    assert ledger["totals"]["unreadable"] == 1 and ledger["totals"]["all"] == 3
    assert one["document"]["path"] == rows["p0"]["path"]
    assert marked["marked_due"] == 1
    assert rows["p0"]["path"] in {r["path"] for r in due["documents"]}


def test_both_surfaces_answer_the_same(tmp_path: Path, site: _Site) -> None:
    pytest.importorskip("mcp")
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app
    from pheasant.mcp_server.tools import PheasantTools

    config = _engine(tmp_path, site, ["p0"]).config
    tools = PheasantTools(config)
    tools.engine.sync_source("web", "incremental")
    client = TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))
    over_mcp = tools.list_sync_pipelines(config.knowledge_base_id)
    over_http = client.get("/sync/pipelines").json()

    def strip(rows: dict) -> dict:
        keys = ("state", "schedule", "health", "quality")
        return {r["name"]: {k: r[k] for k in keys} for r in rows["pipelines"]}

    assert strip(over_mcp) == strip(over_http)
    patched = client.patch("/sync/pipelines/web", json={"paused": True}).json()
    assert patched["state"] == "paused"
    assert tools.describe_sync_pipeline(config.knowledge_base_id, "web")["state"] == "paused"
    documents = client.get("/sync/pipelines/web/documents").json()
    assert documents["totals"]["all"] == 1
    refused = client.patch("/sync/pipelines/web", json={"schedule": "often"})
    assert refused.status_code == 422 and "schedule must be" in refused.json()["detail"]


# -- the trace --------------------------------------------------------------------------


def test_a_sync_run_is_an_otel_shaped_trace_on_stdout(tmp_path: Path, site: _Site, capsys) -> None:
    from pheasant.telemetry.interactions import adopt_trace

    engine = _engine(tmp_path, site, ["p0"])
    parent_trace, parent_span = "a" * 32, "b" * 16
    try:
        capsys.readouterr()
        with adopt_trace(f"00-{parent_trace}-{parent_span}-01"):
            result = engine.sync_source("web", "incremental")
        out = capsys.readouterr().out
        event = engine.state.list_sync_events("web", limit=1)[0]
    finally:
        engine.close()
    spans = [
        json.loads(line) for line in out.splitlines() if line.startswith('{"type":"otel.span"')
    ]
    run = spans[0]
    assert run["name"] == "pheasant.sync" and run["trace_id"] == parent_trace
    assert run["parent_span_id"] == parent_span, "the run did not join the caller's trace"
    assert run["status"]["code"] == "OK"
    assert run["attributes"]["pheasant.source"] == "web"
    assert run["attributes"]["pheasant.sync.indexed"] == 1
    stages = [s for s in spans[1:]]
    assert stages and all(s["parent_span_id"] == run["span_id"] for s in stages)
    assert all(s["trace_id"] == parent_trace for s in stages)
    assert result.details["trace_id"] == parent_trace == event["details"]["trace_id"]


def test_a_sync_child_hands_its_spans_back_rather_than_printing_them(
    tmp_path: Path, site: _Site, capsys, monkeypatch
) -> None:
    from pheasant.telemetry.sync_trace import CHILD_ENV

    monkeypatch.setenv(CHILD_ENV, "1")
    engine = _engine(tmp_path, site, ["p0"])
    try:
        capsys.readouterr()
        result = engine.sync_source("web", "incremental")
        out = capsys.readouterr().out
    finally:
        engine.close()
    assert '"otel.span"' not in out
    assert result.details["otel_spans"][0]["name"] == "pheasant.sync"


def test_a_stopped_pipeline_reads_as_stopped_and_a_beat_run_keeps_its_stages(tmp_path):
    """A manual or paused schedule says so, and a sync with no job still has stages."""

    from pheasant.telemetry.sync_trace import SyncTrace

    trace = SyncTrace("docs", "incremental")
    with trace:
        trace.phase("listing")
        trace.phase("committing")
        seconds = trace.stage_seconds()
    assert [name for name, _ in seconds] == ["listing", "committing"], "in run order"

    from pheasant.services import sync_pipelines

    event = {"details": {"stage_seconds": [["listing", 1.25], ["committing", 3.0]]}}
    assert sync_pipelines._stages(None, event) == [
        {"phase": "listing", "seconds": 1.2},
        {"phase": "committing", "seconds": 3.0},
    ]
    job = {"sources": [{"phase_seconds": {"fetching": 9.0}}]}
    assert sync_pipelines._stages(job, event) == [{"phase": "fetching", "seconds": 9.0}]


def test_a_served_beat_skips_paused_pipelines_in_the_child_processes_too(monkeypatch) -> None:
    """The serving scheduler syncs through WorkerBackedEngine, whose children
    do not know they are the beat: with a paused source among the enabled ones
    each other source gets its own child, and the paused one none."""

    from types import SimpleNamespace

    from pheasant.config.schema import SourceSyncSettings
    from pheasant.sync import worker

    def source(name: str, **sync: Any) -> SimpleNamespace:
        return SimpleNamespace(name=name, enabled=True, sync=SourceSyncSettings(**sync))

    inner = SimpleNamespace(
        enabled_sources=lambda: [source("a"), source("b", paused=True), source("c")]
    )
    engine = worker.WorkerBackedEngine(inner, "pheasant.yaml")
    synced: list[str] = []
    monkeypatch.setattr(engine, "_can_delegate", lambda: True)
    monkeypatch.setattr(engine, "sync_source", lambda name, mode, **_: synced.append(name))
    monkeypatch.setattr(worker, "run_sync", lambda *a, **k: pytest.fail("one child for all"))

    engine.sync_all("incremental", scheduled=True)
    assert synced == ["a", "c"]


def test_the_ledger_is_built_once_per_version_of_the_source(
    tmp_path: Path, site: _Site, monkeypatch
) -> None:
    """A page of the ledger, a filter and one document reuse one build; a
    recheck (a checkpoint write) or a sync (a new event) makes a new one, and
    the source's older version is dropped rather than left holding memory."""

    from pheasant.services import quality

    # Two hours: only the recheck makes a page due within the hour.
    engine = _engine(tmp_path, site, ["p0", "p1"], interval_seconds=7200)
    context = _context(engine)
    built: list[str] = []
    build = ledger_service._build_ledger
    monkeypatch.setattr(
        ledger_service, "_build_ledger", lambda c, s, *a: built.append(s["name"]) or build(c, s, *a)
    )
    try:
        engine.sync_source("web", "incremental")
        first = ledger_service.documents(context, ledger_service.DocumentsRequest("web"))
        ledger_service.documents(
            context, ledger_service.DocumentsRequest("web", state="attention", offset=1)
        )
        ledger_service.document(context, "web", f"{site.base}/p1")
        assert built == ["web"], "one build serves every read of one version"

        ledger_service.recheck(context, "web", [f"{site.base}/p0"])
        due = ledger_service.documents(
            context, ledger_service.DocumentsRequest("web", state="due_soon")
        )
        assert len(built) == 2 and due["totals"]["due_soon"] == 1, "a recheck is a new version"

        engine.sync_source("web", "incremental")
        ledger_service.documents(context, ledger_service.DocumentsRequest("web"))
        assert len(built) == 3, "a sync is a new version"
        held = quality._caches(context.state)[1]["ledgers"]
        assert len(held) == 1, "the source's older ledgers are dropped once replaced"
    finally:
        engine.close()
    assert first["totals"]["all"] == 2


def test_a_span_names_the_role_that_ran_the_sync(monkeypatch) -> None:
    """``pheasant serve`` sets PHEASANT_ROLE; a span (and a sync child, which
    inherits the environment) carries it as ``resource["pheasant.role"]``."""

    from pheasant.telemetry.sync_trace import ROLE_ENV, SyncTrace

    monkeypatch.setenv(ROLE_ENV, "indexer")
    spans = SyncTrace("docs", "incremental").finish("healthy", {})
    assert {span["resource"]["pheasant.role"] for span in spans} == {"indexer"}

    monkeypatch.delenv(ROLE_ENV)
    spans = SyncTrace("docs", "incremental").finish("healthy", {})
    assert "pheasant.role" not in spans[0]["resource"]


def test_a_hand_run_sync_is_the_clis_and_a_serves_child_keeps_its_role(monkeypatch) -> None:
    import os

    from pheasant import cli

    class _Stop(Exception):
        pass

    def stop(*_a: Any, **_k: Any) -> None:
        raise _Stop

    monkeypatch.setattr(cli, "_engine", stop)
    monkeypatch.delenv("PHEASANT_ROLE", raising=False)
    with pytest.raises(_Stop):
        cli.main(["sync", "--all"])
    assert os.environ["PHEASANT_ROLE"] == "cli"

    monkeypatch.setenv("PHEASANT_ROLE", "indexer")
    with pytest.raises(_Stop):
        cli.main(["sync", "--all"])
    assert os.environ["PHEASANT_ROLE"] == "indexer"
