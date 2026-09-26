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
}

AUTO_ORDER = ("anthropic", "openai", "gemini")


def resolve_auto_provider(env: dict[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    return next((name for name in AUTO_ORDER if source.get(PROVIDERS[name].api_key_env)), None)
