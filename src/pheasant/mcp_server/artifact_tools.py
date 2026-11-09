"""Kept artifacts over MCP, and the view that shows one in an MCP host.

A mixin plus a registration function, like ``region_tools.py``: adapters
over `services.artifacts`, the operations behind ``/artifacts`` too. Every
composed answer is kept as an artifact (``compose_artifact``'s result names
it as ``artifact_id``), and any content can be saved as a report.

``get_artifact`` declares ``ui://pheasant/artifact-view.html``, an MCP App
view that shows the artifact as pheasant shows it -- file tabs, each file's
source in pheasant's colours or drawn as what it is (an HTML report runs,
Markdown renders, an SVG is a picture), the whole artifact as one report --
by asking ``view_artifact`` for the rendered document
(`services/artifact_render.py`, the same renderer the web UI and the
downloads use). It shows each document in a nested sandboxed frame, so
nothing an artifact holds runs in the view itself.
"""

from __future__ import annotations

import base64
from functools import cache
from pathlib import Path
from typing import Any

ARTIFACT_APP_URI = "ui://pheasant/artifact-view.html"
ARTIFACT_APP_MIME = "text/html;profile=mcp-app"
ARTIFACT_APP_PATH = Path(__file__).parent / "apps" / "artifact_view.html"


@cache
def artifact_app_html() -> str:
    return ARTIFACT_APP_PATH.read_text(encoding="utf-8")


class ArtifactTools:
    """``services.artifacts`` adapters. Parse, call, return."""

    services: Any

    def _require_knowledge_base(self, knowledge_base: str | None) -> None:  # pragma: no cover
        raise NotImplementedError

    def list_artifacts(
        self,
        knowledge_base: str,
        limit: int = 50,
        owner: str | None = None,
        principal: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import artifacts

        return artifacts.list_artifacts(self.services, principal, limit=limit, owner_name=owner)

    def get_artifact(
        self, knowledge_base: str, artifact_id: str, principal: str | None = None
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import artifacts

        return artifacts.get(self.services, artifact_id, principal)

    def save_artifact(
        self,
        knowledge_base: str,
        title: str | None = None,
        markdown: str | None = None,
        files: list[dict] | None = None,
        summary: str | None = None,
        kind: str = "report",
        principal: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import artifacts

        return artifacts.save(
            self.services,
            artifacts.SaveRequest(
                title=title,
                kind=kind,
                markdown=markdown,
                files=files,
                summary=summary,
                principal=principal,
            ),
        )

    def view_artifact(
        self,
        knowledge_base: str,
        artifact_id: str,
        file: str | None = None,
        mode: str = "code",
        theme: str | None = None,
        principal: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import artifacts

        rendered = artifacts.view(
            self.services, artifact_id, principal, file=file, mode=mode, theme=theme
        )
        return {"html": rendered.content, "filename": rendered.filename}

    def export_artifact(
        self,
        knowledge_base: str,
        artifact_id: str,
        format: str = "markdown",  # noqa: A002 - the public parameter's name
        file: str | None = None,
        principal: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import artifacts

        rendered = artifacts.export(self.services, artifact_id, principal, format=format, file=file)
        content = rendered.content
        if isinstance(content, bytes):
            return {
                "filename": rendered.filename,
                "media_type": rendered.media_type,
                "base64": base64.b64encode(content).decode("ascii"),
            }
        return {
            "filename": rendered.filename,
            "media_type": rendered.media_type,
            "content": content,
        }

    def delete_artifact(
        self, knowledge_base: str, artifact_id: str, principal: str | None = None
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import artifacts

        return artifacts.delete(self.services, artifact_id, principal)


def register_artifact_tools(
    mcp: Any, tools: Any, anticipated: Any, anticipated_resource: Any
) -> None:
    @mcp.resource(
        ARTIFACT_APP_URI,
        name="pheasant-artifact-view",
        description="Shows a kept pheasant artifact: its files, rendered or as source.",
        mime_type=ARTIFACT_APP_MIME,
        meta={"ui": {"prefersBorder": True, "csp": {"connectDomains": [], "resourceDomains": []}}},
    )
    @anticipated_resource
    def artifact_view() -> str:
        """pheasant's artifact viewer (text/html;profile=mcp-app)."""

        return artifact_app_html()

    @mcp.tool()
    @anticipated
    def list_artifacts(
        knowledge_base: str, limit: int = 50, owner: str | None = None, principal: str | None = None
    ) -> dict:
        """List the artifacts this region keeps, newest first (``owner`` for one person's).

        Every answer composed with compose_artifact (or ask_knowledge_base with
        intent "compose") is kept, and save_artifact keeps a report. They are
        knowledge like any other: each lands in the "artifacts" source at
        folder <owner>/<yyyy>/<mm>/<title>--<id>/ (its files, a README.md
        whose Sources link the documents it cites, and report.html for a
        report with a diagram), so search_context finds them, browse_source
        lists them, the graph links each to its sources, and archive_documents
        removes them. Each entry is {id, kind ("files" or "report"), title,
        created_at, owner, source, folder, files: [{name, language,
        previewable}], has_visual}, without contents: get_artifact has those.
        """

        return tools.list_artifacts(knowledge_base, limit=limit, owner=owner, principal=principal)

    @mcp.tool(meta={"ui": {"resourceUri": ARTIFACT_APP_URI}, "ui/resourceUri": ARTIFACT_APP_URI})
    @anticipated
    def get_artifact(knowledge_base: str, artifact_id: str, principal: str | None = None) -> dict:
        """One kept artifact with its contents, shown in pheasant's artifact view.

        Returns its listing entry plus payload: {question, summary, files:
        [{name, language, content}], not_covered, citations, answer (a
        report's text)}. A host that renders MCP Apps shows it with file tabs,
        each file as pheasant-coloured source or drawn (HTML runs sandboxed,
        Markdown renders, SVG is a picture), and the whole artifact as one
        report. Write the files out yourself from payload.files.
        """

        return tools.get_artifact(knowledge_base, artifact_id, principal=principal)

    @mcp.tool()
    @anticipated
    def save_artifact(
        knowledge_base: str,
        title: str | None = None,
        markdown: str | None = None,
        files: list[dict] | None = None,
        summary: str | None = None,
        kind: str = "report",
        principal: str | None = None,
    ) -> dict:
        """Keep a report or a set of files as an artifact, rendered in pheasant's style.

        markdown is the report's text (raw HTML in it is not rendered); files
        are [{name, language, content}] -- an .html file is shown running, in
        a sandbox that cannot reach the network. kind is "report" or "files".
        Returns the artifact; view it with get_artifact, export it with
        export_artifact. Composed answers are kept already: no need to save
        those.
        """

        return tools.save_artifact(
            knowledge_base,
            title=title,
            markdown=markdown,
            files=files,
            summary=summary,
            kind=kind,
            principal=principal,
        )

    @mcp.tool()
    @anticipated
    def view_artifact(
        knowledge_base: str,
        artifact_id: str,
        file: str | None = None,
        mode: str = "code",
        theme: str | None = None,
        principal: str | None = None,
    ) -> dict:
        """The artifact (or one file) as a self-contained, pheasant-styled HTML page.

        Without file: the whole artifact as one report. With file: that file,
        mode "code" (its source, highlighted) or "preview" (an .html file
        running, .md rendered, .svg as a picture). theme is "light" or "dark",
        or omitted to follow the reader's system. Returns {html, filename}.
        The page fetches nothing and may run only its own inline scripts.
        """

        return tools.view_artifact(
            knowledge_base, artifact_id, file=file, mode=mode, theme=theme, principal=principal
        )

    @mcp.tool()
    @anticipated
    def export_artifact(
        knowledge_base: str,
        artifact_id: str,
        format: str = "markdown",  # noqa: A002 - the public parameter's name
        file: str | None = None,
        principal: str | None = None,
    ) -> dict:
        """The artifact as a file to save: format "markdown", "html" or "zip".

        markdown and html come back as {filename, media_type, content}; zip
        (every file at its path, a README and the HTML report) as {filename,
        media_type, base64}. With file, that one file exactly as written.
        """

        return tools.export_artifact(
            knowledge_base, artifact_id, format=format, file=file, principal=principal
        )

    @mcp.tool()
    @anticipated
    def delete_artifact(
        knowledge_base: str, artifact_id: str, principal: str | None = None
    ) -> dict:
        """Delete a kept artifact and remove its files from the index. It cannot be undone."""

        return tools.delete_artifact(knowledge_base, artifact_id, principal=principal)
