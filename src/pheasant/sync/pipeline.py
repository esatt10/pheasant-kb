"""A source's sync pipeline: whether it runs by itself, and how it reads in words.

Every source has a pipeline from the moment it is added -- adding a source
stays one field -- and the Syncs page (`services/sync_pipelines.py`) is where
it is changed. Its settings live on ``sources[].sync``
(`config.schema.SourceSyncSettings`):

* ``schedule: auto`` runs on the scheduler beat, at startup and on the
  source's own triggers (a file change, a commit); ``manual`` only when
  someone asks. Deleting a schedule is ``manual``.
* ``paused`` holds an ``auto`` schedule without forgetting it.
* ``pipeline: false`` is a deleted pipeline: nothing syncs the source, not
  even by hand, and its indexed documents stay until the source is removed.

:func:`runs_automatically` is the one test every automatic trigger asks, so
a paused or manual pipeline cannot be started by a path that forgot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

SCHEDULES = ("auto", "manual")
#: A source that gives way to content runs anyway after this many intervals,
#: so a region that is never idle cannot starve it.
MAX_DEFERRAL_INTERVALS = 4


class PipelineDeleted(ValueError):
    """A sync was asked of a source whose pipeline was deleted."""

    status = 409
    code = "PIPELINE_DELETED"
    retryable = False

    def __init__(self, name: str) -> None:
        super().__init__(
            f"{name}'s sync pipeline was deleted, so nothing syncs it. Restore the pipeline "
            "on the Syncs page (or configure_sync_pipeline with pipeline=true) to sync it again."
        )


def runs_automatically(source: Any) -> bool:
    """Whether the beat, startup and a source's own triggers may sync it now."""

    sync = source.sync
    return (
        bool(getattr(source, "enabled", True))
        and getattr(sync, "pipeline", True)
        and getattr(sync, "schedule", "auto") == "auto"
        and not getattr(sync, "paused", False)
    )


def beat_sources(sources: Any, state: Any, *, queue: Any = None, now: Any = None) -> list[Any]:
    """The sources a scheduler beat syncs: running by themselves, due, and not giving way.

    A paced source (``sync.min_interval_seconds``) runs at most that often; one
    that yields (``sync.yield_to_content``) is held while another source's sync
    is running or queued, until it has waited ``MAX_DEFERRAL_INTERVALS``
    intervals. Paced sources come last, so a beat's content syncs are published
    -- and claimed -- first. Decided before this beat publishes anything, so a
    beat never yields to its own work.
    """

    candidates = [s for s in sources if runs_automatically(s)]
    if not any(_paced(s) for s in candidates):
        return candidates
    now = now or datetime.now(UTC)
    paced = {s.name for s in candidates if _paced(s)}
    busy: bool | None = None
    due = []
    for source in candidates:
        if source.name not in paced:
            due.append(source)
            continue
        interval = int(getattr(source.sync, "min_interval_seconds", None) or 0)
        waited = _seconds_since_indexed(state, source.name, now)
        if waited is not None and waited < interval:
            continue
        if getattr(source.sync, "yield_to_content", False):
            overdue = waited is None or waited >= interval * MAX_DEFERRAL_INTERVALS
            if busy is None:
                busy = content_busy(state, queue, paced)
            if busy and not overdue:
                continue
        due.append(source)
    return sorted(due, key=lambda s: s.name in paced)


def _paced(source: Any) -> bool:
    sync = source.sync
    return bool(
        getattr(sync, "min_interval_seconds", None) or getattr(sync, "yield_to_content", False)
    )


def _seconds_since_indexed(state: Any, name: str, now: datetime) -> float | None:
    try:
        rows = state.rows("SELECT last_indexed_at FROM sources WHERE name=?", (name,))
        stamp = next((r["last_indexed_at"] for r in rows if r["last_indexed_at"]), None)
        if not stamp:
            return None
        then = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=UTC)
        return (now - then).total_seconds()
    except Exception:  # noqa: BLE001 - unknown is "due", never "skip forever"
        return None


def content_busy(state: Any, queue: Any, exclude: set[str]) -> bool:
    """Whether a source outside ``exclude`` is syncing, or anything is queued to.

    Read from what every process shares (``sync_runs`` heartbeats, the queue),
    so it holds on a fleet. Unknown reads as idle: a yielding source waiting on
    a broker that cannot answer would otherwise wait forever.
    """

    from pheasant.sync.locks import SOURCE_STALE_SECONDS
    from pheasant.sync.queue import INFLIGHT, PENDING

    fresh = (datetime.now(UTC) - timedelta(seconds=SOURCE_STALE_SECONDS)).isoformat()
    try:
        for row in state.rows("SELECT source_id FROM sync_runs WHERE heartbeat_at >= ?", (fresh,)):
            if str(row["source_id"]) not in exclude:
                return True
    except Exception:  # noqa: BLE001
        pass
    if queue is None:
        return False
    try:
        listed = queue.outstanding(200)
        if listed is not None:
            return any(
                task.get("status") in (PENDING, INFLIGHT)
                and str(task.get("source_id")) not in exclude
                for task in listed
            )
        depth = queue.depth()
        return bool(depth.get("pending") or depth.get("inflight"))
    except Exception:  # noqa: BLE001
        return False


def startup_sources(sources: Any) -> list[str]:
    """The names of ``sources`` a boot syncs: ``on_startup`` and running by itself."""

    return [s.name for s in sources if s.sync.on_startup and runs_automatically(s)]


def require_pipeline(source: Any) -> None:
    """Refuse a sync of a source whose pipeline was deleted."""

    if not getattr(source.sync, "pipeline", True):
        raise PipelineDeleted(str(source.name))


def state_of(source: Any) -> str:
    """``deleted``, ``paused``, ``manual`` or ``scheduled``."""

    sync = source.sync
    if not getattr(sync, "pipeline", True):
        return "deleted"
    if getattr(sync, "schedule", "auto") != "auto":
        return "manual"
    if getattr(sync, "paused", False):
        return "paused"
    return "scheduled"
