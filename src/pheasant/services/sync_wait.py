"""Wait for a source's sync to finish, and say how it went.

On a role-split region a write only *publishes* a sync: `register_source`,
`edit_source_urls` and `sync_source` answer at once, the job they return reads
``succeeded`` as soon as the task is queued, and an indexer in another process
does the work some time later. An agent that took that answer as "indexed"
searched, composed and graded over content that was not there yet -- and a
sync that did run could read 1 of 80 pages and still be called healthy, with
the reason recorded in a `sync_events` row nothing ever read.

This is the other half: one call that waits, server side and bounded, until
nothing is outstanding for the source, and answers with what the last sync
did. Built from state alone -- the queue, the ``sync_runs`` heartbeat, the
source row and its last event -- so it reads the same on an api replica, an
indexer and a single container, and over both transports.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest, SourceNotFound
from pheasant.sync.queue import INFLIGHT, PENDING

#: The longest one call waits. Under the request timeouts between an agent
#: and a region (proxies, MCP clients); a longer wait is another call.
MAX_WAIT_SECONDS = 55.0
DEFAULT_WAIT_SECONDS = 30.0
#: How often the wait looks again. Each look is a handful of indexed reads.
POLL_SECONDS = 2.0
#: Unreadable items quoted in an outcome; the event keeps up to 25.
UNREADABLE_SAMPLE = 5
#: How long "nothing outstanding" must hold before it is believed. A sync
#: started in this process takes a moment to write its heartbeat row.
NOT_STARTED_GRACE_SECONDS = 6.0


@dataclass(frozen=True)
class WaitRequest:
    source_name: str
    knowledge_base: str | None = None
    #: An ISO timestamp the sync must finish after: the source's
    #: ``last_indexed_at`` before the write, or when the write was made.
    #: Without it, "nothing outstanding" is enough.
    since: str | None = None
    timeout_seconds: float = DEFAULT_WAIT_SECONDS


def wait_for_sync(
    context: ServiceContext,
    request: WaitRequest,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Wait until ``request.source_name`` has nothing outstanding, or time runs out.

    ``state`` is ``finished`` (and ``last_sync`` says how), ``running``,
    ``queued`` (published, not claimed), or ``not_started`` (nothing
    outstanding and no sync finished since ``since``: start one).
    """

    kb_id = context.knowledge_base(request.knowledge_base)
    name = str(request.source_name or "").strip()
    if not name or context.state.get_source(name) is None or context.state.source_removed(name):
        raise SourceNotFound(name or "(no source named)")
    try:
        budget = float(request.timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest("timeout_seconds must be a number of seconds") from exc
    budget = max(0.0, min(budget, MAX_WAIT_SECONDS))
    since = _instant(request.since) if request.since else None

    started = clock()
    while True:
        observed = _observe(context, kb_id, name, since)
        waited = clock() - started
        settled = observed["state"] == "finished" or (
            observed["state"] == "not_started" and waited >= min(budget, NOT_STARTED_GRACE_SECONDS)
        )
        if settled or waited >= budget:
            return _outcome(context, observed, waited)
        sleep(min(POLL_SECONDS, max(0.0, budget - waited)))


def _outcome(context: ServiceContext, observed: dict[str, Any], waited: float) -> dict[str, Any]:
    from pheasant.sync.health import scored_status, source_health

    name = observed["source_name"]
    finished = observed["state"] == "finished"
    health = source_health(context.state, context.state.get_source(name) or {"name": name})
    return {
        **observed,
        "status": scored_status(observed["status"], health),
        "health": health,
        "since": observed["since"].isoformat() if observed["since"] else None,
        "documents": _documents(context, name),
        "last_sync": last_sync(context.state, name, health=health) if finished else None,
        "waited_seconds": round(waited, 1),
        "timed_out": observed["state"] in {"running", "queued"},
        "next": _next(observed["state"]),
    }


def last_sync(
    state: Any, source_name: str, *, health: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """What the source's most recent sync did, from its event; ``None`` if none ran.

    ``status`` is the health score's for a sync that finished reading, so an
    event recorded as ``degraded`` for 4 dead links among 17,000 pages reads
    as what it is, and ``health`` carries the percentage (`sync/health.py`).
    """

    from pheasant.sync.health import scored_status, source_health

    events = state.list_sync_events(source_name, limit=1)
    if not events:
        return None
    event = events[0]
    details = event.get("details") or {}
    unreadable = details.get("unreadable") or []
    if health is None:
        health = source_health(state, state.get_source(source_name) or {"name": source_name})
    return {
        "status": scored_status(event.get("status"), health),
        "health": health,
        "mode": details.get("mode"),
        "started_at": event.get("started_at"),
        "finished_at": event.get("finished_at"),
        "fetched": details.get("fetched"),
        "indexed_artifacts": details.get("indexed_artifacts"),
        "unchanged_artifacts": details.get("skipped_artifacts"),
        "removed_artifacts": details.get("removed_artifacts", 0),
        "unreadable_items": int(details.get("unreadable_items") or len(unreadable)),
        # The items still counted against the source: a removed URL or an
        # archived document is no longer one of them.
        "unreadable": health["unreadable_sample"] if health else unreadable[:UNREADABLE_SAMPLE],
    }


def _observe(
    context: ServiceContext, kb_id: str, name: str, since: datetime | None
) -> dict[str, Any]:
    from pheasant.services.index_queue import queue_status
    from pheasant.sync.cancellation import running

    row = context.state.get_source(name) or {}
    last_indexed_at = row.get("last_indexed_at")
    queue = queue_status(context, kb_id, limit=500)
    queued = [task for task in queue["tasks"] if task["source"] == name and task["state"] != "dead"]
    unlisted = queue["listing"] == "unavailable" and _depth(queue) > 0
    run = running(context.state, name)
    indexed_at = _instant(last_indexed_at, strict=False) if last_indexed_at else None
    finished_since = indexed_at is not None and (since is None or indexed_at > since)
    if run:
        state = "running"
    elif queued or (unlisted and not finished_since):
        state = "queued"
    elif finished_since:
        state = "finished"
    else:
        state = "not_started"
    return {
        "knowledge_base": kb_id,
        "source_name": name,
        "state": state,
        "status": row.get("last_status"),
        "last_indexed_at": last_indexed_at,
        "since": since,
        "running_since": run.get("started_at") if run else None,
        "queued_tasks": [
            {key: task[key] for key in ("task_id", "state", "waiting_seconds", "attempts")}
            for task in queued
        ],
        "queue_depth": _depth(queue) if queue["listing"] == "unavailable" else None,
    }


def _depth(queue: dict[str, Any]) -> int:
    """Work waiting or in hand, region-wide: a backend that cannot list its
    tasks cannot say whose they are."""

    depth = queue.get("depth") or {}
    return sum(int(depth.get(key) or 0) for key in (PENDING, INFLIGHT))


def _instant(value: Any, *, strict: bool = True) -> datetime | None:
    """An ISO timestamp as an aware UTC instant (``Z``, an offset, or naive = UTC)."""

    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        if not strict:
            return None
        raise InvalidRequest(
            f"since must be an ISO timestamp such as 2026-11-06T15:10:11Z, not {value!r}"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _documents(context: ServiceContext, name: str) -> int:
    rows = context.state.rows("SELECT COUNT(*) AS n FROM artifacts WHERE source_id=?", (name,))
    return int(rows[0]["n"]) if rows else 0


def _next(state: str) -> str:
    return {
        "finished": (
            "Compare documents with what you added; last_sync.unreadable names items "
            "the sync could not read."
        ),
        "running": "An indexer is working on it: call wait_for_sync again.",
        "queued": "Published and waiting for an indexer: call wait_for_sync again.",
        "not_started": (
            "Nothing is running or queued for this source and no sync finished since "
            "`since`: start one with sync_source."
        ),
    }[state]
