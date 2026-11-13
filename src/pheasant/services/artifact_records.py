"""Typed artifact operations shared by HTTP, MCP and legacy memory adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pheasant.artifacts.reinforcement import StateReinforcementIndex
from pheasant.artifacts.store import ArtifactRecordStore, artifact_source
from pheasant.config.schema import PheasantConfig
from pheasant.registry.source_registry import SourceRegistry
from pheasant.security.authorization import require_permission
from pheasant.security.identity import effective_principal
from pheasant.services import ServiceContext
from pheasant.services.errors import ArtifactNotFound, InvalidRequest


def source(context: ServiceContext, *, create: bool = False) -> Any:
    found = artifact_source(context.config, context.state)
    if found is not None or not create:
        return found
    config = context.config
    directory = Path(config.pheasant.state_path) / "artifact-records"
    found = PheasantConfig.model_validate(
        {
            "sources": [
                {
                    "name": "artifact-records",
                    "type": "artifact",
                    "path": str(directory),
                    "include": ["**/*.md"],
                    "exclude": ["**/*.archived"],
                    "taxonomy": {"enabled": config.artifacts.taxonomy_enabled},
                    "sync": {"on_startup": False},
                }
            ]
        }
    ).sources[0]
    directory.mkdir(parents=True, exist_ok=True)
    SourceRegistry(config, context.state).register_source(found)
    config.sources.append(found)
    return found


def visible(record: Any, principal: str | None) -> bool:
    return (
        record.scope == "org"
        or not record.written_by
        or record.written_by == effective_principal(principal)
    )


@dataclass(frozen=True)
class WriteRequest:
    text: str
    scope: str = "user"
    subject: str | None = None
    supersedes: str | None = None
    tags: tuple[str, ...] | list[str] = ()
    sync: bool = True
    kind: str = "fact"
    principal: str | None = None
    valid_until: str | None = None
    channel: str = "api"


def write(
    context: ServiceContext, request: WriteRequest, *, legacy: bool = False
) -> dict[str, Any]:
    require_permission("artifacts.manage")
    found = source(context, create=not legacy)
    if found is None:
        raise InvalidRequest(
            "no enabled type: memory (deprecated) or type: artifact source; "
            "use record_artifact or POST /artifacts/records to enable artifact knowledge"
        )
    who = effective_principal(request.principal) or None
    store = ArtifactRecordStore(found.path)
    if request.supersedes:
        previous = next(
            (r for r in store.list_records() if r.record_id == request.supersedes), None
        )
        if previous is None or not visible(previous, who):
            raise InvalidRequest(
                "the replaced artifact is unknown or not visible to this principal"
            )
        if not legacy and (previous.scope != request.scope or previous.written_by != who):
            raise InvalidRequest("a replacement must retain its artifact's scope and writer")
    try:
        record, created = store.append(
            request.text,
            scope=request.scope,
            subject=request.subject,
            supersedes=request.supersedes,
            tags=request.tags,
            kind=request.kind,
            written_by=who,
            valid_until=request.valid_until,
            reinforcement=StateReinforcementIndex(context.state)
            if context.config.memory.reinforcement_enabled
            else None,
        )
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
    from pheasant.telemetry import metrics

    outcome = store.last_outcome or ("created" if created else "duplicate")
    from pheasant.artifacts.publications import keep_version, publish, status

    content = {k: v for k, v in record.as_dict().items() if k != "path"}
    publication = status(context.state, context.config.knowledge_base_id, record.record_id)
    if record.scope != "session" and (created or publication is None):
        replaced = (
            request.supersedes
            if request.supersedes
            and status(context.state, context.config.knowledge_base_id, request.supersedes)
            else None
        )
        publish(
            context.state,
            context.config.knowledge_base_id,
            record.record_id,
            content,
            receipt={"source": found.name, "outcome": outcome},
            replaces=replaced,
        )
    else:
        with context.state.conn:
            keep_version(context.state, context.config.knowledge_base_id, record.record_id, content)
    metrics.record_memory_write(outcome, store.last_fold)
    payload = {
        "record": record.as_dict(),
        "artifact_id": record.record_id,
        "created": created,
        "source": found.name,
        "outcome": outcome,
    }
    if not created and request.text.strip() != record.text:
        payload["submitted_text"] = request.text.strip()
    if request.sync and created:
        try:
            if context.engine is not None and (context.indexing is None or context.indexing.local):
                payload["sync"] = context.engine.sync_source(
                    found.name, "incremental", enrich="deferred"
                ).__dict__
            elif context.indexing is not None:
                payload["sync"] = context.indexing.start_sync(found.name)
        except Exception as exc:
            payload["sync_deferred"] = str(exc)
    context.state.append_source_audit_event(
        uuid4().hex,
        found.name,
        "memory_write" if legacy else "artifact_record_write",
        who or request.channel,
        request.channel,
        None,
        datetime.now(UTC).isoformat(),
        {
            "record_id": record.record_id,
            "scope": record.scope,
            "kind": record.kind,
            "created": created,
        },
    )
    return payload


def list_records(
    context: ServiceContext,
    principal: str | None = None,
    *,
    scope: str | None = None,
    current_only: bool = True,
) -> dict[str, Any]:
    found = source(context)
    if found is None:
        return {"records": [], "source": None}
    try:
        records = ArtifactRecordStore(found.path).list_records(scope, current_only=current_only)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
    return {
        "records": [r.as_dict() for r in records if visible(r, principal)],
        "source": found.name,
    }


def get_record(
    context: ServiceContext, artifact_id: str, principal: str | None = None, *, record: Any = None
) -> dict:
    found = source(context)
    if found is None:
        raise ArtifactNotFound(artifact_id)
    if record is None:
        record = next(
            (
                r
                for r in ArtifactRecordStore(found.path).list_records()
                if r.record_id == artifact_id
            ),
            None,
        )
    if record is None or not visible(record, principal):
        raise ArtifactNotFound(artifact_id)
    from pheasant.artifacts.projection import artifact_id_for

    path = f"{record.scope}/{record.record_id}.md"
    return {
        "id": record.record_id,
        "kind": "report",
        "title": record.subject or record.text[:90],
        "created_at": record.asserted_at,
        "owner": record.written_by,
        "source": found.name,
        "folder": record.scope,
        "files": [],
        "has_visual": False,
        "semantic_type": record.kind,
        "node_id": artifact_id_for(found.name, record),
        "payload": {
            "answer": record.text,
            "files": [],
            "citations": [],
            "record": record.as_dict(),
            "source_path": path,
        },
        "index_status": "indexed"
        if context.state.rows(
            "SELECT id FROM artifacts WHERE source_id=? AND relative_path=?", (found.name, path)
        )
        else "pending",
    }


def retire(context: ServiceContext, artifact_id: str, principal: str | None = None) -> dict:
    require_permission("artifacts.manage")
    found = source(context)
    held = get_record(context, artifact_id, principal)
    store = ArtifactRecordStore(found.path)
    record = store.load(Path(held["payload"]["record"]["path"]))
    store.archive(record)
    import json

    from pheasant.artifacts.publications import deprecate
    from pheasant.persistence import archive
    from pheasant.security.acl import normalize_acl

    now = datetime.now(UTC).isoformat()
    acl = normalize_acl("artifact", record.as_dict())
    with context.state.conn:
        archive.add(
            context.state,
            source_id=found.name,
            relative_path=held["payload"]["source_path"],
            artifact_id=held["node_id"],
            kind="removed",
            now=now,
            archived_by=effective_principal(principal),
            acl=json.dumps(acl) if acl is not None else None,
        )
        context.state.conn.execute(
            "UPDATE memory_records SET valid_until=CASE "
            "WHEN valid_until IS NULL OR valid_until>? THEN ? ELSE valid_until END "
            "WHERE record_id=? AND source_id=?",
            (now, now, artifact_id, found.name),
        )
        deprecate(context.state, context.config.knowledge_base_id, artifact_id)
    if context.engine is not None and (context.indexing is None or context.indexing.local):
        context.engine.sync_source(found.name, "incremental")
    elif context.indexing is not None:
        _queue_retirement(context, found.name)
    return {
        "deleted": artifact_id,
        "title": held["title"],
        "removed_files": [held["payload"]["source_path"]],
    }


def _queue_retirement(context: ServiceContext, source_name: str) -> None:
    from pheasant.sync.queue import IndexTask, queue_from_config

    queue = queue_from_config(context.config, context.state)
    if queue is None:
        remove = context.indexing.remove_documents or context.indexing.start_sync
        remove(source_name)
        return
    try:
        # Each newly retired path is work even within the broker's duplicate
        # window for an earlier sync of this same source configuration.
        queue.publish(
            IndexTask(
                id=f"artifact-retirement-{uuid4().hex}",
                source_id=source_name,
                payload={"operation": "prune_documents"},
            )
        )
    finally:
        queue.close()
