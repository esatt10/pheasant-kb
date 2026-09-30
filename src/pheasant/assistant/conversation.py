"""Conversation continuity: what the earlier turns of a chat contribute.

The region keeps no conversation state. The MCP transport is stateless by
design, and a browser tab is the only thing that knows which turns belong
together, so the caller sends the recent turns and this module decides what
they are allowed to do. Three things, each deliberately narrow:

1. **Find the question.** "What about the second one?" retrieves nothing on
   its own. A follow-up is rewritten into a standalone search question — by
   the model when one is connected (one short call, only for questions that
   look like follow-ups), and deterministically otherwise by joining it to the
   question it follows, which is what a lexical index needs to find the same
   material again.
2. **Keep the evidence.** The previous question is searched again and its hits
   join the new ones at a lower weight. Not the previous turn's cited *node
   ids*: those arrive from the caller, and fetching whatever ids a caller names
   would let it read nodes its principal cannot. Re-running the question goes
   through the same ACL, criteria and memory policy as every other search, and
   over an unchanged index it reproduces that turn's evidence exactly.
3. **Show the model the conversation.** Earlier answers go into the prompt with
   their ``[n]`` markers stripped — those numbers index a citation list that
   no longer exists, and left in they would be read as citations of this
   turn's passages.

A question with no history is answered exactly as it was before this module
existed; ``tests/test_conversation.py`` asserts the prompt is byte-identical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

#: Turns kept, newest last. Older turns rarely change what a follow-up means
#: and every one of them is prompt the model reads on every answer.
MAX_HISTORY_TURNS = 6
#: Characters of each earlier answer shown to the model. Enough to resolve
#: "the second option"; not enough to re-answer the old question.
MAX_ANSWER_CHARS = 1200
#: Hard ceiling on what a caller may send, so a runaway client cannot turn
#: history into an unbounded request body the region has to parse.
MAX_HISTORY_ITEMS = 50
#: Weight on evidence carried over from the previous question. Below 1 so a
#: follow-up's own hits lead; well above 0 so the earlier sources stay in play.
CARRIED_WEIGHT = 0.5


class HistoryError(ValueError):
    """History the region cannot make sense of."""


@dataclass(frozen=True)
class Turn:
    """One earlier exchange, as the model will see it."""

    question: str
    answer: str = ""


_MARKER_RE = re.compile(r"\s?\[(?:\d{1,2}|fig:\d{1,2})\]")


def normalize_history(raw: Any) -> list[Turn]:
    """Validate and trim the caller's turns. Raises :class:`HistoryError`."""

    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise HistoryError("history must be a list of {question, answer} turns")
    if len(raw) > MAX_HISTORY_ITEMS:
        raise HistoryError(
            f"history holds at most {MAX_HISTORY_ITEMS} turns; send the most recent ones"
        )
    turns: list[Turn] = []
    for index, item in enumerate(raw):
        if isinstance(item, Turn):
            turns.append(item)
            continue
        if not isinstance(item, dict):
            raise HistoryError(f"history[{index}] must be an object with a question")
        question = item.get("question")
        if not isinstance(question, str) or not question.strip():
            raise HistoryError(f"history[{index}] needs a non-empty question")
        answer = item.get("answer") or ""
        if not isinstance(answer, str):
            raise HistoryError(f"history[{index}].answer must be text")
        turns.append(Turn(question=question.strip(), answer=_clean_answer(answer)))
    return turns[-MAX_HISTORY_TURNS:]


def _clean_answer(answer: str) -> str:
    text = _MARKER_RE.sub("", answer).strip()
    if len(text) > MAX_ANSWER_CHARS:
        text = text[:MAX_ANSWER_CHARS].rstrip() + "…"
    return text


# Deterministic follow-up signals. A question that leans on something it does
# not name — a pronoun, an ordinal, "what about" — cannot be retrieved alone.
_FOLLOW_UP_PATTERNS = (
    r"^(?:and|also|so|but|then|or)\b",
    r"^(?:what|how) about\b",
    r"^why(?: not| is that| does it)?\??$",
    r"\b(?:tell me more|go on|continue|elaborate|expand on (?:that|this|it)|more detail)\b",
    r"\b(?:it|its|they|them|their|this|that|these|those)\b",
    r"\bthe (?:first|second|third|last|former|latter|same|above|previous|other) (?:one|option|"
    r"step|point|part|file|approach)s?\b",
)
_FOLLOW_UP_RE = re.compile("|".join(_FOLLOW_UP_PATTERNS))


def follow_up_reason(question: str, history: list[Turn]) -> str | None:
    """Why this question reads as a follow-up, or ``None`` if it stands alone.

    Only ever true with history: the same words at the start of a
    conversation are a question about "it" in the corpus, not about a turn.
    """

    if not history:
        return None
    text = " ".join((question or "").lower().split())
    if not text:
        return None
    if len(text.split()) <= 4:
        return "short question after an earlier turn"
    match = _FOLLOW_UP_RE.search(text)
    if match:
        return f"refers back (“{match.group(0).strip()}”)"
    return None


REWRITE_SYSTEM = """You rewrite a follow-up question into a standalone search \
question for a knowledge base. Use the conversation to replace pronouns and \
references ("it", "the second one", "that approach") with what they refer to. \
Keep the user's intent and wording otherwise. Reply with the rewritten \
question only — no quotes, no preamble."""


def standalone_question(
    question: str, history: list[Turn], llm: Any = None
) -> tuple[str, str | None]:
    """The question to *search* for, and how it was derived.

    Returns ``(question, None)`` when the question stands alone. The question
    *answered* is always the user's own; only retrieval sees the rewrite.
    """

    reason = follow_up_reason(question, history)
    if reason is None:
        return question, None
    previous = history[-1]
    if llm is not None:
        rewritten = llm.try_complete(
            REWRITE_SYSTEM,
            f"{history_block(history)}\nFollow-up question: {question}",
            max_output_tokens=120,
        )
        rewritten = " ".join(str(rewritten or "").split()).strip("\"' ")
        if rewritten and len(rewritten) <= 400:
            return rewritten, f"{reason}; rewritten as “{rewritten}”"
        # Say why the model's rewrite was not used: the joined search below is
        # a worse question, and a trace that does not mention the model reads
        # as though no model had been asked.
        failure = getattr(llm, "last_failure", None) or (
            "reply too long" if rewritten else "no reply"
        )
        joined = f"{previous.question} {question}"
        return joined, (
            f"{reason}; model rewrite unavailable ({str(failure).splitlines()[0][:80]}); "
            "searched together with the previous question"
        )
    joined = f"{previous.question} {question}"
    return joined, f"{reason}; searched together with the previous question"


def carried_question(history: list[Turn]) -> str | None:
    """The previous question, whose evidence a follow-up keeps in play."""

    return history[-1].question if history else None


def history_block(history: list[Turn]) -> str:
    """The earlier turns as prompt text. Empty when there are none."""

    if not history:
        return ""
    lines = ["Earlier in this conversation (context only — cite the passages, not this):"]
    for turn in history:
        lines.append(f"Q: {turn.question}")
        if turn.answer:
            lines.append(f"A: {turn.answer}")
    lines.append("")
    return "\n".join(lines)


def carry(found: list[Any], carried: list[Any]) -> list[Any]:
    """Merge a follow-up's hits with the previous question's, which rank lower.

    ``carried`` passages keep their identity but take ``CARRIED_WEIGHT`` of
    their score and ``mode="carried"``, so a caller can tell evidence the
    follow-up found from evidence it inherited.
    """

    merged = {passage.key(): passage for passage in found}
    for passage in carried:
        if passage.key() in merged:
            continue
        # A copy: the retriever memoizes passages per query, and down-weighting
        # the cached object would hand the reduced score to the next caller
        # that asks the previous question for its own sake.
        merged[passage.key()] = replace(
            passage, score=passage.score * CARRIED_WEIGHT, mode="carried"
        )
    return sorted(merged.values(), key=lambda p: (-p.score, p.key()))
