"""Dependency-free provider metadata shared by setup and chat runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    default_model: str
    default_base_url: str
    api_key_env: str
    key_hint: str


PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        "anthropic",
        "Anthropic",
        "claude-sonnet-5-5",
        "https://api.anthropic.com",
        "ANTHROPIC_API_KEY",
        "sk-ant-…",
    ),
    "openai": ProviderSpec(
        "openai", "OpenAI", "gpt-6-luna", "https://api.openai.com/v1", "OPENAI_API_KEY", "sk-…"
    ),
    "gemini": ProviderSpec(
        "gemini",
        "Google Gemini",
        "gemini-2.5-flash",
        "https://generativelanguage.googleapis.com/v1beta",
        "GEMINI_API_KEY",
        "AIza…",
    ),
}

AUTO_ORDER = ("anthropic", "openai", "gemini")

#: The Claude models whose reasoning level ``assistant.reasoning_effort`` may
#: set, with the effort each runs at when none is requested (Opus and Haiku
#: 5.5 default to ``medium``, Sonnet 5.5 to ``high``). ``low`` is sent as
#: ``output_config.effort``; ``none`` turns thinking off in the model's own
#: spelling (``ANTHROPIC_THINKING_OFF``). Any other Claude model is refused a
#: reasoning level rather than sent a field it may answer with a 400.
ANTHROPIC_DEFAULT_EFFORT: dict[str, str] = {
    "claude-opus-5-5": "medium",
    "claude-sonnet-5-5": "high",
    "claude-haiku-5-5": "medium",
}
#: How each spells "no thinking". Opus 5.5 cannot turn thinking off at any
#: effort level, so it has no entry and ``none`` is refused for it; Sonnet
#: 5.5 refuses ``disabled`` and takes ``between_tools`` instead.
ANTHROPIC_THINKING_OFF: dict[str, dict[str, str]] = {
    "claude-sonnet-5-5": {"type": "between_tools"},
    "claude-haiku-5-5": {"type": "disabled"},
}


def resolve_auto_provider(env: dict[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    return next((name for name in AUTO_ORDER if source.get(PROVIDERS[name].api_key_env)), None)
