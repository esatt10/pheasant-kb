"""The models a region offers: listing them, picking per request, switching embeddings.

One implementation behind ``GET /models``, ``PUT /models/embedding`` and MCP
``list_models`` / ``select_embedding_model`` (rule 13), and behind the
``model`` / ``decision_model`` a caller sends with a question.

**Picking is a view of the config, not a second code path.** Every workflow
reads ``config.assistant`` -- the provider, the model, the judgement
settings. :func:`config_for` returns a shallow copy of the config whose
``assistant`` is the chosen catalog entries laid over the configured one, so
the planner, the grader, the long form, visuals and the decision gates all
follow a selection without any of them knowing there was one. With nothing
selected and no default configured it returns the config it was given.

**What is provisioned is the operator's; what is used is the caller's.** The
catalog comes from the deployment (``models.*``); a Reader may pick from it.
Nothing here adds a model, a key or an endpoint.

**The embedding model is the region's, not a caller's** (see
``search/embedding_selection.py``): one vector space at a time. Switching it
is an application admin's act (``security.manage``), is durable for every
process, and re-embeds everything already indexed.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import replace
from dataclasses import replace as dataclass_replace
from typing import Any

from pheasant.connections import environment as connection_environment
from pheasant.search import embedding_selection
from pheasant.security.authorization import require_permission
from pheasant.security.identity import current_identity
from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest, ServiceError

logger = logging.getLogger(__name__)

#: The id a region with no catalog lists its one configured model under.
CONFIGURED = "configured"


class UnknownModel(ServiceError):
    status = 404
    code = "UNKNOWN_MODEL"

    def __init__(self, kind: str, requested: str, known: list[str]) -> None:
        super().__init__(
            f"Unknown {kind} model: {requested}. Available: {', '.join(known) or '(none)'}"
        )


class ModelSelectionDisabled(ServiceError):
    status = 403
    code = "MODEL_SELECTION_DISABLED"

    def __init__(self) -> None:
        super().__init__(
            "This region answers with its default models; choosing one per request is off "
            "(models.selectable)"
        )


def _models(config: Any) -> Any:
    return getattr(config, "models", None)


def _usable(entry: Any, env: dict[str, str], fallback_env: str | None) -> bool:
    """Whether this process could call the entry: a key, or an identity for Foundry."""

    from pheasant import foundry
    from pheasant.security import entra

    if str(getattr(entry, "provider", "")).lower() == "stub":
        return True  # the offline embedder asks nobody
    name = entry.api_key_env or fallback_env
    if name and env.get(str(name)):
        return True
    return bool(
        entry.base_url
        and foundry.is_foundry_url(entry.base_url)
        and entra.managed_identity_available()
    )


def _public(entry: Any, **extra: Any) -> dict[str, Any]:
    return {
        "id": entry.id,
        "label": entry.label or entry.model or entry.id,
        "family": entry.family or None,
        "model": entry.model,
        "description": entry.description or None,
        **extra,
    }


#: The ``region_settings`` section holding an Admin's choice of default
#: models, laid over the catalog's ``default_chat`` / ``default_decision``.
DEFAULTS_SECTION = "model_defaults"


def _saved_defaults(config: Any, state: Any) -> dict[str, Any]:
    if state is None:
        return {}
    try:
        rows = state.rows(
            "SELECT data_json FROM region_settings WHERE kb_id=? AND section=?",
            (config.knowledge_base_id, DEFAULTS_SECTION),
        )
    except Exception:  # noqa: BLE001 - a region without the row answers as provisioned
        return {}
    if not rows:
        return {}
    try:
        saved = json.loads(rows[0]["data_json"] or "{}")
    except ValueError:
        return {}
    return saved if isinstance(saved, dict) else {}


def effective_config(config: Any, state: Any) -> Any:
    """``config`` with the Admin's chosen default models, when there are any.

    The catalog (what is provisioned) stays the deployment's; only which entry
    is the default moves. An id the catalog no longer lists is ignored, so a
    redeploy that drops a model falls back to the provisioned default rather
    than failing every question.
    """

    catalog = _models(config)
    saved = _saved_defaults(config, state) if catalog is not None else {}
    if not saved:
        return config
    chat_ids = {entry.id for entry in catalog.chat}
    decision_ids = {entry.id for entry in catalog.decisions}
    chat = saved.get("chat") if saved.get("chat") in chat_ids else None
    decision = saved.get("decision") if saved.get("decision") in decision_ids else None
    if (chat or catalog.default_chat) == catalog.default_chat and (
        decision or catalog.default_decision
    ) == catalog.default_decision:
        return config
    view = copy.copy(config)
    view.models = replace(
        catalog,
        default_chat=chat or catalog.default_chat,
        default_decision=decision or catalog.default_decision,
    )
    return view


def effective(context: ServiceContext) -> ServiceContext:
    """``context`` whose config carries the Admin's default models."""

    config = effective_config(context.config, context.state)
    return context if config is context.config else dataclass_replace(context, config=config)


def set_defaults(context: ServiceContext, changes: dict[str, Any]) -> dict[str, Any]:
    """Choose the region's default answering and judgement models (Admin).

    ``changes`` names ``chat`` and/or ``decision`` by catalog id; ``null``
    returns that kind to the deployment's own default. Every replica reads the
    row on its next question; nothing is restarted and no key moves.
    """

    require_permission("security.manage")
    if not isinstance(changes, dict) or not changes:
        raise InvalidRequest(
            "send chat and/or decision: a catalog id, or null for the deployment's"
        )
    unknown = set(changes) - {"chat", "decision"}
    if unknown:
        raise InvalidRequest(f"unknown default model field(s): {', '.join(sorted(unknown))}")
    catalog = _models(context.config)
    if catalog is None or not (catalog.chat or catalog.decisions):
        raise InvalidRequest("this region lists no models to choose a default from (models.*)")
    saved = dict(_saved_defaults(context.config, context.state))
    for kind, entries in (("chat", catalog.chat), ("decision", catalog.decisions)):
        if kind not in changes:
            continue
        value = changes[kind]
        if value is None:
            saved.pop(kind, None)
            continue
        saved[kind] = _entry(kind, entries, str(value)).id
    identity = current_identity()
    actor = identity.principal if identity is not None else "operator"
    from datetime import UTC, datetime

    context.state.execute(
        "INSERT INTO region_settings(kb_id, section, data_json, updated_at, updated_by) "
        "VALUES(?,?,?,?,?) ON CONFLICT(kb_id, section) DO UPDATE SET "
        "data_json=excluded.data_json, updated_at=excluded.updated_at, "
        "updated_by=excluded.updated_by",
        (
            context.config.knowledge_base_id,
            DEFAULTS_SECTION,
            json.dumps(saved),
            datetime.now(UTC).isoformat(timespec="seconds"),
            actor,
        ),
    )
    logger.info("default models set: by=%s to=%s", actor, saved)
    return describe(context)


def chat_available(config: Any, model: str, env: dict[str, str]) -> bool:
    from pheasant.assistant.answering import resolve_llm

    if not config.models.selectable:
        # Listing a disabled picker still describes every provisioned entry.
        # This view is only for credential/health inspection, never answering.
        view = copy.copy(config)
        view.models = replace(config.models, selectable=True)
        config = view
    llm = resolve_llm(config_for(config, model), env=env)
    return llm is not None and llm.available()


def describe(context: ServiceContext, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Every model a caller may use, by kind, with the default and what is in force.

    Never an endpoint, a variable name or a key: those are the operator's.
    """

    provisioned = _models(context.config)
    context = effective(context)
    config = context.config
    env = env if env is not None else connection_environment()
    models = _models(config)
    assistant = config.assistant
    chat = [
        _public(
            entry,
            provider=entry.provider,
            reasoning_level=entry.reasoning_level,
            default=entry.id == models.default_chat,
            available=chat_available(config, entry.id, env),
        )
        for entry in (models.chat if models else [])
    ]
    if not chat:
        from pheasant.assistant.chat import resolve_provider

        selected = resolve_provider(config, None, env)
        if selected is not None:
            from pheasant.assistant.catalog import PROVIDERS

            spec = PROVIDERS.get(selected["provider"])
            name = selected.get("model") or (spec.default_model if spec else "")
            chat = [
                {
                    "id": CONFIGURED,
                    "label": name,
                    "family": spec.label if spec else None,
                    "model": name,
                    "description": None,
                    "provider": selected["provider"],
                    "default": True,
                    "available": True,
                }
            ]
    decisions = [
        _public(
            entry,
            api=entry.api,
            default=entry.id == models.default_decision,
            available=_usable(entry, env, assistant.decisions.api_key_env),
        )
        for entry in (models.decisions if models else [])
    ]
    active = embedding_selection.active_id(config, context.state)
    embeddings = [
        _public(
            entry,
            dimensions=entry.dimensions,
            default=entry.id == models.default_embedding,
            active=entry.id == active,
            available=_usable(entry, env, config.search.embeddings.api_key_env),
        )
        for entry in (models.embeddings if models else [])
    ]
    settings = config.search.embeddings
    from pheasant.services import model_routing

    return {
        "routing": model_routing.describe(context),
        "selectable": bool(models.selectable) if models else False,
        "chat": chat,
        "default_chat": (models.default_chat if models else None)
        or (chat[0]["id"] if len(chat) == 1 else None),
        "decisions": decisions,
        "default_decision": models.default_decision if models else None,
        # What the deployment provisioned as defaults, beside the Admin's choice
        # above: "back to the deployment's" needs to say what that is.
        "deployment_defaults": {
            "chat": provisioned.default_chat if provisioned else None,
            "decision": provisioned.default_decision if provisioned else None,
        },
        "can_set_defaults": bool(chat or decisions) and _may("security.manage"),
        "embeddings": embeddings,
        "embedding": {
            "active": active,
            "model": settings.model if settings.enabled else None,
            "dimensions": settings.dimensions if settings.enabled else None,
            **(
                embedding_selection.status(config, context.state)
                if embeddings and settings.enabled
                else {"state": "fixed", "ready": True}
            ),
            # Changing it re-embeds every chunk: an application admin's call.
            "can_change": bool(embeddings) and _may("security.manage"),
        },
    }


def _may(permission: str) -> bool:
    from pheasant.security.authorization import allowed

    return allowed(permission)


def _entry(kind: str, entries: list[Any], requested: str) -> Any:
    wanted = str(requested).strip()
    for entry in entries:
        if entry.id == wanted:
            return entry
    raise UnknownModel(kind, wanted, [entry.id for entry in entries])


def resolve(
    config: Any, chat: str | None = None, decision: str | None = None
) -> tuple[Any | None, Any | None]:
    """The catalog entries a request runs with: named, else the defaults.

    ``(None, None)`` is "the configured models, untouched".
    """

    models = _models(config)
    chat = str(chat).strip() if chat else None
    decision = str(decision).strip() if decision else None
    if chat == CONFIGURED:
        chat = None
    if models is None or not (models.chat or models.decisions):
        if chat or decision:
            raise UnknownModel("chat" if chat else "decision", str(chat or decision), [])
        return None, None
    if (chat or decision) and not models.selectable:
        # Naming the default is not choosing.
        if (chat or models.default_chat) != models.default_chat or (
            decision or models.default_decision
        ) != models.default_decision:
            raise ModelSelectionDisabled()
    chat_id = chat or models.default_chat
    decision_id = decision or models.default_decision
    return (
        _entry("chat", models.chat, chat_id) if chat_id else None,
        _entry("decision", models.decisions, decision_id) if decision_id else None,
    )


def config_for(config: Any, chat: str | None = None, decision: str | None = None) -> Any:
    """``config`` as this request should see it: the chosen models in ``assistant``."""

    chat_entry, decision_entry = resolve(config, chat, decision)
    if chat_entry is None and decision_entry is None:
        return config
    assistant = config.assistant
    if chat_entry is not None:
        same_provider = assistant.provider == chat_entry.provider
        same_model = (assistant.provider, assistant.model) == (
            chat_entry.provider,
            chat_entry.model,
        )
        assistant = replace(
            assistant,
            provider=chat_entry.provider,
            model=chat_entry.model,
            base_url=chat_entry.base_url or (assistant.base_url if same_provider else None),
            api_key_env=chat_entry.api_key_env
            or (assistant.api_key_env if same_provider else None),
            max_output_tokens=chat_entry.max_output_tokens or assistant.max_output_tokens,
        )
        if not same_model:
            # Settings tuned for the configured model name another model on
            # the same wire, or ask for an option only that model takes.
            assistant = replace(
                assistant,
                reasoning_effort=None,
                retrieval=replace(
                    assistant.retrieval,
                    grader_model=None,
                    planner_reasoning_effort=None,
                    grader_reasoning_effort=None,
                ),
            )
    if decision_entry is not None:
        current = assistant.decisions
        assistant = replace(
            assistant,
            decisions=replace(
                current,
                api=decision_entry.api,
                model=decision_entry.model,
                base_url=decision_entry.base_url or current.base_url,
                api_key_env=decision_entry.api_key_env or current.api_key_env,
                **{
                    name: getattr(decision_entry, name)
                    for name in ("sufficient_threshold", "relevant_threshold", "accept_threshold")
                    if getattr(decision_entry, name) is not None
                },
            ),
        )
    view = copy.copy(config)
    view.assistant = assistant
    return view


def chosen(config: Any, chat: str | None = None, decision: str | None = None) -> dict[str, Any]:
    """What an answer reports it ran with."""

    chat_entry, decision_entry = resolve(config, chat, decision)
    return {
        "chat": chat_entry.id if chat_entry is not None else None,
        "decision": decision_entry.id if decision_entry is not None else None,
    }


def select_embedding(context: ServiceContext, embedding_id: str) -> dict[str, Any]:
    """Switch the region's embedding model and start re-embedding everything."""

    require_permission("security.manage")
    config = context.config
    entries = embedding_selection.catalog(config)
    if not entries:
        raise InvalidRequest(
            "this region lists no embedding models to choose from (models.embeddings)"
        )
    entry = _entry("embedding", entries, embedding_id)
    previous = embedding_selection.active_id(config, context.state)
    identity = current_identity()
    actor = identity.principal if identity is not None else "operator"
    embedding_selection.select(context.state, config.knowledge_base_id, entry.id, actor)
    embedding_selection.forget(config)
    logger.info("embedding model selected: by=%s from=%s to=%s", actor, previous, entry.id)
    result: dict[str, Any] = {
        "status": "unchanged" if previous == entry.id else "selected",
        "previous": previous,
        "embedding": entry.id,
    }
    if previous != entry.id:
        result["reindex"] = _start_reindex(context)
    return {**result, **describe(context)}


def _start_reindex(context: ServiceContext) -> dict[str, Any]:
    """Get the re-embed moving: here if this process indexes, else on the indexer."""

    engine = context.engine
    indexing = context.indexing
    if (
        engine is not None
        and getattr(engine, "vectors", None) is not None
        and (indexing is None or getattr(indexing, "local", True))
    ):
        import threading

        from pheasant.security.authorization import background_target

        def run() -> None:
            try:
                embedding_selection.ensure_space(engine)
                follow(context)
            except Exception:  # noqa: BLE001 - reported by `GET /models`, retried by the next sync
                logger.exception("Re-embedding after an embedding switch failed")

        threading.Thread(
            target=background_target(run), name="pheasant-reembed", daemon=True
        ).start()
        return {"status": "started", "where": "this process"}
    # A serving replica writes no vectors. Every sync begins by bringing the
    # store into the selected space, so publishing one is what starts it.
    started: list[Any] = []
    if indexing is not None:
        for row in context.state.rows("SELECT name FROM sources WHERE enabled=1 ORDER BY name"):
            try:
                started.append(indexing.start_sync(str(row["name"])))
            except Exception:  # noqa: BLE001 - one source failing to queue must not hide the rest
                logger.warning("Could not queue %s after an embedding switch", row["name"])
            if started:
                break  # one sync is enough to start it; the rest follow their schedule
    return {"status": "queued" if started else "pending", "where": "the indexer"}


def follow(context: ServiceContext) -> bool:
    """Keep this process's query embedder on the selected model (rate-limited)."""

    engine = context.engine
    searcher = context.searcher
    if searcher is None or not hasattr(searcher, "vector"):
        return False

    def reload() -> None:
        indexer = getattr(engine, "vectors", None) if engine is not None else None
        if indexer is not None and embedding_selection.in_force(context.config, indexer.embedder):
            fresh = engine.vector_searcher()
        else:
            # The indexer has not moved yet (it follows on its next sync) or
            # this process has none: build the query side from the config.
            from pheasant.search.vector_store import vector_searcher_from_config

            fresh = vector_searcher_from_config(context.config, context.state)
        held = searcher.vector
        if held is not None and fresh is not None and type(held) is type(fresh):
            # In place: the HTTP and MCP searchers of one process share this
            # object, and replacing it on one would leave the other embedding
            # queries with the old model.
            held.__dict__.update(fresh.__dict__)
        else:
            searcher.vector = fresh

    return embedding_selection.follow(
        context.config,
        context.state,
        lambda: getattr(searcher.vector, "embedder", None),
        reload,
        key="search",
    )
