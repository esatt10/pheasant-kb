"""Stopping a running sync, over MCP.

A mixin plus a registration function, like ``source_url_tools.py``. One
adapter over `services.sync_control`, the operation behind ``POST
/sources/{name}/cancel``.
"""

from __future__ import annotations

from typing import Any


class SyncControlTools:
    """``services.sync_control`` adapter. Parse, call, return."""

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
