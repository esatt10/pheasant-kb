"""Kept artifacts over MCP, and the view that shows one in an MCP host.

A mixin plus a registration function, like ``region_tools.py``: adapters
over `services.artifacts`, the operations behind ``/artifacts`` too. Keeping
is opt-in (``save_artifact``, or ``save`` on a question) unless the region's
``auto_save`` is on (``configure_artifacts``); a whole conversation can be
kept as a ``thread``. Every MCP call is recorded as channel ``mcp``.

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
        conversation_id: str | None = None,
        kind: str | None = None,
        archived: bool = False,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import artifacts

        return artifacts.list_artifacts(
            self.services,
            principal,
            limit=limit,
            owner_name=owner,
            conversation_id=conversation_id,
            kind=kind,
            archived=archived,
        )

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
        kind: str | None = None,
        principal: str | None = None,
        conversation_id: str | None = None,
        turns: list[dict] | None = None,
        visual: dict | None = None,
        citations: list[dict] | None = None,
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
                conversation_id=conversation_id,
                channel="mcp",
                turns=turns,
                visual=visual,
                citations=citations,
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

    def artifacts_lifecycle(
        self, knowledge_base: str, action: str, *, dry_run: bool = False, **selection: Any
    ) -> dict:
        """``delete`` / ``archive`` / ``restore`` the artifacts a selection names."""

        self._require_knowledge_base(knowledge_base)
        from pheasant.services import artifact_lifecycle as lifecycle

        principal = selection.pop("principal", None)
        chosen = lifecycle.Selection(**selection)
        if action == "delete":
            return lifecycle.delete_many(
                self.services, chosen, principal=principal, dry_run=dry_run
            )
        operation = lifecycle.archive if action == "archive" else lifecycle.restore
        return operation(self.services, chosen, principal=principal)

    def artifact_settings(
        self, knowledge_base: str, changes: dict | None = None, principal: str | None = None
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import artifact_settings
        from pheasant.services.artifacts import owner

        if not changes:
            return artifact_settings.describe(self.services)
        return artifact_settings.update(self.services, changes, owner(principal))


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
        knowledge_base: str,
        limit: int = 50,
        owner: str | None = None,
        principal: str | None = None,
        conversation_id: str | None = None,
        kind: str | None = None,
        archived: bool = False,
    ) -> dict:
        """List kept artifacts: newest day first, then person, then time.

        owner, conversation_id and kind narrow it; archived=true lists what
        retention archived instead. Kept artifacts are knowledge: each lands in
        the "artifacts" source at <yyyy>/<mm>/<dd>/<owner>/<session>/<title>--<id>/
        (its files, a README.md whose Sources link the documents it cites, and
        report.html for a report with a diagram), indexed on a schedule rather
        than at save time -- index_status is "pending" until it is, then
        "indexed". Each entry is {id, kind ("files", "report" or "thread"),
        title, created_at, owner, conversation_id, channel, folder, files,
        has_visual, index_status, trace: {intent, keywords, accepted,
        degraded, trace_id}}, without contents: get_artifact has those.
        """

        return tools.list_artifacts(
            knowledge_base,
            limit=limit,
            owner=owner,
            principal=principal,
            conversation_id=conversation_id,
            kind=kind,
            archived=archived,
        )

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
        kind: str | None = None,
        principal: str | None = None,
        session: str | None = None,
        turns: list[dict] | None = None,
        visual: dict | None = None,
        citations: list[dict] | None = None,
    ) -> dict:
        """Keep a report, a set of files or a whole conversation as an artifact.

        markdown is the report's text (raw HTML in it is not rendered); files
        are [{name, language, content}] -- an .html file is shown running, in
        a sandbox that cannot reach the network. kind is "report", "files" or
        "thread"; for a thread pass turns: [{question, answer: <the
        ask_knowledge_base result>}], oldest first, and every turn keeps its
        trace and citations. session files it under that conversation.
        Composed answers are kept only if you save them, or ask with
        save=true, unless the region auto-saves (get_artifact_settings).
        visual keeps a diagram create_visual returned (pass its citations too),
        drawn in the artifact's report.html; the artifact view's "Save" does this.
        """

        return tools.save_artifact(
            knowledge_base,
            title=title,
            markdown=markdown,
            files=files,
            summary=summary,
            kind=kind,
            principal=principal,
            conversation_id=session,
            turns=turns,
            visual=visual,
            citations=citations,
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

    @mcp.tool()
    @anticipated
    def delete_artifacts(
        knowledge_base: str,
        ids: list[str] | None = None,
        owner: str | None = None,
        session: str | None = None,
        kind: str | None = None,
        before: str | None = None,
        dry_run: bool = True,
        principal: str | None = None,
    ) -> dict:
        """Delete many artifacts at once: by ids, or by owner / session / kind / before.

        dry_run defaults to true and lists what would go; call again with
        dry_run=false to delete. At most 500 per call. A selection naming
        nothing is refused. Deleting cannot be undone; archive_artifacts can.
        """

        return tools.artifacts_lifecycle(
            knowledge_base,
            "delete",
            dry_run=dry_run,
            ids=ids,
            owner=owner,
            conversation_id=session,
            kind=kind,
            before=before,
            principal=principal,
        )

    @mcp.tool()
    @anticipated
    def archive_artifacts(
        knowledge_base: str,
        ids: list[str] | None = None,
        owner: str | None = None,
        session: str | None = None,
        kind: str | None = None,
        before: str | None = None,
        restore: bool = False,
        principal: str | None = None,
    ) -> dict:
        """Archive artifacts (out of search, kept, restorable), or restore=true to bring them back.

        The selection works as on delete_artifacts. Retention archives the same
        way on its own (get_artifact_settings: retention_days).
        """

        return tools.artifacts_lifecycle(
            knowledge_base,
            "restore" if restore else "archive",
            ids=ids,
            owner=owner,
            conversation_id=session,
            kind=kind,
            before=before,
            principal=principal,
        )

    @mcp.tool()
    @anticipated
    def get_artifact_settings(knowledge_base: str) -> dict:
        """How this region keeps artifacts: auto_save, indexing schedule, retention.

        settings is what is in force, defaults what the config says, changed
        the keys somebody changed (and updated_by, updated_at).
        """

        return tools.artifact_settings(knowledge_base)

    @mcp.tool()
    @anticipated
    def configure_artifacts(
        knowledge_base: str, changes: dict, principal: str | None = None
    ) -> dict:
        """Change how this region keeps artifacts, for every replica at once.

        changes is any of: enabled (bool), auto_save (bool: keep every composed
        answer), index_interval_seconds (int: the artifacts source is indexed on
        the beat at most this often), yield_to_content (bool), retention_days
        (int, 0 keeps), retention_kinds (list of files/report/thread).
        """

        return tools.artifact_settings(knowledge_base, changes, principal=principal)
