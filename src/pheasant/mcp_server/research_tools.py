"""Deep research over MCP, as a job.

A mixin plus a registration function, like ``compose_tools.py``. The operation
is ``services.assistant.answer`` with a pinned ``long`` depth -- the one behind
``ask_knowledge_base(depth="long")``, ``@detailed`` / ``@deep`` and ``POST
/assistant/chat`` -- so the research, the per-answer spend limit and the
deep-research slot are the same on every surface.

MCP-only on purpose, for compose's reason: a deep answer runs for minutes,
which outlives some clients' tool timeouts. So by default it is a job
(``get_job`` / ``wait_for_job`` follow it) whose result is the answer with
compact citations and the research trail, without the per-passage evidence.
HTTP has ``POST /assistant/chat`` and its resumable stream for the same work.
"""

from __future__ import annotations

import threading
from typing import Any

from pheasant.security.authorization import background_target


def research_payload(result: dict) -> dict:
    """A deep answer as the job's result: the answer, compact evidence, the trail."""

    return {
        "answer": result.get("answer") or "",
        "answer_mode": result.get("answer_mode"),
        "citations": [
            {
                "index": citation.get("index"),
                "source": citation.get("source_id") or citation.get("source"),
                "path": citation.get("relative_path")
                or citation.get("path")
                or citation.get("title"),
                "section": citation.get("heading_path") or citation.get("section"),
                **{k: citation[k] for k in ("chunk_id", "node_id") if k in citation},
            }
            for citation in result.get("citations") or []
        ],
        "steps": [
            {"name": step.get("name"), "detail": step.get("detail")}
            for step in result.get("steps") or []
            if isinstance(step, dict)
        ],
        "research": result.get("research"),
        "route": result.get("route"),
        "models": result.get("models"),
        "error": result.get("error"),
    }


class ResearchTools:
    """``services.assistant.answer`` (pinned ``long``) as a followable job.

    ``AssistantTools`` provides ``ask_knowledge_base``; the facade provides
    ``jobs`` and ``_require_knowledge_base``.
    """

    jobs: Any

    def research_knowledge_base(
        self,
        knowledge_base: str,
        question: str,
        source_names: list[str] | None = None,
        source_name: str | None = None,
        history: list[dict] | None = None,
        wait: bool = False,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        options: dict | None = None,
        session: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)  # type: ignore[attr-defined]
        from pheasant.services.errors import InvalidRequest

        if not str(question or "").strip():
            raise InvalidRequest("research_knowledge_base needs a question.")

        def research(on_step: Any = None) -> dict:
            return research_payload(
                self.ask_knowledge_base(  # type: ignore[attr-defined]
                    knowledge_base,
                    question,
                    depth="long",
                    source_names=source_names,
                    source_name=source_name,
                    history=list(history or []),
                    principal=principal,
                    principal_groups=principal_groups,
                    options=options,
                    conversation_id=session,
                    on_step=on_step,
                )
            )

        if wait:
            return research()
        # Admission before the job exists: a busy region refuses this call at
        # once (DEEP_RESEARCH_BUSY) rather than as a failed job later.
        from pheasant.services import assistant as assistant_service

        permit = assistant_service.acquire(
            self.services,  # type: ignore[attr-defined]
            assistant_service.AnswerRequest(question=question, depth="long"),
        )
        permit.release()
        job = self.jobs.create("research", f"Researching: {question.strip()[:60]}", ["research"])

        def step(done: Any) -> None:
            self.jobs.progress(
                job.id,
                phase=str(getattr(done, "name", "") or "researching"),
                detail=str(getattr(done, "detail", "") or "")[:200],
            )

        def run() -> None:
            self.jobs.progress(job.id, phase="researching", detail="planning, searching, reading")
            try:
                payload = research(step)
            except Exception as exc:  # background tool work cannot propagate
                self.jobs.finish(job.id, "failed", error=str(exc))
                return
            answered = bool(str(payload.get("answer") or "").strip())
            self.jobs.finish(
                job.id,
                "succeeded" if answered else "failed",
                error=None if answered else (payload.get("error") or "no answer was written"),
                result=payload,
            )

        threading.Thread(
            target=background_target(run), name=f"pheasant-mcp-research-{job.id}", daemon=True
        ).start()
        return {
            **job.as_dict(),
            "job_id": job.id,
            "poll": "call wait_for_job(job_id) or get_job(job_id) until status is succeeded "
            "or failed; the answer is in result. Deep research takes a few minutes.",
        }


def register_research_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    @mcp.tool()
    @anticipated
    def research_knowledge_base(
        knowledge_base: str,
        question: str,
        source_names: list[str] | None = None,
        source_name: str | None = None,
        history: list[dict] | None = None,
        wait: bool = False,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        options: dict | None = None,
        session: str | None = None,
    ) -> dict:
        """Deep research: a thorough, sectioned answer the region researches first.

        The same answer as ask_knowledge_base(depth="long") or a question
        starting with @detailed / @deep: the region works out the aspects a
        complete answer must cover and searches each, follows up on what the
        evidence names, reads more of each document, and writes the answer
        section by section with [n] citations. It stops searching as soon as
        the evidence is sufficient, when a follow-up finds nothing new, or when
        the knowledge base holds nothing on the question, and says which.

        Each deep answer has a spend limit an operator sets (default 5 in the
        catalog's currency): research wraps up at 95% of it counting what
        writing will cost, and get_region_controls shows it. A region runs a
        few deep answers at once per server (default 2); past that this call
        is refused with DEEP_RESEARCH_BUSY, which is retryable.

        By default it returns a job at once: call wait_for_job(job_id) (or
        get_job) until status is "succeeded" or "failed" (a few minutes). The
        result is {answer, answer_mode, citations [{index, source, path,
        section}], steps (the research trail), research {mode, spend {limit,
        spent, enforced}}, route, models, error}. wait=true blocks and returns
        that result directly, for clients without a short tool timeout.
        source_names keeps every search inside those sources; history and
        session are as for ask_knowledge_base.
        """

        return tools.research_knowledge_base(
            knowledge_base,
            question,
            source_names=source_names,
            source_name=source_name,
            history=history,
            wait=wait,
            principal=principal,
            principal_groups=principal_groups,
            options=options,
            session=session,
        )
