"""Answering a question, once — and the assistant's retrieval vocabulary.

``answer`` is the operation behind ``POST /assistant/chat``, its streaming
twin, and the MCP ``ask_knowledge_base`` tool. Before it lived here the HTTP
routes and the tool each called ``assistant.chat.answer_question`` directly
and had already drifted: HTTP refused when ``assistant.enabled`` was off and
MCP answered anyway, HTTP forwarded the memory policy and the tool had no way
to, and only HTTP checked for an empty question. Each difference was one line
on whichever surface had its bug report first.

What stays in the adapters is transport: the session key a browser pasted (an
HTTP concern — MCP has no session to hold one), the progress callback the
streaming route feeds into server-sent events, and observation.

``RETRIEVAL_FIELD_HELP`` is one line per retrieval knob, so a UI — or an agent
reading ``describe_retrieval`` — can explain a setting without going and
reading the workflow module's docstring. It lived in `api/app.py`, and
`mcp_server/tools.py` imported it from there; the text is the operation's.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pheasant.services import ServiceContext
from pheasant.services.errors import AssistantDisabled, EmptyQuestion, InvalidRequest


@dataclass(frozen=True)
class AnswerRequest:
    """One question, in the vocabulary both surfaces already speak."""

    question: str
    knowledge_base: str | None = None
    mode: str = "hybrid"
    max_results: int | None = None
    source_name: str | None = None
    source_types: list[str] | None = None
    exclude_source_types: list[str] | None = None
    principal: str | None = None
    principal_groups: list[str] | None = None
    workflow: str | None = None
    options: dict[str, Any] | None = None
    memory: Any = None
    #: Earlier turns of this conversation, oldest first, as the caller holds
    #: them. The region keeps no conversation state — the MCP transport is
    #: stateless by design and a browser tab is the only thing that knows
    #: which turns belong together — so continuity is something the caller
    #: sends rather than something the server remembers.
    history: list[dict[str, Any]] = field(default_factory=list)
    #: ``short`` / ``medium`` / ``long``, or ``None``/``"auto"`` to read it off
    #: the question. See ``assistant.routing``.
    depth: str | None = None
    #: ``none`` / ``auto`` / ``diagram`` — whether a grounded visual rides
    #: along with the answer. ``None`` reads it off the question.
    visual: str | None = None


def admit(context: ServiceContext, request: AnswerRequest) -> None:
    """Refuse a question this region will not answer, before any work starts.

    Public because the streaming route must refuse with a status code before it
    opens an event stream; after that, a refusal can only be an event.
    """

    context.knowledge_base(request.knowledge_base)
    settings = getattr(context.config, "assistant", None)
    if settings is not None and not getattr(settings, "enabled", True):
        raise AssistantDisabled()
    if not (request.question or "").strip():
        raise EmptyQuestion()
    from pheasant.assistant.conversation import HistoryError, normalize_history

    try:
        normalize_history(request.history)
    except HistoryError as exc:
        raise InvalidRequest(str(exc)) from exc


def answer(
    context: ServiceContext,
    request: AnswerRequest,
    *,
    credential: Any = None,
    env: dict[str, str] | None = None,
    on_step: Callable[[Any], None] | None = None,
    defer_visual: bool = False,
) -> dict[str, Any]:
    """A grounded answer with citations, facts, the route taken and any visual.

    ``credential`` is a key a browser session supplied and ``on_step`` a
    progress sink; both are transport-held, which is why they are keyword
    arguments rather than request fields. ``defer_visual`` returns the answer
    with ``visual.status == "pending"`` so a streaming caller can send the text
    first and finish with :func:`render_visual`.
    """

    admit(context, request)
    from pheasant.assistant.chat import answer_question
    from pheasant.assistant.conversation import normalize_history

    return answer_question(
        request.question,
        search=context.searcher,
        knowledge_base=context.knowledge_base(request.knowledge_base),
        config=context.config,
        graph=context.graph,
        state=context.state,
        credential=credential,
        env=env if env is not None else dict(os.environ),
        mode=request.mode,
        max_results=request.max_results,
        source_name=request.source_name,
        principal=request.principal,
        principal_groups=request.principal_groups,
        workflow=request.workflow,
        options=request.options,
        on_step=on_step,
        memory=request.memory,
        source_types=request.source_types,
        exclude_source_types=request.exclude_source_types,
        history=normalize_history(request.history),
        depth=request.depth,
        visual=request.visual,
        defer_visual=defer_visual,
    )


#: Passages one visual may be drawn from. A diagram of thirty sources is a
#: diagram of nothing in particular.
MAX_VISUAL_PASSAGES = 12


@dataclass(frozen=True)
class VisualRequest:
    """Draw ``request`` from named passages, or from what a search finds for it."""

    request: str
    node_ids: list[str] = field(default_factory=list)
    kind: str | None = None
    knowledge_base: str | None = None
    principal: str | None = None
    principal_groups: list[str] | None = None
    source_name: str | None = None
    max_passages: int = 8


def visualize(
    context: ServiceContext,
    request: VisualRequest,
    *,
    credential: Any = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A grounded visual on demand — "visualize this passage", or "draw X".

    With ``node_ids`` the visual is drawn from exactly those passages (a
    chunk id or an artifact id each, read-checked one by one); without, from
    what an ordinary search for ``request`` returns, under the same criteria,
    ACL and memory policy as ``search``. ``kind="image"`` shows the images
    those passages hold instead of drawing one.
    """

    kb_id = context.knowledge_base(request.knowledge_base)
    settings = getattr(context.config, "assistant", None)
    if settings is not None and not getattr(settings, "enabled", True):
        raise AssistantDisabled()
    text = (request.request or "").strip()
    if not text:
        raise InvalidRequest("request must say what to draw")
    if len(request.node_ids) > MAX_VISUAL_PASSAGES:
        raise InvalidRequest(
            f"a visual is drawn from at most {MAX_VISUAL_PASSAGES} passages; "
            f"this request names {len(request.node_ids)}"
        )

    from pheasant.assistant import answering, chat
    from pheasant.graph.figures import collect_figures, with_full_captions

    if request.node_ids:
        citations = _named_citations(context, request)
    else:
        from pheasant.services import retrieval as retrieval_service

        found = retrieval_service.search(
            context,
            retrieval_service.SearchRequest(
                query=text,
                knowledge_base=kb_id,
                max_results=max(1, min(int(request.max_passages), MAX_VISUAL_PASSAGES)),
                source_name=request.source_name,
                principal=request.principal,
                principal_groups=request.principal_groups,
            ),
        )
        citations = chat.build_citations(found.get("results") or [], MAX_VISUAL_PASSAGES)
    node_ids = [str(c["node_id"]) for c in citations if c.get("node_id")]
    facts = chat.collect_facts(context.graph, node_ids, 12)
    figures = answering.number_figures(
        with_full_captions(context.state, collect_figures(context.graph, node_ids)), citations
    )
    kind = (request.kind or "").strip().lower() or None
    llm = answering.resolve_llm(context.config, credential, env)
    visual = answering.visual_for(
        text,
        "image" if kind == "image" else "diagram",
        citations=citations,
        facts=facts,
        figures=figures,
        llm=llm,
        kind=kind,
        documents=answering.visual_documents(
            citations,
            state=context.state,
            knowledge_base=kb_id,
            graph=context.graph,
            config=context.config,
        )
        if llm is not None
        else None,
    )
    if visual is not None and visual.get("type") == "diagram" and citations:
        visual["redraw"] = answering.redraw_handle(text, citations, kb_id)
    from pheasant.assistant.routing import record_visual

    record_visual(visual)
    return {
        "request": text,
        "visual": visual,
        "citations": citations,
        "facts": facts,
        "figures": figures,
    }


def _named_citations(context: ServiceContext, request: VisualRequest) -> list[dict[str, Any]]:
    """Citation records for the passages a caller named, in the order named."""

    from pheasant.services.errors import NodeNotFound
    from pheasant.services.graph import require_readable

    citations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node_id in request.node_ids:
        node_id = str(node_id)
        chunk = context.state.rows(
            "SELECT id, artifact_id, text, heading_path FROM chunks WHERE id=? LIMIT 1", (node_id,)
        )
        artifact_id = str(chunk[0]["artifact_id"]) if chunk else node_id
        artifact = context.state.rows(
            "SELECT id, relative_path, source_id, type FROM artifacts WHERE id=? LIMIT 1",
            (artifact_id,),
        )
        if not artifact:
            raise NodeNotFound(node_id)
        require_readable(context, artifact_id, request.principal, request.principal_groups)
        if node_id in seen:
            continue
        seen.add(node_id)
        if chunk:
            text = str(chunk[0]["text"] or "")
        else:
            rows = context.state.rows(
                "SELECT text FROM chunks WHERE artifact_id=? ORDER BY chunk_index LIMIT 4",
                (artifact_id,),
            )
            text = "\n\n".join(str(row["text"] or "") for row in rows)
        row = dict(artifact[0])
        citations.append(
            {
                "index": len(citations) + 1,
                "node_id": artifact_id,
                "chunk_id": node_id if chunk else None,
                "title": row.get("relative_path") or artifact_id,
                "relative_path": row.get("relative_path"),
                "source_id": row.get("source_id"),
                "type": row.get("type"),
                "snippet": text[:1500],
                **(
                    {"heading_path": chunk[0]["heading_path"]}
                    if chunk and chunk[0]["heading_path"]
                    else {}
                ),
                "used": False,
            }
        )
    return citations


def render_visual(
    context: ServiceContext,
    payload: dict[str, Any],
    *,
    credential: Any = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Finish a deferred visual from the answer payload it belongs to.

    Built from the payload alone — its citations, facts and figures — so the
    picture is drawn from exactly the evidence the answer was written from.
    """

    from pheasant.assistant.answering import attach_visual, resolve_llm, visual_documents

    llm = resolve_llm(context.config, credential, env)
    kb_id = context.knowledge_base(payload.get("knowledge_base"))
    return attach_visual(
        payload,
        llm=llm,
        documents=visual_documents(
            payload.get("citations") or [],
            state=context.state,
            knowledge_base=kb_id,
            graph=context.graph,
            config=context.config,
        )
        if llm is not None
        else None,
        knowledge_base=kb_id,
    )


#: One line per retrieval knob, so a UI (or an agent reading
#: ``describe_retrieval``) can explain a setting without the caller having to
#: go and read the workflow module's docstring.
RETRIEVAL_FIELD_HELP: dict[str, str] = {
    "max_rounds": "plan → retrieve → grade turns before answering with what is in "
    "hand. 1 disables the re-plan loop.",
    "per_query_results": "passages fetched per query per search mode.",
    "max_context_passages": "total passages offered to the answering step.",
    "retrieval_modes": "search modes to fan out over (text, vector, graph, hybrid). "
    "'vector' is dropped automatically when no vector index is built.",
    "expand_graph": "walk the knowledge graph out of the best hits, reaching "
    "documents that share no vocabulary with the question.",
    "expand_depth": "hops to walk when expanding.",
    "expand_per_node": "neighbours taken per expanded node.",
    "grade_evidence": "ask the model to grade its own evidence before answering.",
    "grader_model": "optional model for evidence sufficiency checks; the assistant model "
    "still writes the answer.",
    "verify_citations": "drop [n] markers that do not resolve to a real citation.",
    "max_facts": "graph facts surfaced alongside the answer.",
}
