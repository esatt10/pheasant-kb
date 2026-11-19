"""Routing decisions use the runtime policy, eligible catalog and authenticated role."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pheasant.assistant import decisions, llm, providers
from pheasant.mcp_server.assistant_tools import AssistantTools
from pheasant.mcp_server.model_tools import ModelTools
from pheasant.persistence.state_store import StateStore
from pheasant.request_budget import DeadlineExceeded, RequestBudget
from pheasant.security.authorization import PermissionDenied
from pheasant.security.identity import Identity, bind_identity, reset_identity
from pheasant.services import ServiceContext, model_routing, models
from pheasant.services.assistant import AnswerRequest
from pheasant.services.errors import InvalidRequest
from tests.test_model_catalog import ADMIN, READER, _config
from tests.test_model_catalog import region as region

ENV = {"TEST_FOUNDRY_KEY": "routing-test-key"}


@pytest.fixture
def context(tmp_path, monkeypatch):
    config = _config(tmp_path)
    luna = config.models.chat[0]
    config.models.chat += [
        replace(luna, id="sol", model="gpt-6-sol", reasoning_level="high"),
        replace(luna, id="terra", model="gpt-6-terra", reasoning_level="medium"),
        replace(luna, id="missing", model="missing", api_key_env="NO_SUCH_ROUTING_KEY"),
    ]
    config.assistant.decisions.api_key_env = "TEST_FOUNDRY_KEY"
    state = StateStore(tmp_path / "routing.db")
    state.migrate()
    decisions.reset_circuit()
    monkeypatch.setattr(llm, "_UNAVAILABLE", {})
    yield ServiceContext(config, state, None)
    state.close()
    decisions.reset_circuit()


def wire(monkeypatch, selected="sol"):
    calls = []

    def fake(url, body, headers, timeout):
        calls.append((url, body, timeout))
        if url.endswith("/systemone"):
            options = body["questions"]["next_model"]["criteria"]
            return {
                "answers": {
                    "next_model": {
                        "type": "choice",
                        "choice": selected,
                        "confidence": 0.8,
                        "probabilities": {i: float(i == selected) for i in options},
                    }
                }
            }
        assert url.endswith("/decisions"), "routing must not generate text to choose a model"
        options = body["questions"][0]["choices"]
        return {
            "answers": [
                {
                    "name": "next_model",
                    "type": "choice",
                    "choice": selected,
                    "confidence": 0.8,
                    "probabilities": [
                        {"value": o["value"], "probability": float(o["value"] == selected)}
                        for o in options
                    ],
                }
            ]
        }

    monkeypatch.setattr(providers, "_http_json", fake)
    return calls


@pytest.mark.parametrize("decision", ["luna", "microsoft-decision-1"])
def test_runtime_rubric_and_options_reach_both_decision_wires(context, monkeypatch, decision):
    calls = wire(monkeypatch)
    policy = {
        "mode": "custom",
        "models": ["sol", "claude-opus-5-5"],
        "criteria": "Prioritize formal verification and detailed code review.",
        "reasoning_levels": {"sol": "high", "claude-opus-5-5": "medium"},
    }
    request = AnswerRequest(
        "Review the code",
        model_routing=policy,
        decision_model=decision,
        history=[{"question": "Check invariants", "answer": "Next."}],
    )
    selected, trace = model_routing.select(context, request, env=ENV)
    assert selected == "sol" and trace["outcome"] == "decided"
    assert trace["candidates"] == ["claude-opus-5-5", "sol"]
    url, body, _ = calls[0]
    assert (url.endswith("/systemone")) == (decision == "microsoft-decision-1")
    serialized = json.dumps(body)
    assert policy["criteria"] in serialized and "Check invariants" in serialized
    assert "high" in serialized and "medium" in serialized
    assert "gpt-6-sol" in serialized and "opus-deploy" in serialized
    assert model_routing.current(context)["criteria"] == model_routing.DEFAULT_CRITERIA


def test_series_and_credential_availability_narrow_the_choices(context, monkeypatch):
    calls = wire(monkeypatch)
    picked, trace = model_routing.select(context, AnswerRequest("Compute"), env=ENV)
    assert picked == "sol"
    assert trace["candidates"] == ["gpt-6-luna", "sol", "terra"]
    assert "missing" not in json.dumps(calls) and "opus" not in json.dumps(calls)
    # Only one available Anthropic entry: no paid decision needed.
    picked, trace = model_routing.select(
        context,
        AnswerRequest("Write", model_routing={"mode": "anthropic"}),
        env=ENV,
    )
    assert picked == "claude-opus-5-5" and trace["outcome"] == "single_available"
    assert len(calls) == 1
    with pytest.raises(model_routing.NoAvailableModel):
        model_routing.select(context, AnswerRequest("q"), env={})


def _judged_shape(calls) -> dict:
    body = json.dumps(calls[-1][1])
    state = json.loads(json.loads(body)["input"])
    return state["answer_shape"]


@pytest.mark.parametrize(
    ("question", "depth", "intent"),
    [
        ("Briefly, what is the harbor subsystem?", "short", "knowledge"),
        ("Compare the harbor and bridge subsystems", "medium", "knowledge"),
        ("Explain the harbor subsystem in detail", "long", "knowledge"),
        ("@detailed What is the harbor subsystem?", "long", "knowledge"),
        ("Write the terraform configuration for a harbor deployment", "short", "compose"),
    ],
)
def test_the_judge_weighs_the_answer_the_router_will_write(
    context, monkeypatch, question, depth, intent
):
    """Depth and kind of answer are read by the router's own rules before the
    judge runs, so "in detail" and "briefly" reach it as the workflow will
    read them, not as the caller's (usually unset) pins."""
    calls = wire(monkeypatch)
    _, trace = model_routing.select(context, AnswerRequest(question), env=ENV)
    shape = _judged_shape(calls)
    assert (shape["depth"], shape["intent"]) == (depth, intent)
    assert shape["depth_why"] and shape["intent_why"]
    assert trace["answer_shape"]["depth"] == depth
    assert "from_index" not in shape


def test_a_pinned_depth_reaches_the_judge_as_pinned(context, monkeypatch):
    calls = wire(monkeypatch)
    model_routing.select(context, AnswerRequest("What is harbor?", depth="long"), env=ENV)
    shape = _judged_shape(calls)
    assert shape["depth"] == "long" and shape["depth_decided_by"] == "pinned"


def test_a_question_the_index_answers_asks_no_judge(context, monkeypatch):
    calls = wire(monkeypatch)
    picked, trace = model_routing.select(context, AnswerRequest("List all sources"), env=ENV)
    assert picked is None and not calls
    assert trace["outcome"] == "not_needed" and trace["answer_shape"]["from_index"]
    picked, trace = model_routing.select(context, AnswerRequest("@search harbor"), env=ENV)
    assert picked is None and trace["outcome"] == "not_needed" and not calls


def test_the_default_rubric_weighs_reasoning_and_answer_depth(context):
    criteria = model_routing.defaults(context)["criteria"]
    for words in ("answer_shape", "depth", "long detailed answer", "compose", "briefly"):
        assert words in criteria


def test_default_reasoning_rubric_applies_to_custom_multi_selection(context):
    policy = model_routing.validate(context, {"mode": "custom", "models": ["sol", "terra"]})
    for level in ("Low:", "Medium:", "High:"):
        assert level in policy["criteria"]


def test_the_judge_keeps_its_credential_when_candidates_are_anthropic(context, monkeypatch):
    opus = context.config.models.chat[1]
    context.config.models.chat.append(replace(opus, id="sonnet", model="sonnet-deploy"))
    context.config.assistant.decisions.api_key_env = None
    calls = wire(monkeypatch, "sonnet")
    selected, trace = model_routing.select(
        context,
        AnswerRequest("Write", model_routing={"mode": "anthropic"}),
        env=ENV,
    )
    assert selected == "sonnet" and trace["outcome"] == "decided" and len(calls) == 1


def test_a_provider_switch_does_not_inherit_another_providers_endpoint_or_key(context):
    entry = context.config.models.chat[1]
    context.config.models.chat[1] = replace(entry, base_url=None, api_key_env=None)
    view = models.config_for(context.config, entry.id)
    assert view.assistant.base_url is None and view.assistant.api_key_env is None
    assert not models.chat_available(context.config, entry.id, ENV)
    assert models.chat_available(context.config, entry.id, {"ANTHROPIC_API_KEY": "native-key"})


def test_shadow_mode_records_a_suggestion_without_changing_the_model(context, monkeypatch):
    context.config.assistant.decisions.mode = "shadow"
    wire(monkeypatch)
    picked, trace = model_routing.select(context, AnswerRequest("q"), env=ENV)
    assert picked == "gpt-6-luna" and trace["outcome"] == "fallback"
    assert trace["suggested"] == "sol"


def test_catalog_still_lists_models_when_picking_is_disabled(context):
    context.config.models.selectable = False
    body = models.describe(context, env=ENV)
    assert body["selectable"] is False and len(body["chat"]) == 5
    assert body["routing"]["settings"]["mode"] == "off"


def test_catalog_capability_hint_is_validated_and_survives_roundtrip(context):
    from pheasant.config.models import ChatModelSettings
    from pheasant.config.schema import PheasantConfig

    with pytest.raises(ValueError, match="reasoning_level"):
        ChatModelSettings(id="bad", reasoning_level="ultra")
    loaded = PheasantConfig.model_validate(context.config.model_dump(mode="json"))
    assert next(m for m in loaded.models.chat if m.id == "sol").reasoning_level == "high"


@pytest.mark.parametrize(
    "bad",
    [
        {"mode": []},
        {"mode": "unknown"},
        {"mode": "custom", "models": []},
        {"models": "sol"},
        {"models": ["unknown"]},
        {"models": ["sol", "sol"]},
        {"criteria": ""},
        {"criteria": "x" * 12001},
        {"reasoning_levels": {"sol": "ultra"}},
        {"endpoint": "https://elsewhere.invalid"},
    ],
)
def test_invalid_policies_are_refused(context, bad):
    with pytest.raises(InvalidRequest):
        model_routing.validate(context, bad)


def test_explicit_selection_and_disabled_selection_preserve_existing_contract(context, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("explicit/off selection must not call a decision model")

    monkeypatch.setattr(decisions.Decider, "decide", forbidden)
    assert model_routing.select(context, AnswerRequest("q", model="sol"), env=ENV) == ("sol", None)
    assert model_routing.select(
        context,
        AnswerRequest("q", model_routing={"mode": "off"}),
        env=ENV,
    ) == (None, None)
    with pytest.raises(InvalidRequest, match="not both"):
        model_routing.admit(context, {"mode": "openai"}, "sol")
    context.config.models.selectable = False
    with pytest.raises(models.ModelSelectionDisabled):
        model_routing.admit(context, {"mode": "openai"}, None)
    assert model_routing.select(context, AnswerRequest("q"), env=ENV) == (None, None)


@pytest.mark.parametrize("response", ["outside", "refusal", "nan", "unavailable"])
def test_unusable_decision_falls_back_only_within_the_selected_pool(context, monkeypatch, response):
    def fake(self, *args):
        if response == "unavailable":
            raise decisions.DecisionUnavailable("404 endpoint details must not escape")
        if response == "refusal":
            return decisions.DecisionResult({"next_model": decisions.Refusal("next_model")}, 0)
        return decisions.DecisionResult(
            {
                "next_model": decisions.Choice(
                    "next_model",
                    "gpt-6-luna" if response == "outside" else "sol",
                    {"sol": float("nan") if response == "nan" else 1, "terra": 0},
                    0.9,
                )
            },
            0,
        )

    monkeypatch.setattr(decisions.Decider, "decide", fake)
    request = AnswerRequest("q", model_routing={"mode": "custom", "models": ["sol", "terra"]})
    picked, trace = model_routing.select(context, request, env=ENV)
    assert picked == "sol" and trace["outcome"] == "fallback"
    assert "endpoint details" not in json.dumps(trace)


def test_routing_obeys_the_end_to_end_deadline(context, monkeypatch):
    wire(monkeypatch)
    budget = RequestBudget(10)
    budget.deadline = 0
    with pytest.raises(DeadlineExceeded):
        model_routing.select(context, AnswerRequest("q"), env=ENV, budget=budget)


def test_recent_failures_exclude_a_model_until_recovery_without_probes(context, monkeypatch):
    from pheasant.assistant.answering import resolve_llm

    handle = resolve_llm(models.config_for(context.config, "sol"), env=ENV)
    now = [100.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        llm,
        "complete",
        lambda *a, **k: (_ for _ in ()).throw(
            providers.ProviderError("429 from provider: overloaded")
        ),
    )
    with pytest.raises(providers.ProviderError):
        handle.complete("s", "p")
    assert not handle.available()
    # Another account's credential is not affected.
    assert replace(handle, api_key="another-key").available()
    wire(monkeypatch, selected="terra")
    _, trace = model_routing.select(context, AnswerRequest("q"), env=ENV)
    assert "sol" not in trace["candidates"]
    now[0] = 161
    assert handle.available()


@pytest.mark.parametrize("role", ["Reader", "Contributor", "Admin"])
def test_runtime_is_available_to_readers_but_saving_requires_a_contributor(context, role):
    policy = {"mode": "custom", "models": ["sol", "terra"], "criteria": "Prefer concise writing"}
    token = bind_identity(Identity("alice", "personal_key", roles=(role,)))
    try:
        assert model_routing.validate(context, policy)["criteria"] == policy["criteria"]
        if role == "Reader":
            with pytest.raises(PermissionDenied):
                model_routing.save(context, policy)
        else:
            model_routing.save(context, policy)
            # Another replica reads the row, without a restart or local YAML change.
            other = StateStore(context.state.path)
            try:
                replica = replace(context, state=other)
                assert model_routing.current(replica)["criteria"] == policy["criteria"]
            finally:
                other.close()
        assert model_routing.describe(context)["can_save"] == (role != "Reader")
    finally:
        reset_identity(token)


def test_http_mcp_save_and_runtime_have_the_same_behavior(region, monkeypatch):
    app, client = region
    context = ServiceContext(app.state.config, app.state.state, None)

    class Tools(AssistantTools, ModelTools):
        services = context

        def _require_knowledge_base(self, knowledge_base):
            context.knowledge_base(knowledge_base)

    tools = Tools()
    policy = {"mode": "custom", "models": ["gpt-6-luna", "claude-opus-5-5"]}
    assert client.put("/models/routing", json=policy, headers=READER).status_code == 403
    saved = client.put("/models/routing", json=policy, headers=ADMIN)
    assert saved.status_code == 200, saved.text
    token = bind_identity(Identity("admin", "personal_key", roles=("Admin",)))
    try:
        assert tools.get_model_routing("kb") == saved.json()
        assert tools.configure_model_routing("kb", policy) == saved.json()
    finally:
        reset_identity(token)
    # A runtime override on either answering surface reaches the same service.
    monkeypatch.setattr("pheasant.assistant.chat.answer_question", lambda *a, **k: {"answer": "ok"})
    wire(monkeypatch, "claude-opus-5-5")
    monkeypatch.setenv("OPENAI_API_KEY", "routing-test-key")
    app.state.config.assistant.decisions.api_key_env = "TEST_FOUNDRY_KEY"
    body = {
        "question": "q",
        "model_routing": {**policy, "criteria": "Prioritize narrative clarity"},
    }
    http = client.post("/assistant/chat", headers=READER, json=body)
    assert http.status_code == 200, http.text
    token = bind_identity(Identity("reader", "personal_key", roles=("Reader",)))
    try:
        mcp = tools.ask_knowledge_base("kb", **body)
        with pytest.raises(PermissionDenied):
            tools.configure_model_routing("kb", policy)
    finally:
        reset_identity(token)
    assert http.json()["models"] == mcp["models"]
    assert http.json()["model_routing"]["selected"] == "claude-opus-5-5"
    assert mcp["model_routing"]["criteria"] == body["model_routing"]["criteria"]
    invalid = {"question": "q", "model_routing": {"mode": "custom", "models": ["invented"]}}
    for route in ("/assistant/chat", "/assistant/chat/stream"):
        assert client.post(route, headers=READER, json=invalid).status_code == 422


def test_session_key_cannot_override_a_routed_model_or_resource(context):
    view = models.config_for(context.config, "sol")
    credential = SimpleNamespace(provider="anthropic", api_key="session-key", base_url=None)
    assert model_routing.credential_environment(view, credential, ENV) is ENV
    credential.provider = "azure"
    credential.base_url = "https://different.services.ai.azure.com/openai/v1"
    assert model_routing.credential_environment(view, credential, ENV) is ENV
    credential.base_url = view.assistant.base_url
    assert (
        model_routing.credential_environment(view, credential, ENV)["TEST_FOUNDRY_KEY"]
        == "session-key"
    )
