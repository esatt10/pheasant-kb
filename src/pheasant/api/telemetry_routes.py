"""Authenticated, bounded first-party browser telemetry ingestion."""

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field


class UIEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    action: Literal["navigate", "ask", "artifact.select", "source.select", "pane.select"]
    route: Literal[
        "/", "/graph", "/artifacts", "/evaluation", "/tuning", "/sources", "/syncs", "/config"
    ]
    started_at: datetime
    duration_ms: float = Field(ge=0, le=60_000, allow_inf_nan=False)


class UIBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[UIEvent] = Field(max_length=50)


def register_telemetry_routes(app: FastAPI, config) -> None:
    @app.post("/telemetry/ui", status_code=204, tags=["telemetry"])
    def ui_events(batch: UIBatch) -> Response:
        from pheasant.telemetry.interactions import TRACING

        if not config.observability.ui_events:
            raise HTTPException(404, "UI telemetry is disabled")
        now = datetime.now(UTC)
        for event in batch.events:
            if (
                event.started_at.tzinfo is None
                or abs((now - event.started_at).total_seconds()) > 300
            ):
                raise HTTPException(422, "UI timestamps must be within five minutes")
            if int(event.trace_id, 16) == 0 or int(event.span_id, 16) == 0:
                raise HTTPException(422, "Invalid trace context")
        for event in batch.events:
            TRACING.export_span(
                {
                    "trace_id": event.trace_id,
                    "span_id": event.span_id,
                    "name": "ui." + event.action,
                    "start_time": event.started_at.isoformat(),
                    "end_time": (
                        event.started_at + timedelta(milliseconds=event.duration_ms)
                    ).isoformat(),
                    "attributes": {"pheasant.modality": "ui", "ui.route": event.route},
                },
                ui=True,
            )
        return Response(status_code=204)
