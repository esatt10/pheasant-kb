"""Deep research: what ``@detailed`` was meant to be.

A ``long`` answer someone *chose* -- ``@detailed`` / ``@deep``, ``depth:
"long"`` (or ``"deep"``) on a request, the UI's Length picker -- researches
before it writes: it plans one query per aspect of the question, follows up on
what the evidence names, reads more of each document and writes more sections.
A ``long`` read off the question's words or chosen by the planner is today's
long answer, unchanged (``routing.deep_research``).

Like compose, its knobs are their own block, ``deep_options``, applied over
:data:`DEEP_DEFAULTS` after the depth profile, because a key the config sets
pins it against every profile: the flagship's ``max_context_passages: 16``
would otherwise hold a deep answer to 16 passages, as it already held ``long``.

**When research stops** (:func:`stop_reason`, after each grade):

* the evidence is judged sufficient -- the loop's own rule, unchanged;
* the knowledge base holds nothing for it: a planned round found no passages;
* another round would not help: a follow-up added no new evidence;
* the cost: what this answer has spent plus what writing it is projected to
  cost reached 95% of the per-answer limit (``telemetry/answer_spend.py``).

Each is said in the answer's steps, so a short research trail reads as a
decision rather than as a loop that gave up.
"""

from __future__ import annotations

from typing import Any

from pheasant.assistant import routing
from pheasant.assistant.workflows import WorkflowStep
from pheasant.telemetry import answer_spend

#: Starting points, not yet measured on real models; ``deep_options``
#: overrides any of them.
DEEP_DEFAULTS: dict[str, Any] = {
    # Research: plan by aspect, and keep following up while it pays.
    "max_rounds": 6,
    "planned_queries": 10,
    "followup_queries": 4,
    # Plan before searching: one staged search of a broad question returns
    # the overview pages its words weigh most, which is what `long` did.
    "staged_retrieval": False,
    "defer_graph_until_insufficient": False,
    "combine_grade_and_answer": False,
    "grade_evidence": True,
    # Retrieval: a wider window, each document's share proportional to it.
    "per_query_results": 12,
    "max_context_passages": 48,
    "passages_per_document": "auto",
    "document_fraction": 0.25,
    "document_floor": 2,
    "document_window_share": 0.75,
    "scale_document_chars": True,
    "expand_per_node": 4,
    # Response: more sections, each with room to say what its passages hold.
    "max_sections": 8,
    "section_output_tokens": 2000,
    "outline_output_tokens": 1200,
    "section_concurrency": 4,
    "deadline_seconds": 300,
}

#: Characters per token when projecting what writing will cost. Generous on
#: purpose (English runs nearer 4): an estimate that errs high stops research
#: a little early, one that errs low lets the write pass the limit.
CHARS_PER_TOKEN = 3.5
#: Prompt text each section call carries beside its passages: rules, question.
SECTION_OVERHEAD_TOKENS = 1500


def active(state: dict) -> bool:
    """Whether this question is deep research. Compose wins: an artifact has its
    own breadth and its own review, and is written in one call."""

    return state.get("intent") != "compose" and routing.deep_research(
        str(state.get("depth") or ""), str(state.get("depth_by") or "")
    )


def apply(options: dict[str, Any]) -> dict[str, Any]:
    """``options`` with :data:`DEEP_DEFAULTS`, then ``deep_options``, laid over."""

    merged = dict(options)
    merged.update(DEEP_DEFAULTS)
    merged.update(options.get("deep_options") or {})
    merged["research"] = "deep"
    return merged


def projected_write_cost(model: str | None, passages: list, options: dict) -> float:
    """What writing this answer from ``passages`` is projected to cost.

    The outline reads every passage's preview once; the sections between them
    read each passage in the window at its hydration allowance
    (``chars_per_passage``), which is what writing actually sends. Output is
    every section at its cap plus the outline. ``0.0`` when the model has no
    stated price: its calls are not counted either, and the step says so.
    """

    from pheasant.telemetry.spend import GUARD

    window = passages[: int(options.get("max_context_passages") or 48)]
    previews = sum(len(str(getattr(p, "snippet", "") or "")) for p in window)
    hydrated = len(window) * int(options.get("chars_per_passage") or 4000)
    sections = int(options.get("max_sections") or 8)
    read = int((previews + hydrated) / CHARS_PER_TOKEN) + SECTION_OVERHEAD_TOKENS * (sections + 1)
    written = sections * int(options.get("section_output_tokens") or 2000) + int(
        options.get("outline_output_tokens") or 1200
    )
    micros = GUARD.call_micros(model, read, written, 0)
    return (micros or 0) / 1_000_000


def stop_reason(state: dict, update: dict, ctx: dict, options: dict) -> str | None:
    """Why research should stop after this grade, or ``None`` to let the loop decide.

    ``update`` is what the grade node is about to write; the answer is the
    state after it.
    """

    grade = update.get("grade") or state.get("grade") or {}
    if grade.get("sufficient"):
        return None  # the loop already writes now; nothing to add
    passages = update.get("passages", state.get("passages")) or []
    if not passages:
        return "the knowledge base holds nothing that answers this"
    if update.get("no_new_evidence", state.get("no_new_evidence")):
        return "another round found nothing new; answering from what the knowledge base holds"
    spend = answer_spend.current()
    if spend is not None and spend.enforced:
        llm = ctx.get("llm")
        projected = projected_write_cost(getattr(llm, "model_id", None), passages, options)
        if spend.wrap_up(projected):
            return (
                f"wrapping up: research spent {spend.money()} and writing is projected at "
                f"{spend.money(projected)}, within 5% of this answer's "
                f"{spend.money(spend.limit)} limit"
            )
    return None


def section_allowed() -> str | None:
    """``None`` when a section may be written by the model; else why it may not."""

    spend = answer_spend.current()
    if spend is not None and spend.reached():
        return f"this answer's {spend.money(spend.limit)} limit was reached"
    return None


def summary_step(spend: answer_spend.AnswerSpend | None) -> str | None:
    """The closing line of a deep answer's trace: what it spent of what limit."""

    if spend is None:
        return None
    if not spend.enforced:
        return "deep research; spend not limited (usage metering is off or no limit is set)"
    detail = f"deep research spent {spend.money()} of its {spend.money(spend.limit)} limit"
    if spend.unpriced:
        detail += f"; {', '.join(sorted(spend.unpriced))} had no stated price and counted nothing"
    return detail


def after_grade(state: dict, update: dict, ctx: dict, options: dict) -> dict:
    """The grade node's update, with research's stop (and its step) when it stops."""

    if not active(state):
        return update
    reason = stop_reason(state, update, ctx, options)
    if not reason:
        return update
    steps = list(update.get("steps") or state.get("steps") or [])
    steps.append(WorkflowStep(name="research", detail=f"stopped: {reason}"))
    return {**update, "research_stop": reason, "steps": steps}


def finish(final: dict, route: dict) -> list:
    """The answer's steps, closed with what deep research spent; marks ``route``."""

    steps = list(final.get("steps", []))
    if active(final):
        route["research"] = "deep"
        closing = summary_step(answer_spend.current())
        if closing:
            steps.append(WorkflowStep(name="research", detail=closing))
    return steps


def most_rounds(options: dict) -> int:
    """The most rounds any reading of this question may run: LangGraph's runaway
    guard is sized before classify knows whether this is deep research."""

    return max(
        int(options.get("max_rounds") or 1),
        int(DEEP_DEFAULTS["max_rounds"]),
        int((options.get("deep_options") or {}).get("max_rounds") or 0),
    )
