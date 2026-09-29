"""The single-pass workflow: retrieve once, answer once.

This is the default when no agent framework is installed and the fallback
whenever anything else is unavailable. It has no dependencies beyond the
standard library, so a question is always answerable — with a model it
synthesizes, without one it returns the retrieved passages attributed.

The prompt, citation numbering and fact collection are shared with the
agentic workflow (see :mod:`pheasant.assistant.chat`), so switching
workflows changes *how much work* is done, not what an answer looks like.
"""

from __future__ import annotations

import time
from typing import Any

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
        retrieve_started = time.perf_counter()
        passages = retriever.search(
            request.question,
            mode=request.mode,
            limit=request.max_results,
            source_name=request.source_name,
            principal=request.principal,
            principal_groups=request.principal_groups,
        )
        citations = passages_to_citations(passages, request.max_results)
        node_ids = [c["node_id"] for c in citations if c.get("node_id")]
        facts = retriever.facts(node_ids, int(request.options.get("max_facts", 12)))
        steps = [
            WorkflowStep(
                name="retrieve",
                detail=f"{request.mode} search for the question as asked",
                passages=len(citations),
                duration_seconds=time.perf_counter() - retrieve_started,
            )
        ]
        # Progress is reported by every workflow, not just the agentic one:
        # a caller streaming the answer should not have to know which one ran.
        request.report(steps[-1])

        # One pass, but not a starved one: the same file-level content and the
        # same intent-shaped prompt the agentic graph uses. What "single pass"
        # buys you is fewer model calls, not a worse answer per call.
        intent = str(request.options.get("intent") or "").strip().lower()
        if intent not in ("knowledge", "procedural"):
            intent, _why = classify_intent(request.question)

        answer_mode = "extractive"
        error: str | None = None
        if llm is not None and citations:
            read_started = time.perf_counter()
            documents = hydrate_citations(retriever, citations, request.options)
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
                        system_prompt_for(intent),
                        build_prompt(request.question, citations, facts, documents),
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
            counts={"passages": len(passages), "citations": len(citations), "intent": intent},
            steps=steps,
            workflow=self.name,
        )
