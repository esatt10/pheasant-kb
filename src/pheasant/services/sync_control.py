"""Stop a source's sync, running or still queued: the operation behind ``cancel_sync``.

MCP ``cancel_sync`` and ``POST /sources/{name}/cancel`` call :func:`cancel_sync`
and nothing else. The request becomes a column on the source's ``sync_runs``
row, which the running sync reads between items (`sync/cancellation.py`), so a
cancel lands whichever process is doing the work -- this one, an indexer that
claimed a published task, or the isolated child a ``role: all`` server indexes
in. That is why it answers ``cancel_requested`` rather than ``cancelled``: the
sync stops at its next item, a moment later, in a process this call may not be
able to see. ``get_sync_status`` / ``list_jobs`` report the stop when it lands.

A sync published to the index queue and not yet started is reached through
its ledger row (`sync/request_ledger.py`): it leaves the queue's listing at
once and is acknowledged, not run, when an indexer claims it -- on any
delivery, including the one after an indexer died. ``stage`` says which the
cancel caught: ``running`` (a live run, possibly with queued ones behind it)
or ``queued``.

A cancelled sync keeps every item it committed, and the next incremental sync
picks up where it stopped. A source with nothing running or waiting is
refused (``SYNC_NOT_RUNNING``) rather than remembered, so a cancel can never
stop a sync somebody starts later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pheasant.services import ServiceContext
from pheasant.services.errors import SourceNotFound, SyncNotRunning
from pheasant.sync.cancellation import cancel_source


@dataclass(frozen=True)
class CancelSyncRequest:
    """Which source's sync to stop, and who is asking."""

    source_name: str
    knowledge_base: str | None = None
    #: Recorded on the run and in the ``sync.cancelled`` event, because "who
    #: stopped this" is the first question about a sync that did not finish.
    requested_by: str = ""
    reason: str = ""


def cancel_sync(context: ServiceContext, request: CancelSyncRequest) -> dict[str, Any]:
    """Stop one source's sync: a running one at its next check, a queued one before it starts."""

    from pheasant.security.authorization import require_permission

    require_permission("sources.manage")
    kb_id = context.knowledge_base(request.knowledge_base)
    name = str(request.source_name or "").strip()
    if not name or not _registered(context, name):
        raise SourceNotFound(name or "(no source named)")
    reached = cancel_source(
        context.state,
        name,
        requested_by=request.requested_by or "a caller",
        reason=str(request.reason or "").strip(),
    )
    if reached is None:
        raise SyncNotRunning(name)
    run = reached["run"]
    queued = reached["requests"]
    # The author and time a caller sees are the first request's: a run (or a
    # request) that was already asked keeps them, because they answer "who
    # stopped this".
    first = run or (queued[0] if queued else {})
    return {
        "knowledge_base": kb_id,
        "source_name": name,
        "status": "cancel_requested",
        "stage": "running" if run else "queued",
        # False when somebody had already asked.
        "first_request": bool(reached["first_request"]),
        "requested_by": first.get("cancel_requested_by"),
        "requested_at": first.get("cancel_requested_at"),
        "reason": first.get("cancel_reason"),
        "run": (
            {
                "run_id": run.get("run_id"),
                "owner": run.get("owner"),
                "started_at": run.get("started_at"),
            }
            if run
            else None
        ),
        # Every queued request the cancel reached: ``cancelled`` ones never
        # start; a ``claimed`` one is the run above, or one an indexer holds
        # and will stop at its first check.
        "queued": [
            {
                "request_id": row.get("id"),
                "task_id": row.get("task_id"),
                "mode": row.get("mode"),
                "requested_at": row.get("requested_at"),
                "state": row.get("state"),
            }
            for row in queued
        ],
    }


def _registered(context: ServiceContext, name: str) -> bool:
    """Whether ``name`` is a source of this region, configured or registered at runtime.

    The state store first: a source registered from another replica's UI or
    MCP call is a row here before it is in this process's config.
    """

    if context.state.source_removed(name):
        return False
    if context.state.get_source(name):
        return True
    return any(source.name == name for source in context.config.sources)
