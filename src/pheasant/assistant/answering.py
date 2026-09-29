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
import re
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
    import os

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
) -> dict | None:
    """The visual a route asked for, built from the answer's own evidence.

    ``image`` shows the figures the cited documents hold; ``diagram`` draws
    from the same passages the answer read (a model spec, validated) or, with
    no model, from the graph edges between the cited sources. ``None`` when no
    visual was asked for.
    """

    if visual == "image":
        if figures:
            return {"type": "images", "status": "ok", "figures": figures}
        # Asked to be shown a picture the corpus does not hold. Drawing one
        # from the same passages is more useful than nothing, and it is still
        # grounded — but the payload says it was drawn, not found.
        drawn = _diagram(question, citations, facts, llm, kind if kind != "image" else None)
        drawn["fallback_from"] = "image"
        drawn["note"] = "none of the cited sources shows an image; drawn from the passages instead"
        return drawn
    if visual != "diagram":
        return None
    return _diagram(question, citations, facts, llm, kind)


def _diagram(
    question: str, citations: list[dict], facts: list[dict], llm: Any, kind: str | None
) -> dict:
    from pheasant.assistant import visuals

    if not citations:
        return visuals.declined("no passages to draw from")
    if llm is None:
        return visuals.graph_diagram(citations, facts)
    return visuals.build_diagram(
        question, citations, llm, prompt=build_prompt(question, citations, facts), kind=kind
    )


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
    memory: Any = None,
    source_types: list[str] | None = None,
    exclude_source_types: list[str] | None = None,
    history: list | None = None,
    depth: str | None = None,
    visual: str | None = None,
    defer_visual: bool = False,
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
    import os

    from pheasant.assistant import conversation, routing
    from pheasant.assistant.retrieval import PheasantRetriever
    from pheasant.assistant.workflows import (
        WorkflowRequest,
        WorkflowStep,
        build_workflow,
        resolve_workflow_name,
    )

    settings = getattr(config, "assistant", None)
    env = env if env is not None else dict(os.environ)
    max_results = max_results or int(getattr(settings, "max_context_chunks", 8) or 8)

    selected = resolve_provider(config, credential, env)
    llm = resolve_llm(config, credential, env)
    retriever = PheasantRetriever(
        search=search,
        knowledge_base=knowledge_base,
        graph=graph,
        state=state,
        config=config,
        memory=memory,
        source_types=source_types,
        exclude_source_types=exclude_source_types,
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
    # A depth named on the request pins it, exactly like a pinned intent;
    # "auto" (or nothing) leaves it to the router.
    if depth and str(depth).lower() in routing.DEPTHS:
        merged_options["depth"] = str(depth).lower()
    visual_route, visual_why, visual_by = routing.classify_visual(
        question, visual or merged_options.get("visual")
    )

    turns = conversation.normalize_history(history)
    search_question, how = conversation.standalone_question(question, turns, llm)
    context_steps = []
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
        history=turns,
        search_question=search_question if how else None,
    )

    try:
        result = build_workflow(name).run(request, retriever, llm)
    except Exception as exc:  # a custom workflow must not take down the API
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
    routing.record_route(route)

    payload = {
        "question": question,
        "answer": answer_text,
        "mode": result.mode,
        "provider": result.provider,
        "model": result.model,
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
            }
            for step in steps
        ],
    }
    if search_question and how:
        payload["search_question"] = search_question
    if visual_route != "none":
        if defer_visual:
            payload["visual"] = {"type": visual_route, "status": "pending"}
        else:
            attach_visual(payload, llm=llm)
    return payload


def attach_visual(payload: dict, *, llm: Any, kind: str | None = None) -> dict | None:
    """Build the visual a payload's route asked for, record the step, return it."""
    import time

    route = payload.get("route") or {}
    started = time.perf_counter()
    visual = visual_for(
        str(payload.get("question") or ""),
        str(route.get("visual") or "none"),
        citations=payload.get("citations") or [],
        facts=payload.get("facts") or [],
        figures=payload.get("figures") or [],
        llm=llm,
        kind=kind,
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
