"""Stopping a running sync, and waiting for one to finish, over MCP.

A mixin plus a registration function, like ``source_url_tools.py``. Adapters
over `services.sync_control` (``POST /sources/{name}/cancel``) and
`services.sync_wait` (``GET /sources/{name}/wait``).
"""

from __future__ import annotations

from typing import Any


class SyncControlTools:
    """``services.sync_control`` and ``services.sync_wait`` adapters. Parse, call, return."""

    services: Any

    def _require_knowledge_base(self, knowledge_base: str | None) -> None:  # pragma: no cover
        raise NotImplementedError

    def cancel_sync(
        self,
        knowledge_base: str,
        source_name: str,
        reason: str | None = None,
        requested_by: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import sync_control

        return sync_control.cancel_sync(
            self.services,
            sync_control.CancelSyncRequest(
                source_name=source_name,
                knowledge_base=knowledge_base,
                requested_by=requested_by or "mcp",
                reason=reason or "",
            ),
        )

    def wait_for_sync(
        self,
        knowledge_base: str,
        source_name: str,
        since: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import sync_wait

        return sync_wait.wait_for_sync(
            self.services,
            sync_wait.WaitRequest(
                source_name=source_name,
                knowledge_base=knowledge_base,
                since=since,
                timeout_seconds=timeout_seconds,
            ),
        )


def register_sync_control_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    @mcp.tool()
    @anticipated
    def cancel_sync(
        knowledge_base: str,
        source_name: str,
        reason: str | None = None,
        requested_by: str | None = None,
    ) -> dict:
        """Stop the sync of one source that is running now.

        The sync stops at its next item, which is usually within a second or
        two, so this answers ``status: "cancel_requested"``; get_sync_status
        and list_jobs show it as cancelled once it has stopped. It works
        whichever process is running the sync, including an indexer that
        claimed a queued task. Everything indexed before the stop is kept, and
        the next incremental sync (sync_source) carries on from there. Refused
        with SYNC_NOT_RUNNING when no sync of the source is running, including
        one still waiting in the index queue (get_index_queue): cancel it once
        it has started. ``reason`` and ``requested_by`` are recorded with the
        cancel.
        """

        return tools.cancel_sync(knowledge_base, source_name, reason, requested_by)

    @mcp.tool()
    @anticipated
    def wait_for_sync(
        knowledge_base: str,
        source_name: str,
        since: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict:
        """Wait (up to 55 s) for a source's sync to finish, then say how it went.

        Call after register_source, edit_source_urls, submit_documents +
        sync_source, or sync_source: on a fleet those only publish the sync,
        and a job can read ``succeeded`` before anything is indexed. Pass
        ``since`` -- the source's ``last_indexed_at`` from describe_source
        before the write, or the time you made it -- so a sync that finished
        earlier does not count.

        ``state`` is ``finished``, ``running`` or ``queued`` (call again while
        ``timed_out`` is true), or ``not_started`` (start one with
        sync_source). When finished, compare ``documents`` with what you added:
        ``last_sync.status`` is ``degraded`` when the sync could not read some
        items, and ``last_sync.unreadable`` names them with the error.
        """

        return tools.wait_for_sync(knowledge_base, source_name, since, timeout_seconds)
