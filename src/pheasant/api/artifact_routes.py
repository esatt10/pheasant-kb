"""Kept artifacts over HTTP: `services.artifacts`, one adapter per operation.

The same operations MCP's artifact tools call. What is HTTP's own is how a
document leaves: ``view`` sends a rendered page with a sandbox policy (opened
directly it runs in an opaque origin, never the API's), and ``export`` sends
a file to save. And the channel: a call is ``ui`` when the web UI says so
(``X-Pheasant-Modality``), ``api`` otherwise.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import BaseModel

from pheasant.services import artifact_lifecycle, artifact_render, artifact_settings
from pheasant.services import artifacts as artifact_service


class SaveArtifactBody(BaseModel):
    """Keep an answer the stream kept (``answer_id``), given content, or a thread."""

    title: str | None = None
    #: ``files`` / ``report`` / ``thread``; unset reads it off the content.
    kind: str | None = None
    answer_id: str | None = None
    markdown: str | None = None
    files: list[dict[str, Any]] | None = None
    summary: str | None = None
    principal: str | None = None
    conversation_id: str | None = None
    #: ``kind: thread``: ``[{question, answer, answer_id?}]``, oldest first.
    turns: list[dict[str, Any]] | None = None
    #: A diagram drawn after the answer, and its own citations.
    visual: dict[str, Any] | None = None
    citations: list[dict[str, Any]] | None = None
    published: bool = True
    replaces: str | None = None
    scope: str = "org"


class SelectionBody(BaseModel):
    """Artifacts by ids, or by a filter; ``dry_run`` says what a delete would take."""

    ids: list[str] | None = None
    owner: str | None = None
    conversation_id: str | None = None
    kind: str | None = None
    before: str | None = None
    dry_run: bool = False
    principal: str | None = None

    def selection(self) -> artifact_lifecycle.Selection:
        return artifact_lifecycle.Selection(
            ids=self.ids,
            owner=self.owner,
            conversation_id=self.conversation_id,
            kind=self.kind,
            before=self.before,
        )


def channel(request: Request) -> str:
    from pheasant.services.artifact_trace import channel_of

    return channel_of(request.headers.get("x-pheasant-modality"), default="api")


def _send(rendered: artifact_service.Rendered, *, attachment: bool) -> Response:
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
    if rendered.media_type.startswith("text/html"):
        headers["Content-Security-Policy"] = artifact_render.VIEW_HEADER_POLICY
    if attachment:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in rendered.filename)
        headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    return Response(content=rendered.content, media_type=rendered.media_type, headers=headers)


def register_artifact_routes(app: FastAPI, *, services: Any) -> None:
    # Retention runs on the scheduler beat wherever this app's process runs
    # one; `cli.py` adds it to the beat (`services/artifact_lifecycle.py`).
    from pheasant.services.job_history import retain

    app.state.scheduler_upkeep = [
        lambda: artifact_lifecycle.run_retention(services),
        lambda: retain(services),
    ]

    from pheasant.api.artifact_record_routes import register_record_routes

    register_record_routes(app, services)

    @app.get("/artifacts")
    def list_artifacts(
        limit: int = 50,
        owner: str | None = None,
        conversation_id: str | None = None,
        kind: str | None = None,
        archived: bool = False,
        principal: str | None = None,
    ) -> dict:
        """Transport adapter. The operation is `services.artifacts.list_artifacts`."""

        return artifact_service.list_artifacts(
            services,
            principal,
            limit=limit,
            owner_name=owner,
            conversation_id=conversation_id,
            kind=kind,
            archived=archived,
        )

    @app.post("/artifacts")
    def save_artifact(body: SaveArtifactBody, request: Request) -> dict:
        """Transport adapter. The operation is `services.artifacts.save`."""

        return artifact_service.save(
            services,
            artifact_service.SaveRequest(
                title=body.title,
                kind=body.kind,
                answer_id=body.answer_id,
                markdown=body.markdown,
                files=body.files,
                summary=body.summary,
                principal=body.principal,
                conversation_id=body.conversation_id,
                channel=channel(request),
                turns=body.turns,
                visual=body.visual,
                citations=body.citations,
                published=body.published,
                replaces=body.replaces,
                scope=body.scope,
            ),
        )

    @app.get("/artifacts/settings")
    def get_artifact_settings() -> dict:
        """Transport adapter. The operation is `services.artifact_settings.describe`."""

        return artifact_settings.describe(services)

    @app.put("/artifacts/settings")
    def configure_artifacts(body: dict[str, Any], principal: str | None = None) -> dict:
        """Transport adapter. The operation is `services.artifact_settings.update`."""

        return artifact_settings.update(services, body, artifact_service.owner(principal))

    @app.post("/artifacts/delete")
    def delete_artifacts(body: SelectionBody) -> dict:
        """Bulk delete (`services.artifact_lifecycle.delete_many`); ``dry_run`` first."""

        return artifact_lifecycle.delete_many(
            services, body.selection(), principal=body.principal, dry_run=body.dry_run
        )

    @app.post("/artifacts/archive")
    def archive_artifacts(body: SelectionBody) -> dict:
        """Transport adapter. The operation is `services.artifact_lifecycle.archive`."""

        return artifact_lifecycle.archive(services, body.selection(), principal=body.principal)

    @app.post("/artifacts/restore")
    def restore_artifacts(body: SelectionBody) -> dict:
        """Transport adapter. The operation is `services.artifact_lifecycle.restore`."""

        return artifact_lifecycle.restore(services, body.selection(), principal=body.principal)

    @app.get("/artifacts/{artifact_id}")
    def get_artifact(artifact_id: str, principal: str | None = None) -> dict:
        """Transport adapter. The operation is `services.artifacts.get`."""

        return artifact_service.get(services, artifact_id, principal)

    @app.delete("/artifacts/{artifact_id}")
    def delete_artifact(artifact_id: str, principal: str | None = None) -> dict:
        """Transport adapter. The operation is `services.artifacts.delete`."""

        return artifact_service.delete(services, artifact_id, principal)

    @app.get("/artifacts/{artifact_id}/view")
    def view_artifact(
        artifact_id: str,
        file: str | None = None,
        mode: str = "code",
        theme: str | None = None,
        principal: str | None = None,
    ) -> Response:
        """The artifact, or one file, as a pheasant-styled page (`services.artifacts.view`)."""

        rendered = artifact_service.view(
            services, artifact_id, principal, file=file, mode=mode, theme=theme
        )
        return _send(rendered, attachment=False)

    @app.get("/artifacts/{artifact_id}/export")
    def export_artifact(
        artifact_id: str,
        format: str = "html",  # noqa: A002 - the public parameter's name
        file: str | None = None,
        principal: str | None = None,
    ) -> Response:
        """A file to save: ``html``, ``markdown`` or ``zip``, or one ``file`` as written."""

        rendered = artifact_service.export(
            services, artifact_id, principal, format=format, file=file
        )
        return _send(rendered, attachment=True)
