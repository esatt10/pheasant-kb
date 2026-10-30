"""How an answer's draft is written: from how many passages, at what effort.

The knobs that trade answer time against answer quality, kept apart from the
gates that judge the result (``decision_gates``), which is the module that
asks for them. Each defaults to what the loop did before it existed:

* ``synthesis_passages`` — how many of the ranked window the answer is
  written from. Lowering ``max_context_passages`` would also shrink the
  window the evidence is *judged* on, and a request's ``max_results`` floors
  that one; this changes only the answer's prompt.
* ``repair_efforts`` — the effort each rewrite after a rejected draft is
  written at, entry by entry, ``full`` past the end; with ``max_repairs`` it
  is what lets a cheap rewrite escalate only when it is rejected too.

An effort is only ever *lowered*, and only on ``gpt-6-luna``, the one model
the wire sends an effort to.
"""

from __future__ import annotations

from typing import Any

from pheasant.assistant.catalog import OPENAI_WIRE
from pheasant.assistant.decisions import Decider
from pheasant.config.assistant_decisions import REPAIR_EFFORTS

#: The most rewrites one request may ask for through ``options.max_repairs``.
MAX_REQUEST_REPAIRS = 3

_EFFORT_ORDER = {"none": 0, "low": 1, None: 2}


def window(passages: list, options: dict[str, Any]) -> list:
    """The passages the answer is written from: the top ``synthesis_passages``."""

    cap = options.get("synthesis_passages")
    if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
        return passages[:cap]
    return passages


def for_request(decider: Decider, options: dict[str, Any]) -> Decider:
    """Apply the per-request ``max_repairs`` / ``repair_efforts`` options."""

    max_repairs, efforts = decider.max_repairs, decider.repair_efforts
    requested = options.get("max_repairs")
    if isinstance(requested, int) and not isinstance(requested, bool):
        if 0 <= requested <= MAX_REQUEST_REPAIRS:
            max_repairs = requested
    ladder = options.get("repair_efforts")
    ladder = [ladder] if isinstance(ladder, str) else ladder
    # All or nothing: dropping one bad entry would shift every later rewrite
    # onto the wrong effort. What ran is in the payload's ``decisions`` block.
    if isinstance(ladder, (list, tuple)) and all(e in REPAIR_EFFORTS for e in ladder):
        efforts = tuple(ladder)
    if (max_repairs, efforts) == (decider.max_repairs, decider.repair_efforts):
        return decider
    return decider.with_repairs(max_repairs, efforts)


def repair_llm(llm: Any, decider: Decider, attempt: int) -> tuple[Any, str | None]:
    """The model rewrite number ``attempt`` (1-based) is written with, and its effort."""

    effort = decider.repair_effort(attempt)
    return at_effort(llm, None if effort == "full" else effort)


def at_effort(llm: Any, effort: str | None) -> tuple[Any, str | None]:
    """``llm`` lowered to ``effort``, and the effort it now writes at.

    ``None`` back means "unchanged": no effort asked for, a model other than
    ``gpt-6-luna``, or one already configured at or below it — an effort is
    never raised above the assistant's own.
    """

    if (
        llm is None
        or effort is None
        or getattr(llm, "provider", None) not in OPENAI_WIRE
        or getattr(llm, "model_id", None) != "gpt-6-luna"
        or not callable(getattr(llm, "with_reasoning_effort", None))
    ):
        return llm, None
    configured = getattr(llm, "reasoning_effort", None)
    lowest = min((effort, configured), key=lambda e: _EFFORT_ORDER.get(e, 2))
    if lowest == configured:
        return llm, None
    return llm.with_reasoning_effort(lowest), lowest
