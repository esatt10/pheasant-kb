"""One field, anything in it: a path, a repository URL, a page, ``gdrive:``.

The registration half of ``POST /sources/quick-add`` and MCP ``add_source``,
and of ``pheasant up <target>``'s detection: the target is classified
(``targets.resolve_targets``), placed beside the sources that exist
(``registry.placement``: a page joins its host's collection, a taken name moves
a checkout), cloned when this process materializes sources, and registered.
Syncing is the caller's: HTTP can wait for it, MCP publishes it through the
context's ``indexing``.

``connection`` names a connected account (``services/connections.py``) the new
sources read with -- a private repository's token, a Drive sign-in -- and rides
on each source as ``connector.connection``, so every later sync, on whichever
tier runs it, reads with the same account.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pheasant.services import ServiceContext
from pheasant.services.errors import ConnectionNotFound, InvalidRequest


@dataclass(frozen=True)
class AddTargetRequest:
    target: str
    name: str | None = None
    split: bool = False
    taxonomy: bool = False
    connection: str | None = None
    actor: str = "ui"
    transport: str = "http"


def _live_source(context: ServiceContext, name: str) -> Any:
    from pheasant.registry.source_registry import SourceRegistry

    if context.state.source_removed(name):
        return None
    SourceRegistry(context.config, context.state).adopt(name)  # another replica may have changed it
    return next((source for source in context.config.sources if source.name == name), None)


def _audit(context: ServiceContext, request: AddTargetRequest, source: str, kind: str) -> None:
    from pheasant.ingestion.pipeline import utc_now

    created_at = utc_now()
    ordinal = len(context.state.list_source_audit_events(source, limit=10000))
    digest = hashlib.sha256(
        json.dumps(
            {"source_id": source, "action": "quick_add", "created_at": created_at},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    context.state.append_source_audit_event(
        f"audit:{digest}:{ordinal}",
        source,
        "quick_add",
        request.actor,
        request.transport,
        None,
        created_at,
        {"target": request.target, "type": kind, "connection": request.connection},
    )


def add_targets(context: ServiceContext, request: AddTargetRequest) -> dict[str, Any]:
    """Classify, place, (clone,) and register ``request.target``'s sources."""

    from pheasant.security.authorization import require_permission

    require_permission("sources.manage")
    from pheasant.config.schema import PheasantConfig
    from pheasant.registry.placement import place_all
    from pheasant.registry.source_registry import SourceRegistry
    from pheasant.security.path_policy import (
        PathPolicyError,
        allowed_roots,
        resolve_source_path,
        resolve_under,
    )
    from pheasant.targets import TargetError, fetch_target, merged_urls, resolve_targets

    target_text = (request.target or "").strip()
    if not target_text:
        raise InvalidRequest("name something to add: a path, a URL, or gdrive:")
    connection = (request.connection or "").strip() or None
    if connection:
        from pheasant.connections import vault

        row = vault.get(context.state, connection)
        if row is None or row.status != "connected":
            raise ConnectionNotFound(connection)
    config = context.config
    # In a role-split fleet the api owns registration, not materialization:
    # its /state and /workspace are read-only, and the indexer clones a
    # managed repository under the source's write lease.
    materialize_here = context.indexing is None or bool(context.indexing.local)
    if materialize_here:
        base = Path(config.pheasant.state_path)
        clone_root, external = base / "sources", base / "external"
    else:
        base = Path(config.pheasant.workspace_root)
        clone_root, external = base / "sources", base / "external"
    try:
        targets = resolve_targets(
            [target_text],
            clone_root=clone_root,
            workspace=external,
            split=request.split,
            name=request.name,
        )
        if connection:
            for target in targets:
                target.connector = {**(target.connector or {}), "connection": connection}
        # Before fetching: a name taken by another source moves a checkout.
        placed = place_all(targets, lambda name: _live_source(context, name))
        if materialize_here:
            for _, target, _ in placed:
                fetch_target(target)
    except TargetError as exc:
        raise InvalidRequest(str(exc)) from exc

    registry = SourceRegistry(config, context.state)
    created: list[dict[str, Any]] = []
    extended: set[str] = set()
    for kind, target, existing in placed:
        payload = target.to_source_dict()
        if kind != "new":
            payload = existing.model_dump(mode="json")
            if connection:
                payload["connector"] = {
                    **(payload.get("connector") or {}),
                    "connection": connection,
                }
        if kind == "join":
            payload["urls"] = merged_urls(list(existing.urls), target.urls)
            extended.add(target.name)
        if request.taxonomy:
            payload["taxonomy"] = {**(payload.get("taxonomy") or {}), "enabled": True}
        if target.local:
            try:
                if target.clone_url and not materialize_here:
                    # The managed checkout does not exist yet: hold it to the
                    # allowlist without the existence check; the indexer
                    # creates it while processing the queued sync.
                    payload["path"] = str(resolve_under(payload["path"], allowed_roots(config)))
                else:
                    payload["path"] = str(resolve_source_path(payload["path"], config))
            except PathPolicyError as exc:
                raise InvalidRequest(str(exc)) from exc
        try:
            source = PheasantConfig.model_validate({"sources": [payload]}).sources[0]
        except (ValueError, TypeError) as exc:
            raise InvalidRequest(f"Invalid source: {exc}") from exc
        registry.register_source(source)
        # The engine resolves sources off the live config object.
        config.sources = [s for s in config.sources if s.name != source.name]
        config.sources.append(source)
        _audit(context, request, source.name, target.type)
        created.append(payload)
    return {"status": "registered", "sources": created, "extended": sorted(extended)}
