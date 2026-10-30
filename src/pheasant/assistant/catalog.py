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
        "claude-sonnet-5",
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
    # Azure AI Foundry's OpenAI v1 surface: OpenAI models deployed there and
    # Foundry's own (MAI and the rest of the catalog). No default base URL,
    # because the resource is the operator's; `assistant.base_url` names it,
    # and `model` names a *deployment*. The default assumes the deployment is
    # named after its model, as Foundry names it unless told otherwise.
    "azure": ProviderSpec(
        "azure",
        "Azure AI Foundry",
        "gpt-6-luna",
        "",
        "AZURE_FOUNDRY_API_KEY",
        "Foundry resource key",
    ),
}

#: Not `azure`: a Foundry key without the resource's URL reaches nothing, so
#: `auto` would select a provider that cannot answer.
AUTO_ORDER = ("anthropic", "openai", "gemini")

#: Providers that speak OpenAI's chat wire, and so take its options:
#: `reasoning_effort` and streamed drafts, where the model is `gpt-6-luna`.
OPENAI_WIRE = frozenset({"openai", "azure"})


def resolve_auto_provider(env: dict[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    return next((name for name in AUTO_ORDER if source.get(PROVIDERS[name].api_key_env)), None)
