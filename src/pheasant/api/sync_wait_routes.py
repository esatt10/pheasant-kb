"""Wait for a source's sync, over HTTP.

One router per plane, as `api/app.py`'s ceiling comment asks. The handler
parses the query and calls `services.sync_wait`; MCP's ``wait_for_sync`` calls
the same function. A sync handler, so the bounded wait runs on a worker thread
rather than on the event loop.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from pheasant.services import sync_wait


def register_sync_wait_routes(app: FastAPI, *, services: Any) -> None:
    @app.get("/sources/{source_name}/wait")
    def wait_for_sync(
        source_name: str,
        since: str | None = None,
        timeout_seconds: float = sync_wait.DEFAULT_WAIT_SECONDS,
    ) -> dict:
        """Wait (up to 55 s) until nothing is outstanding for the source.

        ``state`` is ``finished`` (``last_sync`` says how), ``running``,
        ``queued`` or ``not_started``; call again while ``timed_out``."""

        return sync_wait.wait_for_sync(
            services,
            sync_wait.WaitRequest(
                source_name=source_name, since=since, timeout_seconds=timeout_seconds
            ),
        )
