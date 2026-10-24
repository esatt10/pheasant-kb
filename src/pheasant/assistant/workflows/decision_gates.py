"""The agentic loop's judgements, asked through the Decisions API.

Measured on the lab region before this existed, 94% of an answer's time was
one model call (``grade_answer``, 6.0 s mean against 0.25 s of retrieval):
the combined path writes a whole answer in order to learn whether the
evidence was sufficient, and throws it away when it was not. The four gates
here take the *judgements* out of that call:

* **sufficiency** — a predicate over the evidence, plus a choice naming the
  kind of miss (content / location / file kind / absent). Nothing is written
  until the evidence is judged sufficient, so a thin round costs one
  decision (0.24 s p50) instead of one answer.
* **followup** — a choice among follow-up searches built from the evidence
  itself (the directories and symbols the hits carry, the question's own
  terms). The API writes nothing, so it can only *pick*; "none of these"
  hands the round to the model planner, as before.
* **prune** — a relevance predicate per passage, asked in the same request,
  so the answer is written from what bears on the question rather than from
  every hit.
* **accept** — after the answer is written and its citations verified: is
  every claim supported by the passage it cites, and does it answer the
  question. Because a rejected draft is rewritten (at the assistant's own
  effort unless ``repair_efforts`` says otherwise), the first draft can be
  written at ``draft_reasoning_effort`` — cheap first, escalate on doubt.

Every judgement that cannot be had (no key, a refusal, a timeout, a circuit
opened by an earlier 401/404) falls back to the model-written path the loop
always had, and the step says so. ``shadow`` mode asks the same questions
beside that path and acts on none of them, recording only whether they
agreed — which is how a threshold is checked against reality before it is
trusted.
"""

from __future__ import annotations

import contextvars
import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, TypedDict

from pheasant.assistant import decisions
from pheasant.assistant.decisions import Decider, DecisionUnavailable
from pheasant.assistant.workflows import WorkflowStep, drafting

#: How much of each cited document the acceptance gate reads. The writer read
#: whole documents (``chat.hydrate_citations``); a judge reading less would
#: call every claim drawn from past its cut unsupported — measured on the lab
#: region, a 4,000-character cut sent a correct answer to a rewrite.
ACCEPT_DOCUMENT_CHARS = 16_000
ACCEPT_TOTAL_CHARS = 120_000

#: Kinds of miss, as the sufficiency request offers them. The description is
#: what the trace says is missing, since the API writes no prose of its own.
MISS_KINDS: dict[str, str] = {
    "none": "Nothing the question needs is missing.",
    "content": "The passages are about the right thing but lack the specific facts, steps "
    "or examples the answer needs.",
    "location": "The passages are the right kind of file from the wrong package, directory "
    "or component.",
    "kind": "The passages are the wrong kind of file: documentation where source code or a "
    "usage example is needed, or tests instead of the implementation.",
    "absent": "Nothing in the passages bears on the question; the knowledge base may not cover it.",
}
#: Insufficient, yet the miss choice said "none": the two judgements are
#: independent questions and can disagree. Said as it is rather than reported
#: as "missing: none" beside "insufficient".
UNCLEAR = "Below the sufficiency threshold, with no specific gap named."


def miss_of(choice: Any, sufficient: bool) -> str:
    """The miss kind to report: ``none`` when sufficient, ``unclear`` when the
    judgements disagree, otherwise the chosen kind."""

    if sufficient:
        return "none"
    kind = choice.choice if choice is not None and choice.choice in MISS_KINDS else "unclear"
    return "unclear" if kind == "none" else kind


def miss_description(kind: str) -> str:
    return UNCLEAR if kind == "unclear" else MISS_KINDS.get(kind, UNCLEAR)


SUFFICIENCY = (
    "Taken together, do the passages contain enough evidence to answer the question "
    "correctly and specifically? Judge the substance, not the wording, and use the file "
    "metadata: a result set from the wrong place or of the wrong kind is not enough even "
    "when each passage reads plausibly."
)
CRITERIA = {
    "knowledge": " This is a knowledge-summary question: enough means one could say what the "
    "thing is, what its main parts are and what each is for. Exhaustive coverage is not "
    "required.",
    "procedural": " This is a procedural question: enough means a followable sequence AND at "
    "least one real example with the actual identifiers, arguments or config keys. Naming the "
    "right file, or describing a capability in prose, is not enough.",
}
GROUNDED = (
    "Is the ANSWER supported by the passages it cites with [n] markers? Say no if it states "
    "something a cited passage contradicts, attributes a claim to a passage that does not "
    "support it, or makes a substantive factual claim that none of the passages supports. "
    "Summaries, paraphrases and connecting sentences that follow from the passages count as "
    "supported, and saying that evidence is missing is not a claim."
)
RESPONSIVE = (
    "Does the ANSWER respond to the QUESTION that was asked? An answer that says plainly what "
    "the knowledge base does not cover counts as responding."
)
NONE_OF_THESE = "none"

_STOPWORDS = frozenset(
    "a an and are as at be by can could do does for from how i in is it its me of on or "
    "should show tell that the their there these this to use using was what when where which "
    "who why will with would you your about into does explain describe".split()
)

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pheasant-decision")


class DecisionState(TypedDict, total=False):
    """The gates' half of the agent state (``AgentState`` inherits it).

    Declared rather than smuggled through other keys because LangGraph keeps
    only the keys its state schema names: a node returning one it does not
    know is a write that silently goes nowhere.
    """

    decided: bool
    decision_log: list[dict]
    relevance: dict[str, float]
    evidence_texts: dict[int, str]
    draft_effort: str | None
    acceptance: dict
    repair_reason: str
    repair_pending: bool
    repairs: int


@dataclass
class GateOutcome:
    """What a gate did before the classic node ran (or instead of it)."""

    update: dict[str, Any] | None = None
    notes: list[WorkflowStep] = field(default_factory=list)
    shadow: Future | None = None


# ----------------------------------------------------------------- handles


def for_request(decider: Decider | None, options: dict[str, Any]) -> Decider | None:
    """Apply the per-request ``decisions`` / ``decision_gates`` options."""

    if decider is None:
        return None
    mode = str(options.get("decisions") or decider.mode).strip().lower()
    if mode not in decisions.MODES or mode == "off":
        return None
    gates = options.get("decision_gates")
    if isinstance(gates, (list, tuple)):
        decider = decider.with_gates(tuple(g for g in gates if g in decisions.GATES))
    return drafting.for_request(decider, options).with_mode(mode)


def active(ctx: dict, gate: str, mode: str = "beta") -> bool:
    """True when ``gate`` acts in this request and its endpoint is usable."""

    decider: Decider | None = ctx.get("decider")
    return bool(decider and decider.gate(gate, mode) and not decider.unavailable_reason)


def _log(state: dict, entry: dict[str, Any]) -> list[dict[str, Any]]:
    return [*(state.get("decision_log") or []), entry]


def _unavailable(gate: str, exc: Exception | str) -> tuple[str, dict[str, Any]]:
    reason = str(exc) or type(exc).__name__
    decisions.record(gate, "unavailable")
    return reason, {"gate": gate, "outcome": "unavailable", "reason": reason}


# ---------------------------------------------------------------- evidence


@dataclass
class Shown:
    """A passage as the gates read one: what a caller's own evidence becomes."""

    title: str
    snippet: str
    node_id: str | None = None
    relative_path: str | None = None


def evidence_input(
    question: str, intent: str, passages: list, shapes: dict, *, chars: int = 700
) -> str:
    """The shared evidence every sufficiency question reads.

    The same shape the model grader is given: numbered passages with file
    metadata and their preview text, and the result set's file types —
    because the *shape* of a result set is most of what a sufficiency
    judgement turns on, and a snippet alone does not show it.
    """

    from pheasant.assistant.workflows.agentic import _describe

    lines = [f"Question: {question}", f"Question type: {intent}", "", "Passages:"]
    for index, passage in enumerate(passages, start=1):
        lines.append(f"[{index}] {passage.title}{_describe(shapes.get(passage.node_id or ''))}")
        lines.append(" ".join(str(passage.snippet or "").split())[:chars])
        lines.append("")
    if shapes:
        kinds = sorted({str(meta.get("type") or "?") for meta in shapes.values()})
        lines.append(f"Result set: {len(passages)} passage(s); file types: {', '.join(kinds)}")
    return "\n".join(lines)


def followup_candidates(
    question: str, intent: str, passages: list, shapes: dict, searched: list[str]
) -> list[str]:
    """Follow-up searches built from what the evidence already names.

    The Decisions API picks; it cannot write. So the candidates come from
    the hits' own directories and symbols and from the question's terms —
    the structural vocabulary the model planner is told to use, read off the
    index rather than invented. Deterministic: the same evidence offers the
    same candidates.
    """

    from pheasant.assistant.workflows.agentic import _dedupe, _query_key

    terms = [
        word
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_.\-]*", question)
        if word.lower() not in _STOPWORDS and len(word) > 2
    ]
    base = " ".join(dict.fromkeys(terms))[:80].strip()
    if not base:
        return []
    short = " ".join(list(dict.fromkeys(terms))[:3])
    candidates: list[str] = []
    directories: list[str] = []
    for passage in passages[:8]:
        path = str(passage.relative_path or "")
        if "/" in path:
            directory = path.rsplit("/", 1)[0]
            if directory not in directories:
                directories.append(directory)
    candidates += [f"{directory} {short}" for directory in directories[:3]]
    lowered = question.lower()
    symbols: list[str] = []
    for meta in shapes.values():
        for symbol in meta.get("symbols") or []:
            if symbol.lower() not in lowered and symbol not in symbols:
                symbols.append(str(symbol))
    suffix = "example usage" if intent == "procedural" else ""
    candidates += [f"{symbol} {suffix}".strip() for symbol in symbols[:3]]
    if intent == "procedural":
        candidates.append(f"{base} example configuration")
    else:
        candidates.append(f"{base} overview")
    done = {_query_key(query) for query in searched}
    return [query for query in _dedupe(candidates) if _query_key(query) not in done][:6]


def _relevance_questions(count: int) -> list[dict[str, Any]]:
    return [
        decisions.predicate(
            f"relevant_{index}",
            f"Does passage [{index}] contain information a correct answer to the question "
            "would use?",
        )
        for index in range(1, count + 1)
    ]


# --------------------------------------------------------------- sufficiency


def grade(state: dict, ctx: dict, options: dict) -> GateOutcome:
    """Judge the evidence instead of (beta) or beside (shadow) the model grader."""

    decider: Decider | None = ctx.get("decider")
    if decider is None or "sufficiency" not in decider.gates or not options.get("grade_evidence"):
        return GateOutcome()
    passages = state.get("passages") or []
    if decider.mode == "shadow":
        if not passages or decider.unavailable_reason:
            return GateOutcome()
        context = contextvars.copy_context()
        return GateOutcome(
            shadow=_POOL.submit(context.run, _ask_sufficiency, state, ctx, options, False)
        )
    if decider.mode != "beta":
        return GateOutcome()
    round_index = int(state.get("round", 0)) + (0 if state.get("broadening_grade_pending") else 1)
    if decider.unavailable_reason:
        reason, entry = _unavailable("sufficiency", decider.unavailable_reason)
        return GateOutcome(
            notes=[_note(f"decisions unavailable ({reason}); grading with the model")]
        )
    if not passages:
        return GateOutcome(
            update=_decided(
                state,
                round_index,
                {"sufficient": False, "missing": "no matching passages", "next_queries": []},
                "no matching evidence to judge",
            )
        )
    if state.get("no_new_evidence") and state.get("round", 0) > 0:
        missing = str((state.get("grade") or {}).get("missing") or "the missing evidence")
        return GateOutcome(
            update=_decided(
                state,
                round_index,
                {"sufficient": False, "missing": missing, "next_queries": [], "stop": True},
                "stopped: the follow-up added no new evidence",
            )
        )
    try:
        judged = _ask_sufficiency(state, ctx, options, True)
    except DecisionUnavailable as exc:
        reason, entry = _unavailable("sufficiency", exc)
        return GateOutcome(
            notes=[_note(f"decisions unavailable ({reason}); grading with the model")]
        )
    update = _decided(state, round_index, judged["grade"], judged["detail"])
    update["relevance"] = {**(state.get("relevance") or {}), **judged["relevance"]}
    update["decision_log"] = [*update["decision_log"], *judged["log"]]
    update["steps"][-1].duration_seconds = judged["seconds"]
    return GateOutcome(update=update)


def _note(detail: str) -> WorkflowStep:
    return WorkflowStep(name="decide", detail=detail, duration_seconds=0.0)


def _decided(state: dict, round_index: int, grade: dict, detail: str) -> dict[str, Any]:
    grade = {**grade, "next_query": (grade.get("next_queries") or [""])[0]}
    return {
        "round": round_index,
        "broadening_grade_pending": False,
        "decided": True,
        "grade": grade,
        "decision_log": list(state.get("decision_log") or []),
        "steps": [
            *state.get("steps", []),
            WorkflowStep(name="decide", detail=detail, passages=len(state.get("passages") or [])),
        ],
    }


def _ask_sufficiency(state: dict, ctx: dict, options: dict, acting: bool) -> dict[str, Any]:
    """One Decisions request: sufficiency, miss kind, follow-up, relevance."""

    decider: Decider = ctx["decider"]
    retriever = ctx["retriever"]
    intent = str(state.get("intent") or "knowledge")
    window = state.get("passages", [])[: int(options["max_context_passages"])]
    shapes = retriever.metadata([p.node_id for p in window if p.node_id])
    round_index = int(state.get("round", 0)) + 1
    questions = [
        decisions.predicate("sufficient", SUFFICIENCY + CRITERIA.get(intent, "")),
        decisions.choice(
            "miss",
            "What, if anything, is the main gap between the passages and what the question needs?",
            list(MISS_KINDS.items()),
        ),
    ]
    candidates: list[str] = []
    if decider.gate("followup", decider.mode) and round_index < int(options["max_rounds"]):
        candidates = followup_candidates(
            state["question"], intent, window, shapes, state.get("searched_queries") or []
        )
        if candidates:
            questions.append(
                decisions.choice(
                    "followup",
                    "If the passages are not enough, which search would most likely find what "
                    "is missing?",
                    [(query, f"Search for: {query}") for query in candidates]
                    + [(NONE_OF_THESE, "None of these searches targets the gap.")],
                )
            )
    relevance_window = window if decider.gate("prune", decider.mode) else []
    if len(relevance_window) > decider.prune_min_keep:
        questions += _relevance_questions(len(relevance_window))
    else:
        relevance_window = []
    result = decider.decide(evidence_input(state["question"], intent, window, shapes), questions)
    probability = result.probability("sufficient")
    if probability is None:
        raise DecisionUnavailable("the sufficiency question was refused")
    sufficient = probability >= decider.sufficient_threshold
    miss = result.choice("miss")
    miss_kind = miss_of(miss, sufficient)
    followup = result.choice("followup")
    chosen = (
        followup.choice if followup and followup.choice in candidates and not sufficient else ""
    )
    relevance = {
        passage.key(): result.probability(f"relevant_{index}")
        for index, passage in enumerate(relevance_window, start=1)
        if result.probability(f"relevant_{index}") is not None
    }
    outcome = "sufficient" if sufficient else "insufficient"
    if acting:
        decisions.record("sufficiency", outcome, result.seconds)
        if candidates and not sufficient:
            decisions.record("followup", "chosen" if chosen else "none")
    missing = "" if sufficient else miss_description(miss_kind)
    if sufficient:
        detail = f"evidence is sufficient (p={probability:.2f})"
    else:
        detail = f"missing: {miss_kind} (p={probability:.2f})" + (
            f"; next: “{chosen}”" if chosen else ""
        )
    log = [
        {
            "gate": "sufficiency",
            "outcome": outcome,
            "probability": round(probability, 4),
            "threshold": decider.sufficient_threshold,
            "miss": miss_kind,
            "seconds": round(result.seconds, 3),
            "input_tokens": result.input_tokens,
            "round": round_index,
        }
    ]
    if candidates and not sufficient:
        log.append(
            {
                "gate": "followup",
                "outcome": "chosen" if chosen else "none",
                "candidates": candidates,
                "choice": followup.choice if followup else None,
                "confidence": followup.confidence if followup else None,
            }
        )
    return {
        "grade": {
            "sufficient": sufficient,
            "missing": missing,
            "next_queries": [chosen] if chosen else [],
            "miss": miss_kind,
            "probability": probability,
        },
        "relevance": relevance,
        "detail": detail,
        "log": log,
        "seconds": result.seconds,
    }


def after_classic(outcome: GateOutcome, state: dict, ctx: dict, result: dict) -> dict:
    """Merge a gate's notes and shadow judgement into the classic grade."""

    result = dict(result)
    result["decided"] = False
    if outcome.notes:
        before = len(state.get("steps") or [])
        steps = list(result.get("steps") or state.get("steps") or [])
        result["steps"] = steps[:before] + outcome.notes + steps[before:]
        result["decision_log"] = _log(
            state,
            {"gate": "sufficiency", "outcome": "unavailable", "detail": outcome.notes[0].detail},
        )
    if outcome.shadow is not None:
        decider: Decider = ctx["decider"]
        try:
            judged = outcome.shadow.result(timeout=decider.timeout + 1.0)
        except Exception as exc:  # noqa: BLE001 - a shadow never fails the answer
            _, entry = _unavailable("sufficiency", exc)
            result["decision_log"] = _log(state, {**entry, "mode": "shadow"})
            return result
        acted = bool((result.get("grade") or {}).get("sufficient", True))
        agree = acted == judged["grade"]["sufficient"]
        decisions.record_shadow("sufficiency", agree)
        result["decision_log"] = [
            *(state.get("decision_log") or []),
            *(
                {**entry, "mode": "shadow", "acted_on": acted, "agree": agree}
                for entry in judged["log"]
            ),
        ]
    return result


def route_after_grade(state: dict, ctx: dict, options: dict) -> str | None:
    """Where a decided round goes. ``None`` for a round the model graded."""

    if not state.get("decided"):
        return None
    from pheasant.assistant.workflows.agentic import _defer_graph_for_request

    grade = state.get("grade") or {}
    if grade.get("sufficient") or grade.get("stop"):
        return "synthesize"
    if int(state.get("round", 0)) >= int(options["max_rounds"]):
        return "synthesize"
    if _defer_graph_for_request(state, ctx, options) and not state.get("graph_fallback_done"):
        return "plan"
    if options.get("staged_retrieval") and not state.get("fallback_done"):
        return "broaden"
    if not grade.get("next_queries") and not state.get("passages"):
        return "synthesize"
    return "plan"


# ---------------------------------------------------------------- synthesis


@dataclass
class Draft:
    """How ``synthesize_node`` writes this answer."""

    passages: list
    llm: Any
    system_suffix: str = ""
    on_delta: Any = None
    effort: str | None = None
    steps: list[WorkflowStep] = field(default_factory=list)


def prepare_synthesis(state: dict, ctx: dict, options: dict, passages: list) -> Draft:
    """Cap and prune the passages, and pick the draft's effort and stream."""

    llm = ctx.get("llm")
    passages = drafting.window(passages, options)
    draft = Draft(passages=passages, llm=llm)
    decider: Decider | None = ctx.get("decider")
    if decider is None or decider.mode != "beta":
        return draft
    relevance = state.get("relevance") or {}
    repairing = bool(state.get("repair_reason"))
    if "prune" in decider.gates and relevance and len(passages) > decider.prune_min_keep:
        kept = [p for p in passages if relevance.get(p.key(), 1.0) >= decider.relevant_threshold]
        if len(kept) < decider.prune_min_keep:
            ranked = sorted(passages, key=lambda p: -float(relevance.get(p.key(), 1.0)))
            chosen = {p.key() for p in ranked[: decider.prune_min_keep]}
            kept = [p for p in passages if p.key() in chosen]
        # A rewrite reads the same cut as the draft it replaces, so the two
        # cite the same numbered passages; it is counted and shown once.
        draft.passages = kept
        dropped = len(passages) - len(kept)
        if not repairing:
            decisions.record("prune", "pruned" if dropped else "kept")
        if dropped and not repairing:
            draft.steps.append(
                WorkflowStep(
                    name="prune",
                    detail=f"left out {dropped} of {len(passages)} passage(s) judged irrelevant "
                    f"(p < {decider.relevant_threshold:g})",
                    passages=len(kept),
                    duration_seconds=0.0,
                )
            )
    if repairing:
        draft.system_suffix = (
            "\n\nREVISION: a reviewer judged an earlier draft of this answer "
            f"{state['repair_reason']}. Write it again so that every claim is stated by the "
            "passage it cites, and answer the question that was asked."
        )
    long_answer = str(state.get("depth") or "short") == "long"
    if repairing:
        draft.llm, draft.effort = drafting.repair_llm(llm, decider, int(state["repairs"]))
    elif not long_answer and active(ctx, "accept"):
        draft.llm, draft.effort = drafting.at_effort(llm, decider.draft_reasoning_effort)
    request = ctx.get("request")
    if (
        not repairing
        and not long_answer
        and state.get("decided")
        and request is not None
        and getattr(request, "on_draft", None) is not None
        and getattr(llm, "provider", None) == "openai"
    ):
        from pheasant.assistant.streaming import TextPreview

        draft.on_delta = TextPreview(request.on_draft)
    return draft


def finish_synthesis(
    state: dict, ctx: dict, answer: str, citations: list[dict], documents: dict, draft: Draft
) -> dict[str, Any]:
    """The evidence the acceptance gate reads, and an evidence-gap note."""

    out: dict[str, Any] = {"repair_pending": False}
    grade = state.get("grade") or {}
    if state.get("decided") and not grade.get("sufficient") and "Evidence gap:" not in answer:
        gap = grade.get("missing") or "the available passages are incomplete"
        answer = f"{answer}\n\nEvidence gap: {gap}" if answer else f"Evidence gap: {gap}"
    out["answer"] = answer
    if ctx.get("decider") is not None and "accept" in ctx["decider"].gates:
        texts: dict[int, str] = {}
        budget = ACCEPT_TOTAL_CHARS
        for citation in citations:
            index = citation["index"]
            text = documents[index].text if index in documents else ""
            text = (text or str(citation.get("snippet") or ""))[
                : min(ACCEPT_DOCUMENT_CHARS, budget)
            ]
            budget -= len(text)
            texts[int(index)] = text
        out["evidence_texts"] = texts
    out["draft_effort"] = draft.effort
    return out


# ---------------------------------------------------------------- acceptance


def accept_node(state: dict, ctx: dict) -> dict:
    """Judge the finished answer; ask for one rewrite when it is rejected."""

    decider: Decider | None = ctx.get("decider")
    if decider is None or "accept" not in decider.gates or decider.mode not in ("beta", "shadow"):
        return {}
    answer = str(state.get("answer") or "")
    citations = state.get("citations") or []
    if state.get("answer_mode") != "llm" or not answer or not citations:
        return {}
    shadow = decider.mode == "shadow"
    if decider.unavailable_reason:
        reason, entry = _unavailable("accept", decider.unavailable_reason)
        if shadow:
            return {"decision_log": _log(state, {**entry, "mode": "shadow"})}
        return _accept_step(state, f"decisions unavailable ({reason}); answer not judged", entry)
    texts = state.get("evidence_texts") or {}
    used = [c for c in citations if c.get("used")] or citations
    lines = [f"QUESTION: {state['question']}", "", "ANSWER:", answer, "", "PASSAGES:"]
    for citation in used:
        text = texts.get(int(citation["index"])) or str(citation.get("snippet") or "")
        lines += [f"[{citation['index']}] {citation.get('title') or ''}", text, ""]
    try:
        result = decider.decide(
            "\n".join(lines),
            [
                decisions.predicate("grounded", GROUNDED),
                decisions.predicate("responsive", RESPONSIVE),
            ],
        )
    except DecisionUnavailable as exc:
        reason, entry = _unavailable("accept", exc)
        if shadow:
            return {"decision_log": _log(state, {**entry, "mode": "shadow"})}
        return _accept_step(state, f"decisions unavailable ({reason}); answer not judged", entry)
    grounded, responsive = result.probability("grounded"), result.probability("responsive")
    if grounded is None or responsive is None:
        reason, entry = _unavailable("accept", "the acceptance questions were refused")
        if shadow:
            return {"decision_log": _log(state, {**entry, "mode": "shadow"})}
        return _accept_step(state, f"decisions unavailable ({reason}); answer not judged", entry)
    threshold = decider.accept_threshold
    accepted = grounded >= threshold and responsive >= threshold
    repairs = int(state.get("repairs") or 0)
    entry = {
        "gate": "accept",
        "grounded": round(grounded, 4),
        "responsive": round(responsive, 4),
        "threshold": threshold,
        "seconds": round(result.seconds, 3),
        "input_tokens": result.input_tokens,
        "repairs": repairs,
        "draft_effort": state.get("draft_effort"),
    }
    scores = f"grounded p={grounded:.2f}, responsive p={responsive:.2f}"
    if shadow:
        decisions.record_shadow("accept", accepted)
        return {
            "decision_log": _log(
                state,
                {**entry, "outcome": "accepted" if accepted else "rejected", "mode": "shadow"},
            )
        }
    acceptance = {"accepted": accepted, "grounded": grounded, "responsive": responsive}
    if accepted:
        decisions.record("accept", "accepted", result.seconds)
        return {
            **_accept_step(state, f"answer accepted ({scores})", {**entry, "outcome": "accepted"}),
            "acceptance": acceptance,
        }
    can_repair = (
        repairs < decider.max_repairs
        and ctx.get("llm") is not None
        and str(state.get("depth") or "short") != "long"
    )
    if can_repair:
        problems = []
        if grounded < threshold:
            problems.append("to make claims its cited passages do not state")
        if responsive < threshold:
            problems.append("not to answer the question that was asked")
        decisions.record("accept", "repaired", result.seconds)
        _, rewrite_effort = drafting.repair_llm(ctx.get("llm"), decider, repairs + 1)
        return {
            **_accept_step(
                state,
                f"draft rejected ({scores}); rewriting at {rewrite_effort or 'full'} effort",
                {**entry, "outcome": "repaired"},
            ),
            "acceptance": acceptance,
            "repair_reason": " and ".join(problems),
            "repair_pending": True,
            "repairs": repairs + 1,
        }
    decisions.record("accept", "rejected", result.seconds)
    return {
        **_accept_step(
            state,
            f"answer rejected ({scores}); returned as written",
            {**entry, "outcome": "rejected"},
        ),
        "acceptance": acceptance,
    }


def _accept_step(state: dict, detail: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision_log": _log(state, entry),
        "steps": [
            *state.get("steps", []),
            WorkflowStep(
                name="accept",
                detail=detail,
                passages=len(state.get("citations") or []),
                duration_seconds=entry.get("seconds"),
            ),
        ],
    }


def route_after_accept(state: dict, ctx: dict) -> str:
    return "synthesize" if state.get("repair_pending") else "end"


def summary(final: dict, ctx: dict) -> dict[str, Any]:
    """The ``decisions`` block of the answer payload; empty when none ran."""

    decider: Decider | None = ctx.get("decider")
    if decider is None:
        return {}
    acceptance = final.get("acceptance") or {}
    return {
        **decider.describe(),
        "available": decider.unavailable_reason is None,
        **(
            {"unavailable_reason": decider.unavailable_reason} if decider.unavailable_reason else {}
        ),
        "calls": list(final.get("decision_log") or []),
        "accepted": acceptance.get("accepted"),
        "repairs": int(final.get("repairs") or 0),
        "draft_effort_used": final.get("draft_effort"),
    }
