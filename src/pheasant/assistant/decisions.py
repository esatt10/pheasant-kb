"""Typed judgements from the OpenAI Decisions API, for the agent loop's gates.

The agentic workflow asks a model four questions that are *judgements*, not
writing: is this evidence enough, which passages matter, which follow-up
search is most promising, and is the finished answer acceptable. Asking them
through a chat completion means paying for a reply, and for a reasoning model
the hidden thinking that precedes it — measured on the lab region, a
sufficiency check through ``/chat/completions`` took 1.20 s p50 and the same
check through ``/v1/decisions`` took 0.24 s p50. The Decisions API answers in
probabilities over typed questions and bills input tokens only.

Three things it does not do, which shape everything built on it:

* **It writes nothing.** No answer, no follow-up query, no "what is missing"
  prose. A follow-up query is therefore *chosen* from candidates built from
  the evidence, never invented here.
* **Its probabilities are not labels.** A threshold is set from labelled
  examples (``scripts/calibrate_decisions.py``), never from the API agreeing
  with itself — the evaluation plane's "a measurement of what a system chose
  to show measures its own confidence" applies verbatim.
* **It is one beta endpoint on one model.** Every failure is a
  :class:`DecisionUnavailable` the caller falls back from *and reports*; an
  unusable judgement read as "sufficient" is how a model switch once turned
  off every follow-up round without anything showing it. A failure that will
  not fix itself (no key, no model, no endpoint) opens a per-process circuit
  so a misconfigured region pays one failed call, not one per question.

Requests go through :func:`pheasant.assistant.providers._http_json`, the one
seam the offline suite patches, and nothing here imports an SDK.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any

# The module, not `from pheasant.assistant import providers`: the layering test
# reads that spelling as importing the (layer-2) package. Module access keeps
# `_providers._http_json` the one seam the offline suite patches.
import pheasant.assistant.providers as _providers
from pheasant.assistant.providers import ProviderError
from pheasant.config.assistant_decisions import DEFAULT_REPAIR_EFFORTS
from pheasant.request_budget import DeadlineExceeded

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"

#: ``beta`` acts on the judgements; ``shadow`` asks them beside the
#: existing model-written grade and acts on nothing, so the two can be
#: compared; ``off`` is the workflow exactly as it was before this module.
MODES = ("beta", "shadow", "off")

#: The four places a judgement replaces (or, in shadow, sits beside) a model
#: call. ``assistant.decisions.gates`` and the per-request ``decision_gates``
#: option select from these.
GATES = ("sufficiency", "followup", "prune", "accept")
#: What runs when nothing says otherwise (``assistant.decisions.gates``).
DEFAULT_GATES = ("sufficiency", "followup", "accept")

#: HTTP statuses that will not change by asking again: credentials, access,
#: a model or endpoint that does not exist here. A 400 is deliberately not
#: one: a request this module built that the endpoint calls malformed is a
#: bug to see on every call, not a reason to stop asking.
_PERMANENT = ("401 ", "403 ", "404 ")

_UNAVAILABLE: dict[tuple[str, str], str] = {}
_UNAVAILABLE_LOCK = threading.Lock()


class DecisionUnavailable(ProviderError):
    """No usable judgement: the caller falls back and says why."""


def reset_circuit() -> None:
    """Forget every endpoint this process gave up on. For tests."""

    with _UNAVAILABLE_LOCK:
        _UNAVAILABLE.clear()


# ------------------------------------------------------------------ answers


@dataclass(frozen=True)
class Predicate:
    name: str
    probability: float


@dataclass(frozen=True)
class Choice:
    name: str
    choice: str
    probabilities: dict[str, float]
    confidence: float | None


@dataclass(frozen=True)
class Score:
    name: str
    score: float
    probabilities: dict[str, float]
    confidence: float | None


@dataclass(frozen=True)
class Refusal:
    name: str


@dataclass
class DecisionResult:
    """One request's answers, keyed by the question names that were sent."""

    answers: dict[str, Predicate | Choice | Score | Refusal]
    seconds: float
    input_tokens: int | None = None

    def probability(self, name: str) -> float | None:
        """A predicate's probability, or ``None`` if refused or absent."""
        answer = self.answers.get(name)
        return answer.probability if isinstance(answer, Predicate) else None

    def choice(self, name: str) -> Choice | None:
        answer = self.answers.get(name)
        return answer if isinstance(answer, Choice) else None

    def score(self, name: str) -> Score | None:
        answer = self.answers.get(name)
        return answer if isinstance(answer, Score) else None

    def refused(self, name: str) -> bool:
        return isinstance(self.answers.get(name), Refusal)


# ---------------------------------------------------------------- questions


def predicate(name: str, instructions: str) -> dict[str, Any]:
    return {"type": "predicate", "name": name, "instructions": instructions}


def choice(name: str, instructions: str, options: list[tuple[str, str]]) -> dict[str, Any]:
    """A choice question. The API refuses fewer than two options."""
    return {
        "type": "choice",
        "name": name,
        "instructions": instructions,
        "choices": [{"value": value, "description": text} for value, text in options],
    }


def score(name: str, instructions: str, levels: list[tuple[str, str]]) -> dict[str, Any]:
    """Ordered levels, lowest first; the answer is their weighted index."""
    return {
        "type": "score",
        "name": name,
        "instructions": instructions,
        "levels": [{"label": label, "description": text} for label, text in levels],
    }


def _validate(questions: list[dict[str, Any]]) -> None:
    """Refuse locally what the endpoint would refuse with a 400."""
    if not questions:
        raise DecisionUnavailable("no questions to ask")
    names = [str(question.get("name") or "") for question in questions]
    if any(not name for name in names) or len(set(names)) != len(names):
        raise DecisionUnavailable("question names must be present and unique")
    for question in questions:
        if question.get("type") == "choice" and len(question.get("choices") or []) < 2:
            raise DecisionUnavailable(f"choice {question['name']!r} needs at least two options")
        if question.get("type") == "score" and len(question.get("levels") or []) < 2:
            raise DecisionUnavailable(f"score {question['name']!r} needs at least two levels")


def _parse(data: Any) -> dict[str, Predicate | Choice | Score | Refusal]:
    if not isinstance(data, dict) or not isinstance(data.get("answers"), list):
        raise DecisionUnavailable("decisions endpoint returned no answers array")
    parsed: dict[str, Predicate | Choice | Score | Refusal] = {}
    for raw in data["answers"]:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        name, kind = str(raw["name"]), raw.get("type")
        try:
            if kind == "predicate":
                parsed[name] = Predicate(name, float(raw["probability"]))
            elif kind == "choice":
                parsed[name] = Choice(
                    name,
                    str(raw["choice"]),
                    {
                        str(item.get("value")): float(item.get("probability") or 0.0)
                        for item in raw.get("probabilities") or []
                        if isinstance(item, dict)
                    },
                    _float_or_none(raw.get("confidence")),
                )
            elif kind == "score":
                parsed[name] = Score(
                    name,
                    float(raw["score"]),
                    {
                        str(item.get("label") or item.get("value")): float(
                            item.get("probability") or 0.0
                        )
                        for item in raw.get("probabilities") or []
                        if isinstance(item, dict)
                    },
                    _float_or_none(raw.get("confidence")),
                )
            else:
                # "refusal", and any type this build does not know yet: either
                # way there is no judgement here to act on.
                parsed[name] = Refusal(name)
        except (KeyError, TypeError, ValueError):
            parsed[name] = Refusal(name)
    return parsed


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ handle


@dataclass
class Decider:
    """A resolved, callable Decisions endpoint plus the gate policy for it."""

    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str | None = None
    timeout: float = 10.0
    mode: str = "beta"
    gates: tuple[str, ...] = DEFAULT_GATES
    sufficient_threshold: float = 0.25
    relevant_threshold: float = 0.2
    accept_threshold: float = 0.55
    prune_min_keep: int = 3
    draft_reasoning_effort: str | None = "low"
    max_repairs: int = 1
    #: Effort per rewrite (``none`` / ``low`` / ``full``); past the end, ``full``.
    repair_efforts: tuple[str, ...] = DEFAULT_REPAIR_EFFORTS
    #: ``environment`` / ``assistant`` (the answering model's own OpenAI
    #: credential) — reported, never the key itself.
    source: str = "environment"
    deadline_monotonic: float | None = None
    last_failure: str | None = field(default=None, compare=False, repr=False)

    @property
    def base(self) -> str:
        return (self.base_url or DEFAULT_BASE_URL).rstrip("/")

    @property
    def unavailable_reason(self) -> str | None:
        """Why this process stopped asking this endpoint, if it did."""
        with _UNAVAILABLE_LOCK:
            return _UNAVAILABLE.get((self.base, self.model))

    def with_deadline(self, deadline: float | None) -> Decider:
        return replace(self, deadline_monotonic=deadline)

    def with_mode(self, mode: str) -> Decider:
        return replace(self, mode=mode)

    def with_gates(self, gates: tuple[str, ...]) -> Decider:
        return replace(self, gates=gates)

    def with_repairs(self, max_repairs: int, efforts: tuple[str, ...]) -> Decider:
        return replace(self, max_repairs=max_repairs, repair_efforts=efforts)

    def repair_effort(self, attempt: int) -> str:
        """The effort rewrite number ``attempt`` (1-based) is asked for."""
        efforts = self.repair_efforts
        return efforts[attempt - 1] if 0 < attempt <= len(efforts) else "full"

    def gate(self, name: str, mode: str = "beta") -> bool:
        """Is ``name`` a gate this handle runs in ``mode``?"""
        return self.mode == mode and name in self.gates

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "model": self.model,
            "gates": list(self.gates),
            "credential_source": self.source,
            "thresholds": {
                "sufficient": self.sufficient_threshold,
                "relevant": self.relevant_threshold,
                "accept": self.accept_threshold,
            },
            "draft_reasoning_effort": self.draft_reasoning_effort,
            "max_repairs": self.max_repairs,
            "repair_efforts": list(self.repair_efforts),
        }

    def decide(self, input: Any, questions: list[dict[str, Any]]) -> DecisionResult:
        """One request. Raises :class:`DecisionUnavailable` on any failure."""

        reason = self.unavailable_reason
        if reason:
            raise DecisionUnavailable(reason)
        _validate(questions)
        timeout = self.timeout
        if self.deadline_monotonic is not None:
            remaining = self.deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise DeadlineExceeded("assistant request deadline exceeded")
            timeout = min(timeout, remaining)
        payload = {"model": self.model, "input": input, "questions": questions}
        headers = {"authorization": f"Bearer {self.api_key}"}
        started = time.perf_counter()
        try:
            _providers.note_model_call()
            data = _providers._http_json(f"{self.base}/decisions", payload, headers, timeout)
        except ProviderError as exc:
            message = str(exc) or type(exc).__name__
            if message.startswith(_PERMANENT):
                with _UNAVAILABLE_LOCK:
                    _UNAVAILABLE[(self.base, self.model)] = _short(message)
            if self.deadline_monotonic is not None and time.monotonic() >= self.deadline_monotonic:
                raise DeadlineExceeded("assistant request deadline exceeded") from None
            self.last_failure = _short(message)
            raise DecisionUnavailable(self.last_failure) from exc
        finally:
            from pheasant.request_budget import record_active_timing

            record_active_timing(
                "decision_request", time.perf_counter() - started, model=self.model
            )
        seconds = time.perf_counter() - started
        usage = data.get("usage") if isinstance(data, dict) else None
        input_tokens = None
        if isinstance(usage, dict):
            details = usage.get("input_tokens_details") or {}
            _providers._record_usage(
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                cached_input_tokens=details.get("cached_tokens"),
            )
            if isinstance(usage.get("input_tokens"), int):
                input_tokens = int(usage["input_tokens"])
        return DecisionResult(answers=_parse(data), seconds=seconds, input_tokens=input_tokens)


def _short(message: str) -> str:
    message = " ".join(message.split())
    return message if len(message) <= 160 else message[:157] + "…"


def resolve_decider(
    settings: Any, llm: Any, env: dict[str, str], *, mode: str | None = None
) -> Decider | None:
    """The Decisions handle for this request, or ``None`` (the classic loop).

    ``settings`` is ``assistant``. The key is ``decisions.api_key_env`` when
    one is named; otherwise the answering model's own credential when that is
    OpenAI (a browser session's key included, since it is the same account),
    otherwise ``OPENAI_API_KEY``. No key is not an error: the workflow is
    exactly what it was before this module existed. ``mode`` is the
    per-request ``decisions`` option, which wins over the configured mode in
    both directions.
    """

    if settings is None or not getattr(settings, "enabled", True):
        return None
    decisions = getattr(settings, "decisions", None)
    requested = str(mode or "").strip().lower()
    mode = requested if requested in MODES else str(getattr(decisions, "mode", "beta") or "beta")
    if mode == "off":
        return None
    named_env = getattr(decisions, "api_key_env", None)
    base_url = getattr(decisions, "base_url", None)
    source = "environment"
    if named_env:
        api_key = env.get(str(named_env))
    elif llm is not None and getattr(llm, "provider", None) == "openai":
        api_key = getattr(llm, "api_key", None)
        base_url = base_url or getattr(llm, "base_url", None)
        source = "assistant"
    else:
        api_key = env.get(DEFAULT_API_KEY_ENV)
    if not api_key:
        return None
    return Decider(
        api_key=str(api_key),
        model=str(getattr(decisions, "model", None) or DEFAULT_MODEL),
        base_url=base_url,
        timeout=float(getattr(decisions, "timeout_seconds", 10.0) or 10.0),
        mode=mode,
        gates=tuple(getattr(decisions, "gates", None) or DEFAULT_GATES),
        sufficient_threshold=float(getattr(decisions, "sufficient_threshold", 0.25)),
        relevant_threshold=float(getattr(decisions, "relevant_threshold", 0.2)),
        accept_threshold=float(getattr(decisions, "accept_threshold", 0.55)),
        prune_min_keep=int(getattr(decisions, "prune_min_keep", 3)),
        draft_reasoning_effort=getattr(decisions, "draft_reasoning_effort", "low"),
        max_repairs=int(getattr(decisions, "max_repairs", 1)),
        repair_efforts=tuple(getattr(decisions, "repair_efforts", DEFAULT_REPAIR_EFFORTS) or ()),
        source=source,
    )


# --------------------------------------------------------------- telemetry


def record(gate: str, outcome: str, seconds: float | None = None) -> None:
    """Count one gate outcome. In-memory; never a database write."""

    from pheasant.telemetry import metrics

    metrics.REGISTRY.inc("pheasant_assistant_decision_total", gate=gate, outcome=outcome)
    if seconds is not None:
        metrics.REGISTRY.observe("pheasant_assistant_decision_seconds", seconds, gate=gate)


def record_shadow(gate: str, agree: bool) -> None:
    """Count whether a shadowed judgement agreed with the one that was acted on."""

    from pheasant.telemetry import metrics

    metrics.REGISTRY.inc(
        "pheasant_assistant_decision_shadow_total",
        gate=gate,
        agreement="agree" if agree else "disagree",
    )


# ------------------------------------------------------------- calibration


def choose_threshold(
    samples: list[tuple[float, bool]],
    *,
    false_positive_cost: float = 1.0,
    false_negative_cost: float = 1.0,
) -> dict[str, Any]:
    """The probability cut that minimises expected cost over labelled samples.

    ``samples`` are ``(probability, label)``; a sample is predicted positive
    when its probability is at or above the threshold. The costs say which
    mistake is worse — for passage pruning a false *negative* drops evidence
    the answer needed, so it is weighted up. Candidate cuts are the observed
    probabilities themselves (plus the two extremes), so the result is a
    function of the data and the costs alone.
    """

    if not samples:
        return {"threshold": None, "reason": "no labelled samples"}
    positives = sum(1 for _, label in samples if label)
    negatives = len(samples) - positives
    cuts = sorted({0.0, 1.000001, *(round(p, 6) for p, _ in samples)})
    best: dict[str, Any] | None = None
    for cut in cuts:
        fp = sum(1 for p, label in samples if p >= cut and not label)
        fn = sum(1 for p, label in samples if p < cut and label)
        cost = fp * false_positive_cost + fn * false_negative_cost
        row = {
            "threshold": min(cut, 1.0),
            "cost": cost,
            "false_positives": fp,
            "false_negatives": fn,
            "accuracy": (len(samples) - fp - fn) / len(samples),
        }
        # Ties go to the cut nearest 0.5: the least committal of equally good
        # answers, rather than whichever extreme the sort reached first.
        if (
            best is None
            or cost < best["cost"]
            or (cost == best["cost"] and abs(cut - 0.5) < abs(best["threshold"] - 0.5))
        ):
            best = row
    assert best is not None
    best.update({"samples": len(samples), "positives": positives, "negatives": negatives})
    return best
