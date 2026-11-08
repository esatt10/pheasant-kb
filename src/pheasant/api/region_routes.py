"""Whether the region is awake, over HTTP: ``GET /region/status``.

One router per plane, as `api/app.py`'s ceiling comment asks. The handler
calls `services.region_status`, which MCP ``get_region_status`` and the web
UI's banner use too. While the database is still starting, the same path is
answered by `deployment/wake_gate.py` instead, in the same shape.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from pheasant.services import region_status


def register_region_routes(app: FastAPI, *, services: Any) -> None:
    @app.get("/region/status")
    def region_status_route() -> dict:
        """What is still waking or warming after the region scaled up, if anything."""

        return region_status.region_status(services)
