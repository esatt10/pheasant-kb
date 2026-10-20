"""``assistant.decisions`` — the agent loop's judgements (one section, one module).

Lives outside ``schema.py`` because that module is at its size ceiling
(``tests/test_module_budget.py``), whose stated remedy is one module per
section. ``schema.py`` imports and nests it like any other section, so
``model_validate``, the wizard and the docs test see no difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pheasant.config.model import ModelMixin


@dataclass
class AssistantDecisionsSettings(ModelMixin):
    """The agent loop's judgements through the OpenAI Decisions API (beta).

    ``mode``: ``beta`` (the default) asks typed questions — is the evidence
    sufficient, which follow-up search, which passages are relevant, is the
    answer acceptable — instead of a model-written grade, and writes the
    answer once, at ``draft_reasoning_effort``, escalating only when the
    acceptance check rejects it. ``shadow`` asks the same questions beside
    the existing grade and acts on none of them, so they can be compared.
    ``off`` is the agentic workflow exactly as it was before.

    It needs an OpenAI key (``api_key_env``, else the answering model's own
    OpenAI credential, else ``OPENAI_API_KEY``). Without one, or when the
    endpoint fails, every gate falls back to the existing behaviour and the
    trace says so. Thresholds are probabilities; set them from labelled
    examples with ``scripts/calibrate_decisions.py``.
    """

    mode: str = "beta"  # beta | shadow | off
    model: str = "gpt-6-luna"
    #: Defaults to the answering model's base URL when that is OpenAI, else
    #: ``https://api.openai.com/v1``.
    base_url: str | None = None
    api_key_env: str | None = None
    timeout_seconds: float = 10.0
    #: Which judgements run: sufficiency, followup, prune, accept. ``prune``
    #: is available and off by default: measured on the lab region it saved no
    #: time (the answer's cost is output, not input) and, together with the
    #: cheap draft, it cost an answer a detail the full context carried.
    gates: list[str] = field(default_factory=lambda: ["sufficiency", "followup", "accept"])
    #: Evidence is sufficient at or above this probability. Calibrated on
    #: SciFact's expert annotations (scripts/calibrate_decisions.py, 120
    #: windows): 0.25 measured 17% false "sufficient" and 25% false "thin" —
    #: equal costs, because the acceptance gate backstops the first.
    sufficient_threshold: float = 0.25
    #: A passage below this probability of relevance is left out of the
    #: answer's context. Low on purpose: dropping evidence the answer needed
    #: costs more than reading one passage it did not. Measured flat from
    #: 0.05 to 0.7 on SciFact (304 passages: ~7% irrelevant kept, ~5% needed
    #: dropped), so the low end, which is the safer one on another corpus.
    relevant_threshold: float = 0.2
    #: Never prune below this many passages.
    prune_min_keep: int = 3
    #: The answer is accepted when both "grounded" and "responsive" are at or
    #: above this probability. SciFact (180 answers): 0.55 measured ~22%
    #: bad answers accepted and ~20% good ones sent to a rewrite.
    accept_threshold: float = 0.55
    #: Reasoning effort for the first draft (``none`` / ``low``; ``gpt-6-luna``
    #: only — other models write at their normal effort). ``null`` keeps the
    #: assistant's own effort.
    draft_reasoning_effort: str | None = "low"
    #: Rewrites at the assistant's own effort after a rejected draft.
    max_repairs: int = 1

    def __post_init__(self) -> None:
        if self.mode not in {"beta", "shadow", "off"}:
            raise ValueError("assistant.decisions.mode must be 'beta', 'shadow' or 'off'")
        unknown = set(self.gates) - {"sufficiency", "followup", "prune", "accept"}
        if unknown:
            raise ValueError(f"assistant.decisions.gates has unknown gate(s): {sorted(unknown)}")
        for name in ("sufficient_threshold", "relevant_threshold", "accept_threshold"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"assistant.decisions.{name} must be between 0 and 1")
        if self.draft_reasoning_effort not in {None, "none", "low"}:
            raise ValueError(
                "assistant.decisions.draft_reasoning_effort must be 'none', 'low' or unset"
            )
        if self.max_repairs < 0 or self.prune_min_keep < 1 or self.timeout_seconds <= 0:
            raise ValueError(
                "assistant.decisions: max_repairs >= 0, prune_min_keep >= 1, timeout_seconds > 0"
            )
