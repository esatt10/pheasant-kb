"""The operator's controls over HTTP: ``GET``/``PUT /region/controls``.

One router per plane. Each handler calls `services.region_controls`, which MCP
``get_region_controls`` / ``set_region_controls`` use too. The kill switch
itself is enforced in front of every route (``api/auth.py``), and the spend
limit at every model call (``telemetry/spend.py``); a call refused mid-way is
answered here too, as ``SPEND_LIMIT_REACHED`` rather than a 500.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from pheasant.services import region_controls
from pheasant.telemetry.spend import SpendLimitReached


def register_control_routes(app: FastAPI, *, services: Any) -> None:
    @app.get("/region/controls")
    def get_region_controls() -> dict:
        """The kill switch and this month's model spend against its limit (Admin;
        the amounts are an operator admin's)."""

        return region_controls.describe(services)

    @app.put("/region/controls")
    def set_region_controls(body: dict[str, Any]) -> dict:
        """Suspend or restore access, or set the monthly spend limit (operator
        admin): ``suspended``, ``reason``, ``monthly_spend_limit`` (``null``
        for the deployment's default)."""

        return region_controls.update(services, body)

    @app.exception_handler(SpendLimitReached)
    async def spend_limit(_request: Any, exc: SpendLimitReached) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"detail": str(exc), "code": exc.code, "retryable": exc.retryable},
        )
