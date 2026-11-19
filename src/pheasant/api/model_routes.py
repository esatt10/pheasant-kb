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

    @app.put("/models/defaults")
    def set_default_models(body: dict[str, Any]) -> dict:
        """Choose the region's default answering and judgement models (Admin):
        ``chat`` / ``decision`` by catalog id, ``null`` for the deployment's."""

        return models.set_defaults(services, body)

    @app.get("/usage/sessions")
    def usage_sessions(
        days: int = 7,
        person: str | None = None,
        organization: str | None = None,
        model: str | None = None,
        calls: str = "questions",
        match: str | None = None,
    ) -> dict:
        """The sessions a person, organization or model took part in (Admin),
        newest first, read from the interaction ledger when asked. ``match`` is
        a wildcard search (``*`` any characters, ``?`` one) over each call's
        question, answer, operation and session."""

        from pheasant.services import usage_traces

        return usage_traces.sessions(
            services,
            days=days,
            principal=person,
            organization=organization,
            model=model,
            calls=calls,
            match=match,
        )

    @app.get("/usage/sessions/{session_key:path}/export")
    def export_usage_session(
        session_key: str, days: int = 7, calls: str = "questions", match: str | None = None
    ):
        return _export(days=days, session_key=session_key, calls=calls, match=match)

    @app.get("/usage/sessions/{session_key:path}")
    def usage_session(
        session_key: str, days: int = 7, calls: str = "questions", match: str | None = None
    ) -> dict:
        """One session's calls, each answer's steps and models, and a KQL query
        for its spans in Log Analytics (Admin); with ``match``, the calls it finds."""

        from pheasant.services import usage_traces

        return usage_traces.session(services, session_key, days=days, calls=calls, match=match)

    @app.get("/usage/traces/export")
    def export_usage_traces(
        days: int = 7,
        person: str | None = None,
        organization: str | None = None,
        model: str | None = None,
        calls: str = "questions",
        match: str | None = None,
    ):
        return _export(
            days=days,
            principal=person,
            organization=organization,
            model=model,
            calls=calls,
            match=match,
        )

    def _export(**selection: Any):
        """The selected ledger rows as JSON lines. HTTP only, like a raw
        document: it is a file for a browser to save, not a tool result."""

        from fastapi.responses import StreamingResponse

        from pheasant.services import usage_traces

        lines = usage_traces.export_lines(services, **selection)
        first = next(lines, None)  # a refusal raises here, before the stream opens

        def body():
            if first is not None:
                yield first
            yield from lines

        stamp = str(selection.get("session_key") or "traces")
        stamp = "".join(c if c.isalnum() or c in "-_" else "-" for c in stamp)[:60]
        return StreamingResponse(
            body(),
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": f'attachment; filename="pheasant-{stamp}.jsonl"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/usage")
    def usage(days: int = 30) -> dict:
        """Tokens by model, questions, searches, people, latency and size over
        the last ``days`` days (Admin). Cost, at the prices the operator stated
        in the catalog, is shown to the operator only."""

        from pheasant.services import usage as usage_service

        return usage_service.describe(services, days=days)
