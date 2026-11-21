"""The model catalog: what an operator provisioned, and what callers may pick.

A region has always had one answering model (``assistant.*``), one judgement
model (``assistant.decisions.*``) and one embedding model
(``search.embeddings.*``). This section lists *several* of each, so that:

* a caller picks the answering and judgement model per request (``model`` /
  ``decision_model`` on ``POST /assistant/chat`` and MCP
  ``ask_knowledge_base``), with a default chosen for them;
* an application admin switches the embedding model for the whole region,
  which re-embeds everything already indexed (one vector space at a time: a
  query and the passages it is compared with must come from the same model).

Empty, which is the default, the region is exactly what it was: the three
sections above are the only models, and nothing here is consulted.

Each entry is a *wire*, not an SDK: ``provider`` names one of the HTTP shapes
``assistant/providers.py`` already speaks (``openai``, ``azure`` -- the OpenAI
wire on a Foundry resource -- ``anthropic``, ``gemini``), and ``base_url``
says where. Claude deployed on Azure AI Foundry is ``provider: anthropic``
with the resource's ``/anthropic`` endpoint; nothing is tied to a vendor's
client library. Credentials are named by environment variable, never written.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pheasant.config.model import ModelMixin


@dataclass
class ChatModelSettings(ModelMixin):
    """One answering model a caller may select."""

    #: What a caller sends to select it. Stable: a client stores this.
    id: str = ""
    label: str = ""
    #: Who makes it, for grouping in a picker ("OpenAI", "Anthropic").
    family: str = ""
    provider: str = "openai"
    #: The model, or on Foundry the *deployment*, named on the wire.
    model: str = ""
    base_url: str | None = None
    api_key_env: str | None = None
    description: str = ""
    #: Optional routing hint; describes capability, not a provider-specific effort argument.
    reasoning_level: str | None = None
    #: Overrides ``assistant.max_output_tokens`` for this model when set.
    max_output_tokens: int | None = None
    #: What the operator pays per million tokens, in ``models.currency``:
    #: stated, never looked up. Unset, usage reports this model's tokens and
    #: no cost. ``cached_input_price`` defaults to ``input_price``.
    input_price: float | None = None
    output_price: float | None = None
    cached_input_price: float | None = None

    def __post_init__(self) -> None:
        if self.reasoning_level not in (None, "low", "medium", "high"):
            raise ValueError("models.chat.reasoning_level must be low, medium, high or unset")


@dataclass
class DecisionModelSettings(ModelMixin):
    """One judgement model: the agentic loop's sufficiency and acceptance gates.

    ``api`` is the request shape: ``decisions`` (the OpenAI Decisions API,
    ``gpt-6-luna``) or ``systemone`` (Microsoft-Decision-1 on Foundry). Their
    probabilities are not on one scale, so each carries its own thresholds;
    unset, ``assistant.decisions``'s apply.
    """

    id: str = ""
    label: str = ""
    family: str = ""
    api: str = "decisions"
    model: str = ""
    base_url: str | None = None
    api_key_env: str | None = None
    description: str = ""
    sufficient_threshold: float | None = None
    relevant_threshold: float | None = None
    accept_threshold: float | None = None
    input_price: float | None = None
    output_price: float | None = None
    cached_input_price: float | None = None


@dataclass
class EmbeddingModelSettings(ModelMixin):
    """One embedding model the region can index with."""

    id: str = ""
    label: str = ""
    family: str = ""
    provider: str = "openai-spec"
    model: str = ""
    base_url: str | None = None
    api_key_env: str | None = None
    dimensions: int | None = None
    description: str = ""
    input_price: float | None = None


@dataclass
class ModelsSettings(ModelMixin):
    """Every model this region can call, and the default of each kind."""

    chat: list[ChatModelSettings] = field(default_factory=list)
    #: The ``id`` used when a caller names none. Unset: ``assistant.*`` as
    #: configured, which need not be in the list.
    default_chat: str | None = None
    decisions: list[DecisionModelSettings] = field(default_factory=list)
    default_decision: str | None = None
    embeddings: list[EmbeddingModelSettings] = field(default_factory=list)
    #: The embedding a new region indexes with. A running region's choice is
    #: a row in ``/state`` (``search/embedding_selection.py``), laid over this.
    default_embedding: str | None = None
    #: Whether a caller may pick per request. Off, everyone gets the defaults
    #: and naming a model is refused.
    selectable: bool = True
    #: The currency every price above is stated in. A label, not a conversion.
    currency: str = "USD"
    #: What this knowledge base may spend on chat, judgement and embedding
    #: calls per calendar month (UTC), in ``currency`` at the prices above. At
    #: it, model calls are refused until the 1st or until an operator admin
    #: raises it (``PUT /region/controls``, which overrides this at runtime).
    #: ``null``: no limit. A model with no stated price counts nothing.
    monthly_spend_limit: float | None = 500.0
    #: What one deep-research answer (``@detailed`` / ``@deep``, or a pinned
    #: ``depth: "long"``) may spend, in ``currency``. Research wraps up once
    #: its spend plus the projected cost of writing reaches 95% of it, and a
    #: section not yet written when it is reached is written from its
    #: passages. An operator admin overrides it at runtime
    #: (``PUT /region/controls``). ``null``: no per-answer limit.
    deep_answer_spend_limit: float | None = 5.0

    def __post_init__(self) -> None:
        if self.monthly_spend_limit is not None and float(self.monthly_spend_limit) < 0:
            raise ValueError("models.monthly_spend_limit must be zero or more, or null")
        if self.deep_answer_spend_limit is not None and float(self.deep_answer_spend_limit) < 0:
            raise ValueError("models.deep_answer_spend_limit must be zero or more, or null")
        for kind, entries, default in (
            ("chat", self.chat, self.default_chat),
            ("decisions", self.decisions, self.default_decision),
            ("embeddings", self.embeddings, self.default_embedding),
        ):
            ids = [str(entry.id) for entry in entries]
            if any(not i for i in ids):
                raise ValueError(f"models.{kind}: every entry needs an id")
            if len(set(ids)) != len(ids):
                raise ValueError(f"models.{kind}: ids must be unique")
            if default and default not in ids:
                raise ValueError(
                    f"models.{kind}: default {default!r} is not one of {', '.join(ids) or '(none)'}"
                )
