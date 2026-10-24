"""Stop a source's running sync: the operation behind ``cancel_sync``.

MCP ``cancel_sync`` and ``POST /sources/{name}/cancel`` call :func:`cancel_sync`
and nothing else. The request becomes a column on the source's ``sync_runs``
row, which the running sync reads between items (`sync/cancellation.py`), so a
cancel lands whichever process is doing the work -- this one, an indexer that
claimed a published task, or the isolated child a ``role: all`` server indexes
in. That is why it answers ``cancel_requested`` rather than ``cancelled``: the
sync stops at its next item, a moment later, in a process this call may not be
able to see. ``get_sync_status`` / ``list_jobs`` report the stop when it lands.

A cancelled sync keeps every item it committed, and the next incremental sync
picks up where it stopped. A source with no sync running is refused
(``SYNC_NOT_RUNNING``) rather than remembered, so a cancel can never stop a
sync somebody starts later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pheasant.services import ServiceContext
from pheasant.services.errors import SourceNotFound, SyncNotRunning
from pheasant.sync.cancellation import request_cancel


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
    """Ask the running sync of one source to stop at its next item."""

    kb_id = context.knowledge_base(request.knowledge_base)
    name = str(request.source_name or "").strip()
    if not name or not _registered(context, name):
        raise SourceNotFound(name or "(no source named)")
    accepted = request_cancel(
        context.state,
        name,
        requested_by=request.requested_by or "a caller",
        reason=str(request.reason or "").strip(),
    )
    if accepted is None:
        raise SyncNotRunning(name)
    return {
        "knowledge_base": kb_id,
        "source_name": name,
        "status": "cancel_requested",
        # False when somebody had already asked: the run keeps the first
        # request's author and time, and reports those.
        "first_request": bool(accepted.get("first_request")),
        "requested_by": accepted.get("cancel_requested_by"),
        "requested_at": accepted.get("cancel_requested_at"),
        "reason": accepted.get("cancel_reason"),
        "run": {
            "run_id": accepted.get("run_id"),
            "owner": accepted.get("owner"),
            "started_at": accepted.get("started_at"),
        },
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
