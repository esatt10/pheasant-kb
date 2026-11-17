"""Background work over HTTP: exports as jobs, waiting on any job, following one.

One router per plane. ``POST /sources/{name}/exports`` and MCP
``start_source_export`` call ``services.export_jobs.start_export``;
``GET /jobs/{id}/wait`` and MCP ``wait_for_job`` call
``services.job_wait.wait_for_job``. Two routes are this transport's own, on
purpose: ``/download`` sends a finished export's bytes to a browser or
``curl -O`` (an MCP client reads the same documents with ``export_source``),
and ``/events`` streams one job as server-sent events for a reader that keeps a
connection open.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from pheasant.services import export_jobs, job_wait, source_export
from pheasant.services.errors import UnknownJob


class ExportJobBody(BaseModel):
    format: str = "zip"
    path_prefix: str | None = None
    include_chunks: bool = False
    principal: str | None = None
    principal_groups: list[str] = Field(default_factory=list)


def register_job_routes(app: FastAPI, *, services: Any, jobs: Any) -> None:
    @app.post("/sources/{source_name}/exports", status_code=202)
    def start_source_export(source_name: str, body: ExportJobBody | None = None) -> dict:
        """Start exporting a source (or one folder of it) as a background job.

        Idempotent: the same request over unchanged content returns the job
        already running, or the finished one and its file (``reused``).
        """

        body = body or ExportJobBody()
        return export_jobs.start_export(
            services,
            jobs,
            source_export.ExportRequest(
                source_name=source_name,
                format=body.format,
                include_chunks=body.include_chunks,
                principal=body.principal,
                principal_groups=body.principal_groups or None,
                path_prefix=body.path_prefix,
            ),
        )

    @app.get("/jobs/{job_id}/download", response_model=None)
    def download_job_file(job_id: str, part: int = 0) -> FileResponse:
        """A finished export's file (``part`` for a selection past one file)."""

        path, filename, media_type = export_jobs.download(services, jobs, job_id, part)
        return FileResponse(path, media_type=media_type, filename=filename)

    @app.get("/jobs/{job_id}/wait")
    def wait_for_job(job_id: str, timeout_seconds: float = 30.0) -> dict:
        """The job once it finishes, or as it stands after the wait (``timed_out``)."""

        return job_wait.wait_for_job(jobs, job_id, timeout_seconds)

    @app.get("/jobs/{job_id}/events")
    def job_events(job_id: str) -> StreamingResponse:
        """One job as server-sent events: every change, then the terminal record.

        An async generator polling the shared record, for the reason
        ``/jobs/stream`` gives: the job may be running in another process.
        """

        if jobs.get(job_id) is None:
            raise UnknownJob(job_id)

        async def publish():
            last = None
            since_ping = 0.0
            while True:
                job = jobs.get(job_id)
                if job is None:
                    return
                signature = json.dumps(job, sort_keys=True)
                if signature != last:
                    last = signature
                    since_ping = 0.0
                    yield f"data: {json.dumps({'type': 'job', 'job': job})}\n\n"
                    if not job.get("active"):
                        return
                elif since_ping >= 4.0:
                    since_ping = 0.0
                    yield ": ping\n\n"
                await asyncio.sleep(0.5)
                since_ping += 0.5

        return StreamingResponse(
            publish(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
