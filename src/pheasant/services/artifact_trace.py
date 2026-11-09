"""What an artifact records about how it was made, and a whole thread as one artifact.

Traceability is the point of keeping generated work: a file nobody can trace
back to the question, the evidence and the judgement behind it is a file
nobody can trust. Every kept answer carries a ``trace``:

* **where it came from** -- the conversation, the channel (``ui`` / ``api`` /
  ``mcp``), the answer and trace ids that join it to the stream, the ledger
  and the span;
* **what was asked for** -- the ``@`` keywords, the routed intent, depth and
  workflow;
* **how sure it was** -- the judgements the answering loop made
  (``decisions``: sufficiency, acceptance and their probabilities), whether
  the answer was degraded, and how it was produced (``answer_mode``);
* **how it got there** -- the steps the workflow took, each bounded;
* **what it rests on** -- the citations, kept beside it as the artifact's
  evidence and drawn into the graph as ``references`` edges by its README.

Bounded throughout: a trace is evidence about an answer, not a second copy
of it, and a thread of forty turns must still fit in one row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pheasant.services.errors import InvalidRequest

#: Steps kept per answer, and characters per step detail.
MAX_STEPS = 60
MAX_DETAIL = 300
#: Turns one thread artifact holds.
MAX_TURNS = 100
CHANNELS = ("ui", "api", "mcp", "cli", "a2a")


def channel_of(value: Any, default: str = "api") -> str:
    channel = str(value or "").strip().lower()
    return channel if channel in CHANNELS else default


def _confidence(result: dict[str, Any]) -> dict[str, Any]:
    decisions = result.get("decisions")
    return {
        "decisions": decisions if isinstance(decisions, dict) else None,
        "accepted": (decisions or {}).get("accepted") if isinstance(decisions, dict) else None,
        "degraded": bool(result.get("degraded")),
        "status": result.get("status"),
        "answer_mode": result.get("answer_mode"),
    }


def _steps(result: dict[str, Any]) -> list[dict[str, Any]]:
    kept = []
    for step in (result.get("steps") or [])[:MAX_STEPS]:
        if not isinstance(step, dict):
            continue
        kept.append(
            {
                "name": step.get("name"),
                "detail": str(step.get("detail") or "")[:MAX_DETAIL],
                "passages": step.get("passages"),
                "duration_seconds": step.get("duration_seconds"),
            }
        )
    return kept


def trace_of(
    result: dict[str, Any],
    *,
    question: str,
    conversation_id: str | None = None,
    channel: str | None = None,
    answer_id: str | None = None,
) -> dict[str, Any]:
    """The trace a kept answer carries (see the module docstring)."""

    route = result.get("route") if isinstance(result.get("route"), dict) else {}
    lineage = result.get("lineage") if isinstance(result.get("lineage"), dict) else {}
    return {
        "question": question,
        "conversation_id": conversation_id,
        "channel": channel_of(channel),
        "answer_id": answer_id or result.get("answer_id"),
        "trace_id": result.get("trace_id") or lineage.get("trace_id"),
        "keywords": result.get("keywords"),
        "intent": route.get("intent"),
        "depth": route.get("depth"),
        "workflow": result.get("workflow") or route.get("workflow"),
        "confidence": _confidence(result),
        "steps": _steps(result),
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def transcript(turns: list[dict[str, Any]]) -> str:
    """The thread as Markdown: each question, its answer, and what it rested on."""

    lines: list[str] = []
    for turn in turns:
        trace = turn.get("trace") or {}
        meta = [str(v) for v in (trace.get("intent"), trace.get("channel")) if v]
        lines += [f"## {turn['turn']}. {turn['question'] or '(no question)'}", ""]
        if meta:
            lines += [f"*{' · '.join(meta)}*", ""]
        lines += [turn["answer"].strip() or "_(no answer)_", ""]
        cited = [c for c in turn.get("citations") or [] if c.get("path")]
        if cited:
            lines.append(
                "Sources: "
                + "; ".join(
                    f"[{c.get('index')}] {c.get('source')} · {c.get('path')}" for c in cited
                )
            )
            lines.append("")
    return "\n".join(lines)


def thread_payload(
    turns: Any, *, conversation_id: str | None, channel: str | None
) -> tuple[dict[str, Any], str]:
    """A whole conversation as an artifact's payload; its title.

    The region keeps no chat state, so the caller sends the turns it holds:
    ``[{question, answer: <the answer payload>}]``, oldest first. Every turn
    keeps its own trace and citations, and any files a compose turn wrote are
    the thread's files too, so the turns before and after a compose stay with
    what it produced.
    """

    from pheasant.assistant.artifact_files import artifact_payload

    if not isinstance(turns, list) or not turns:
        raise InvalidRequest("a thread needs turns: [{question, answer}], oldest first")
    if len(turns) > MAX_TURNS:
        raise InvalidRequest(f"a thread holds at most {MAX_TURNS} turns; this one has {len(turns)}")
    from pheasant.services.artifact_files import kept_visual

    kept: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    visual: dict[str, Any] | None = None
    citations: list[dict[str, Any]] = []
    for number, turn in enumerate(turns, 1):
        if not isinstance(turn, dict):
            raise InvalidRequest(f"turns[{number - 1}] must be an object")
        question = str(turn.get("question") or "")
        answer = turn.get("answer") if isinstance(turn.get("answer"), dict) else {}
        compact = artifact_payload({**answer, "answer_mode": None}) if answer else {}
        turn_citations = compact.get("citations") or []
        if (answer.get("route") or {}).get("intent") == "compose":
            for file in artifact_payload(answer).get("files") or []:
                files.append({**file, "name": f"turn-{number}-{file['name']}"})
        citations += [{**c, "turn": number} for c in turn_citations]
        # The diagram drawn for this turn after the fact wins over the one it
        # was answered with; the thread shows the last one drawn.
        answered = answer.get("visual") if isinstance(answer.get("visual"), dict) else None
        turn_visual = kept_visual(turn.get("visual")) or (
            kept_visual(answered)
            if (answered or {}).get("status") == "ok" and (answered or {}).get("diagram")
            else None
        )
        visual = turn_visual or visual
        kept.append(
            {
                "turn": number,
                "question": question,
                "answer": str(answer.get("answer") or turn.get("text") or ""),
                "citations": turn_citations,
                "visual": turn_visual,
                "trace": trace_of(
                    answer,
                    question=question,
                    conversation_id=conversation_id,
                    channel=channel,
                    answer_id=turn.get("answer_id"),
                ),
            }
        )
    title = next((t["question"] for t in kept if t["question"].strip()), "Conversation")
    payload = {
        "question": kept[0]["question"],
        # The transcript is what every viewer and export renders, as a report's
        # answer is; `turns` keeps each turn's own trace beside it.
        "answer": transcript(kept),
        "turns": kept,
        "files": files,
        "citations": citations,
        "visual": visual,
    }
    return payload, title
