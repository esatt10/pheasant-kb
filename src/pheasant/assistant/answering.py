"""Answering a question: everything around the workflow that answers it.

``chat`` holds the prompts and the evidence helpers every workflow shares;
``workflows`` hold the strategies. This module is the part in between that
applies to *every* workflow, a plugin included, so none of them can forget it:

* resolve the model and the retrieval toolbelt;
* read the route (``assistant.routing``) and rewrite a follow-up into a
  standalone search question (``assistant.conversation``);
* after the workflow: verify ``[fig:n]`` markers against the figures the cited
  documents actually show, and build the visual the route asked for
  (``assistant.visuals``) — or leave it ``pending`` for a streaming caller.

Split out of ``chat.py`` when these arrived, which put that file over its
size ceiling; the seam is real — prompts and evidence on one side, the
per-request orchestration on the other.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import replace
from typing import Any

from pheasant.assistant.chat import (
    _known_workflow_names,
    build_prompt,
    resolve_provider,
)

logger = logging.getLogger(__name__)

_FIGURE_RE = re.compile(r"\[fig:(\d{1,2})\]")


def number_figures(figures: list[dict], citations: list[dict]) -> list[dict]:
    """Number figures and name the citations whose documents show them.

    ``[fig:n]`` is a second marker space beside ``[n]`` rather than a reuse of
    it: a figure is not a passage and must not be counted as one when the
    answer's citations are verified.
    """
    by_node = {c.get("node_id"): c["index"] for c in citations if c.get("node_id")}
    numbered = []
    for position, figure in enumerate(figures, start=1):
        shown_in = sorted(
            {by_node[node] for node in figure.get("embedded_in") or [] if node in by_node}
        )
        if figure.get("node_id") in by_node:
            shown_in = sorted({*shown_in, by_node[figure["node_id"]]})
        numbered.append({**figure, "figure": position, "cited_in": shown_in, "shown": False})
    return numbered


def verify_figures(answer: str, figures: list[dict]) -> tuple[str, int]:
    """Drop ``[fig:n]`` markers with no figure; flag the ones shown.

    The same rule ``verify_node`` applies to ``[n]``: a marker that resolves
    to nothing would render as a broken image, which is worse than none.
    Returns ``(answer, dropped)``.
    """
    valid = {int(f["figure"]) for f in figures}
    dropped = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal dropped
        if int(match.group(1)) in valid:
            return match.group(0)
        dropped += 1
        return ""

    cleaned = _FIGURE_RE.sub(replace, answer or "")
    shown = {int(n) for n in _FIGURE_RE.findall(cleaned)}
    for figure in figures:
        figure["shown"] = int(figure["figure"]) in shown
    return cleaned, dropped


def resolve_llm(config: Any, credential: Any = None, env: dict[str, str] | None = None) -> Any:
    """The model to call for this request, or ``None`` (the extractive path)."""
    from pheasant.assistant.llm import llm_from_selection

    settings = getattr(config, "assistant", None)
    selected = resolve_provider(config, credential, env if env is not None else dict(os.environ))
    return llm_from_selection(selected, settings)


def visual_for(
    question: str,
    visual: str,
    *,
    citations: list[dict],
    facts: list[dict],
    figures: list[dict],
    llm: Any,
    kind: str | None = None,
    documents: dict | None = None,
) -> dict | None:
    """The visual a route asked for, built from the answer's own evidence.

    ``image`` shows the figures the cited documents hold; ``diagram`` draws
    from the same passages the answer read (a model spec, validated) or, with
    no model, from the graph edges between the cited sources. ``None`` when no
    visual was asked for.

    ``documents`` are the whole files behind the citations
    (:func:`visual_documents`). Without them the model sees each passage's
    search preview, which is 500 characters — a five-step process whose
    steps start past that is drawn as three steps, or as five with two
    invented.
    """

    if visual == "image":
        if figures:
            return {"type": "images", "status": "ok", "figures": figures}
        # Asked to be shown a picture the corpus does not hold. Drawing one
        # from the same passages is more useful than nothing, and it is still
        # grounded — but the payload says it was drawn, not found.
        drawn = _diagram(
            question, citations, facts, llm, kind if kind != "image" else None, documents
        )
        drawn["fallback_from"] = "image"
        note = "none of the cited sources shows an image; drawn from the passages instead"
        drawn["note"] = f"{note}; {drawn['note']}" if drawn.get("note") else note
        return drawn
    if visual != "diagram":
        return None
    return _diagram(question, citations, facts, llm, kind, documents)


def _diagram(
    question: str,
    citations: list[dict],
    facts: list[dict],
    llm: Any,
    kind: str | None,
    documents: dict | None = None,
) -> dict:
    from pheasant.assistant import visuals
    from pheasant.assistant.visual_specs import normalize_kind

    if not citations:
        return visuals.declined("no passages to draw from")
    if llm is None:
        drawn = visuals.graph_diagram(citations, facts)
        wanted = normalize_kind(kind)
        if drawn.get("status") == "ok" and wanted and wanted != "concept":
            drawn["note"] = (
                f"drawn as a concept map from the index's own links; a {wanted} "
                "needs a connected model to read the passages"
            )
        return drawn
    documents = documents or {}
    evidence = {
        int(c["index"]): (
            documents[c["index"]].text if c["index"] in documents else str(c.get("snippet") or "")
        )
        for c in citations
        if c.get("index") is not None
    }
    drawn = visuals.build_diagram(
        question,
        citations,
        llm,
        prompt=build_prompt(question, citations, facts, documents),
        kind=kind,
        evidence=evidence,
    )
    if drawn.get("status") == "ok":
        return drawn
    # The model could not draw it — no reply, nothing readable after a repair,
    # or a picture mostly of guesses. The index's own links between the same
    # cited sources are grounded by construction and need no model, so a
    # reader who asked for a visual gets one, told which it is and why.
    fallback = visuals.graph_diagram(citations, facts)
    if fallback.get("status") != "ok":
        return drawn
    reason = str(drawn.get("reason") or "declined")
    fallback["fallback_from"] = "model"
    fallback["model_declined"] = reason
    fallback["note"] = (
        f"the model's drawing could not be used ({reason}); "
        "drawn as a concept map from the index's own links instead"
    )
    visuals.record_model_outcome(llm, "fallback")
    return fallback


def visual_documents(
    citations: list[dict],
    *,
    state: Any,
    knowledge_base: str,
    graph: Any = None,
    config: Any = None,
    options: dict | None = None,
) -> dict:
    """The whole files behind ``citations``, as the answer's prompt read them.

    The same reassembly the workflows do (``chat.hydrate_citations``), so a
    visual and the answer it rides with read identical evidence. Needs only
    the state store — a deferred or on-demand visual has no retriever of its
    own. Best-effort: ``{}`` falls back to the passages' previews.
    """

    if state is None or not citations:
        return {}
    from pheasant.assistant.chat import hydrate_citations
    from pheasant.assistant.retrieval import PheasantRetriever

    retriever = PheasantRetriever(
        search=None, knowledge_base=knowledge_base, graph=graph, state=state, config=config
    )
    return hydrate_citations(retriever, citations, options)


def redraw_handle(request: str, citations: list[dict], knowledge_base: str | None) -> dict:
    """What a viewer needs to redraw the same passages in another shape.

    ``create_visual`` with these passages and a different ``kind`` — on MCP
    or HTTP — draws the same evidence without a new search, so switching a
    flow to a timeline cannot quietly change what it is a picture of.
    """

    from pheasant.assistant.visual_specs import KINDS

    node_ids: list[str] = []
    for citation in citations:
        node_id = citation.get("chunk_id") or citation.get("node_id")
        if node_id and str(node_id) not in node_ids:
            node_ids.append(str(node_id))
    return {
        "request": request,
        "node_ids": node_ids[:12],
        "knowledge_base": knowledge_base,
        "kinds": list(KINDS),
    }


def answer_question(
    question: str,
    *,
    search: Any,
    knowledge_base: str,
    config: Any,
    graph: Any = None,
    state: Any = None,
    credential: Any = None,
    env: dict[str, str] | None = None,
    mode: str = "hybrid",
    max_results: int | None = None,
    source_name: str | None = None,
    principal: str | None = None,
    principal_groups: list[str] | None = None,
    workflow: str | None = None,
    options: dict | None = None,
    on_step: Any = None,
    on_draft: Any = None,
    memory: Any = None,
    source_types: list[str] | None = None,
    exclude_source_types: list[str] | None = None,
    history: list | None = None,
    depth: str | None = None,
    visual: str | None = None,
    defer_visual: bool = False,
    request_budget: Any = None,
) -> dict:
    """Answer ``question`` from the knowledge base, with citations and facts.

    This is the single entry point behind the UI chat panel, ``POST
    /assistant/chat`` and the MCP ``ask_knowledge_base`` tool (both through
    ``services.assistant.answer``). It resolves a credential, builds the
    retrieval toolbelt, and hands both to the selected
    :mod:`~pheasant.assistant.workflows` workflow — so which workflow runs is
    a configuration choice, not a code path.

    Around the workflow, and therefore for every workflow including plugins:
    a follow-up is rewritten into a standalone search question
    (``assistant.conversation``), figure markers are verified, and the visual
    the route asked for is built — unless ``defer_visual``, which the
    streaming route uses to send the answer first and the picture after.
    """
    from pheasant.assistant import conversation, inventory, inventory_answer, routing, search_answer
    from pheasant.assistant import keywords as first_words
    from pheasant.assistant.retrieval import PheasantRetriever
    from pheasant.assistant.workflows import (
        WorkflowRequest,
        WorkflowStep,
        build_workflow,
        resolve_workflow_name,
    )

    settings = getattr(config, "assistant", None)
    env = env if env is not None else dict(os.environ)
    # First-word keywords (`@table`, `@detailed`, `@doc`, `@search` …) say
    # what kind of answer is wanted. They are read off the words and then
    # removed, so what is searched and written about is the question itself.
    asked_as = question
    directives = (
        first_words.read(question)
        if getattr(settings, "keywords", True) is not False
        else first_words.Directives(text=question)
    )
    routed_question = (
        first_words.inventory_form(question)
        if directives.inventory
        else directives.text
        if directives.used
        else question
    )
    if directives.used:
        question = directives.text
    depth = directives.depth or depth
    visual = directives.visual or visual
    max_results = max_results or int(getattr(settings, "max_context_chunks", 8) or 8)

    from pheasant.request_budget import RequestBudget

    latency = getattr(settings, "latency", None)
    requested_depth = depth or (options or {}).get("depth")
    routed_depth, _, _ = routing.classify_depth(question, requested_depth)
    budget_seconds = getattr(latency, f"{routed_depth}_deadline_seconds", None)
    budget = request_budget or RequestBudget(budget_seconds)

    selected = resolve_provider(config, credential, env)
    llm = resolve_llm(config, credential, env)
    if llm is not None:
        llm = llm.with_deadline(budget.deadline)
    retriever = PheasantRetriever(
        search=search,
        knowledge_base=knowledge_base,
        graph=graph,
        state=state,
        config=config,
        memory=memory,
        source_types=source_types,
        exclude_source_types=exclude_source_types,
        source_name=source_name,
        principal=principal,
        principal_groups=principal_groups,
    )

    name = resolve_workflow_name(
        workflow or getattr(settings, "workflow", "auto"), has_llm=llm is not None
    )
    # `workflow_options` is documented as "keyed by workflow name" and every
    # example nests it that way — but this splatted it flat, so a config of
    # `workflow_options: {agentic: {max_rounds: 3}}` produced an option
    # literally named "agentic" and every key inside it was silently ignored.
    # Accept both shapes: keys matching the selected workflow are merged in,
    # any other workflow's block is skipped, and flat keys still work.
    configured_options = dict(getattr(settings, "workflow_options", None) or {})
    merged_options = {"max_facts": int(getattr(settings, "max_facts", 12) or 12)}
    # Typed retrieval criteria (`assistant.retrieval`) sit UNDER
    # `workflow_options`, so a config that already tuned the untyped dict is
    # unchanged by their arrival — the block only fills in keys nobody set.
    retrieval = getattr(settings, "retrieval", None)
    if retrieval is not None and hasattr(retrieval, "as_options"):
        merged_options.update(retrieval.as_options())
    nested_for_workflow: dict = {}
    for key, value in configured_options.items():
        if isinstance(value, dict) and (key in _known_workflow_names() or key == name):
            if key == name:
                nested_for_workflow = value
            continue  # another workflow's block — not ours
        merged_options[key] = value
    merged_options.update(nested_for_workflow)
    merged_options.update(options or {})
    if directives.form:
        merged_options["form"] = directives.form
    # A depth named on the request pins it, exactly like a pinned intent;
    # "auto" (or nothing) leaves it to the router.
    if depth and str(depth).lower() in routing.DEPTHS:
        merged_options["depth"] = str(depth).lower()
    visual_pin = visual or merged_options.get("visual")
    visual_route, visual_why, visual_by = routing.classify_visual(question, visual_pin)
    merged_options["visual"] = visual_route
    shape, shape_why = (
        routing.classify_shape(question, visual_pin) if visual_route == "diagram" else (None, "")
    )

    turns = conversation.normalize_history(history)
    # A question about the knowledge base itself ("list the sources") is read
    # off the user's own words *before* the rewrite, so it costs no model call;
    # a follow-up the model rewrote is read again below.
    inventory_settings = getattr(settings, "inventory", None)
    inventory_mode = str(getattr(inventory_settings, "mode", "auto") or "auto")
    asked = inventory.route(
        routed_question, mode=inventory_mode, state=state, visual=visual_route, history=turns
    )
    if (
        asked is None
        and directives.used
        and not (directives.search or directives.inventory)
        and not question.strip()
    ):
        asked = inventory.InventoryQuestion(
            action="help",
            trigger="keyword",
            why=f"{directives.used[-1]} with no question after it",
            notes=(f"add a question after {directives.used[-1]}",),
        )
    if asked is not None and directives.inventory:
        asked = replace(asked, keyword=directives.used[-1])
    deterministic = asked is not None or directives.search
    rewrite_started = time.perf_counter()
    from pheasant.assistant.providers import collect_token_usage

    with collect_token_usage() as rewrite_usage:
        if not deterministic:
            search_question, how = conversation.standalone_question(question, turns, llm)
        else:
            search_question, how = question, None
    rewrite_seconds = time.perf_counter() - rewrite_started
    if asked is None and how and turns and search_question != f"{turns[-1].question} {question}":
        # The model resolved a follow-up ("and which of those are PDFs?") into
        # a standalone question, which may be one about the index.
        asked = inventory.route(
            search_question, mode=inventory_mode, state=state, visual=visual_route
        )
    context_steps = []
    context_steps.append(
        WorkflowStep(
            name="history_rewrite",
            detail=how
            or (
                "skipped: a question about the knowledge base itself"
                if asked is not None
                else "skipped: @search lists the hits for the words as typed"
                if directives.search
                else "no conversational rewrite needed"
            ),
            duration_seconds=rewrite_seconds,
            input_tokens=rewrite_usage.reported_input,
            output_tokens=rewrite_usage.reported_output,
            cached_input_tokens=rewrite_usage.reported_cached_input,
            reasoning_tokens=rewrite_usage.reported_reasoning,
            provider_calls=rewrite_usage.calls,
            provider_retries=rewrite_usage.retries,
        )
    )
    if how:
        context_steps.append(WorkflowStep(name="context", detail=f"follow-up: {how}"))
    for step in context_steps:
        if on_step is not None:
            try:
                on_step(step)
            except Exception:  # pragma: no cover - progress is never load-bearing
                logger.debug("progress callback failed", exc_info=True)

    request = WorkflowRequest(
        question=question,
        mode=mode,
        max_results=max_results,
        source_name=source_name,
        principal=principal,
        principal_groups=principal_groups or [],
        options=merged_options,
        # Live progress for callers that want it (the streaming chat route).
        # None keeps the workflow byte-identical to before.
        on_step=on_step,
        on_draft=on_draft,
        history=turns,
        search_question=search_question if how else None,
    )

    result = None
    inventory_data = None
    if asked is not None:
        result, inventory_data = inventory_answer.answer(
            asked,
            search_question,
            config=config,
            state=state,
            search=search,
            graph=graph,
            principal=principal,
            principal_groups=principal_groups,
            max_items=int(getattr(inventory_settings, "max_items", 50) or 50),
            report=lambda step: _report(on_step, step),
        )
        if result is not None:
            visual_route, visual_why, visual_by, shape, shape_why = (
                "none",
                "a question about the knowledge base itself",
                "rule",
                None,
                "",
            )
        else:
            failed = WorkflowStep(
                name="inventory", detail="index lookup failed; answered by searching instead"
            )
            context_steps.append(failed)
            _report(on_step, failed)
    if result is None and directives.search:
        result = search_answer.answer(
            question,
            retriever,
            source_name=source_name,
            principal=principal,
            principal_groups=principal_groups,
            report=lambda step: _report(on_step, step),
        )
        visual_route, visual_why, visual_by, shape, shape_why = (
            "none",
            "@search lists hits",
            "keyword",
            None,
            "",
        )
    try:
        budget.check()
        if result is None:
            result = build_workflow(name).run(request, retriever, llm)
    except Exception as exc:
        from pheasant.request_budget import DeadlineExceeded

        if isinstance(exc, DeadlineExceeded):
            raise
        # a custom workflow must not take down the API
        logger.exception("assistant workflow %r failed; falling back to simple", name)
        from pheasant.assistant.workflows.simple import SimpleWorkflow

        result = SimpleWorkflow().run(request, retriever, llm)
        result.error = f"workflow {name!r} failed ({exc}); answered with the simple workflow"

    figures = list(getattr(result, "figures", None) or [])
    if not figures and result.citations and (visual_route == "image" or "[fig:" in result.answer):
        figures = retriever.figures(result.citations)
    answer_text, dropped = verify_figures(result.answer, figures)
    steps = [*context_steps, *result.steps]
    if dropped:
        steps.append(
            WorkflowStep(name="verify", detail=f"dropped {dropped} figure marker(s) with no image")
        )

    route = dict(getattr(result, "route", None) or {})
    route.setdefault("intent", (result.counts or {}).get("intent", "knowledge"))
    route.setdefault("depth", (result.counts or {}).get("depth", "short"))
    route["visual"] = visual_route
    route.setdefault("why", {})["visual"] = visual_why
    route.setdefault("decided_by", {})["visual"] = visual_by
    route["shape"] = shape
    if shape_why:
        route["why"]["shape"] = shape_why
    keyword_axes = {"depth": directives.depth, "visual": directives.visual, "form": directives.form}
    for axis, value in keyword_axes.items():
        if value and route.get("intent") not in {"inventory", "search"}:
            route.setdefault("decided_by", {})[axis] = "keyword"
            route.setdefault("why", {})[axis] = f"asked with {' '.join(directives.used)}"
    if directives.form and route.get("intent") not in {"inventory", "search"}:
        route["form"] = directives.form
    routing.record_route(route)

    assistant_effort = getattr(settings, "reasoning_effort", None)
    planner_effort = merged_options.get("planner_reasoning_effort")
    grader_effort = merged_options.get("grader_reasoning_effort")
    is_openai = getattr(llm, "provider", None) == "openai"

    def effective_effort(requested: Any, model: str | None) -> str | None:
        if not is_openai or model != "gpt-6-luna":
            return None
        return str(requested) if requested is not None else "medium"

    effective_answer_effort = effective_effort(assistant_effort, result.model)
    effective_planner_effort = effective_effort(planner_effort or assistant_effort, result.model)
    effective_grader_effort = effective_effort(
        grader_effort or assistant_effort,
        str(merged_options.get("grader_model") or result.model or "") or None,
    )

    payload = {
        "question": asked_as,
        "answer": answer_text,
        "answer_mode": result.mode,
        "mode": result.mode,
        "provider": result.provider,
        "model": result.model,
        "reasoning_effort_requested": assistant_effort,
        "reasoning_effort_effective": effective_answer_effort,
        "reasoning_effort": {
            "answer": {
                "requested": assistant_effort,
                "effective": effective_answer_effort,
            },
            "planner": {
                "requested": planner_effort,
                "effective": effective_planner_effort,
            },
            "grader": {
                "requested": grader_effort,
                "effective": effective_grader_effort,
                "model": str(merged_options.get("grader_model") or result.model or "") or None,
            },
        },
        "credential_source": selected.get("source") if selected else None,
        "error": result.error,
        "citations": result.citations,
        "facts": result.facts,
        "figures": figures,
        "focus_node_ids": result.focus_node_ids,
        "search_mode": result.search_mode,
        "counts": result.counts,
        "workflow": result.workflow,
        "route": route,
        "visual": None,
        "steps": [
            {
                "name": step.name,
                "detail": step.detail,
                "passages": step.passages,
                "duration_seconds": step.duration_seconds,
                "input_tokens": step.input_tokens,
                "output_tokens": step.output_tokens,
                "cached_input_tokens": step.cached_input_tokens,
                "reasoning_tokens": step.reasoning_tokens,
                "provider_calls": step.provider_calls,
                "provider_retries": step.provider_retries,
                "fanout_timings": step.fanout_timings,
            }
            for step in steps
        ],
        "provider_call_count": sum(step.provider_calls or 0 for step in steps),
        "provider_retry_count": sum(step.provider_retries or 0 for step in steps),
        "retrieved_evidence_ids": result.retrieved_evidence_ids,
        "termination_reason": (
            "workflow_or_provider_error"
            if result.error
            else "insufficient_evidence"
            if (result.counts or {}).get("insufficient_evidence")
            else "extractive_fallback"
            if result.mode == "extractive"
            else "degraded_retrieval"
            if retriever._arm_failures
            else "completed"
        ),
        "retrieval_arm_failures": list(retriever._arm_failures),
        "degraded": bool(
            result.error
            or retriever._arm_failures
            or (result.counts or {}).get("insufficient_evidence")
            or (llm is not None and result.mode == "extractive")
        ),
    }
    if search_question and how:
        payload["search_question"] = search_question
    if directives.used or directives.unknown:
        payload["keywords"] = directives.as_dict()
    if inventory_data is not None:
        payload["inventory"] = inventory_data
    elif inventory_mode != "off" and not directives.used and inventory.looks_close(question):
        # Not routed, but close: say how to ask it, without touching the answer.
        payload["inventory_hint"] = inventory.HINT
    if visual_route != "none":
        if defer_visual:
            payload["visual"] = {"type": visual_route, "status": "pending"}
        else:
            draws = visual_route == "diagram" or (visual_route == "image" and not figures)
            attach_visual(
                payload,
                llm=llm,
                documents=visual_documents(
                    result.citations,
                    state=state,
                    knowledge_base=knowledge_base,
                    graph=graph,
                    config=config,
                    options=merged_options,
                )
                if draws and llm is not None
                else None,
                knowledge_base=knowledge_base,
            )
    return payload


def _report(on_step: Any, step: Any) -> None:
    if on_step is None:
        return
    try:
        on_step(step)
    except Exception:  # pragma: no cover - progress is never load-bearing
        logger.debug("progress callback failed", exc_info=True)


def attach_visual(
    payload: dict,
    *,
    llm: Any,
    kind: str | None = None,
    documents: dict | None = None,
    knowledge_base: str | None = None,
) -> dict | None:
    """Build the visual a payload's route asked for, record the step, return it."""
    import time

    route = payload.get("route") or {}
    started = time.perf_counter()
    citations = payload.get("citations") or []
    visual = visual_for(
        str(payload.get("question") or ""),
        str(route.get("visual") or "none"),
        citations=citations,
        facts=payload.get("facts") or [],
        figures=payload.get("figures") or [],
        llm=llm,
        kind=kind or route.get("shape"),
        documents=documents,
    )
    if visual is not None and visual.get("type") == "diagram" and citations:
        visual["redraw"] = redraw_handle(
            str(payload.get("question") or ""), citations, knowledge_base
        )
    payload["visual"] = visual
    from pheasant.assistant.routing import record_visual

    record_visual(visual)
    if visual is not None:
        detail = (
            f"drew a {visual.get('diagram', {}).get('kind', visual.get('type'))} "
            f"({visual.get('source', 'figures')})"
            if visual.get("status") == "ok"
            else f"no visual: {visual.get('reason', 'declined')}"
        )
        payload.setdefault("steps", []).append(
            {
                "name": "visual",
                "detail": detail,
                "passages": len(visual.get("citations") or []),
                "duration_seconds": time.perf_counter() - started,
                "input_tokens": None,
                "output_tokens": None,
            }
        )
    return visual
