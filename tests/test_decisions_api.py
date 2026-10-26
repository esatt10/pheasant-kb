"""The agent loop's Decisions API gates (``assistant.decisions``).

Acceptance:

1. The client speaks the measured wire: ``POST {base}/decisions`` with
   ``model`` / ``input`` / named ``questions``, typed answers back, usage
   recorded as input tokens. A key, model or route that is not there opens a
   per-process circuit; a transient failure does not.
2. ``beta`` writes nothing until the evidence is judged sufficient: a thin
   round costs a judgement and a search, never an answer that is thrown away.
3. A follow-up search is *chosen* from candidates the evidence names, and
   "none" hands the round to the model planner.
4. Passages judged irrelevant are left out of the answer's context, never
   below ``prune_min_keep``.
5. The first draft is written at ``draft_reasoning_effort``; a rejected draft
   is rewritten once at the assistant's own effort (or as ``repair_efforts``
   says, per config or per request), and a second rejection is returned and
   reported as degraded.
6. Every unusable judgement falls back to the model-written grade the loop
   always had, *and says so* — never silently read as "sufficient".
7. ``shadow`` acts on nothing: same answer, same model calls as ``off``, with
   the agreement recorded.
8. ``off`` (or no key) is the classic loop: no decisions request, no payload
   block, the same trace.

The workflow half needs the ``agent`` extra and skips without it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from pheasant.assistant import decisions
from pheasant.assistant import providers as providers_module
from pheasant.assistant.decisions import Decider, DecisionUnavailable, resolve_decider
from pheasant.assistant.providers import ProviderError, collect_token_usage

QUESTION = "How do I enable staged retrieval?"


@pytest.fixture(autouse=True)
def _clean_circuit():
    decisions.reset_circuit()
    yield
    decisions.reset_circuit()


# ------------------------------------------------------------------ wire fakes


def _answers(payload: dict, script: dict[str, Any]) -> dict:
    """Answer every question a request asks, from a ``{name: value}`` script.

    Predicates default to 0.9 (relevant_* included); choices to their first
    option; anything scripted as ``"refuse"`` comes back as a refusal.
    """

    answers = []
    for question in payload["questions"]:
        name = question["name"]
        value = script.get(name, script.get("relevant_*") if name.startswith("relevant_") else None)
        if value == "refuse":
            answers.append({"type": "refusal", "name": name})
        elif question["type"] == "predicate":
            answers.append(
                {"type": "predicate", "name": name, "probability": 0.9 if value is None else value}
            )
        elif question["type"] == "choice":
            options = [c["value"] for c in question["choices"]]
            chosen = value if value in options else options[0]
            answers.append(
                {
                    "type": "choice",
                    "name": name,
                    "choice": chosen,
                    "probabilities": [
                        {"value": o, "probability": 1.0 if o == chosen else 0.0} for o in options
                    ],
                    "confidence": 0.9,
                }
            )
    return {
        "model": payload["model"],
        "answers": answers,
        "usage": {"input_tokens": 321, "output_tokens": 0, "input_tokens_details": {}},
    }


class _Wire:
    """A ``_http_json`` stand-in: scripted decisions, recorded requests."""

    def __init__(self, *scripts: dict[str, Any], error: str | None = None):
        self.scripts = list(scripts)
        self.error = error
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, url, payload, headers, timeout):
        self.requests.append((url, payload))
        if self.error:
            raise ProviderError(self.error)
        script = self.scripts.pop(0) if len(self.scripts) > 1 else self.scripts[0]
        return _answers(payload, script)

    def names(self, index: int) -> list[str]:
        return [q["name"] for q in self.requests[index][1]["questions"]]


def _decider(**overrides: Any) -> Decider:
    return replace(Decider(api_key="sk-test"), **overrides)


# ------------------------------------------------------------------ the client


def test_the_client_posts_typed_questions_and_parses_typed_answers(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake(url, payload, headers, timeout):
        seen.update(url=url, payload=payload, headers=headers, timeout=timeout)
        return {
            "answers": [
                {"type": "predicate", "name": "ok", "probability": 0.92},
                {
                    "type": "choice",
                    "name": "dept",
                    "choice": "billing",
                    "probabilities": [
                        {"value": "billing", "probability": 0.95},
                        {"value": "other", "probability": 0.05},
                    ],
                    "confidence": 0.93,
                },
                {
                    "type": "score",
                    "name": "sev",
                    "score": 1.1,
                    "probabilities": [
                        {"value": 0, "label": "low", "probability": 0.1},
                        {"value": 1, "label": "high", "probability": 0.9},
                    ],
                    "confidence": 0.55,
                },
                {"type": "refusal", "name": "nope"},
            ],
            "usage": {"input_tokens": 569, "output_tokens": 0, "input_tokens_details": {}},
        }

    monkeypatch.setattr(providers_module, "_http_json", fake)
    with collect_token_usage() as usage:
        result = _decider(base_url="https://example.test/v1/").decide(
            "evidence",
            [
                decisions.predicate("ok", "Is it?"),
                decisions.choice("dept", "Which?", [("billing", "b"), ("other", "o")]),
                decisions.score("sev", "How bad?", [("low", "l"), ("high", "h")]),
                decisions.predicate("nope", "?"),
            ],
        )

    assert seen["url"] == "https://example.test/v1/decisions"
    assert seen["headers"] == {"authorization": "Bearer sk-test"}
    assert seen["payload"]["model"] == "gpt-6-luna"
    assert [q["type"] for q in seen["payload"]["questions"]] == [
        "predicate",
        "choice",
        "score",
        "predicate",
    ]
    assert result.probability("ok") == pytest.approx(0.92)
    assert result.choice("dept").choice == "billing"
    assert result.choice("dept").probabilities == {"billing": 0.95, "other": 0.05}
    assert result.score("sev").score == pytest.approx(1.1)
    assert result.refused("nope") and result.probability("nope") is None
    assert result.input_tokens == 569
    assert (usage.calls, usage.input_tokens, usage.output_tokens) == (1, 569, 0)


def test_requests_the_endpoint_would_refuse_are_refused_locally(monkeypatch) -> None:
    monkeypatch.setattr(providers_module, "_http_json", _Wire({}))
    decider = _decider()
    with pytest.raises(DecisionUnavailable, match="unique"):
        decider.decide("x", [decisions.predicate("a", "?"), decisions.predicate("a", "?")])
    with pytest.raises(DecisionUnavailable, match="two options"):
        decider.decide("x", [decisions.choice("c", "?", [("only", "one")])])


@pytest.mark.parametrize("status", ["401", "403", "404"])
def test_a_missing_key_model_or_route_opens_the_circuit(monkeypatch, status) -> None:
    wire = _Wire({}, error=f"{status} from provider: no")
    monkeypatch.setattr(providers_module, "_http_json", wire)
    decider = _decider()

    with pytest.raises(DecisionUnavailable):
        decider.decide("x", [decisions.predicate("a", "?")])
    with pytest.raises(DecisionUnavailable, match=status):
        decider.decide("x", [decisions.predicate("a", "?")])

    assert len(wire.requests) == 1, "the second question must not pay a failed call"
    assert decider.unavailable_reason


@pytest.mark.parametrize(
    "error", ["500 from provider: busy", "provider did not answer within 10s", "400 from provider"]
)
def test_a_transient_or_malformed_failure_does_not_open_the_circuit(monkeypatch, error) -> None:
    wire = _Wire({}, error=error)
    monkeypatch.setattr(providers_module, "_http_json", wire)
    decider = _decider()
    for _ in range(2):
        with pytest.raises(DecisionUnavailable):
            decider.decide("x", [decisions.predicate("a", "?")])
    assert len(wire.requests) == 2
    assert decider.unavailable_reason is None


def test_a_reply_without_answers_is_unavailable_not_empty(monkeypatch) -> None:
    monkeypatch.setattr(providers_module, "_http_json", lambda *a: {"choices": []})
    with pytest.raises(DecisionUnavailable, match="no answers"):
        _decider().decide("x", [decisions.predicate("a", "?")])


# ------------------------------------------------------------- key resolution


def _settings(**decisions_fields: Any) -> SimpleNamespace:
    from pheasant.config.assistant_decisions import AssistantDecisionsSettings

    return SimpleNamespace(enabled=True, decisions=AssistantDecisionsSettings(**decisions_fields))


def test_no_key_or_mode_off_resolves_to_the_classic_loop() -> None:
    assert resolve_decider(_settings(), None, {}) is None
    assert resolve_decider(_settings(mode="off"), None, {"OPENAI_API_KEY": "k"}) is None
    disabled = _settings()
    disabled.enabled = False
    assert resolve_decider(disabled, None, {"OPENAI_API_KEY": "k"}) is None


def test_the_answering_models_openai_credential_is_reused() -> None:
    llm = SimpleNamespace(provider="openai", api_key="sk-session", base_url="https://proxy/v1")
    decider = resolve_decider(_settings(), llm, {"OPENAI_API_KEY": "sk-env"})
    assert (decider.api_key, decider.base, decider.source) == (
        "sk-session",
        "https://proxy/v1",
        "assistant",
    )


def test_a_non_openai_answering_model_uses_the_openai_env_key() -> None:
    llm = SimpleNamespace(provider="anthropic", api_key="sk-ant", base_url=None)
    decider = resolve_decider(_settings(), llm, {"OPENAI_API_KEY": "sk-env"})
    assert (decider.api_key, decider.base, decider.source) == (
        "sk-env",
        decisions.DEFAULT_BASE_URL,
        "environment",
    )
    named = resolve_decider(_settings(api_key_env="DECIDE_KEY"), llm, {"DECIDE_KEY": "sk-d"})
    assert named.api_key == "sk-d"


def test_the_per_request_mode_wins_in_both_directions() -> None:
    env = {"OPENAI_API_KEY": "k"}
    assert resolve_decider(_settings(), None, env, mode="off") is None
    assert resolve_decider(_settings(mode="off"), None, env, mode="shadow").mode == "shadow"
    assert resolve_decider(_settings(), None, env, mode="nonsense").mode == "beta"


def test_the_config_section_refuses_nonsense() -> None:
    from pheasant.config.assistant_decisions import AssistantDecisionsSettings

    for bad in (
        {"mode": "on"},
        {"gates": ["sufficiency", "vibes"]},
        {"accept_threshold": 1.5},
        {"draft_reasoning_effort": "high"},
        {"repair_efforts": ["low", "medium"]},
        {"prune_min_keep": 0},
    ):
        with pytest.raises(ValueError):
            AssistantDecisionsSettings(**bad)


# ------------------------------------------------------------- calibration


def test_choose_threshold_minimises_expected_cost() -> None:
    samples = [(0.95, True), (0.9, True), (0.7, True), (0.6, False), (0.3, False), (0.1, False)]
    best = decisions.choose_threshold(samples)
    assert 0.6 < best["threshold"] <= 0.7
    assert (best["false_positives"], best["false_negatives"]) == (0, 0)
    # Dropping a needed passage costs five times reading an unneeded one.
    noisy = [(0.9, True), (0.25, True), (0.3, False), (0.2, False), (0.1, False)]
    lenient = decisions.choose_threshold(noisy, false_negative_cost=5.0)
    assert lenient["threshold"] <= 0.25 and lenient["false_negatives"] == 0
    assert decisions.choose_threshold([])["threshold"] is None


def test_text_preview_emits_increments_and_does_not_repeat_after_restart() -> None:
    from pheasant.assistant.streaming import TextPreview

    sent: list[str] = []
    preview = TextPreview(sent.append)
    for delta in ("Staged ", "retrieval ", "is"):
        preview(delta)
    preview.restart()
    for delta in ("Staged retrieval is", " on [1]."):
        preview(delta)
    assert "".join(sent) == "Staged retrieval is on [1]."


# ---------------------------------------------------------------- the workflow


def _hit(name: str) -> dict[str, Any]:
    node_id = f"file:kb:docs/{name}.md"
    return {
        "node_id": node_id,
        "chunk_id": f"{node_id}#c0",
        "type": "chunk",
        "title": f"docs/{name}.md",
        "relative_path": f"docs/{name}.md",
        "source_id": "notes",
        "score": 1.0,
        "chunks": [{"text_preview": f"Evidence from {name}."}],
    }


class _Search:
    vector = None

    def __init__(self, results: dict[str, list[dict[str, Any]]]):
        self.results = results
        self.calls: list[str] = []

    def search_context(self, _kb, query, _mode, _limit, _source_name, **_kwargs):
        self.calls.append(query)
        return {"results": self.results.get(query, []), "counts": {}}


class _LLM:
    """An OpenAI ``gpt-6-luna`` handle that records how it was asked."""

    provider = "openai"
    model_id = "gpt-6-luna"
    api_key = "sk-test"
    base_url = None

    def __init__(self, replies: list[Any], effort: str | None = None, log: list | None = None):
        self.replies = replies
        self.reasoning_effort = effort
        self.log = log if log is not None else []

    def with_reasoning_effort(self, effort):
        return _LLM(self.replies, effort, self.log)

    def complete(self, system, prompt, **kwargs):
        self.log.append(
            {"system": system, "prompt": prompt, "effort": self.reasoning_effort, **kwargs}
        )
        if not self.replies:
            raise AssertionError("unexpected model call")
        reply = self.replies.pop(0)
        return reply if isinstance(reply, str) else json.dumps(reply)

    def try_complete(self, system, prompt, **kwargs):
        try:
            return self.complete(system, prompt, **kwargs)
        except ProviderError:
            return None

    @property
    def calls(self) -> int:
        return len(self.log)


def _run(search, llm, decider, monkeypatch=None, wire=None, **options: Any):
    pytest.importorskip("langgraph")
    from pheasant.assistant.retrieval import PheasantRetriever
    from pheasant.assistant.workflows import WorkflowRequest
    from pheasant.assistant.workflows.agentic import AgenticWorkflow

    if wire is not None:
        monkeypatch.setattr(providers_module, "_http_json", wire)
    retriever = PheasantRetriever(search=search, knowledge_base="kb", graph=None, state=None)
    request_options = {
        "staged_retrieval": True,
        "combine_grade_and_answer": True,
        "retrieval_modes": ["hybrid"],
        "expand_graph": False,
        "max_rounds": 2,
        **options,
    }
    return AgenticWorkflow().run(
        WorkflowRequest(question=QUESTION, options=request_options, decider=decider),
        retriever,
        llm,
    )


def _names(result) -> list[str]:
    return [step.name for step in result.steps]


def test_beta_judges_first_and_writes_once_at_draft_effort(monkeypatch) -> None:
    search = _Search({QUESTION: [_hit("config")]})
    llm = _LLM(["Set staged_retrieval: true [1]."])
    wire = _Wire({"sufficient": 0.97, "grounded": 0.95, "responsive": 0.96})

    result = _run(search, llm, _decider(), monkeypatch, wire)

    assert llm.calls == 1, "one write, no JSON grade-and-answer call"
    assert "json_mode" not in llm.log[0]
    assert llm.log[0]["effort"] == "low"
    assert _names(result) == [
        "classify",
        "retrieve",
        "expand",
        "decide",
        "synthesize",
        "verify",
        "accept",
    ]
    assert result.answer == "Set staged_retrieval: true [1]."
    assert result.counts["insufficient_evidence"] is False
    assert [entry["gate"] for entry in result.decisions["calls"]] == ["sufficiency", "accept"]
    assert result.decisions["accepted"] is True and result.decisions["repairs"] == 0
    assert result.decisions["draft_effort_used"] == "low"
    assert all(url.endswith("/decisions") for url, _ in wire.requests)


def test_a_thin_round_costs_a_judgement_and_a_chosen_search_not_an_answer(monkeypatch) -> None:
    search = _Search({QUESTION: [_hit("overview")]})
    wire = _Wire(
        {"sufficient": 0.1, "miss": "location"},  # round 1: thin; pick a candidate
        {"sufficient": 0.95},  # round 2
        {"grounded": 0.9, "responsive": 0.9},
    )
    llm = _LLM(["It is configured under workflow_options [1] [2]."])

    def search_context(kb, query, mode, limit, source, **kwargs):
        search.calls.append(query)
        if query == QUESTION:
            return {"results": [_hit("overview")], "counts": {}}
        return {"results": [_hit("staged")], "counts": {}}

    search.search_context = search_context
    result = _run(search, llm, _decider(), monkeypatch, wire)

    first = wire.names(0)
    assert "followup" in first
    candidates = [
        c["value"] for c in wire.requests[0][1]["questions"][first.index("followup")]["choices"]
    ]
    assert candidates[-1] == "none"
    assert candidates[0] == "docs enable staged retrieval", "the hit's directory + the terms"
    assert search.calls == [QUESTION, candidates[0]], "the chosen candidate is searched next"
    assert llm.calls == 1, "the thin round wrote nothing"
    assert result.counts["retrieval_rounds"] == 2
    assert result.counts["insufficient_evidence"] is False


def test_exhausted_rounds_write_once_with_an_evidence_gap(monkeypatch) -> None:
    search = _Search({QUESTION: [_hit("overview")]})
    wire = _Wire({"sufficient": 0.05, "miss": "absent", "followup": "none"})
    planner = {"queries": ["salesforce connector"], "modes": ["hybrid"], "reasoning": "r"}
    llm = _LLM([planner, "The knowledge base does not describe this [1]."])

    result = _run(search, llm, _decider(gates=("sufficiency", "followup")), monkeypatch, wire)

    assert llm.calls == 2, "the model planner (followup 'none') and one write"
    assert "plan retrieval" in llm.log[0]["system"]
    assert result.counts["insufficient_evidence"] is True
    assert "Evidence gap:" in result.answer
    assert result.decisions["calls"][-1]["outcome"] == "insufficient"


def test_irrelevant_passages_are_left_out_but_never_below_the_floor(monkeypatch) -> None:
    hits = [_hit(name) for name in ("a", "b", "c", "d", "e")]
    search = _Search({QUESTION: hits})
    relevance = {"relevant_1": 0.9, "relevant_2": 0.05, "relevant_3": 0.8, "relevant_4": 0.01}
    wire = _Wire({"sufficient": 0.9, "relevant_*": 0.9, **relevance})
    llm = _LLM(["Answer [1] [2] [3]."])

    result = _run(search, llm, _decider(gates=("sufficiency", "prune")), monkeypatch, wire)

    assert [c["relative_path"] for c in result.citations] == ["docs/a.md", "docs/c.md", "docs/e.md"]
    assert any(s.name == "prune" and "left out 2 of 5" in s.detail for s in result.steps)

    floor = _Wire({"sufficient": 0.9, "relevant_*": 0.01, "relevant_2": 0.6})
    result = _run(
        _Search({QUESTION: hits}),
        _LLM(["Answer [1]."]),
        _decider(gates=("sufficiency", "prune"), prune_min_keep=3),
        monkeypatch,
        floor,
    )
    assert len(result.citations) == 3
    assert "docs/b.md" in [c["relative_path"] for c in result.citations]


def test_a_rejected_draft_is_rewritten_once_at_full_effort(monkeypatch) -> None:
    search = _Search({QUESTION: [_hit("config")]})
    wire = _Wire(
        {"sufficient": 0.95},
        {"grounded": 0.2, "responsive": 0.9},
        {"grounded": 0.9, "responsive": 0.9},
    )
    llm = _LLM(["A made-up claim [1].", "Set staged_retrieval: true [1]."])

    result = _run(search, llm, _decider(), monkeypatch, wire)

    assert [entry["effort"] for entry in llm.log] == ["low", None]
    assert any(s.name == "accept" and "rewriting at full effort" in s.detail for s in result.steps)
    assert "REVISION" in llm.log[1]["system"] and "do not state" in llm.log[1]["system"]
    assert result.answer == "Set staged_retrieval: true [1]."
    assert [e["outcome"] for e in result.decisions["calls"] if e["gate"] == "accept"] == [
        "repaired",
        "accepted",
    ]
    assert result.decisions["repairs"] == 1 and result.decisions["accepted"] is True


def test_a_rewrite_runs_at_the_repair_effort_it_is_given(monkeypatch) -> None:
    search = _Search({QUESTION: [_hit("config")]})
    wire = _Wire(
        {"sufficient": 0.95},
        {"grounded": 0.2, "responsive": 0.9},
        {"grounded": 0.9, "responsive": 0.9},
    )
    llm = _LLM(["A made-up claim [1].", "Set staged_retrieval: true [1]."])

    result = _run(search, llm, _decider(repair_efforts=("low",)), monkeypatch, wire)

    assert [entry["effort"] for entry in llm.log] == ["low", "low"]
    assert "REVISION" in llm.log[1]["system"]
    assert any(s.name == "accept" and "rewriting at low effort" in s.detail for s in result.steps)
    accepts = [e for e in result.decisions["calls"] if e["gate"] == "accept"]
    assert [e["draft_effort"] for e in accepts] == ["low", "low"], "each judged draft's effort"
    assert result.decisions["repair_efforts"] == ["low"]
    assert result.decisions["accepted"] is True


def test_a_cheap_rewrite_that_is_rejected_escalates_to_full_effort(monkeypatch) -> None:
    search = _Search({QUESTION: [_hit("config")]})
    wire = _Wire(
        {"sufficient": 0.95},
        {"grounded": 0.2, "responsive": 0.9},
        {"grounded": 0.3, "responsive": 0.9},
        {"grounded": 0.9, "responsive": 0.9},
    )
    llm = _LLM(["Draft [1].", "Cheap rewrite [1].", "Set staged_retrieval: true [1]."])

    result = _run(search, llm, _decider(repair_efforts=("low",), max_repairs=2), monkeypatch, wire)

    assert [entry["effort"] for entry in llm.log] == ["low", "low", None]
    details = [s.detail for s in result.steps if s.name == "accept"]
    assert "rewriting at low effort" in details[0] and "rewriting at full effort" in details[1]
    assert result.answer == "Set staged_retrieval: true [1]."
    assert result.decisions["repairs"] == 2 and result.decisions["accepted"] is True


def test_a_request_picks_its_own_repair_ladder(monkeypatch) -> None:
    def run(**options):
        wire = _Wire(
            {"sufficient": 0.95},
            {"grounded": 0.2, "responsive": 0.9},
            {"grounded": 0.2, "responsive": 0.9},
            {"grounded": 0.9, "responsive": 0.9},
        )
        llm = _LLM(["Draft [1].", "Rewrite [1].", "Again [1]."])
        result = _run(
            _Search({QUESTION: [_hit("config")]}), llm, _decider(), monkeypatch, wire, **options
        )
        return [entry["effort"] for entry in llm.log], result.decisions

    efforts, judged = run(repair_efforts="low", max_repairs=2)
    assert efforts == ["low", "low", None]
    assert (judged["max_repairs"], judged["repair_efforts"]) == (2, ["low"])
    # One bad entry voids the ladder rather than shifting it: the config's runs.
    efforts, judged = run(repair_efforts=["medium", "low"])
    assert efforts == ["low", None] and judged["repair_efforts"] == []
    # Out of range or not an integer: the configured count.
    for bad in (9, -1, True, "2"):
        assert run(max_repairs=bad)[1]["max_repairs"] == 1


def test_synthesis_passages_narrows_the_answer_and_not_the_judgement(monkeypatch) -> None:
    hits = [_hit(name) for name in ("a", "b", "c", "d", "e")]
    wire = _Wire({"sufficient": 0.95, "grounded": 0.9, "responsive": 0.9})
    llm = _LLM(["Answer [1] [2]."])

    result = _run(
        _Search({QUESTION: hits}), llm, _decider(), monkeypatch, wire, synthesis_passages=2
    )

    assert [c["relative_path"] for c in result.citations] == ["docs/a.md", "docs/b.md"]
    judged = wire.requests[0][1]["input"]
    assert "[5] docs/e.md" in judged, "the sufficiency judgement still read every passage"
    assert "docs/b.md" in llm.log[0]["prompt"] and "docs/c.md" not in llm.log[0]["prompt"]
    unset = _run(
        _Search({QUESTION: hits}),
        _LLM(["Answer [1]."]),
        _decider(),
        monkeypatch,
        _Wire({"sufficient": 0.95, "grounded": 0.9, "responsive": 0.9}),
    )
    assert len(unset.citations) == 5


def test_a_second_rejection_is_returned_and_reported(monkeypatch) -> None:
    search = _Search({QUESTION: [_hit("config")]})
    wire = _Wire({"sufficient": 0.95}, {"grounded": 0.1, "responsive": 0.9})
    llm = _LLM(["Draft [1].", "Rewrite [1]."])

    result = _run(search, llm, _decider(), monkeypatch, wire)

    assert llm.calls == 2
    assert result.answer == "Rewrite [1]."
    assert result.decisions["accepted"] is False
    assert any(s.name == "accept" and "returned as written" in s.detail for s in result.steps)


def test_an_unavailable_endpoint_falls_back_to_the_model_grade_and_says_so(monkeypatch) -> None:
    search = _Search({QUESTION: [_hit("config")]})
    wire = _Wire({}, error="404 from provider: model_not_found")
    llm = _LLM([{"sufficient": True, "answer": "Set it [1].", "missing": ""}])

    result = _run(search, llm, _decider(), monkeypatch, wire)

    assert len(wire.requests) == 1, "the circuit stops the acceptance call"
    assert llm.calls == 1 and llm.log[0]["json_mode"] is True, "the combined path answered"
    decide = [s for s in result.steps if s.name == "decide"]
    assert (
        decide
        and "unavailable" in decide[0].detail
        and "grading with the model" in decide[0].detail
    )
    assert any(s.name == "grade_answer" for s in result.steps)
    assert any(s.name == "accept" and "not judged" in s.detail for s in result.steps)
    assert result.decisions["available"] is False
    assert result.counts["insufficient_evidence"] is False


def test_a_refused_sufficiency_question_is_never_read_as_sufficient(monkeypatch) -> None:
    search = _Search({QUESTION: [_hit("config")]})
    wire = _Wire({"sufficient": "refuse"})
    llm = _LLM([{"sufficient": False, "answer": "", "missing": "the flag", "next_queries": []}])

    result = _run(search, llm, _decider(gates=("sufficiency",)), monkeypatch, wire)

    assert any("refused" in s.detail for s in result.steps if s.name == "decide")
    assert result.counts["insufficient_evidence"] is True


def test_shadow_acts_on_nothing_and_records_agreement(monkeypatch) -> None:
    combined = {"sufficient": True, "answer": "Set it [1].", "missing": ""}

    off = _run(_Search({QUESTION: [_hit("config")]}), _LLM([dict(combined)]), None)
    wire = _Wire({"sufficient": 0.97, "grounded": 0.4, "responsive": 0.9})
    llm = _LLM([dict(combined)])
    shadow = _run(
        _Search({QUESTION: [_hit("config")]}), llm, _decider(mode="shadow"), monkeypatch, wire
    )

    assert shadow.answer == off.answer
    assert _names(shadow) == _names(off)
    assert llm.calls == 1
    logged = shadow.decisions["calls"]
    assert {"sufficiency", "accept"} <= {e["gate"] for e in logged}
    assert next(e for e in logged if e["gate"] == "sufficiency")["agree"] is True
    assert next(e for e in logged if e["gate"] == "accept")["outcome"] == "rejected"
    assert all(e["mode"] == "shadow" for e in logged)


def test_off_is_the_classic_loop(monkeypatch) -> None:
    combined = {"sufficient": True, "answer": "Set it [1].", "missing": ""}
    wire = _Wire({})
    monkeypatch.setattr(providers_module, "_http_json", wire)

    none = _run(_Search({QUESTION: [_hit("config")]}), _LLM([dict(combined)]), None)
    off = _run(
        _Search({QUESTION: [_hit("config")]}),
        _LLM([dict(combined)]),
        _decider(),
        decisions="off",
    )

    assert wire.requests == []
    assert (
        _names(none)
        == _names(off)
        == [
            "classify",
            "retrieve",
            "expand",
            "grade_answer",
            "verify",
        ]
    )
    assert none.decisions == off.decisions == {}


def test_per_request_gates_narrow_what_runs(monkeypatch) -> None:
    wire = _Wire({"sufficient": 0.95})
    llm = _LLM(["Set it [1]."])
    result = _run(
        _Search({QUESTION: [_hit("config")]}),
        llm,
        _decider(),
        monkeypatch,
        wire,
        decision_gates=["sufficiency"],
    )
    assert len(wire.requests) == 1 and "accept" not in _names(result)
    assert llm.log[0]["effort"] is None, "no acceptance gate, so no cheap draft"


# ------------------------------------------------------------ end to end


def _config(mode: str = "beta"):
    from pheasant.config.schema import PheasantConfig

    return PheasantConfig.model_validate(
        {
            "assistant": {
                "provider": "openai",
                "workflow": "agentic",
                "decisions": {"mode": mode},
                "workflow_options": {
                    "agentic": {
                        "staged_retrieval": True,
                        "combine_grade_and_answer": True,
                        "retrieval_modes": ["hybrid"],
                        "expand_graph": False,
                    }
                },
            }
        }
    )


def _chat_and_decisions(script: dict[str, Any], reply: str):
    seen: list[str] = []

    def fake(url, payload, headers, timeout):
        seen.append(url)
        if url.endswith("/decisions"):
            return _answers(payload, script)
        return {
            "choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    return fake, seen


def test_the_answer_payload_carries_the_decisions_block(monkeypatch) -> None:
    pytest.importorskip("langgraph")
    from pheasant.assistant.answering import answer_question

    fake, seen = _chat_and_decisions({"sufficient": 0.96}, "Set it [1].")
    monkeypatch.setattr(providers_module, "_http_json", fake)

    payload = answer_question(
        QUESTION,
        search=_Search({QUESTION: [_hit("config")]}),
        knowledge_base="kb",
        config=_config(),
        env={"OPENAI_API_KEY": "sk-test"},
    )

    assert payload["answer"] == "Set it [1]."
    assert payload["decisions"]["mode"] == "beta"
    assert payload["decisions"]["credential_source"] == "assistant"
    assert payload["decisions"]["accepted"] is True
    assert payload["degraded"] is False
    assert [url.rsplit("/", 1)[-1] for url in seen] == ["decisions", "completions", "decisions"]
    assert "sk-test" not in json.dumps(payload)


def test_mode_off_sends_no_decisions_request_and_no_payload_block(monkeypatch) -> None:
    pytest.importorskip("langgraph")
    from pheasant.assistant.answering import answer_question

    combined = json.dumps({"sufficient": True, "answer": "Set it [1].", "missing": ""})
    fake, seen = _chat_and_decisions({}, combined)
    monkeypatch.setattr(providers_module, "_http_json", fake)

    payload = answer_question(
        QUESTION,
        search=_Search({QUESTION: [_hit("config")]}),
        knowledge_base="kb",
        config=_config("off"),
        env={"OPENAI_API_KEY": "sk-test"},
    )

    assert "decisions" not in payload
    assert all(url.endswith("/chat/completions") for url in seen)
    assert payload["answer"] == "Set it [1]."


# ------------------------------------------------- assess_evidence, both surfaces


@pytest.fixture
def surfaces(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app
    from pheasant.config.schema import PheasantConfig
    from pheasant.mcp_server.tools import PheasantTools

    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "assess",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "sources": [],
        }
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    tools = PheasantTools(config)
    client = TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))
    return {"tools": tools, "client": client, "kb": config.knowledge_base_id}


EVIDENCE = [
    {
        "title": "docs/configuration.md",
        "text": "Set staged_retrieval: true under workflow_options.",
    },
    {"title": "README.md", "text": "pheasant is a Docker-first MCP context server."},
]


def _both(surfaces, **body):
    http = surfaces["client"].post("/assistant/assess", json=body)
    try:
        mcp = surfaces["tools"].assess_evidence(surfaces["kb"], **body)
    except Exception as exc:  # noqa: BLE001 - the refusal is what is compared
        mcp = exc
    return http, mcp


def test_assess_evidence_answers_identically_on_both_surfaces(surfaces, monkeypatch) -> None:
    wire = _Wire(
        {"sufficient": 0.93, "miss": "none", "relevant_1": 0.97, "relevant_2": 0.04},
        {"grounded": 0.9, "responsive": 0.95},
    )
    monkeypatch.setattr(providers_module, "_http_json", wire)
    wire.scripts = [wire.scripts[0], wire.scripts[1], wire.scripts[0], wire.scripts[1]]

    http, mcp = _both(
        surfaces,
        question=QUESTION,
        passages=EVIDENCE,
        answer="Set staged_retrieval: true [1].",
    )

    assert http.status_code == 200, http.text
    assert http.json() == mcp
    assert mcp["sufficient"] == {"verdict": True, "probability": 0.93, "threshold": 0.25}
    assert mcp["missing"]["kind"] is None
    assert [p["relevant"] for p in mcp["passages"]] == [True, False]
    assert mcp["answer"]["accepted"] is True
    assert mcp["intent"] == "procedural"
    first = wire.requests[0][1]
    assert "Set staged_retrieval: true under workflow_options." in first["input"]
    assert [q["name"] for q in first["questions"]][:2] == ["sufficient", "miss"]


def test_insufficient_with_no_named_gap_is_reported_as_unclear(surfaces, monkeypatch) -> None:
    monkeypatch.setattr(providers_module, "_http_json", _Wire({"sufficient": 0.1, "miss": "none"}))
    found = surfaces["tools"].assess_evidence(surfaces["kb"], QUESTION, EVIDENCE, relevance=False)
    assert found["missing"]["kind"] == "unclear"
    assert "no specific gap" in found["missing"]["description"]


def test_assess_evidence_names_the_miss_when_evidence_is_thin(surfaces, monkeypatch) -> None:
    monkeypatch.setattr(
        providers_module, "_http_json", _Wire({"sufficient": 0.05, "miss": "location"})
    )
    found = surfaces["tools"].assess_evidence(surfaces["kb"], QUESTION, EVIDENCE, relevance=False)
    assert found["sufficient"]["verdict"] is False
    assert found["missing"]["kind"] == "location"
    assert "wrong package" in found["missing"]["description"]
    assert "passages" not in found and "answer" not in found


@pytest.mark.parametrize(
    ("body", "status", "text"),
    [
        ({"question": " ", "passages": EVIDENCE}, 400, "question"),
        ({"question": QUESTION, "passages": []}, 422, "non-empty"),
        ({"question": QUESTION, "passages": [{"title": "x"}]}, 422, "passage 1 has no text"),
        ({"question": QUESTION, "passages": [{"text": "x"}] * 25}, 422, "at most 24"),
    ],
)
def test_assess_evidence_refuses_with_one_text(surfaces, body, status, text) -> None:
    from pheasant.services.errors import ServiceError

    http, mcp = _both(surfaces, **body)
    assert isinstance(mcp, ServiceError)
    assert http.status_code == status
    assert http.json()["detail"] == str(mcp)
    assert text in str(mcp)


def test_no_key_and_a_failed_endpoint_are_different_refusals(surfaces, monkeypatch) -> None:
    from pheasant.services.errors import DecisionsNotConfigured, DecisionsUnavailable

    monkeypatch.setattr(providers_module, "_http_json", _Wire({}, error="503 from provider: busy"))
    http, mcp = _both(surfaces, question=QUESTION, passages=EVIDENCE)
    assert isinstance(mcp, DecisionsUnavailable) and mcp.retryable
    assert (http.status_code, http.json()["detail"]) == (503, str(mcp))

    monkeypatch.delenv("OPENAI_API_KEY")
    http, mcp = _both(surfaces, question=QUESTION, passages=EVIDENCE)
    assert isinstance(mcp, DecisionsNotConfigured) and not mcp.retryable
    assert (http.status_code, http.json()["detail"]) == (503, str(mcp))
    assert "OPENAI_API_KEY" in str(mcp)
