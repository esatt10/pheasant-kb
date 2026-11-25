"""Register and inspect managed GTM sources from HTTP or MCP."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Annotated, Any

from pydantic import Field, StringConstraints

from pheasant.config.schema import PLACEHOLDER_SOURCE_PATH
from pheasant.registry.source_registry import SourceRegistry
from pheasant.security.authorization import require_permission
from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest, ServiceError, SourceNotFound
from pheasant_gtm import catalog_store as gtm_catalog
from pheasant_gtm import collection_store as gtm_collections
from pheasant_gtm.catalog import Model, Request
from pheasant_gtm.connector import DOCUMENT, effective_target
from pheasant_gtm.plugin import SOURCE_TYPE, gtm_settings
from pheasant_gtm.sources import url

SourceName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")]
CompanyId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")]
Segment = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=48)]


class CollectionRequest(Model):
    name: SourceName
    company_id: CompanyId | None = None
    segment: Segment | None = None
    target: Request
    input_source: SourceName | None = None
    sync_now: bool = True
    refresh_interval_seconds: int | None = Field(default=None, ge=900, le=604800)


class CollectionConflict(ServiceError):
    status = 409
    code = "GTM_COLLECTION_CONFLICT"


def register(
    context: ServiceContext,
    request: CollectionRequest,
    knowledge_base: str | None = None,
) -> dict[str, Any]:
    require_permission("sources.manage")
    kb_id = context.knowledge_base(knowledge_base)
    existing = context.state.get_source(request.name)
    if existing and existing["type"] != SOURCE_TYPE:
        raise CollectionConflict("A source with this name already has another connector type")
    if request.target.review or request.target.decisions:
        raise InvalidRequest("Managed GTM sources use deterministic extraction during sync")
    if request.segment is not None and request.company_id is None:
        raise InvalidRequest("A company segment requires a catalog company ID")
    try:
        target = request.target.model_dump(mode="json", exclude={"urls"})
        if target["website"]:
            target["website"] = url(target["website"])
        seeds = list(dict.fromkeys(url(seed) for seed in request.target.urls))
        settings = {"target": target, "input_source": request.input_source}
        resolved = effective_target(context.state, settings, seeds)
        # Exercise the same identity and URL rules without fetching anything.
        from pheasant_gtm.workflow import initialize

        initialize(resolved)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
    before = json.loads(existing["config_json"]) if existing else None
    catalog = request.company_id is not None
    if before is not None and bool(gtm_settings(before).get("catalog")) != catalog:
        raise CollectionConflict("Use a separate source name for a GTM catalog")
    payload = (
        dict(before)
        if before is not None
        else {
            "name": request.name,
            "type": SOURCE_TYPE,
            "path": PLACEHOLDER_SOURCE_PATH,
            "description": "Curated GTM company context",
            "sync": {
                "min_interval_seconds": 900 if catalog else 86400,
                "on_startup": False,
                "on_file_change": False,
                "on_git_commit": False,
            },
        }
    )
    payload.pop("gtm", None)
    payload.update(
        extension_settings={"catalog": True} if catalog else settings,
        urls=[] if catalog else seeds,
    )
    payload["taxonomy"] = {**payload.get("taxonomy", {}), "enabled": True, "detect": ["markdown"]}
    if existing:
        payload["enabled"] = bool(existing["enabled"])
    if request.refresh_interval_seconds is not None and not catalog:
        payload["sync"]["min_interval_seconds"] = request.refresh_interval_seconds
    if payload != before:
        source = SourceRegistry(context.config, context.state).install(payload)
        stored = source.model_dump(mode="json")
    else:
        stored = payload
    company_changed = False
    if catalog:
        company_changed = gtm_catalog.register(
            context.state,
            request.name,
            request.company_id,
            {
                **settings,
                "urls": seeds,
                **({"segment": request.segment} if request.segment else {}),
            },
            request.refresh_interval_seconds,
        )
        if request.sync_now:
            gtm_catalog.request_refresh(context.state, request.name, request.company_id)
    sync = (
        context.indexing.start_sync(request.name) if request.sync_now and context.indexing else None
    )
    return {
        "knowledge_base": kb_id,
        "status": "registered"
        if not existing
        else "updated"
        if payload != before or company_changed
        else "unchanged",
        "source": stored,
        "sync": sync,
        **(
            {
                "company_id": request.company_id,
                "path": gtm_catalog.path(request.company_id),
                "company_pending": company_changed
                or gtm_catalog.needs_publication(context.state, request.name, request.company_id),
            }
            if catalog
            else {}
        ),
    }


def _source(context: ServiceContext, name: str, knowledge_base: str | None) -> tuple[str, dict]:
    kb_id = context.knowledge_base(knowledge_base)
    row = context.state.get_source(name)
    if not row or row["type"] != SOURCE_TYPE or row["knowledge_base_id"] != kb_id:
        raise SourceNotFound(name)
    return kb_id, row


def describe(context: ServiceContext, name: str, knowledge_base: str | None = None) -> dict:
    require_permission("knowledge.use")
    kb_id, row = _source(context, name, knowledge_base)
    checkpoint = context.state.get_source_checkpoint(name) or {}
    state = gtm_collections.load(context.state, name)
    pending = (state.get("pending") or {}).get("run") or {}
    snapshot = state.get("snapshot") or {}
    settings = gtm_settings(json.loads(row["config_json"]))
    revision = (checkpoint.get("cursor") or {}).get("revision")
    return {
        "knowledge_base": kb_id,
        "source_name": name,
        "source_type": SOURCE_TYPE,
        "document": "{company_id}/" + DOCUMENT if settings.get("catalog") else DOCUMENT,
        "catalog": bool(settings.get("catalog")),
        "input_source": settings.get("input_source"),
        "enabled": bool(row["enabled"]),
        "last_status": row["last_status"],
        "last_indexed_at": row["last_indexed_at"],
        "indexed_revision": revision,
        "curated_revision": snapshot.get("revision"),
        "progress": {
            "phase": pending.get("phase"),
            "page_attempts": sum(task["attempts"] for task in pending.get("tasks", [])),
            "next_step_at": pending.get("next_step_at"),
        },
        "coverage": (checkpoint.get("high_watermark") or {}).get("gtm"),
        "acquisition": snapshot.get("acquisition"),
        "token_usage": snapshot.get("token_usage"),
    }


def evidence(context: ServiceContext, name: str, knowledge_base: str | None = None) -> dict:
    require_permission("sources.manage")
    kb_id, _ = _source(context, name, knowledge_base)
    snapshot = gtm_collections.load(context.state, name).get("snapshot") or {}
    if not snapshot:
        raise SourceNotFound(f"{name} curated evidence")
    links = {
        sha256(source["url"].encode()).hexdigest()[:24]: source["url"]
        for source in snapshot["profile"]["source_outcomes"]
    }
    return {
        "knowledge_base": kb_id,
        "source_name": name,
        "revision": snapshot["revision"],
        "source_links": links,
        "profile": snapshot["profile"],
        "curated_markdown": snapshot["markdown"],
        "acquisition": snapshot.get("acquisition"),
        "page_tasks": snapshot.get("page_tasks"),
        "token_usage": snapshot.get("token_usage"),
        "persisted": {
            "parsed_outputs": len(snapshot.get("parsed", {})),
            "curated_markdown_bytes": len(snapshot["markdown"].encode()),
            "snapshot_bytes": len(json.dumps(snapshot).encode()),
        },
    }


def refresh(context: ServiceContext, name: str, knowledge_base: str | None = None) -> dict:
    require_permission("sources.manage")
    kb_id, _ = _source(context, name, knowledge_base)
    if context.indexing is None:
        raise InvalidRequest("This region has no source sync adapter")
    return {"knowledge_base": kb_id, "source_name": name, "sync": context.indexing.start_sync(name)}
