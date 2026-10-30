"""Azure AI Foundry as a model provider: chat, embeddings, captions, decisions.

Acceptance:

1. A Foundry URL is signed with ``api-key``; every other URL keeps
   ``Authorization: Bearer`` byte for byte — the OpenAI path is unchanged.
2. ``assistant.provider: azure`` speaks the OpenAI chat wire on
   ``{resource}/openai/v1``, whichever form of the endpoint was pasted, with
   the deployment as ``model``; without an endpoint it refuses by name.
3. ``search.embeddings``, ``ingestion.captioner`` and
   ``ingestion.transcriber`` reach Foundry as ``openai-spec`` with a Foundry
   ``base_url`` — no new provider, no new client.
4. ``assistant.decisions`` reaches a Foundry ``gpt-6-luna`` deployment's
   Decisions API (``api: decisions``) and Microsoft-Decision-1's System One
   API (``api: systemone``), whose questions and answers are translated so
   the gates read the same ``DecisionResult`` either way.
5. Luna-only options (reasoning effort, streamed drafts) follow the model
   through Foundry, keyed on the OpenAI wire rather than on the vendor.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from pheasant import foundry
from pheasant.assistant import decisions, systemone
from pheasant.assistant import providers as providers_module
from pheasant.assistant.decisions import Decider, DecisionUnavailable, resolve_decider
from pheasant.assistant.decisions import Predicate as PredicateAnswer
from pheasant.assistant.providers import ProviderError, complete
from pheasant.config.assistant_decisions import AssistantDecisionsSettings

RESOURCE = "https://contoso-ai.services.ai.azure.com"


@pytest.fixture(autouse=True)
def _clean_circuit():
    decisions.reset_circuit()
    yield
    decisions.reset_circuit()


def _capture(monkeypatch: Any, reply: dict) -> list[dict]:
    calls: list[dict] = []

    def fake(url: str, payload: dict, headers: dict, timeout: float) -> dict:
        calls.append({"url": url, "payload": payload, "headers": headers})
        return reply

    monkeypatch.setattr(providers_module, "_http_json", fake)
    return calls


CHAT_REPLY = {
    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
}


# ----------------------------------------------------------------- endpoints


@pytest.mark.parametrize(
    "pasted",
    [
        RESOURCE,
        RESOURCE + "/",
        RESOURCE + "/openai/v1",
        RESOURCE + "/openai/v1/",
        RESOURCE + "/api/projects/kb",
    ],
)
def test_every_form_of_the_endpoint_reaches_the_v1_surface(pasted: str) -> None:
    assert foundry.openai_v1_url(pasted) == RESOURCE + "/openai/v1"
    assert foundry.systemone_url(pasted) == RESOURCE + "/providers/microsoft/v1/systemone"


def test_the_classic_azure_openai_host_is_foundry_too() -> None:
    classic = "https://contoso.openai.azure.com/openai/v1"
    assert foundry.is_foundry_url(classic)
    assert foundry.auth_headers(classic, "k") == {"api-key": "k"}


def test_a_url_off_azure_is_untouched_and_keeps_bearer() -> None:
    for url in ("https://api.openai.com/v1", "http://vllm:8000/v1", "https://azure.com.evil.io"):
        assert not foundry.is_foundry_url(url)
        assert foundry.openai_v1_url(url) == url.rstrip("/")
        assert foundry.auth_headers(url, "k") == {"authorization": "Bearer k"}


# ---------------------------------------------------------------------- chat


def test_azure_chat_is_the_openai_wire_with_an_api_key(monkeypatch: Any) -> None:
    calls = _capture(monkeypatch, CHAT_REPLY)
    text = complete(
        "azure",
        api_key="foundry-key",
        system="s",
        prompt="p",
        model="mai-thinking",
        base_url=RESOURCE + "/api/projects/kb",
    )
    assert text == "ok"
    (call,) = calls
    assert call["url"] == RESOURCE + "/openai/v1/chat/completions"
    assert call["headers"] == {"api-key": "foundry-key"}
    assert call["payload"]["model"] == "mai-thinking"


def test_azure_without_an_endpoint_refuses_by_name(monkeypatch: Any) -> None:
    calls = _capture(monkeypatch, CHAT_REPLY)
    with pytest.raises(ProviderError, match="assistant.base_url"):
        complete("azure", api_key="k", system="s", prompt="p")
    assert calls == []


def test_the_openai_provider_is_unchanged(monkeypatch: Any) -> None:
    calls = _capture(monkeypatch, CHAT_REPLY)
    complete("openai", api_key="sk-x", system="s", prompt="p")
    (call,) = calls
    assert call["url"] == "https://api.openai.com/v1/chat/completions"
    assert call["headers"] == {"authorization": "Bearer sk-x"}


def test_luna_reasoning_effort_reaches_a_foundry_luna_deployment(monkeypatch: Any) -> None:
    calls = _capture(monkeypatch, CHAT_REPLY)
    complete(
        "azure",
        api_key="k",
        system="s",
        prompt="p",
        model="gpt-6-luna",
        base_url=RESOURCE,
        reasoning_effort="low",
    )
    assert calls[0]["payload"]["reasoning_effort"] == "low"
    assert "max_completion_tokens" in calls[0]["payload"]


def test_drafting_lowers_effort_for_luna_on_foundry() -> None:
    from pheasant.assistant.llm import LLM
    from pheasant.assistant.workflows import drafting

    for provider in ("openai", "azure"):
        llm = LLM(provider=provider, api_key="k", model="gpt-6-luna", base_url=RESOURCE)
        lowered, effort = drafting.at_effort(llm, "low")
        assert effort == "low" and lowered.reasoning_effort == "low"
    other = LLM(provider="azure", api_key="k", model="mai-thinking", base_url=RESOURCE)
    assert drafting.at_effort(other, "low") == (other, None)


def test_azure_is_never_picked_by_auto() -> None:
    """A key with no resource URL reaches nothing, so `auto` must not choose it."""

    assert providers_module.resolve_auto_provider({"AZURE_FOUNDRY_API_KEY": "k"}) is None


def test_resolve_provider_carries_the_configured_resource() -> None:
    from pheasant.assistant.chat import resolve_provider

    config = SimpleNamespace(
        assistant=SimpleNamespace(
            enabled=True,
            provider="azure",
            api_key_env="AZURE_FOUNDRY_API_KEY",
            model="gpt-6-luna",
            base_url=RESOURCE + "/openai/v1",
        )
    )
    selected = resolve_provider(config, None, {"AZURE_FOUNDRY_API_KEY": "k"})
    assert selected is not None
    assert selected["provider"] == "azure"
    assert selected["base_url"] == RESOURCE + "/openai/v1"


# ------------------------------------------------- embeddings, captions, audio


def test_embeddings_sign_a_foundry_request_with_api_key(monkeypatch: Any) -> None:
    from pheasant.search import embedding_client

    sent: list[Any] = []

    def fake_transport(request: Any, timeout: float) -> bytes:
        sent.append(request)
        return json.dumps({"data": [{"index": 0, "embedding": [0.1, 0.2]}]}).encode()

    monkeypatch.setattr(embedding_client, "_transport", fake_transport)
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "foundry-key")
    embedder = embedding_client.OpenAISpecEmbedder(
        base_url=RESOURCE + "/openai/v1",
        model="text-embedding-3-small",
        api_key_env="AZURE_FOUNDRY_API_KEY",
        dimensions=1536,
    )
    assert embedder.embed(["hello"]) == [[0.1, 0.2]]
    (request,) = sent
    assert request.full_url == RESOURCE + "/openai/v1/embeddings"
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["api-key"] == "foundry-key"
    assert "authorization" not in headers
    body = json.loads(request.data)
    assert body["model"] == "text-embedding-3-small" and body["dimensions"] == 1536


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.mark.parametrize("kind", ["captioner", "transcriber"])
def test_captions_and_transcripts_sign_a_foundry_request(monkeypatch: Any, kind: str) -> None:
    from pheasant.ingestion import captioner, transcriber

    sent: list[Any] = []
    payload = (
        {"choices": [{"message": {"content": "a cat"}}]} if kind == "captioner" else {"text": "hi"}
    )
    module = captioner if kind == "captioner" else transcriber

    def fake_urlopen(request: Any, timeout: float) -> _Response:
        sent.append(request)
        return _Response(payload)

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "foundry-key")
    base = RESOURCE + "/openai/v1"
    if kind == "captioner":
        client = captioner.OpenAISpecVisionCaptioner(base, "gpt-6-luna", "AZURE_FOUNDRY_API_KEY")
        assert client.caption(b"\x89PNG", "a.png") == "a cat"
    else:
        client = transcriber.OpenAISpecTranscriber(base, "whisper", "AZURE_FOUNDRY_API_KEY")
        assert client.transcribe(b"RIFF", "a.wav") == "hi"
    headers = {name.lower(): value for name, value in sent[0].header_items()}
    assert headers["api-key"] == "foundry-key"
    assert "authorization" not in headers


# ------------------------------------------------------------------ decisions


def test_a_foundry_luna_deployment_answers_the_decisions_api(monkeypatch: Any) -> None:
    calls = _capture(
        monkeypatch,
        {"answers": [{"type": "predicate", "name": "ok", "probability": 0.8}]},
    )
    decider = Decider(api_key="k", model="gpt-6-luna", base_url=RESOURCE)
    result = decider.decide("state", [decisions.predicate("ok", "Is it ok?")])
    assert result.probability("ok") == 0.8
    assert calls[0]["url"] == RESOURCE + "/openai/v1/decisions"
    assert calls[0]["headers"] == {"api-key": "k"}
    assert calls[0]["payload"]["model"] == "gpt-6-luna"


QUESTIONS = [
    decisions.predicate("sufficient", "Is the evidence enough?"),
    decisions.choice(
        "next", "Which search next?", [("auth module", "the auth code"), ("none", "")]
    ),
    decisions.score("grounded", "How grounded?", [("no", "unsupported"), ("yes", "")]),
]


def test_systemone_request_carries_every_question_in_its_own_spelling() -> None:
    body = systemone.request("ms-decision-1", "the evidence", QUESTIONS)
    assert body["model"] == "ms-decision-1"
    assert body["state"] == "the evidence"
    assert body["questions"]["sufficient"] == {
        "type": "noul",
        "instructions": "Is the evidence enough?",
    }
    criteria = body["questions"]["next"]["criteria"]
    assert list(criteria) == ["auth module", "none"]  # order is meaning
    assert criteria == {"auth module": "the auth code", "none": None}
    assert body["questions"]["grounded"]["criteria"] == [
        {"label": "no", "description": "unsupported"},
        "yes",
    ]


def test_microsoft_decision_1_answers_through_system_one(monkeypatch: Any) -> None:
    calls = _capture(
        monkeypatch,
        {
            "model": "Microsoft-Decision-1",
            "answers": {
                "sufficient": {"type": "noul", "noul": 0.71},
                "next": {
                    "type": "choice",
                    "choice": "auth module",
                    "confidence": 0.4,
                    "probabilities": {"auth module": 0.7, "none": 0.3},
                },
                "grounded": {
                    "type": "score",
                    "score": 0.9,
                    "confidence": 0.8,
                    "probabilities": {"0": 0.1, "1": 0.9},
                },
            },
            "usage": {"input_tokens": 120, "output_tokens": 0},
        },
    )
    decider = Decider(
        api_key="k", model="ms-decision-1", base_url=RESOURCE + "/openai/v1", api="systemone"
    )
    result = decider.decide("the evidence", QUESTIONS)
    (call,) = calls
    assert call["url"] == RESOURCE + "/providers/microsoft/v1/systemone"
    assert call["headers"] == {"api-key": "k"}
    assert result.answers["sufficient"] == PredicateAnswer("sufficient", 0.71)
    chosen = result.choice("next")
    assert chosen is not None and chosen.choice == "auth module"
    assert chosen.probabilities == {"auth module": 0.7, "none": 0.3}
    graded = result.score("grounded")
    assert graded is not None and graded.score == 0.9
    assert graded.probabilities == {"no": 0.1, "yes": 0.9}
    assert result.input_tokens == 120


def test_an_unanswered_or_mistyped_system_one_answer_is_a_refusal(monkeypatch: Any) -> None:
    _capture(
        monkeypatch,
        {"answers": {"sufficient": {"type": "choice", "choice": "x"}, "next": {"type": "choice"}}},
    )
    decider = Decider(api_key="k", model="ms-decision-1", base_url=RESOURCE, api="systemone")
    result = decider.decide("e", QUESTIONS)
    assert all(result.refused(name) for name in ("sufficient", "next", "grounded"))


def test_system_one_off_foundry_is_refused_once_and_remembered(monkeypatch: Any) -> None:
    calls = _capture(monkeypatch, {})
    decider = Decider(api_key="k", model="ms-decision-1", api="systemone")
    for _ in range(2):
        with pytest.raises(DecisionUnavailable, match="Foundry resource"):
            decider.decide("e", QUESTIONS)
    assert calls == []


def test_a_missing_deployment_opens_the_circuit_for_that_endpoint(monkeypatch: Any) -> None:
    def fail(*args: Any) -> dict:
        raise ProviderError('404 from provider: {"error": {"code": "DeploymentNotFound"}}')

    monkeypatch.setattr(providers_module, "_http_json", fail)
    decider = Decider(api_key="k", model="ms-decision-1", base_url=RESOURCE, api="systemone")
    with pytest.raises(DecisionUnavailable):
        decider.decide("e", QUESTIONS)
    assert "DeploymentNotFound" in (decider.unavailable_reason or "")
    # The OpenAI-wire endpoint on the same resource is a different endpoint.
    assert Decider(api_key="k", base_url=RESOURCE).unavailable_reason is None


def test_resolve_decider_reuses_the_foundry_assistants_credential() -> None:
    from pheasant.assistant.llm import LLM

    settings = SimpleNamespace(
        enabled=True,
        decisions=AssistantDecisionsSettings(api="systemone", model="ms-decision-1"),
    )
    llm = LLM(provider="azure", api_key="foundry-key", base_url=RESOURCE + "/openai/v1")
    decider = resolve_decider(settings, llm, {})
    assert decider is not None
    assert decider.api_key == "foundry-key" and decider.source == "assistant"
    assert decider.endpoint == RESOURCE + "/providers/microsoft/v1/systemone"
    assert decider.describe()["api"] == "systemone"


def test_decisions_api_setting_is_validated() -> None:
    assert AssistantDecisionsSettings().api == "decisions"
    with pytest.raises(ValueError, match="assistant.decisions.api"):
        AssistantDecisionsSettings(api="jev")


# ----------------------------------------------------- the shipped example


def test_the_foundry_example_leaves_nothing_on_openai(monkeypatch: Any) -> None:
    """Every model call in deploy/compose/foundry/ goes to the Foundry resource.

    Built the way a region builds it (the layered loader, pheasant-kb's table
    underneath), then resolved the way a request resolves it — so a flagship
    default that still points at api.openai.com, or a section that falls back
    to OPENAI_API_KEY, fails here rather than in somebody's bill.
    """

    from pathlib import Path

    from pheasant.assistant.answering import resolve_llm
    from pheasant.config.loader import load_config

    path = Path(__file__).resolve().parents[1] / "deploy/compose/foundry/pheasant-kb.foundry.yaml"
    config = load_config(path)
    assert config.pheasant.defaults == "flagship"
    assert config.storage.backend == "postgres"  # pheasant-kb underneath, unchanged
    text = json.dumps(config.model_dump(mode="json"))
    assert "api.openai.com" not in text
    assert "OPENAI_API_KEY" not in text

    env = {"AZURE_FOUNDRY_API_KEY": "foundry-key", "OPENAI_API_KEY": ""}
    llm = resolve_llm(config, None, env)
    assert llm is not None and llm.provider == "azure"
    decider = resolve_decider(config.assistant, llm, env)
    assert decider is not None and decider.api_key == "foundry-key"
    assert foundry.is_foundry_url(decider.endpoint)
    for section in (
        config.search.embeddings,
        config.ingestion.captioner,
        config.ingestion.transcriber,
    ):
        assert foundry.is_foundry_url(section.base_url)
        assert section.api_key_env == "AZURE_FOUNDRY_API_KEY"
