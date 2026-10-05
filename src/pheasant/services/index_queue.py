"""The index queue as a caller sees it: what is waiting, and for what.

On a role-split region a sync is not run where it is requested. An ``api``
replica — and MCP's ``sync_source`` on a fleet — *publishes* a task, and an
indexer in another process claims it. Between the two the work exists only as
a row, and every surface used to report that interval the same way it reports
"nothing happened": the HTTP route answered ``status: queued`` and the UI's
jobs tray, which reads the in-process registry, showed nothing at all. A
person who pressed *Sync* saw no work; an agent whose ingest barrier was
waiting on an unclaimed task saw ``still_accepted`` hold and could not tell a
busy indexer from a missing one.

This operation names that interval. Each outstanding task carries one of five
states, decided by its row and the clock and nothing else:

* ``awaiting_claim`` — published, visible, and no indexer has taken it. The
  pre-claim state. Its ``waiting_seconds`` is the number to watch: a value
  that only grows means no indexer is draining this queue.
* ``retry_scheduled`` — an indexer gave it back (``nack``) and it becomes
  claimable again at ``visible_at``.
* ``claimed`` — an indexer holds it and is heartbeating the claim.
* ``claim_lapsed`` — it was claimed, the heartbeat stopped, and it will be
  redelivered. Usually a killed indexer.
* ``dead`` — out of attempts. ``pheasant queue requeue-dead`` replays it.

Read-only: nothing here claims, acks or touches a row, so polling it cannot
perturb the claim race `sync/queue.py` argues for. A backend that can count
its backlog but not list it (JetStream) reports ``listing: "unavailable"``
with its depth, never an empty list — an empty list would read as "nothing
waiting" while three syncs sit in the stream.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pheasant.services import ServiceContext
from pheasant.sync.queue import DEAD, INFLIGHT, PENDING, QueueUnavailable, queue_from_config

#: The states a caller can act on, in the order a task normally passes them.
STATES = ("awaiting_claim", "retry_scheduled", "claimed", "claim_lapsed", "dead")


def queue_status(
    context: ServiceContext,
    knowledge_base: str | None,
    *,
    limit: int = 50,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Outstanding index tasks, oldest first, each with its pre-claim state."""

    kb_id = context.knowledge_base(knowledge_base)
    settings = getattr(context.config.sync, "queue", None)
    backend = str(getattr(settings, "backend", "local") or "local").lower()
    queue = queue_from_config(context.config, context.state)
    if queue is None:
        # Not a degraded answer: with the queue off every sync runs in the
        # process that was asked, so there is no interval to report.
        return {
            "knowledge_base": kb_id,
            "enabled": False,
            "backend": None,
            "listing": "not_applicable",
            "depth": None,
            "tasks": [],
            "counts": dict.fromkeys(STATES, 0),
        }
    clock = now or datetime.now(UTC)
    try:
        try:
            depth: dict[str, int] | None = queue.depth()
        except QueueUnavailable:
            depth = None
        rows = queue.outstanding(limit=max(1, min(int(limit), 500)))
    finally:
        queue.close()
    tasks = [] if rows is None else [_task(row, clock) for row in rows]
    position = 0
    for task in tasks:
        if task["state"] == "awaiting_claim":
            position += 1
            task["position"] = position
    counts = dict.fromkeys(STATES, 0)
    for task in tasks:
        counts[task["state"]] += 1
    return {
        "knowledge_base": kb_id,
        "enabled": True,
        "backend": backend,
        "listing": "unavailable" if rows is None else "complete",
        "depth": depth,
        "tasks": tasks,
        "counts": counts,
    }


def _task(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    status = str(row.get("status") or "")
    visible_at = _parse(row.get("visible_at"))
    enqueued_at = _parse(row.get("enqueued_at"))
    visible = visible_at is None or visible_at <= now
    if status == DEAD:
        state = "dead"
    elif status == INFLIGHT:
        state = "claim_lapsed" if visible else "claimed"
    elif status == PENDING:
        state = "awaiting_claim" if visible else "retry_scheduled"
    else:  # pragma: no cover - `outstanding` selects only the three above
        state = "dead"
    owner = row.get("owner")
    return {
        "task_id": str(row.get("id")),
        "source": str(row.get("source_id") or ""),
        "mode": str(row.get("mode") or ""),
        "state": state,
        "enqueued_at": row.get("enqueued_at"),
        "visible_at": row.get("visible_at"),
        "waiting_seconds": (
            round(max(0.0, (now - enqueued_at).total_seconds()), 1) if enqueued_at else None
        ),
        # `host:pid:uuid` — the host is what a person recognises.
        "claimed_by": str(owner).split(":", 1)[0] if owner and state == "claimed" else None,
        "attempts": int(row.get("attempts") or 0),
        "max_attempts": int(row.get("max_attempts") or 0),
        "last_error": row.get("last_error"),
        "position": None,
    }


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
