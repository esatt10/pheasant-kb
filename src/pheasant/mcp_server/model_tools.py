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


def register_model_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
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
