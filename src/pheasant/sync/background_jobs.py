"""Syncs the region starts by itself, as jobs a person can watch.

A sync somebody asks for is a job: an id, a phase and counters in
`jobs.JobRegistry`, which `/jobs` and the web UI's tray read. The two an
indexer starts on its own were not -- the startup sync after a restart or a
wake from zero, and the scheduler's beat -- and both *drain the index queue*
while they run. So a sync asked for while one of them ran was claimed by it
and ran with no job at all. Found on the Azure fleet: "Run now" on the Syncs
page, an indexer that had just woken, and nothing in the tray for the four
minutes the sync took, so it was pressed again.

One job per pass, over every source it covers, with the same per-source
progress a requested sync reports. A task the pass claims from the queue shows
up as its source's row rather than as nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from pheasant.job_snapshots import FAILED_SYNC_STATUSES, sync_job_status

logger = logging.getLogger(__name__)

#: A worker progress event (`sync/worker.py`'s wire): phase, counters, meta.
ProgressEvent = dict[str, Any]


def forward_progress(
    jobs: Any, job_id: str, source: str | None = None
) -> Callable[[ProgressEvent], None]:
    """A worker progress hook that records into ``job_id``, per source."""

    def forward(event: ProgressEvent) -> None:
        meta = event.get("meta") or {}
        jobs.progress(
            job_id,
            phase=event.get("phase"),
            current=event.get("current"),
            total=event.get("total"),
            detail=event.get("detail"),
            source=meta.get("source") or source,
            stats=meta,
        )

    return forward


def finish_sync_job(jobs: Any, job_id: str, results: Sequence[Any]) -> None:
    """End ``job_id`` with what ``results`` say happened, failures first."""

    failed = [r for r in results if r.status in FAILED_SYNC_STATUSES]
    jobs.finish(
        job_id,
        sync_job_status([r.status for r in results]),
        error=(failed[0].details.get("error") or failed[0].status) if failed else None,
        result={
            "results": [
                {
                    "source_id": r.source_id,
                    "indexed_artifacts": r.indexed_artifacts,
                    "skipped_artifacts": r.skipped_artifacts,
                    "status": r.status,
                }
                for r in results
            ]
        },
    )


def run_sync_job(
    jobs: Any,
    label: str,
    targets: Sequence[str],
    run: Callable[[Callable[[ProgressEvent], None] | None], Sequence[Any]],
) -> list[Any]:
    """``run(on_progress)`` as one job over ``targets``; with no registry, run it bare.

    The job reaches a terminal state on every exit, and an exception still
    propagates: the caller's own handling (the scheduler logs and carries on)
    is unchanged, the tray just stops saying "running".
    """

    if jobs is None or not targets:
        return list(run(None))
    job = jobs.create("sync", label, list(targets))
    try:
        results = list(run(forward_progress(jobs, job.id)))
    except Exception as exc:
        jobs.finish(job.id, "failed", error=str(exc))
        raise
    finish_sync_job(jobs, job.id, results)
    return results
