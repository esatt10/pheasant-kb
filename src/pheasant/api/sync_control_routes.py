"""Stopping a running sync, over HTTP.

One router per plane, as `api/app.py`'s ceiling comment asks. The handler
parses the request and calls `services.sync_control`; MCP's ``cancel_sync``
calls the same function.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from pheasant.services import sync_control


class CancelSyncRequest(BaseModel):
    reason: str | None = None
    requested_by: str | None = None


def register_sync_control_routes(app: FastAPI, *, services: Any) -> None:
    @app.post("/sources/{source_name}/cancel")
    def cancel_sync(source_name: str, req: CancelSyncRequest | None = None) -> dict:
        """Stop the source's sync, running or queued, keeping what it indexed.

        ``status: cancel_requested``, ``stage`` ``running`` or ``queued``: a
        running sync stops a moment later, in whichever process is running it,
        and ``GET /jobs`` and ``GET /sources`` show it as cancelled once it
        has; a queued one never starts. 409 ``SYNC_NOT_RUNNING`` when nothing
        of the source is running or waiting."""

        req = req or CancelSyncRequest()
        return sync_control.cancel_sync(
            services,
            sync_control.CancelSyncRequest(
                source_name=source_name,
                requested_by=req.requested_by or "http",
                reason=req.reason or "",
            ),
        )
