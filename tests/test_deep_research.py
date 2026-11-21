"""Deep research: a ``long`` answer someone chose researches before it writes.

Acceptance:

1. **Only a choice starts it.** ``@detailed`` / ``@deep``, ``depth: "long"``
   or ``"deep"`` are deep research; a ``long`` read off the question's words
   ("a comprehensive report on ...") is today's long answer, with today's
   options, and its planner is never told to research.
2. **Its breadth is its own block.** ``DEEP_DEFAULTS`` lands over a config
   that pins ``max_context_passages``, ``deep_options`` overrides it, and
   compose is never deep.
3. **It stops when more would not help**: no passages, a follow-up with
   nothing new, or the cost -- spend plus the projected write at 95% of the
   per-answer limit -- each said in the steps. A section not yet written when
   the limit is reached is written from its passages.
4. **The limit is the operator's**, beside the monthly one: an operator admin
   sets, resets and is refused bad values on HTTP and MCP alike; a granted
   Admin sees no amounts.
5. **A few at a time.** A process runs ``max_concurrent_deep_answers`` (2)
   deep answers at once; past it a deep question is refused
   ``DEEP_RESEARCH_BUSY`` while an ordinary one is still admitted, and the
   operator's figure takes over within the controls' TTL.
6. **The meter feeds it**: a priced call made while a deep answer is open is
   added to that answer, an unpriced one named.
"""

# ruff: noqa: F811 - the region/operator/served fixtures are imported, then requested

from __future__ import annotations

import json
from typing import Any

import pytest

from pheasant.assistant import longform, routing
from pheasant.assistant.workflows import WorkflowRequest, deep
from pheasant.security.identity import reset_identity
from pheasant.services import answer_admission, region_controls
from pheasant.services import assistant as assistant_service
from pheasant.services.errors import DeepResearchBusy, InvalidRequest, OperatorRequired
from pheasant.telemetry import answer_spend, spend
from pheasant.telemetry.usage import METER
from tests.test_intent_routing import _Recorder, _retriever, _Search
from tests.test_region_controls import (  # noqa: F401 - fixtures
    ADMIN,
    READER,
    _granted_admin,
    _mcp,
    _mcp_body,
    operator,
    region,
    served,
)

# ---------------------------------------------------------------- 1. a choice


@pytest.mark.parametrize(
    ("question", "requested", "deep_research"),
    [
        ("@deep how does the sync engine commit?", None, True),
        ("@detailed how does the sync engine commit?", None, True),
        ("how does the sync engine commit?", "long", True),
        ("how does the sync engine commit?", "deep", True),
        ("how does the sync engine commit?", "DEEP", True),
        # Words are not a choice: today's long answer, without research.
        ("explain the sync engine in detail", None, False),
        ("write a comprehensive report on the sync engine", None, False),
        ("a deep dive into the sync engine", None, False),
        ("how does the sync engine commit?", "auto", False),
        ("how does the sync engine commit?", "medium", False),
        ("@brief how does the sync engine commit?", "long", False),
    ],
)
def test_only_a_choice_is_deep_research(question, requested, deep_research):
    assert routing.pinned_deep(question, requested) is deep_research


def test_with_keywords_off_only_a_pinned_depth_is_deep():
    assert routing.pinned_deep("@deep how?", keywords_on=False) is False
    assert routing.pinned_deep("@deep how?", "long", keywords_on=False) is True


def test_deep_is_a_spelling_of_long_and_the_words_are_a_rule():
    assert routing.classify_depth("anything", "deep") == ("long", "pinned by the caller", "pinned")
    depth, _, by = routing.classify_depth("write a comprehensive report on the pipeline")
    assert (depth, by) == ("long", "rule")
    assert routing.deep_research(depth, by) is False
    assert routing.deep_research("long", "pinned") is True
    assert routing.deep_research("long", "planner") is False


# ------------------------------------------------------- 2. its own block


def _state(depth_by: str, **options: Any) -> dict:
    from pheasant.assistant.workflows.agentic import DEFAULTS

    return {
        "options": {**DEFAULTS, "max_context_passages": 16, **options},
        "explicit_options": ["max_context_passages", *options],
        "intent": options.pop("intent", "knowledge") if "intent" in options else "knowledge",
        "depth": "long",
        "depth_by": depth_by,
    }


def test_deep_defaults_win_over_a_config_that_pins_the_window():
    pytest.importorskip("langgraph")
    from pheasant.assistant.workflows.agentic import resolve_options

    chosen = resolve_options(_state("pinned"))
    assert chosen["max_context_passages"] == deep.DEEP_DEFAULTS["max_context_passages"] == 48
    assert chosen["planned_queries"] == 10 and chosen["max_sections"] == 8
    assert chosen["research"] == "deep"

    ruled = resolve_options(_state("rule"))
    assert ruled["max_context_passages"] == 16, "the config's pin still holds today's long"
    assert ruled["max_sections"] == routing.DEPTH_PROFILES["long"]["max_sections"]
    assert "research" not in ruled

    tuned = resolve_options(_state("pinned", deep_options={"max_rounds": 2}))
    assert tuned["max_rounds"] == 2, "deep_options is the operator's knob over the defaults"


def test_compose_is_never_deep_research():
    assert deep.active({"intent": "compose", "depth": "long", "depth_by": "pinned"}) is False
    assert deep.active({"intent": "knowledge", "depth": "long", "depth_by": "pinned"}) is True
    request = assistant_service.AnswerRequest(question="build it", depth="long", intent="compose")
    assert answer_admission.is_deep(request) is False


# ------------------------------------------------- 3. stopping, end to end


def _reply(*, sufficient: bool):
    def reply(system, prompt):
        if "plan retrieval" in system:
            return '{"queries": ["pipeline stages", "pipeline limits"], "modes": ["text"]}'
        if "judge whether" in system:
            if sufficient:
                return '{"sufficient": true}'
            return '{"sufficient": false, "missing": "limits", "next_query": "pipeline limits"}'
        if "plan a long, sectioned answer" in system:
            return (
                '{"overview": "The pipeline has stages [1].", "sections": ['
                '{"heading": "Stages", "passages": [1, 2]},'
                '{"heading": "Limits", "passages": [3]}]}'
            )
        heading = prompt.split("Section to write: ", 1)[1].splitlines()[0]
        return f"Body for {heading} [1]."

    return reply


def _run(question: str, llm: Any, search: Any = None, **options: Any):
    pytest.importorskip("langgraph")
    from pheasant.assistant.workflows.agentic import AgenticWorkflow

    return AgenticWorkflow().run(
        WorkflowRequest(question=question, max_results=8, options=options),
        _retriever(search or _Search()),
        llm,
    )


def test_a_chosen_long_answer_researches_by_aspect():
    search, llm = _Search(), _Recorder(_reply(sufficient=True))
    result = _run("how does the pipeline work?", llm, search, depth="long")

    planner = next(system for system, _, _ in llm.calls if "plan retrieval" in system)
    assert "DEEP RESEARCH" in planner and "up to 10" in planner
    assert result.route["research"] == "deep"
    assert set(search.limits) == {deep.DEEP_DEFAULTS["per_query_results"]}
    assert "sections" in [step.name for step in result.steps]


def test_a_long_answer_read_off_the_words_does_not_research():
    search, llm = _Search(), _Recorder(_reply(sufficient=True))
    result = _run("explain the pipeline in detail", llm, search)

    planner = next(system for system, _, _ in llm.calls if "plan retrieval" in system)
    assert "DEEP RESEARCH" not in planner
    assert "research" not in result.route and result.route["depth"] == "long"
    assert "research" not in [step.name for step in result.steps]


def test_research_stops_when_a_follow_up_finds_nothing_new():
    """The fake search returns the same passages for every query, so the
    follow-up adds nothing: research stops there rather than at six rounds."""

    result = _run("how does the pipeline work?", _Recorder(_reply(sufficient=False)), depth="long")

    stops = [s.detail for s in result.steps if s.name == "research" and "stopped" in s.detail]
    assert stops == [
        "stopped: another round found nothing new; answering from what the knowledge base holds"
    ]
    assert result.counts["retrieval_rounds"] == 2


class _Empty:
    def search_context(self, *args: Any, **kwargs: Any) -> dict:
        return {"results": []}


def test_research_stops_at_once_when_the_knowledge_base_has_nothing():
    llm = _Recorder(_reply(sufficient=False))
    result = _run("how does the pipeline work?", llm, _Empty(), depth="long")

    details = [s.detail for s in result.steps if s.name == "research"]
    assert any("holds nothing that answers this" in d for d in details)
    assert result.counts["retrieval_rounds"] == 1
    assert not any("plan a long, sectioned answer" in s for s, _, _ in llm.calls)


def _spend(spent: float, limit: float = 5.0) -> answer_spend.AnswerSpend:
    open_ = answer_spend.AnswerSpend(limit, enforced=True)
    open_.add(int(spent * 1_000_000), "gpt-6-luna")
    return open_


def test_research_wraps_up_within_five_percent_of_the_limit(monkeypatch):
    monkeypatch.setattr(deep, "projected_write_cost", lambda *a: 0.30)
    with answer_spend.scope(_spend(4.50)):  # $4.50 + $0.30 projected = 96% of $5
        result = _run(
            "how does the pipeline work?", _Recorder(_reply(sufficient=False)), depth="long"
        )
    details = [s.detail for s in result.steps if s.name == "research"]
    assert details[0].startswith("stopped: wrapping up: research spent $4.50 and writing is ")
    assert "$0.30" in details[0] and "$5.00 limit" in details[0]
    assert result.counts["retrieval_rounds"] == 1, "no follow-up round once wrapping up"
    assert details[-1] == "deep research spent $4.50 of its $5.00 limit"
    assert "### Stages\n\nBody for Stages [1]." in result.answer, "it still writes the answer"


def test_research_goes_on_below_the_wrap_up_line(monkeypatch):
    monkeypatch.setattr(deep, "projected_write_cost", lambda *a: 0.20)
    with answer_spend.scope(_spend(4.50)):  # 94%
        result = _run(
            "how does the pipeline work?", _Recorder(_reply(sufficient=False)), depth="long"
        )
    assert result.counts["retrieval_rounds"] == 2, "a follow-up ran"


def test_sections_past_the_limit_are_written_from_their_passages():
    with answer_spend.scope(_spend(5.00)):
        result = _run(
            "how does the pipeline work?", _Recorder(_reply(sufficient=True)), depth="long"
        )
    sections = next(s.detail for s in result.steps if s.name == "sections")
    assert "2 from passages instead" in sections and "$5.00 answer limit reached" in sections
    assert "Body for" not in result.answer


def test_unmetered_research_says_its_spend_is_not_limited():
    with answer_spend.scope(answer_spend.AnswerSpend(5.0, enforced=False)):
        result = _run(
            "how does the pipeline work?", _Recorder(_reply(sufficient=True)), depth="long"
        )
    assert result.steps[-1].detail.startswith("deep research; spend not limited")


def test_the_projection_counts_the_window_and_every_section(monkeypatch):
    seen: list[tuple[int, int]] = []

    def priced(model, fresh, written, cached):
        seen.append((fresh, written))
        return fresh * 1 + written * 4  # 1.0 in / 4.0 out per million, in micros

    monkeypatch.setattr(spend.GUARD, "call_micros", priced)

    class P:
        snippet = "x" * 350

    few = deep.projected_write_cost("gpt-6-luna", [P()] * 4, dict(deep.DEEP_DEFAULTS))
    many = deep.projected_write_cost("gpt-6-luna", [P()] * 40, dict(deep.DEEP_DEFAULTS))
    assert many > few > 0
    assert seen[0][1] == 8 * 2000 + 1200, "every section at its cap, plus the outline"


# --------------------------------------------------- 4. the operator's limit


def test_an_operator_sets_and_resets_the_deep_limits(region, operator):
    shown = region_controls.describe(region)["deep_research"]
    assert shown["answer_spend_limit"] == 5.0 and shown["max_concurrent"] == 2
    assert shown["wrap_up_percent"] == 95 and shown["enforced"] is True

    changed = region_controls.update(
        region, {"deep_answer_spend_limit": 7.5, "max_concurrent_deep_answers": "3"}
    )["deep_research"]
    assert changed["answer_spend_limit"] == 7.5 and changed["custom_answer_spend_limit"] is True
    assert changed["max_concurrent"] == 3 and changed["custom_max_concurrent"] is True

    reset = region_controls.update(
        region, {"deep_answer_spend_limit": None, "max_concurrent_deep_answers": None}
    )["deep_research"]
    assert (reset["answer_spend_limit"], reset["max_concurrent"]) == (5.0, 2)
    assert not reset["custom_answer_spend_limit"] and not reset["custom_max_concurrent"]


@pytest.mark.parametrize(
    "changes",
    [
        {"deep_answer_spend_limit": -1},
        {"deep_answer_spend_limit": "five"},
        {"deep_answer_spend_limit": True},
        {"deep_answer_spend_limit": 1e9},
        {"max_concurrent_deep_answers": -1},
        {"max_concurrent_deep_answers": 2.5},
        {"max_concurrent_deep_answers": 65},
        {"max_concurrent_deep_answers": True},
    ],
)
def test_a_bad_deep_limit_is_refused(region, operator, changes):
    with pytest.raises(InvalidRequest, match=next(iter(changes))):
        region_controls.update(region, changes)


def test_a_granted_admin_sees_no_amounts_and_changes_nothing(region):
    token = _granted_admin(region.config)
    try:
        shown = region_controls.describe(region)["deep_research"]
        assert shown["answer_spend_limit"] is None and shown["currency"] is None
        assert shown["max_concurrent"] == 2, "how many run at once is not an amount"
        with pytest.raises(OperatorRequired):
            region_controls.update(region, {"deep_answer_spend_limit": 50})
    finally:
        reset_identity(token)


def test_both_surfaces_set_the_deep_limits(served):
    app, client = served
    set_ = _mcp_body(
        _mcp(
            client,
            ADMIN,
            "set_region_controls",
            {"deep_answer_spend_limit": 8, "max_concurrent_deep_answers": 4},
        )
    )
    deep_ = json.loads(set_["result"]["content"][0]["text"])["deep_research"]
    assert (deep_["answer_spend_limit"], deep_["max_concurrent"]) == (8.0, 4)
    http = client.get("/region/controls", headers=ADMIN).json()["deep_research"]
    assert (http["answer_spend_limit"], http["max_concurrent"]) == (8.0, 4)

    back = client.put(
        "/region/controls",
        json={"deep_answer_spend_limit": None, "max_concurrent_deep_answers": None},
        headers=ADMIN,
    )
    assert back.status_code == 200
    assert back.json()["deep_research"]["answer_spend_limit"] == 5.0
    refused = client.put("/region/controls", json={"deep_answer_spend_limit": 50}, headers=READER)
    assert refused.status_code == 403


# ------------------------------------------------------- 5. a few at a time


def _deep_request() -> assistant_service.AnswerRequest:
    return assistant_service.AnswerRequest(question="@deep how does it work?")


def test_a_process_runs_two_deep_answers_at_once(region, operator):
    held = [assistant_service.acquire(region, _deep_request()) for _ in range(2)]
    try:
        with pytest.raises(DeepResearchBusy) as busy:
            assistant_service.acquire(region, _deep_request())
        assert busy.value.code == "DEEP_RESEARCH_BUSY" and busy.value.retryable
        assert busy.value.status == 429
        ordinary = assistant_service.acquire(
            region, assistant_service.AnswerRequest(question="what is it?")
        )
        ordinary.release()  # an ordinary answer is still admitted
        held.pop().release()
        held.append(assistant_service.acquire(region, _deep_request()))
    finally:
        for permit in held:
            permit.release()


def test_the_operators_figure_takes_over(region, operator):
    region_controls.update(region, {"max_concurrent_deep_answers": 1})
    first = assistant_service.acquire(region, _deep_request())
    try:
        with pytest.raises(DeepResearchBusy):
            assistant_service.acquire(region, _deep_request())
        region_controls.update(region, {"max_concurrent_deep_answers": 0})  # no separate cap
        assistant_service.acquire(region, _deep_request()).release()
    finally:
        first.release()


def test_a_released_permit_is_released_once(region):
    permit = assistant_service.acquire(region, _deep_request())
    permit.release()
    permit.release()
    held = [assistant_service.acquire(region, _deep_request()) for _ in range(2)]
    for p in held:
        p.release()


def test_the_research_tool_refuses_at_once_when_every_slot_is_taken(served):
    pytest.importorskip("mcp")
    app, client = served
    client.put("/region/controls", json={"max_concurrent_deep_answers": 1}, headers=ADMIN)
    spend.GUARD.forget()
    from pheasant.services import ServiceContext

    held = assistant_service.acquire(
        ServiceContext(app.state.config, app.state.state, None), _deep_request()
    )
    try:
        reply = _mcp_body(
            _mcp(client, READER, "research_knowledge_base", {"question": "how does it work?"})
        )
        assert "DEEP_RESEARCH_BUSY" in json.dumps(reply) or "Deep research is at its limit" in (
            json.dumps(reply)
        )
    finally:
        held.release()


def test_the_research_tool_is_listed_for_everyone_who_can_ask(served):
    pytest.importorskip("mcp")
    from pheasant.security.permissions import MCP_PERMISSIONS

    assert MCP_PERMISSIONS["research_knowledge_base"] == "knowledge.use"
    assert MCP_PERMISSIONS["research_knowledge_base"] == MCP_PERMISSIONS["ask_knowledge_base"]
    app, client = served
    listed = client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={**READER, "accept": "application/json, text/event-stream"},
    )
    names = {tool["name"] for tool in _mcp_body(listed)["result"]["tools"]}
    assert {"research_knowledge_base", "ask_knowledge_base", "wait_for_job"} <= names


# ------------------------------------------------------------ 6. the meter


def test_the_meter_adds_each_priced_call_to_the_open_answer(region, monkeypatch):
    METER.bind(region.state, "kb")
    monkeypatch.setattr(METER, "_maybe_flush", lambda: None)
    try:
        with answer_spend.scope(answer_spend.AnswerSpend(5.0, enforced=True)) as open_:
            METER.model_call("chat", "gpt-6-luna", 1_000_000, 250_000)  # $1 in + $1 out
            METER.model_call("chat", "gpt-6-sol", 1_000_000)  # no stated price
        METER.model_call("chat", "gpt-6-luna", 1_000_000)  # after it closed: not this answer's
        assert open_.spent == pytest.approx(2.0)
        assert open_.unpriced == {"gpt-6-sol"} and open_.calls == 2
    finally:
        METER.unbind(region.state)


def test_a_section_check_reads_the_open_answer():
    assert deep.section_allowed() is None
    with answer_spend.scope(_spend(5.0)):
        assert deep.section_allowed() == "this answer's $5.00 limit was reached"
    with answer_spend.scope(_spend(4.0)):
        assert deep.section_allowed() is None
    assert longform.AnswerLimitReached.__mro__[1] is RuntimeError
