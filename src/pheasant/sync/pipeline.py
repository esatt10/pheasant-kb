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

import json
from datetime import UTC, datetime, timedelta
from typing import Any

SCHEDULES = ("auto", "manual")
#: A source that gives way to content runs anyway after this many intervals,
#: so a region that is never idle cannot starve it.
MAX_DEFERRAL_INTERVALS = 4
#: The cadences the Syncs page offers, in seconds: 15 min to a week.
CADENCES = (900, 1800, 3600, 7200, 21600, 43200, 86400, 604800)
#: The ``region_settings`` section holding the runtime default cadence.
SCHEDULE_SECTION = "sync_schedule"


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


def default_interval(config: Any, state: Any) -> int | None:
    """The region's default cadence: the Syncs page's runtime value over the config's."""

    scheduler = getattr(getattr(config, "sync", None), "scheduler", None)
    configured = getattr(scheduler, "default_source_interval_seconds", None)
    try:
        rows = state.rows(
            "SELECT data_json FROM region_settings WHERE kb_id=? AND section=?",
            (config.knowledge_base_id, SCHEDULE_SECTION),
        )
    except Exception:  # noqa: BLE001 - a store without the table has no override
        rows = []
    if rows:
        data = json.loads(rows[0]["data_json"] or "{}")
        if "default_source_interval_seconds" in data:
            return data["default_source_interval_seconds"] or None
    return configured or None


def interval_of(source: Any, default: int | None) -> int:
    """A source's cadence on the beat: its own (0 is every beat), else the region's."""

    own = getattr(source.sync, "min_interval_seconds", None)
    return int(own if own is not None else (default or 0))


def beat_sources(
    sources: Any,
    state: Any,
    *,
    queue: Any = None,
    now: Any = None,
    default_interval: int | None = None,
) -> list[Any]:
    """The sources a scheduler beat syncs: running by themselves, due, and not giving way.

    A paced source (``sync.min_interval_seconds``) runs at most that often; one
    that yields (``sync.yield_to_content``) is held while another source's sync
    is running or queued, until it has waited ``MAX_DEFERRAL_INTERVALS``
    intervals. Paced sources come last, so a beat's content syncs are published
    -- and claimed -- first. Decided before this beat publishes anything, so a
    beat never yields to its own work.
    """

    candidates = [s for s in sources if runs_automatically(s)]
    paced = {s.name for s in candidates if _paced(s, default_interval)}
    if not paced:
        return candidates
    now = now or datetime.now(UTC)
    busy: bool | None = None
    due = []
    for source in candidates:
        if source.name not in paced:
            due.append(source)
            continue
        interval = interval_of(source, default_interval)
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
    # What gives way goes last, so a beat's content is published first.
    return sorted(due, key=lambda s: bool(getattr(s.sync, "yield_to_content", False)))


def _paced(source: Any, default: int | None = None) -> bool:
    return bool(interval_of(source, default) or getattr(source.sync, "yield_to_content", False))


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


def startup_sources(sources: Any, config: Any = None) -> list[str]:
    """The names of ``sources`` a boot syncs: ``on_startup`` and running by itself.

    None when the region's ``sync.scheduler.startup_sync`` is off: then a boot
    syncs nothing and every source waits for its schedule -- what a region
    that scales to zero wants, since each wake would otherwise re-sync all of
    it.
    """

    scheduler = getattr(getattr(config, "sync", None), "scheduler", None)
    if scheduler is not None and getattr(scheduler, "startup_sync", True) is False:
        return []
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
