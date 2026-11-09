"""Syncs the region starts by itself are jobs: `sync/background_jobs.py`.

The startup sync and the scheduler's beat drained the index queue with no job
of their own, so a sync somebody asked for while one ran was claimed by it and
showed nothing in the tray for its whole run. Each pass is one job now, with a
row per source, and it ends whatever happens inside it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pheasant.jobs import JobRegistry
from pheasant.sync.background_jobs import run_sync_job
from pheasant.sync.scheduler import SchedulerService
from pheasant.sync.worker import WorkerBackedEngine


def _result(source: str, status: str = "succeeded", **details: Any) -> Any:
    return SimpleNamespace(
        source_id=source,
        status=status,
        indexed_artifacts=2,
        skipped_artifacts=5,
        details=details,
    )


def test_a_pass_is_one_job_with_a_row_per_source_that_moves() -> None:
    jobs = JobRegistry()

    def run(progress: Any) -> list[Any]:
        assert progress is not None
        progress({"phase": "indexing", "current": 3, "total": 9, "meta": {"source": "docs"}})
        progress({"phase": "listing", "current": 0, "total": None, "meta": {"source": "web"}})
        return [_result("docs"), _result("web")]

    results = run_sync_job(jobs, "Scheduled sync", ["docs", "web"], run)

    assert [r.source_id for r in results] == ["docs", "web"]
    [job] = jobs.list()
    assert job["label"] == "Scheduled sync" and job["status"] == "succeeded"
    sources = {row["source"]: row for row in job["sources"]}
    assert set(sources) == {"docs", "web"}
    assert sources["docs"]["total"] == 9, "the source's own counter reached its row"


def test_a_pass_that_fails_or_raises_still_ends_its_job() -> None:
    jobs = JobRegistry()
    run_sync_job(
        jobs, "Startup sync", ["docs"], lambda _p: [_result("docs", "failed", error="403")]
    )
    assert jobs.list()[0]["status"] == "failed" and jobs.list()[0]["error"] == "403"

    def explode(_progress: Any) -> list[Any]:
        raise RuntimeError("worker died")

    with pytest.raises(RuntimeError, match="worker died"):
        run_sync_job(jobs, "Startup sync", ["docs"], explode)
    assert not any(job["active"] for job in jobs.list()), "a job was left running"


def test_the_beat_is_a_job_over_the_pipelines_that_run_by_themselves() -> None:
    def source(name: str, **sync: Any) -> Any:
        settings = {"pipeline": True, "schedule": "auto", "paused": False, **sync}
        return SimpleNamespace(name=name, enabled=True, sync=SimpleNamespace(**settings))

    class Engine:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def enabled_sources(self) -> list[Any]:
            return [source("docs"), source("web"), source("held", paused=True)]

        def sync_all(self, mode: str, **kwargs: Any) -> list[Any]:
            self.calls.append(kwargs)
            progress = kwargs.get("on_progress")
            if progress:
                progress({"phase": "saving", "current": 1, "total": 1, "meta": {"source": "web"}})
            return [_result("docs"), _result("web")]

    jobs = JobRegistry()
    engine = Engine()
    SchedulerService(engine, enabled=True, jobs=jobs)._scheduled_sync()
    [job] = jobs.list()
    assert job["label"] == "Scheduled sync" and job["status"] == "succeeded"
    assert {row["source"] for row in job["sources"]} == {"docs", "web"}, (
        "a paused source has no row"
    )

    # Without a registry the beat is the call it always was.
    bare = Engine()
    SchedulerService(bare, enabled=True)._scheduled_sync()
    assert bare.calls == [{"scheduled": True}]


def test_a_startup_sync_reports_each_source_as_it_goes(sync_engine: Any, tmp_path: Any) -> None:
    """In process, the real engine: its four-argument hook reaches the job's rows."""

    worker = WorkerBackedEngine(sync_engine, str(tmp_path / "not-this-region.yaml"))
    names = [source.name for source in sync_engine.enabled_sources()]
    jobs = JobRegistry()
    results = run_sync_job(jobs, "Startup sync", names, worker.startup)

    [job] = jobs.list()
    assert job["status"] == "succeeded", job
    rows = {row["source"]: row for row in job["sources"]}
    assert set(rows) == set(names)
    assert {r.source_id for r in results} <= set(names)
    assert any(row["phase"] not in (None, "queued") for row in rows.values()), rows
