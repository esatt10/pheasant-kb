"""A one-method model handle, so workflows never touch provider plumbing.

A workflow should not care whether the key came from the environment or a
browser session, nor which of the three vendors is behind it. It gets an
:class:`LLM` (or ``None``) and calls ``complete``.

``None`` is a first-class state: with no provider reachable every workflow
still has to produce a useful answer from retrieval alone. That is what
makes the offline path real rather than an error branch.

**The cap a caller passes is for the words it wants back.** A model that
reasons before it writes — GPT-6, Gemini 2.5, any thinking model — spends
hidden tokens out of that same cap, and a cap sized for a 300-token grade or
a 120-token rewrite is spent on thinking alone: an empty reply, reported as
:class:`OutputBudgetExhausted`. Every caller sized its cap for a model that
does not think, and each would need re-sizing for every model that does, so
the room is added here instead: the first exhaustion is retried once with
:data:`REASONING_HEADROOM` on top, and the model is remembered as one that
thinks, so every later call to it gets that room up front rather than paying
for a wasted turn. A cap is a ceiling, not a spend — a model that does not
think writes what it writes and stops — so the headroom costs nothing where
it is not needed.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from typing import Any

from pheasant.assistant.providers import (
    PROVIDERS,
    OutputBudgetExhausted,
    ProviderError,
    complete,
    note_model_call,
)

#: Output tokens added to a caller's cap for a model known to think first.
REASONING_HEADROOM = 8192

#: ``(provider, base_url, model)`` of every model seen to spend a whole cap
#: before writing. Per process, learned, never persisted: a model that stops
#: thinking (a config change, a new release) costs nothing but a larger
#: ceiling until the process restarts.
_THINKING: set[tuple[str, str, str]] = set()
_THINKING_LOCK = threading.Lock()


def forget_thinking_models() -> None:
    """Clear what this process has learned. For tests."""

    with _THINKING_LOCK:
        _THINKING.clear()


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
    #: Why the last :meth:`try_complete` returned ``None``, so a step that
    #: fell back can say what it fell back from.
    last_failure: str | None = field(default=None, compare=False, repr=False)

    @property
    def model_id(self) -> str:
        spec = PROVIDERS.get(self.provider)
        return self.model or (spec.default_model if spec else "unknown")

    def with_model(self, model: str) -> LLM:
        """Use another model with the same provider, credentials and limits."""
        return replace(self, model=model)

    def complete(
        self,
        system: str,
        prompt: str,
        *,
        max_output_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        """One turn. Raises :class:`ProviderError` on failure.

        ``max_output_tokens`` is the room the *reply* needs; a model that
        thinks first gets :data:`REASONING_HEADROOM` on top (module
        docstring). ``json_mode`` asks for a reply that is one JSON object
        where the provider can be told so; the caller still parses
        defensively.
        """
        cap = max_output_tokens or self.max_output_tokens
        key = (self.provider, self.base_url or "", self.model_id)
        with _THINKING_LOCK:
            thinks = key in _THINKING
        try:
            return self._call(
                system, prompt, cap + (REASONING_HEADROOM if thinks else 0), json_mode
            )
        except OutputBudgetExhausted:
            if thinks:
                raise
            with _THINKING_LOCK:
                _THINKING.add(key)
            return self._call(system, prompt, cap + REASONING_HEADROOM, json_mode)

    def _call(self, system: str, prompt: str, cap: int, json_mode: bool) -> str:
        note_model_call()
        kwargs: dict[str, Any] = {"json_mode": True} if json_mode else {}
        return complete(
            self.provider,
            api_key=self.api_key,
            system=system,
            prompt=prompt,
            model=self.model,
            base_url=self.base_url,
            max_output_tokens=cap,
            timeout=self.timeout,
            **kwargs,
        )

    def try_complete(self, system: str, prompt: str, **kwargs: Any) -> str | None:
        """Best-effort turn: returns None instead of raising.

        Used for the *optional* model calls inside an agent loop — planning
        and grading. If the planner is unreachable the workflow falls back to
        a deterministic plan rather than failing the whole question; only the
        final synthesis call is allowed to surface an error. The reason is
        kept in :attr:`last_failure`: a fallback nobody can see is how a model
        switch silently turned off the planner and the grader.
        """
        self.last_failure = None
        try:
            return self.complete(system, prompt, **kwargs)
        except ProviderError as exc:
            self.last_failure = str(exc) or type(exc).__name__
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
