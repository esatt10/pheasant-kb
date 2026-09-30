"""Every model call works on a model that thinks before it writes.

A reasoning model (GPT-6, Gemini 2.5) spends hidden tokens out of the same
output cap its reply comes from. Every structured call in the assistant had a
cap sized for a model that does not: 400 for the planner, 300 for the grader,
120 for the follow-up rewrite, 700 for a long answer's outline, 1,200 per
section, 2,000 for a medium answer. On a thinking model each came back empty,
and each fell back without a word — the planner to the literal question, the
rewrite to a joined one, and the grader to **"sufficient"**, which quietly
turned off every follow-up retrieval round. The outline raised, which turned
a long answer into extracted passages.

What is asserted, against the real provider wire through its one
``_http_json`` seam:

* ``LLM.complete`` adds room to think on the first exhaustion, remembers the
  model, and gives it that room up front afterwards — so no caller has to
  size its cap for a model it has never met, and a model that does not think
  is sent exactly the cap it always was;
* every step that falls back says so in the trace, with the reason;
* a read timeout or a garbled body is a ``ProviderError``, so a best-effort
  call degrades rather than failing the question;
* the planner, grader and outline all run end to end on a thinking model.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

import pytest

from pheasant.assistant import conversation, providers
from pheasant.assistant.conversation import Turn
from pheasant.assistant.llm import LLM, REASONING_HEADROOM, forget_thinking_models
from pheasant.assistant.providers import OutputBudgetExhausted, ProviderError
from pheasant.assistant.replies import json_object


@pytest.fixture(autouse=True)
def _fresh_models() -> Any:
    forget_thinking_models()
    yield
    forget_thinking_models()


def _thinking_endpoint(thinking: int, reply: Any, seen: list[dict]) -> Any:
    """An OpenAI endpoint whose model spends ``thinking`` tokens before a word.

    ``reply`` is a string, or a function of the system prompt.
    """

    def fake_http(url, payload, headers, timeout):
        seen.append(payload)
        cap = payload.get("max_completion_tokens") or payload.get("max_tokens")
        if cap <= thinking:
            return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        system = payload["messages"][0]["content"]
        text = reply(system) if callable(reply) else reply
        return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}

    return fake_http


def _caps(seen: list[dict]) -> list[int]:
    return [payload.get("max_completion_tokens") or payload.get("max_tokens") for payload in seen]


# ---------------------------------------------------------------------------
# LLM.complete: room to think, learned once
# ---------------------------------------------------------------------------


def test_a_small_cap_on_a_thinking_model_is_retried_with_room_then_remembered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(providers, "_http_json", _thinking_endpoint(2000, "ok", seen))
    llm = LLM(provider="openai", api_key="k", model="gpt-6-luna")

    assert llm.complete("s", "p", max_output_tokens=300) == "ok"
    assert _caps(seen) == [300, 300 + REASONING_HEADROOM]

    # A new handle on the same model — each request builds one — learns
    # nothing new and wastes no turn.
    seen.clear()
    again = LLM(provider="openai", api_key="k", model="gpt-6-luna")
    assert again.complete("s", "p", max_output_tokens=120) == "ok"
    assert _caps(seen) == [120 + REASONING_HEADROOM]


def test_a_model_that_does_not_think_is_sent_exactly_its_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(providers, "_http_json", _thinking_endpoint(0, "ok", seen))
    thinker = LLM(provider="openai", api_key="k", model="gpt-6-luna")
    monkeypatch.setattr(providers, "_http_json", _thinking_endpoint(2000, "ok", []))
    thinker.complete("s", "p", max_output_tokens=300)

    monkeypatch.setattr(providers, "_http_json", _thinking_endpoint(0, "ok", seen))
    LLM(provider="openai", api_key="k", model="gpt-6-sol").complete("s", "p", max_output_tokens=300)
    LLM(provider="openai", api_key="k", model="gpt-6-luna", base_url="http://other/v1").complete(
        "s", "p", max_output_tokens=300
    )
    assert _caps(seen) == [300, 300], "what one model taught says nothing about another"


def test_a_model_that_exhausts_even_the_headroom_fails_once_not_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(providers, "_http_json", _thinking_endpoint(10**6, "ok", seen))
    llm = LLM(provider="openai", api_key="k", model="gpt-6-luna")

    with pytest.raises(OutputBudgetExhausted):
        llm.complete("s", "p", max_output_tokens=300)
    assert len(seen) == 2
    seen.clear()
    with pytest.raises(OutputBudgetExhausted):
        llm.complete("s", "p", max_output_tokens=300)
    assert len(seen) == 1, "a known thinker gets its room up front and no second retry"


def test_try_complete_keeps_the_reason_it_returned_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: Any) -> dict:
        raise ProviderError("401 from provider: bad key")

    monkeypatch.setattr(providers, "_http_json", boom)
    llm = LLM(provider="openai", api_key="k")
    assert llm.try_complete("s", "p") is None
    assert llm.last_failure == "401 from provider: bad key"

    monkeypatch.setattr(providers, "_http_json", _thinking_endpoint(0, "fine", []))
    assert llm.try_complete("s", "p") == "fine"
    assert llm.last_failure is None, "a success clears the last failure"


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def read(self) -> bytes:
        if isinstance(self.body, BaseException):
            raise self.body
        return self.body


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (TimeoutError("The read operation timed out"), "did not answer within 90s"),
        (b"<html>502 Bad Gateway</html>", "not JSON"),
    ],
)
def test_a_timeout_or_a_garbled_body_is_a_provider_error(
    monkeypatch: pytest.MonkeyPatch, body: Any, message: str
) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Response(body))
    llm = LLM(provider="openai", api_key="k")

    assert llm.try_complete("s", "p") is None, "a best-effort call must not raise"
    assert message in (llm.last_failure or "")


# ---------------------------------------------------------------------------
# the callers
# ---------------------------------------------------------------------------


def test_the_follow_up_rewrite_works_on_a_thinking_model(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(
        providers, "_http_json", _thinking_endpoint(1500, "how does sync skip files", seen)
    )
    history = [Turn(question="how does sync skip unchanged files?", answer="By sha256 [1].")]

    searched, why = conversation.standalone_question(
        "and how does it do that?", history, LLM(provider="openai", api_key="k", model="gpt-6-luna")
    )

    assert searched == "how does sync skip files"
    assert "rewritten as" in (why or "")


def test_a_rewrite_that_fails_says_why_in_the_trace() -> None:
    class Down(LLM):
        def __init__(self) -> None:
            super().__init__(provider="openai", api_key="k")

        def complete(self, system, prompt, **kwargs):  # type: ignore[override]
            raise ProviderError("503 from provider: overloaded")

    history = [Turn(question="how does sync skip unchanged files?")]
    searched, why = conversation.standalone_question("and how does it do that?", history, Down())

    assert searched.startswith("how does sync skip unchanged files?")
    assert "model rewrite unavailable (503 from provider: overloaded)" in (why or "")


def _agent_reply(system: str) -> str:
    if "plan retrieval" in system:
        return json.dumps({"queries": ["hash"], "reasoning": "planned by the model"})
    if "judge whether" in system:
        return json.dumps({"sufficient": True})
    if "plan a long, sectioned answer" in system:
        return json.dumps(
            {
                "overview": "Artifacts are hashed [1].",
                "sections": [
                    {"heading": "Hashing", "passages": [1]},
                    {"heading": "Skipping", "passages": [1]},
                ],
            }
        )
    return "Artifacts are hashed with sha256 [1]."


@pytest.fixture
def agentic() -> Any:
    pytest.importorskip("langgraph")
    from pheasant.assistant.workflows.agentic import AgenticWorkflow

    return AgenticWorkflow()


class _Search:
    """One passage for any query: enough for every step to have evidence."""

    vector = None

    def search_context(self, kb, query, mode, max_results, source_name, **kwargs):
        hit = {
            "node_id": "file:kb:docs/hashing.md",
            "chunk_id": "chunk:kb:docs/hashing.md:0",
            "type": "chunk",
            "title": "docs/hashing.md",
            "relative_path": "docs/hashing.md",
            "score": 1.0,
            "chunks": [{"text_preview": "Artifacts are hashed with sha256."}],
        }
        return {"query": query, "mode": mode, "results": [hit], "counts": {}}


def _run(agentic: Any, llm: LLM, **options: Any) -> Any:
    from pheasant.assistant.retrieval import PheasantRetriever
    from pheasant.assistant.workflows import WorkflowRequest

    retriever = PheasantRetriever(search=_Search(), knowledge_base="kb", graph=None, state=None)
    return agentic.run(WorkflowRequest(question="hash", options=options), retriever, llm)


@pytest.mark.parametrize("depth", ["short", "long"])
def test_the_whole_agent_loop_runs_on_a_thinking_model(
    agentic: Any, monkeypatch: pytest.MonkeyPatch, depth: str
) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(providers, "_http_json", _thinking_endpoint(1500, _agent_reply, seen))

    result = _run(agentic, LLM(provider="openai", api_key="k", model="gpt-6-luna"), depth=depth)
    steps = {step.name: step.detail for step in result.steps}

    assert result.mode == "llm"
    assert "planned by the model" in steps["plan"]
    assert steps["grade"] == "evidence is sufficient"
    if depth == "long":
        assert steps["outline"] == "2 sections, planned by the model"
    structured = [p for p in seen if "response_format" in p]
    assert structured, "the planner, grader and outline ask for JSON"


def test_a_planner_and_grader_that_fail_say_so(agentic: Any) -> None:
    class JsonDown(LLM):
        """Answers prose; every structured call fails."""

        def __init__(self) -> None:
            super().__init__(provider="openai", api_key="k")

        def complete(self, system, prompt, **kwargs):  # type: ignore[override]
            if kwargs.get("json_mode"):
                raise OutputBudgetExhausted("spent its budget before writing any text")
            return "Artifacts are hashed with sha256 [1]."

    result = _run(agentic, JsonDown())
    steps = {step.name: step.detail for step in result.steps}

    assert steps["plan"].startswith("planner unavailable (spent its budget")
    assert steps["grade"].startswith("grader unavailable (spent its budget")
    assert result.mode == "llm", "the answer itself still comes from the model"


def test_one_reader_for_every_structured_reply() -> None:
    from pheasant.assistant import longform
    from pheasant.assistant.workflows import agentic

    reply = '<think>{"draft": 1}</think>Sure! ```json\n{"queries": ["a",],}\n```'
    assert json_object(reply) == {"queries": ["a"]}
    assert agentic._parse_json(reply) == longform._parse_json(reply) == {"queries": ["a"]}
