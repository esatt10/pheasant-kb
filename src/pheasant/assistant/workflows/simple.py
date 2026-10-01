"""The single-pass workflow: retrieve once, answer once.

This is the default when no agent framework is installed and the fallback
whenever anything else is unavailable. It has no dependencies beyond the
standard library, so a question is always answerable — with a model it
synthesizes, without one it returns the retrieved passages attributed.

The prompt, citation numbering and fact collection are shared with the
agentic workflow (see :mod:`pheasant.assistant.chat`), so switching
workflows changes *how much work* is done, not what an answer looks like.

It honours the router's depth as far as one call can: ``medium`` fetches
more and asks for sections; ``long`` is answered as ``medium`` and the step
list says so, because an outline-then-sections answer is several model calls
and "one call" is the whole promise of this workflow. A follow-up keeps the
previous question's evidence (``assistant.conversation``).
"""

from __future__ import annotations

import time
from typing import Any

from pheasant.assistant import conversation, routing
from pheasant.assistant.chat import (
    build_prompt,
    classify_intent,
    extractive_answer,
    hydrate_citations,
    mark_used_citations,
    passages_to_citations,
    short_reason,
    system_prompt_for,
)
from pheasant.assistant.providers import ProviderError, collect_token_usage
from pheasant.assistant.workflows import WorkflowRequest, WorkflowResult, WorkflowStep


class SimpleWorkflow:
    """Retrieve once, synthesize once."""

    name = "simple"

    def run(self, request: WorkflowRequest, retriever: Any, llm: Any) -> WorkflowResult:
        intent = str(request.options.get("intent") or "").strip().lower()
        intent_pinned = intent in ("knowledge", "procedural")
        if not intent_pinned:
            intent, intent_why = classify_intent(request.question)
        else:
            intent_why = "pinned by configuration"
        route = routing.route_question(
            request.question,
            intent=(intent, intent_why),
            depth=request.options.get("depth"),
            intent_pinned=intent_pinned,
        )
        depth = route.depth
        options = routing.depth_options(depth, dict(request.options), set(request.options))
        # `max_context_passages` also arrives from `assistant.retrieval` for the
        # agentic workflow's sake, so it widens this search only when the depth
        # asked for more — a short answer fetches exactly what it always did.
        limit = int(request.max_results)
        if depth != "short":
            limit = max(limit, int(routing.DEPTH_PROFILES[depth]["max_context_passages"]))

        steps: list[WorkflowStep] = []
        if depth != "short":
            # Only when the route differs from what this workflow always did,
            # so a plain question's trace is the one it has always been.
            note = "; answered as medium in one call" if depth == "long" else ""
            steps.append(
                WorkflowStep(name="classify", detail=f"{depth} answer — {route.why['depth']}{note}")
            )
            request.report(steps[-1])

        retrieve_started = time.perf_counter()
        fanout_timings: list[dict[str, Any]] = []

        def timed_search(query: str, query_index: int, query_label: str) -> list[Any]:
            started = time.perf_counter()
            found = retriever.search(
                query,
                mode=request.mode,
                limit=limit,
                source_name=request.source_name,
                principal=request.principal,
                principal_groups=request.principal_groups,
            )
            fanout_timings.append(
                {
                    "mode": request.mode,
                    "phase": "search",
                    "query_index": query_index,
                    "query_label": query_label,
                    "duration_seconds": time.perf_counter() - started,
                    "passages": len(found),
                }
            )
            return found

        passages = timed_search(request.search_question or request.question, 0, "question")
        carried_from = conversation.carried_question(request.history)
        if request.search_question and carried_from:
            passages = conversation.carry(
                passages,
                timed_search(carried_from, 1, "prior question"),
            )
        citations = passages_to_citations(passages, limit)
        node_ids = [c["node_id"] for c in citations if c.get("node_id")]
        facts = retriever.facts(node_ids, int(options.get("max_facts", 12)))
        figures = _figures(retriever, citations)
        steps.append(
            WorkflowStep(
                name="retrieve",
                detail=f"{request.mode} search for the question as asked"
                if not request.search_question
                else f"{request.mode} search for “{request.search_question}”, keeping the "
                "previous question's sources",
                passages=len(citations),
                duration_seconds=time.perf_counter() - retrieve_started,
                fanout_timings=fanout_timings,
            )
        )
        # Progress is reported by every workflow, not just the agentic one:
        # a caller streaming the answer should not have to know which one ran.
        request.report(steps[-1])

        # One pass, but not a starved one: the same file-level content and the
        # same intent-shaped prompt the agentic graph uses. What "single pass"
        # buys you is fewer model calls, not a worse answer per call.
        answer_mode = "extractive"
        error: str | None = None
        answering_depth = "medium" if depth == "long" else depth
        if llm is not None and citations:
            read_started = time.perf_counter()
            documents = hydrate_citations(retriever, citations, options)
            if documents:
                steps.append(
                    WorkflowStep(
                        name="read",
                        detail=f"read {len(documents)} file(s) in full from their chunks",
                        passages=len(documents),
                        duration_seconds=time.perf_counter() - read_started,
                    )
                )
                request.report(steps[-1])
            try:
                answer_started = time.perf_counter()
                with collect_token_usage() as usage:
                    answer = llm.complete(
                        system_prompt_for(intent, answering_depth, figures=bool(figures)),
                        build_prompt(
                            request.question,
                            citations,
                            facts,
                            documents,
                            history_text=conversation.history_block(request.history),
                            figures=figures,
                        ),
                        max_output_tokens=options.get("max_output_tokens"),
                    )
                answer_mode = "llm"
                steps.append(
                    WorkflowStep(
                        name="answer",
                        detail=f"synthesized with {llm.model_id}",
                        duration_seconds=time.perf_counter() - answer_started,
                        input_tokens=usage.reported_input,
                        output_tokens=usage.reported_output,
                    )
                )
                request.report(steps[-1])
            except ProviderError as exc:
                error = str(exc)
                answer = extractive_answer(request.question, citations, reason=short_reason(error))
                steps.append(
                    WorkflowStep(
                        name="answer",
                        detail="model unavailable; returned extracted passages",
                        duration_seconds=time.perf_counter() - answer_started,
                        input_tokens=usage.reported_input,
                        output_tokens=usage.reported_output,
                    )
                )
                request.report(steps[-1])
        else:
            answer = extractive_answer(request.question, citations)

        mark_used_citations(answer, citations)
        return WorkflowResult(
            answer=answer,
            citations=citations,
            facts=facts,
            focus_node_ids=node_ids,
            mode=answer_mode,
            provider=llm.provider if llm else None,
            model=llm.model_id if llm else None,
            error=error,
            search_mode=request.mode,
            counts={
                "passages": len(passages),
                "citations": len(citations),
                "intent": intent,
                "depth": depth,
            },
            steps=steps,
            workflow=self.name,
            figures=figures,
            route=route.as_dict(),
        )


def _figures(retriever: Any, citations: list[dict]) -> list[dict]:
    collect = getattr(retriever, "figures", None)
    return collect(citations) if callable(collect) and citations else []
