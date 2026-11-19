"""The model catalog over MCP: ``list_models`` and ``select_embedding_model``.

A mixin plus a registration function, like ``region_tools.py``. Adapters over
`services.models`, the operations behind ``GET /models`` and ``PUT
/models/embedding``. Picking an answering model is not a tool: it is the
``model`` / ``decision_model`` arguments of ``ask_knowledge_base``.
"""

from __future__ import annotations

from typing import Any


class ModelTools:
    """``services.models`` adapters. Parse, call, return."""

    services: Any

    def get_model_routing(self, knowledge_base: str) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import model_routing

        return model_routing.describe(self.services)

    def configure_model_routing(self, knowledge_base: str, configuration: dict) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import model_routing

        return model_routing.save(self.services, configuration)

    def _require_knowledge_base(self, knowledge_base: str | None) -> None:  # pragma: no cover
        raise NotImplementedError

    def list_models(self, knowledge_base: str) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import models

        return models.describe(self.services)

    def select_embedding_model(self, knowledge_base: str, embedding: str) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import models

        return models.select_embedding(self.services, embedding)

    def get_usage(self, knowledge_base: str, days: int = 30) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import usage

        return usage.describe(self.services, days=days)


def register_model_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    @mcp.tool()
    @anticipated
    def get_model_routing(knowledge_base: str) -> dict:
        """Get automatic model routing settings, defaults and whether you can save them."""
        return tools.get_model_routing(knowledge_base)

    @mcp.tool()
    @anticipated
    def configure_model_routing(knowledge_base: str, configuration: dict) -> dict:
        """Save region routing defaults (Contributor/Admin). configuration has mode
        (openai, anthropic, custom or off), models (catalog ids for custom),
        criteria (the decision model's runtime rubric) and optional reasoning_levels
        (catalog id to low/medium/high). For an ephemeral session/request choice,
        pass model_routing to ask_knowledge_base instead; no save permission needed.
        """
        return tools.configure_model_routing(knowledge_base, configuration)

    @mcp.tool()
    @anticipated
    def list_models(knowledge_base: str) -> dict:
        """List the models this region offers, by kind, and the default of each.

        ``chat`` are the answering models and ``decisions`` the judgement
        models: pass an ``id`` as ``model`` / ``decision_model`` to
        ask_knowledge_base to answer with it (``selectable`` false means the
        region answers with its defaults only). ``embeddings`` are the models
        the region can index with; ``embedding.active`` is the one in force
        and ``embedding.state`` whether the index is in its space (``ready``)
        or still being re-embedded (``reindex_pending``). ``available`` false
        means the operator listed a model this process holds no credential
        for."""

        return tools.list_models(knowledge_base)

    @mcp.tool()
    @anticipated
    def select_embedding_model(knowledge_base: str, embedding: str) -> dict:
        """Switch the embedding model the whole region indexes and searches with
        (application Admin only).

        ``embedding`` is an id from list_models. Every chunk already indexed is
        re-embedded with the new model (no source is re-read), which on a large
        knowledge base takes a while and costs one embedding call per chunk;
        until ``embedding.state`` reads ``ready`` in list_models, semantic
        search is partial and answers lean on the text and graph arms."""

        return tools.select_embedding_model(knowledge_base, embedding)

    @mcp.tool()
    @anticipated
    def get_usage(knowledge_base: str, days: int = 30) -> dict:
        """What the region was used for over the last ``days`` days (Admin only).

        ``totals`` and ``by_day`` count questions, searches, model calls and
        tokens; ``by_model`` splits tokens by model, ``people`` and
        ``by_organization`` by who asked, ``by_channel`` by surface.
        ``latency`` gives answer and search p50 / p95 in milliseconds (bucket
        upper bounds, not exact). ``size`` is documents, chunks and indexed
        bytes against the region's ceiling. ``cost`` is at the prices the
        operator stated in the model catalog and is null unless you operate
        the region; ``unpriced_models`` names models whose tokens are counted
        with no price. USAGE_NOT_ENABLED means the region keeps no totals."""

        return tools.get_usage(knowledge_base, days)
