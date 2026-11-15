"""The model catalog over HTTP: ``GET /models`` and ``PUT /models/embedding``.

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
