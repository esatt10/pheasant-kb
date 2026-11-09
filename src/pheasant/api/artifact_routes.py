"""Kept artifacts over HTTP: `services.artifacts`, one adapter per operation.

The same operations MCP's artifact tools call. What is HTTP's own is how a
document leaves: ``view`` sends a rendered page with a sandbox policy (opened
directly it runs in an opaque origin, never the API's), and ``export`` sends
a file to save.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel

from pheasant.services import artifact_render
from pheasant.services import artifacts as artifact_service


class SaveArtifactBody(BaseModel):
    """Keep an answer the stream kept (``answer_id``), or given content."""

    title: str | None = None
    kind: str = "report"
    answer_id: str | None = None
    markdown: str | None = None
    files: list[dict[str, Any]] | None = None
    summary: str | None = None
    principal: str | None = None


def _send(rendered: artifact_service.Rendered, *, attachment: bool) -> Response:
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
    if rendered.media_type.startswith("text/html"):
        headers["Content-Security-Policy"] = artifact_render.VIEW_HEADER_POLICY
    if attachment:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in rendered.filename)
        headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    return Response(content=rendered.content, media_type=rendered.media_type, headers=headers)


def register_artifact_routes(app: FastAPI, *, services: Any) -> None:
    @app.get("/artifacts")
    def list_artifacts(limit: int = 50, principal: str | None = None) -> dict:
        """Transport adapter. The operation is `services.artifacts.list_artifacts`."""

        return artifact_service.list_artifacts(services, principal, limit=limit)

    @app.post("/artifacts")
    def save_artifact(body: SaveArtifactBody) -> dict:
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
            ),
        )

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
