"""Every source's sync pipeline, over HTTP: the Syncs page's API.

One router per plane, as `api/app.py`'s ceiling comment asks. Each handler
parses the request and calls `services.sync_pipelines`; the MCP
``*_sync_pipeline`` tools call the same functions.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

from pheasant.services import sync_ledger, sync_pipelines


class PipelineSettings(BaseModel):
    """The settings to change; a field left out is left as it is."""

    schedule: str | None = None
    paused: bool | None = None
    pipeline: bool | None = None
    interval_seconds: int | None = None
    max_refresh_seconds: int | None = None
    max_items_per_run: int | None = None
    min_request_interval_seconds: float | None = None
    #: How often the beat syncs this source; null: the region's default.
    min_interval_seconds: int | None = None
    yield_to_content: bool | None = None
    respect_robots: bool | None = None
    on_startup: bool | None = None
    on_file_change: str | bool | None = None
    on_git_commit: bool | None = None
    #: Start a sync once the change is saved.
    sync_now: bool = False


class Recheck(BaseModel):
    paths: list[str] = Field(default_factory=list)


def register_sync_pipeline_routes(app: FastAPI, *, services: Any) -> None:
    @app.get("/sync/pipelines")
    def list_pipelines() -> dict:
        """One row per source: schedule, last and next run, health, quality, pacing."""

        return sync_pipelines.pipelines(services)

    @app.put("/sync/pipelines/defaults")
    def set_pipeline_defaults(body: dict[str, Any], principal: str | None = None) -> dict:
        """The region's default cadence (``default_source_interval_seconds``; null: every beat).

        Transport adapter. The operation is `services.sync_pipelines.set_defaults`."""

        return sync_pipelines.set_defaults(
            services, body.get("default_source_interval_seconds"), principal
        )

    @app.get("/sync/pipelines/{source_name}")
    def describe_pipeline(source_name: str) -> dict:
        """One pipeline in depth: stages, recent runs, failures by cause, pages due next."""

        return sync_pipelines.pipeline(services, source_name)

    @app.patch("/sync/pipelines/{source_name}")
    def configure_pipeline(source_name: str, req: PipelineSettings) -> dict:
        """Change a pipeline's settings (only the fields sent)."""

        settings = req.model_dump(exclude_unset=True, exclude={"sync_now"})
        return sync_pipelines.configure(
            services,
            sync_pipelines.PipelineChange(source_name, settings=settings, sync=req.sync_now),
        )

    @app.delete("/sync/pipelines/{source_name}/schedule")
    def delete_schedule(source_name: str) -> dict:
        """Manual runs only from now on; the pipeline stays."""

        return sync_pipelines.delete(services, source_name, "schedule")

    @app.delete("/sync/pipelines/{source_name}")
    def delete_pipeline(source_name: str) -> dict:
        """Nothing syncs the source; its indexed documents stay until it is removed."""

        return sync_pipelines.delete(services, source_name, "pipeline")

    @app.post("/sync/pipelines/{source_name}/restore")
    def restore_pipeline(source_name: str) -> dict:
        """A deleted pipeline back, on its automatic schedule."""

        return sync_pipelines.restore(services, source_name)

    @app.get("/sync/pipelines/{source_name}/documents")
    def pipeline_documents(  # noqa: PLR0913 - one argument per filter
        source_name: str,
        state: str = "all",
        path_contains: str | None = None,
        limit: int = 100,
        offset: int = 0,
        principal: str | None = None,
        principal_groups: list[str] | None = Query(default=None),
    ) -> dict:
        """The document ledger: last read, last changed, next check, quality."""

        return sync_ledger.documents(
            services,
            sync_ledger.DocumentsRequest(
                source_name=source_name,
                state=state,
                path_contains=path_contains,
                limit=limit,
                offset=offset,
                principal=principal,
                principal_groups=principal_groups,
            ),
        )

    @app.get("/sync/pipelines/{source_name}/documents/detail")
    def pipeline_document(
        source_name: str,
        path: str,
        principal: str | None = None,
        principal_groups: list[str] | None = Query(default=None),
    ) -> dict:
        """One document's schedule, quality and the runs that could not read it."""

        return sync_ledger.document(services, source_name, path, None, principal, principal_groups)

    @app.post("/sync/pipelines/{source_name}/documents/recheck")
    def recheck_documents(source_name: str, req: Recheck) -> dict:
        """Make these pages due now and start a sync."""

        return sync_ledger.recheck(services, source_name, req.paths)
