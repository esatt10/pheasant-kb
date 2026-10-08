"""The Claude 5.5 family on the Anthropic wire.

Opus 5.5, Sonnet 5.5 and Haiku 5.5 each spell a reasoning level the same way
(``output_config.effort``) and "no thinking" three different ways: Opus 5.5
cannot turn it off at all, Sonnet 5.5 refuses ``disabled`` and takes
``between_tools``, Haiku 5.5 takes ``disabled``. A wrong spelling is a 400 on
every answer, so each is asserted on the payload the provider is sent.
"""

from __future__ import annotations

import pytest

from pheasant.assistant import providers as providers_module
from pheasant.assistant.catalog import PROVIDERS
from pheasant.assistant.llm import LLM
from pheasant.assistant.providers import ProviderError, complete

FAMILY = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    sent: list[dict] = []

    def fake_http(url, payload, headers, timeout):
        sent.append({"url": url, "payload": payload, "headers": headers})
        return {
            "stop_reason": "end_turn",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "Grounded [1]."},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    monkeypatch.setattr(providers_module, "_http_json", fake_http)
    return sent


def _ask(model: str | None, effort: str | None = None) -> str:
    return complete(
        "anthropic",
        api_key="k",
        system="Answer from the passages.",
        prompt="Why?",
        model=model,
        reasoning_effort=effort,
    )


def test_the_default_anthropic_model_is_sonnet_5_5(wire: list[dict]) -> None:
    assert PROVIDERS["anthropic"].default_model == "claude-sonnet-5-5"
    assert _ask(None) == "Grounded [1]."
    assert wire[0]["payload"]["model"] == "claude-sonnet-5-5"


@pytest.mark.parametrize("model", FAMILY)
def test_each_model_is_sent_no_sampling_and_no_prefill(wire: list[dict], model: str) -> None:
    # A thinking block ahead of the text is skipped, not read as the answer.
    assert _ask(model) == "Grounded [1]."
    payload = wire[0]["payload"]
    assert wire[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert payload["messages"] == [{"role": "user", "content": "Why?"}]
    assert "temperature" not in payload
    # Unset keeps the model's own default: no effort, no thinking block.
    assert "output_config" not in payload and "thinking" not in payload


@pytest.mark.parametrize("model", FAMILY)
def test_low_is_output_config_effort(wire: list[dict], model: str) -> None:
    _ask(model, "low")
    assert wire[0]["payload"]["output_config"] == {"effort": "low"}
    assert "thinking" not in wire[0]["payload"]


@pytest.mark.parametrize(
    ("model", "thinking"),
    [
        ("claude-sonnet-5-5", {"type": "between_tools"}),
        ("claude-haiku-5-5", {"type": "disabled"}),
    ],
)
def test_none_turns_thinking_off_in_each_models_spelling(
    wire: list[dict], model: str, thinking: dict
) -> None:
    _ask(model, "none")
    assert wire[0]["payload"]["thinking"] == thinking
    assert "output_config" not in wire[0]["payload"]


def test_opus_5_5_refuses_none_before_the_wire(wire: list[dict]) -> None:
    with pytest.raises(ProviderError, match="cannot turn thinking off"):
        _ask("claude-opus-5-5", "none")
    assert wire == []


def test_a_reasoning_level_for_an_unlisted_claude_model_is_refused(wire: list[dict]) -> None:
    with pytest.raises(ProviderError, match="not enabled for Anthropic model"):
        _ask("claude-sonnet-5", "low")
    assert wire == []


def test_the_llm_handle_carries_its_reasoning_level_to_the_wire(wire: list[dict]) -> None:
    llm = LLM(provider="anthropic", api_key="k", model="claude-haiku-5-5")
    llm.with_reasoning_effort("low").complete("Answer.", "Why?")
    assert wire[0]["payload"]["output_config"] == {"effort": "low"}


def test_a_refusal_is_a_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        providers_module,
        "_http_json",
        lambda *a, **k: {
            "stop_reason": "refusal",
            "stop_details": {"type": "refusal", "category": "cyber"},
            "content": [],
        },
    )
    with pytest.raises(ProviderError, match="declined"):
        _ask("claude-opus-5-5")
