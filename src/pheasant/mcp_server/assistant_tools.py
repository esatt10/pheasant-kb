"""The assistant's half of the MCP facade, and pheasant's MCP App view.

A mixin, like ``readiness_tools.py`` and for the same reason: `tools.py` sits at
its size ceiling and the ratchet asks for a split by bounded context. Three
tools, all adapters over `services.assistant` / `services.media`:

* ``ask_knowledge_base`` — a grounded answer, now with the conversation so
  far (``history``), a length (``depth``) and an optional visual.
* ``create_visual`` — a grounded diagram of named passages or of a request,
  or the images those passages hold.
* ``get_image`` — an indexed image's bytes as MCP image content, so a
  vision-capable agent can look at what a document shows.

**MCP Apps.** Each of the three declares ``_meta.ui.resourceUri`` pointing at
one view, ``ui://pheasant/knowledge-view.html``, served as
``text/html;profile=mcp-app`` (the MCP Apps extension, protocol 2026-01-26).
A host that supports apps renders the answer, the diagram or the image in a
sandboxed iframe; a host that does not reads the same structured result and
the text fallback, and loses nothing it had before. The view is self-contained
— no network, no CDN — and reaches back only through ``tools/call get_image``,
which is ACL-checked like every other read. pheasant's own UI hosts the very
same file, so there is one renderer for agents and people.
"""

from __future__ import annotations

import base64
import json
from functools import cache
from pathlib import Path
from typing import Any

APP_URI = "ui://pheasant/knowledge-view.html"
APP_MIME = "text/html;profile=mcp-app"
APP_PATH = Path(__file__).parent / "apps" / "knowledge_view.html"


@cache
def app_html() -> str:
    return APP_PATH.read_text(encoding="utf-8")


def app_meta() -> dict[str, Any]:
    """The tool ``_meta`` that links a tool to the view.

    Both spellings: ``ui.resourceUri`` is the current one and the flat
    ``ui/resourceUri`` is what hosts built against the draft read. The flat key
    is deprecated, not removed, and costs nothing to send.
    """

    return {"ui": {"resourceUri": APP_URI}, "ui/resourceUri": APP_URI}


class AssistantTools:
    """Answering, visuals and images over MCP."""

    config: Any
    services: Any

    def _require_knowledge_base(self, knowledge_base: str | None) -> None:  # pragma: no cover
        raise NotImplementedError

    def ask_knowledge_base(
        self,
        knowledge_base: str,
        question: str,
        workflow: str | None = None,
        mode: str = "hybrid",
        max_results: int = 8,
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        options: dict | None = None,
        source_types: list[str] | None = None,
        exclude_source_types: list[str] | None = None,
        memory: Any = None,
        history: list[dict] | None = None,
        depth: str | None = None,
        visual: str | None = None,
    ) -> dict:
        """Answer a question from the knowledge base, with citations and graph facts.

        Runs the configured question-answering workflow — by default the
        LangGraph agent when the ``[agent]`` extra is installed and a model
        is reachable, otherwise a single retrieval pass. Prefer this over
        ``search_context`` when you want a synthesized answer rather than
        raw passages to reason over yourself; the returned ``steps`` show
        what the agent actually did.

        With no model configured the answer is extractive (the retrieved
        passages, attributed), so this is always safe to call.
        """
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import assistant as assistant_service

        # Transport adapter. The operation is `services.assistant.answer`.
        return assistant_service.answer(
            self.services,
            assistant_service.AnswerRequest(
                question=question,
                knowledge_base=knowledge_base,
                mode=mode,
                max_results=max_results,
                source_name=source_name,
                source_types=source_types,
                exclude_source_types=exclude_source_types,
                principal=principal,
                principal_groups=principal_groups,
                workflow=workflow,
                options=options,
                memory=memory,
                # As sent: coercing here would turn a string into a list of
                # characters and hide the refusal the service gives it.
                history=history if history is not None else [],
                depth=depth,
                visual=visual,
            ),
        )

    def create_visual(
        self,
        knowledge_base: str,
        request: str,
        node_ids: list[str] | None = None,
        kind: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        source_name: str | None = None,
    ) -> dict:
        """A grounded diagram of named passages or of a request (`services.assistant.visualize`)."""
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import assistant as assistant_service

        return assistant_service.visualize(
            self.services,
            assistant_service.VisualRequest(
                request=request,
                node_ids=list(node_ids or []),
                kind=kind,
                knowledge_base=knowledge_base,
                principal=principal,
                principal_groups=principal_groups,
                source_name=source_name,
            ),
        )

    def get_image(
        self,
        knowledge_base: str,
        node_id: str,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        """An indexed image: its metadata, and its bytes base64-encoded as ``data``."""
        self._require_knowledge_base(knowledge_base)
        from pheasant.services.media import MediaRequest, get_media

        found = get_media(
            self.services,
            MediaRequest(
                node_id=node_id,
                knowledge_base=knowledge_base,
                principal=principal,
                principal_groups=principal_groups,
            ),
        )
        content = found.pop("content")
        return {**found, "data": base64.b64encode(content).decode("ascii")}


def image_result(found: dict) -> Any:
    """``get_image``'s answer as MCP content: the image, and its metadata as text."""

    import mcp.types as types

    metadata = {key: value for key, value in found.items() if key != "data"}
    return types.CallToolResult(
        content=[
            types.ImageContent(type="image", data=found["data"], mime_type=found["mime_type"]),
            types.TextContent(type="text", text=json.dumps(metadata)),
        ],
        structured_content=metadata,
    )


def register_assistant_tools(
    mcp: Any, tools: Any, anticipated: Any, anticipated_resource: Any
) -> None:
    """Register the assistant tools and the MCP App view on an MCP server.

    ``get_image`` carries no return annotation on purpose: it returns an SDK
    ``CallToolResult``, which the SDK passes through as-is, and this module
    must not import the SDK at import time (the mixin above is also the HTTP
    surface's and a test's, with no MCP installed).
    """

    @mcp.resource(
        APP_URI,
        name="pheasant-knowledge-view",
        description="Renders pheasant answers, grounded diagrams and indexed images.",
        mime_type=APP_MIME,
        meta={"ui": {"prefersBorder": True, "csp": {"connectDomains": [], "resourceDomains": []}}},
    )
    @anticipated_resource
    def knowledge_view() -> str:
        """pheasant's MCP App view (text/html;profile=mcp-app)."""

        return app_html()

    @mcp.tool(meta=app_meta())
    @anticipated
    def ask_knowledge_base(  # noqa: PLR0913 - mirrors the HTTP surface
        knowledge_base: str,
        question: str,
        workflow: str | None = None,
        mode: str = "hybrid",
        max_results: int = 8,
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        session: str | None = None,
        options: dict | None = None,
        source_types: list[str] | None = None,
        exclude_source_types: list[str] | None = None,
        memory: dict | str | None = None,
        history: list[dict] | None = None,
        depth: str | None = None,
        visual: str | None = None,
    ) -> dict:
        """Answer a question from the knowledge base, with citations and graph facts.

        Runs the configured agent workflow over pheasant's own search. Use
        this for a synthesized, cited answer; use search_context when you
        want the raw passages to reason over yourself.

        A question about the knowledge base itself ("list all sources", "how
        many PDFs are in notes", "sync status") is answered directly from the
        index rather than by searching: route.intent is "inventory" and
        "inventory" holds the describe_knowledge_base / list_documents
        result. Start the question with @pheasant to ask one explicitly
        ("@pheasant list documents in notes").

        history is the conversation so far, oldest first, as
        [{"question": ..., "answer": ...}]. Pass it for follow-ups ("what
        about the second one?"): the region keeps no conversation state, so
        continuity is what you send. At most the last 6 turns are used.

        depth is "short" (a direct answer; the default), "medium" (a few
        sections) or "long" (an outlined, sectioned write-up). Leave it unset
        to let the question decide ("in detail" reads as long, "briefly" as
        short). visual is "diagram" (a visual whose every element cites a
        passage), "image" (images the cited documents show), "none", or a
        shape to draw the diagram as (any create_visual kind, e.g.
        "timeline", "table", "mindmap"); unset reads it off the question
        ("draw…", "a timeline of…", "show me the figure…"). A host
        that supports MCP Apps renders the result; the JSON is complete
        without one. Figures appear in the answer as [fig:n] markers and in
        "figures"; fetch one with get_image.

        memory controls how this region's agent memory takes part, exactly as
        on search_context.

        session identifies the conversation this call belongs to. It is
        recorded, never enforced -- pass a stable opaque string and the
        region can keep one refined memory per session; omit it and nothing
        changes. Like principal, it is asserted by you and verified by
        nobody.
        """

        return tools.ask_knowledge_base(
            knowledge_base,
            question,
            workflow=workflow,
            mode=mode,
            max_results=max_results,
            source_name=source_name,
            principal=principal,
            principal_groups=principal_groups,
            options=options,
            source_types=source_types,
            exclude_source_types=exclude_source_types,
            memory=memory,
            history=history,
            depth=depth,
            visual=visual,
        )

    @mcp.tool(meta=app_meta())
    @anticipated
    def create_visual(
        knowledge_base: str,
        request: str,
        node_ids: list[str] | None = None,
        kind: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        source_name: str | None = None,
    ) -> dict:
        """Draw a visual grounded in the knowledge base, in any shape, or show its images.

        request says what to draw and from what viewpoint ("the release
        process for a new engineer", "compare the three rollout options").
        node_ids (up to 12 chunk or file ids, e.g. from a search_context hit
        or an answer's citations) draws from exactly those passages —
        "visualize this passage", or redraw the same evidence in another
        shape; without them the region searches for the request.

        kind picks the shape: "flow", "sequence", "hierarchy" (tree / org
        chart), "mindmap", "concept" (network), "cycle", "timeline",
        "swimlane", "layers" (stack), "groups" (categories), "table"
        (comparison), "quadrant" (2x2), "chart" (bar or line, from numbers the
        passages state), "canvas" (free layout, any node shapes), the UML
        diagrams "class", "activity", "state" (state machine, the behavior
        diagram) and "usecase", or "image" to show the images those passages
        hold. Common names work too ("org chart", "venn", "2x2", "bar chart",
        "class diagram", "state machine"). Unset, the request decides.

        Every node, edge, lane and table cell in visual.diagram lists the
        passage numbers ("cites") that support it; an element nothing
        supports is marked "inferred", a chart value no cited passage states
        is marked unverified, and a visual that is mostly inference is
        declined with a reason rather than drawn. visual.mermaid is the same
        visual as Mermaid text where Mermaid has the shape (visual.markdown
        for a table); visual.redraw carries the node_ids to redraw it as
        another kind. With no model connected the visual is built from the
        graph edges the index recorded between those passages.
        """

        return tools.create_visual(
            knowledge_base,
            request,
            node_ids=node_ids,
            kind=kind,
            principal=principal,
            principal_groups=principal_groups,
            source_name=source_name,
        )

    @mcp.tool(meta=app_meta())
    @anticipated
    def get_image(
        knowledge_base: str,
        node_id: str,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ):
        """Return an indexed image as image content, with its caption and path.

        node_id is an image's id — from an answer's "figures", a search hit
        whose type is "image", or create_visual with kind "image". Images a
        document references (![...](img.png)) are linked to it at index time,
        so a figure is always something this region holds and may show you.
        Raster formats only (png, jpeg, webp, gif).
        """

        return image_result(
            tools.get_image(
                knowledge_base, node_id, principal=principal, principal_groups=principal_groups
            )
        )

    @mcp.resource("pheasant://knowledge-bases/{kb_id}/media/{node_id}")
    @anticipated_resource
    def media_resource(kb_id: str, node_id: str) -> bytes:
        """An indexed image's bytes (the same read `get_image` performs)."""

        found = tools.get_image(kb_id, node_id)
        return base64.b64decode(found["data"])
