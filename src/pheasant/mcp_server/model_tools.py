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

    def get_model_prices(self, knowledge_base: str) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import model_prices

        return model_prices.describe(self.services)

    def set_model_prices(self, knowledge_base: str, prices: dict) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import model_prices

        return model_prices.update(self.services, {"prices": prices})

    def export_usage(self, knowledge_base: str, days: int = 30, format: str = "csv") -> dict:  # noqa: A002
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import usage_export

        return usage_export.export_usage(self.services, days=days, format=format)

    def export_usage_traces(  # noqa: PLR0913 - one argument per filter, as on HTTP
        self,
        knowledge_base: str,
        days: int = 7,
        person: str | None = None,
        organization: str | None = None,
        model: str | None = None,
        session: str | None = None,
        calls: str = "questions",
        match: str | None = None,
        offset: int = 0,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import usage_export

        return usage_export.export_traces(
            self.services,
            days=days,
            principal=person,
            organization=organization,
            model=model,
            session_key=session,
            calls=calls,
            match=match,
            offset=offset,
        )

    def set_default_models(
        self, knowledge_base: str, chat: str | None = None, decision: str | None = None
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import models

        changes: dict = {}
        if chat is not None:
            changes["chat"] = None if chat == "deployment" else chat
        if decision is not None:
            changes["decision"] = None if decision == "deployment" else decision
        return models.set_defaults(self.services, changes)

    def list_usage_sessions(
        self,
        knowledge_base: str,
        days: int = 7,
        person: str | None = None,
        organization: str | None = None,
        model: str | None = None,
        calls: str = "questions",
        match: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import usage_traces

        return usage_traces.sessions(
            self.services,
            days=days,
            principal=person,
            organization=organization,
            model=model,
            calls=calls,
            match=match,
        )

    def get_usage_session(
        self,
        knowledge_base: str,
        session: str,
        days: int = 7,
        calls: str = "questions",
        match: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import usage_traces

        return usage_traces.session(self.services, session, days=days, calls=calls, match=match)


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

    @mcp.tool()
    @anticipated
    def get_model_prices(knowledge_base: str) -> dict:
        """What each catalog model costs per million tokens (operator admin only).

        For each model (``kind`` chat, decision or embedding): ``catalog`` (the
        deployment's price), ``override`` (one set at runtime, if any) and
        ``effective`` (the one charged now; null is unpriced), plus
        ``history``, newest first: who changed which price, when, from what to
        what. Same operation as GET /models/prices."""

        return tools.get_model_prices(knowledge_base)

    @mcp.tool()
    @anticipated
    def set_model_prices(knowledge_base: str, prices: dict) -> dict:
        """Set or clear model prices at runtime (operator admin only).

        ``prices`` maps a catalog id (from get_model_prices) to ``{"input": ...,
        "output": ..., "cached_input": ...}`` per million tokens in the
        catalog's currency (the fields you omit keep the catalog's), or to
        null to go back to the catalog's price. A price applies to calls made
        after it is set: what was already spent keeps the price it was charged
        at, in the spend limit and every report. Returns get_model_prices."""

        return tools.set_model_prices(knowledge_base, prices)

    @mcp.tool()
    @anticipated
    def export_usage(knowledge_base: str, days: int = 30, format: str = "csv") -> dict:  # noqa: A002
        """The Admin page's usage measures as one file (Admin only).

        ``format`` "csv" is a ZIP (``encoding: base64``) holding one CSV per
        section of get_usage (totals, by_day, by_model, by_organization,
        by_channel, people, latency), ``spend.csv`` (this month's model spend
        against its limit and whether access is suspended, from
        get_region_controls) and ``report.json``; "json" is the same as one
        document (``encoding: utf-8``). Returns ``filename``, ``media_type``,
        ``encoding``, ``content`` and ``bytes``: decode ``content`` and write it
        to ``filename``. Cost and spend amounts are null unless you operate the
        region. Same operation as GET /usage/export."""

        return tools.export_usage(knowledge_base, days, format)

    @mcp.tool()
    @anticipated
    def export_usage_traces(  # noqa: PLR0913 - one argument per filter, as on HTTP
        knowledge_base: str,
        days: int = 7,
        person: str | None = None,
        organization: str | None = None,
        model: str | None = None,
        session: str | None = None,
        calls: str = "questions",
        match: str | None = None,
        offset: int = 0,
    ) -> dict:
        """The interaction ledger's rows behind the usage totals, as JSON lines (Admin only).

        Filter as list_usage_sessions does (``person``, ``organization``,
        ``model``, ``calls``, ``match``) or name one ``session``. Returns
        ``filename``, ``content`` (one JSON object per line, oldest first,
        attributes included: models, route, steps, tokens), ``rows`` and
        ``has_more``: while it is true call again with ``offset=next_offset``
        (2,000 rows a page). The same rows as GET /usage/traces/export and
        GET /usage/sessions/{session}/export."""

        return tools.export_usage_traces(
            knowledge_base, days, person, organization, model, session, calls, match, offset
        )

    @mcp.tool()
    @anticipated
    def set_default_models(
        knowledge_base: str, chat: str | None = None, decision: str | None = None
    ) -> dict:
        """Choose the region's default answering (``chat``) and judgement
        (``decision``) models by list_models id (Admin only). Pass
        ``"deployment"`` to return a kind to the deployment's own default; omit
        a kind to leave it. Every replica answers with the new default from its
        next question. Returns list_models."""

        return tools.set_default_models(knowledge_base, chat, decision)

    @mcp.tool()
    @anticipated
    def list_usage_sessions(
        knowledge_base: str,
        days: int = 7,
        person: str | None = None,
        organization: str | None = None,
        model: str | None = None,
        calls: str = "questions",
        match: str | None = None,
    ) -> dict:
        """The sessions behind the usage totals (Admin only), newest first.

        Filter by ``person`` (a person as get_usage's people name them),
        ``organization`` (a tenant id from get_usage's by_organization; "" for
        the region's own indexing and shared credentials) or ``model`` (a model
        id). ``days`` is the window and the depth of the drill-down;
        ``retention`` says how many days the interaction ledger keeps. A
        session is a conversation; a call made outside one is keyed
        ``trace:<id>``. Pass a session to get_usage_session for its calls.
        ``calls`` is ``questions`` (default: the questions and searches the
        totals count) or ``all`` (every request, the UI's polling included).
        ``match`` is a wildcard search over each call's question, answer,
        operation and session: ``*`` any characters, ``?`` one, case ignored,
        matched anywhere ("deploy*worker"); it keeps the sessions with a call it
        finds, counting those calls.
        TRACES_NOT_KEPT means the region keeps no interaction ledger."""

        return tools.list_usage_sessions(
            knowledge_base, days, person, organization, model, calls, match
        )

    @mcp.tool()
    @anticipated
    def get_usage_session(
        knowledge_base: str,
        session: str,
        days: int = 7,
        calls: str = "questions",
        match: str | None = None,
    ) -> dict:
        """One session's calls in order (Admin only): each question and answer,
        the models that ran, the steps the answer took with their tokens and
        timings, and ``kql``, a Log Analytics query for the session's spans
        (paste it into the workspace's Logs). The JSON-lines export is HTTP
        only: GET /usage/sessions/{session}/export. ``calls`` and ``match`` as
        in list_usage_sessions; with ``match``, only the calls it finds."""

        return tools.get_usage_session(knowledge_base, session, days, calls, match)
