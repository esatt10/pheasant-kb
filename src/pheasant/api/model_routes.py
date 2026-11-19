"""The model catalog over HTTP: ``GET /models`` and ``PUT /models/embedding``,
and what the models were used for: ``GET /usage``.

One router per plane. Each handler calls `services.models`, which MCP
``list_models`` / ``select_embedding_model`` use too. Picking an answering
model is the ``model`` / ``decision_model`` fields of ``POST /assistant/chat``.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from pheasant.services import models


class EmbeddingSelection(BaseModel):
    embedding: str


def register_model_routes(app: FastAPI, *, services: Any) -> None:
    @app.get("/models/routing")
    def get_model_routing() -> dict:
        from pheasant.services import model_routing

        return model_routing.describe(services)

    @app.put("/models/routing")
    def save_model_routing(body: dict[str, Any]) -> dict:
        from pheasant.services import model_routing

        return model_routing.save(services, body)

    @app.get("/models")
    def list_models() -> dict:
        """The answering, judgement and embedding models this region offers,
        the default of each, and the embedding in force."""

        return models.describe(services)

    @app.put("/models/embedding")
    def select_embedding(body: EmbeddingSelection) -> dict:
        """Switch the region's embedding model (application Admin). Every
        chunk already indexed is re-embedded; ``embedding.state`` says when
        the index is back in one space."""

        return models.select_embedding(services, body.embedding)

    @app.get("/usage")
    def usage(days: int = 30) -> dict:
        """Tokens by model, questions, searches, people, latency and size over
        the last ``days`` days (Admin). Cost, at the prices the operator stated
        in the catalog, is shown to the operator only."""

        from pheasant.services import usage as usage_service

        return usage_service.describe(services, days=days)
