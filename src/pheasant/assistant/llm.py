"""A one-method model handle, so workflows never touch provider plumbing.

A workflow should not care whether the key came from the environment or a
browser session, nor which of the three vendors is behind it. It gets an
:class:`LLM` (or ``None``) and calls ``complete``.

``None`` is a first-class state: with no provider reachable every workflow
still has to produce a useful answer from retrieval alone. That is what
makes the offline path real rather than an error branch.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from pheasant.assistant.providers import PROVIDERS, ProviderError, complete, note_model_call


@dataclass
class LLM:
    """A resolved, callable model."""

    provider: str
    api_key: str
    model: str | None = None
    base_url: str | None = None
    source: str = "environment"
    max_output_tokens: int = 4096
    timeout: float = 90.0

    @property
    def model_id(self) -> str:
        spec = PROVIDERS.get(self.provider)
        return self.model or (spec.default_model if spec else "unknown")

    def with_model(self, model: str) -> LLM:
        """Use another model with the same provider, credentials and limits."""
        return replace(self, model=model)

    def complete(self, system: str, prompt: str, *, max_output_tokens: int | None = None) -> str:
        """One turn. Raises :class:`ProviderError` on failure."""
        note_model_call()
        return complete(
            self.provider,
            api_key=self.api_key,
            system=system,
            prompt=prompt,
            model=self.model,
            base_url=self.base_url,
            max_output_tokens=max_output_tokens or self.max_output_tokens,
            timeout=self.timeout,
        )

    def try_complete(self, system: str, prompt: str, **kwargs: Any) -> str | None:
        """Best-effort turn: returns None instead of raising.

        Used for the *optional* model calls inside an agent loop — planning
        and grading. If the planner is unreachable the workflow falls back to
        a deterministic plan rather than failing the whole question; only the
        final synthesis call is allowed to surface an error.
        """
        try:
            return self.complete(system, prompt, **kwargs)
        except ProviderError:
            return None


def llm_from_selection(selection: dict | None, settings: Any) -> LLM | None:
    """Build an :class:`LLM` from :func:`chat.resolve_provider` output."""
    if not selection:
        return None
    return LLM(
        provider=selection["provider"],
        api_key=selection["api_key"],
        model=selection.get("model"),
        base_url=selection.get("base_url"),
        source=selection.get("source", "environment"),
        max_output_tokens=int(getattr(settings, "max_output_tokens", 4096) or 4096),
        timeout=float(getattr(settings, "request_timeout_seconds", 90.0) or 90.0),
    )
