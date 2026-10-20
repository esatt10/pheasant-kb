"""Removing a document from the index, and keeping it removed.

The operation behind ``POST /documents/archive``, ``POST /documents/reinstate``
and ``GET /documents/archived``, the MCP ``archive_documents`` /
``reinstate_documents`` / ``list_archived_documents`` tools, the taxonomy
view's **Remove** button and the chat's ``@remove``. One implementation; the
adapters only say how their process reaches the index (:class:`Indexing`).

**Removed means removed.** A document's artifact row, chunks, full-text rows,
vectors and graph nodes all go, exactly as when its file is deleted
(`sync/archived.py` makes the same calls `sync/vanished.py` does). What keeps
it gone is a row in ``archived_documents``: every sync skips an archived path
before reading it, so neither an incremental nor a full sync brings it back.

**What comes back depends on where it came from.** A document in a folder,
repository or connector source is *archived*: its file is still there, and
reinstating it deletes the row and runs an incremental sync of its source,
which reads the file as if for the first time — content, chunks, embeddings
and graph nodes rebuilt from it. A document from an **upload** source lives
only in the region's own landing zone, so removing it deletes its bytes and
there is nothing to reinstate it from; asking is refused
(``DOCUMENT_NOT_REINSTATABLE``).

**A name must name one document.** Search exclusions resolve ``deploy.md`` to
every document with that name; a removal refuses it (``AMBIGUOUS_DOCUMENT``)
and lists the candidates, because removing three documents for a short name is
not what anybody meant. Resolution runs under the inventory ACL rule, so a
caller cannot remove, or learn of, a document it may not read.

**Where the work happens.** A process that writes its own index (``--role
all``, an indexer) removes the document before the call returns. A fleet's api
replica cannot: it writes the archive row, which is a state-store write it is
allowed, and publishes an incremental sync, whose prune step does the removal
on the indexer. Reinstating always syncs, in the background where there is a
job registry, and through the queue where there is an indexer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from pheasant.ingestion.landing import owned_upload_directory
from pheasant.persistence import archive
from pheasant.services import ServiceContext
from pheasant.services.errors import (
    AmbiguousDocument,
    DocumentNotFound,
    InvalidRequest,
    NotReinstatable,
)

#: Documents one call may remove or reinstate. A longer list is a source to
#: narrow with ``exclude`` globs, not a list of documents.
MAX_DOCUMENTS = 50


@dataclass(frozen=True)
class Indexing:
    """How the calling process changes a source's index.

    ``local``: this process writes its own index, so a removal is applied here
    and now. ``start_sync(source)`` starts an incremental sync of one source
    and returns what happened (``status`` plus a job id or queued task ids);
    on an api replica it publishes to the indexer.
    """

    local: bool
    start_sync: Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class ArchiveRequest:
    """Documents to remove (or reinstate), by id, ``source/path``, path or a tail."""

    documents: list[str]
    knowledge_base: str | None = None
    source_name: str | None = None
    principal: str | None = None
    principal_groups: list[str] | None = None
    #: Recorded on the archive row: who asked.
    actor: str | None = None


@dataclass(frozen=True)
class ArchivedRequest:
    """One page of archived documents, of one source or folder."""

    knowledge_base: str | None = None
    source_name: str | None = None
    path_prefix: str | None = None
    limit: int = 200
    offset: int = 0
    principal: str | None = None
    principal_groups: list[str] | None = None


def archive_documents(context: ServiceContext, request: ArchiveRequest) -> dict[str, Any]:
    """Remove each named document from the index and hold it out of every sync."""

    kb_id = context.knowledge_base(request.knowledge_base)
    names = _requested(request.documents)
    from pheasant.services import scope

    chosen: dict[str, Any] = {}
    for name in names:
        rows = scope.named(
            context,
            name,
            source_name=request.source_name,
            principal=request.principal,
            principal_groups=request.principal_groups,
        )
        if not rows:
            raise DocumentNotFound(f"{name} (no document by that name to remove)")
        if len(rows) > 1:
            raise AmbiguousDocument(name, sorted(_qualified(r) for r in rows))
        chosen[str(rows[0]["id"])] = rows[0]
    now = _now()
    documents: list[dict[str, Any]] = []
    by_source: dict[str, list[str]] = {}
    for artifact_id, row in chosen.items():
        source_name = str(row["source_id"])
        kind = "removed" if _is_upload(context, source_name) else "archived"
        archive.add(
            context.state,
            source_id=source_name,
            relative_path=str(row["relative_path"]),
            artifact_id=artifact_id,
            kind=kind,
            now=now,
            archived_by=request.actor or request.principal,
            acl=_acl_of(context, artifact_id),
        )
        documents.append(_document(source_name, str(row["relative_path"]), artifact_id, kind))
        by_source.setdefault(source_name, []).append(str(row["relative_path"]))
    sources = {name: _apply_removal(context, name) for name in sorted(by_source)}
    return {
        "knowledge_base": kb_id,
        "status": _overall(sources.values(), "removed"),
        "documents": documents,
        "sources": sources,
    }


def reinstate_documents(context: ServiceContext, request: ArchiveRequest) -> dict[str, Any]:
    """Let archived documents back in, and sync their sources to read them again."""

    kb_id = context.knowledge_base(request.knowledge_base)
    names = _requested(request.documents)
    held = _readable(context, _held(context, request.source_name), request)
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for name in names:
        rows = [row for row in held if _names_row(row, name)]
        if not rows:
            raise DocumentNotFound(f"{name} (no archived document by that name)")
        if len(rows) > 1:
            raise AmbiguousDocument(name, sorted(_qualified(r) for r in rows))
        row = rows[0]
        if row["kind"] == "removed":
            raise NotReinstatable(_qualified(row))
        chosen[(str(row["source_id"]), str(row["relative_path"]))] = row
    by_source: dict[str, list[str]] = {}
    for source_name, path in chosen:
        by_source.setdefault(source_name, []).append(path)
    for source_name, paths in by_source.items():
        archive.remove(context.state, source_name, paths)
    sources = {name: _indexing(context).start_sync(name) for name in sorted(by_source)}
    return {
        "knowledge_base": kb_id,
        "status": _overall(sources.values(), "reinstated"),
        "documents": [
            _document(source, path, row.get("artifact_id"), "reinstated")
            for (source, path), row in chosen.items()
        ],
        "sources": sources,
    }


def archived_documents(context: ServiceContext, request: ArchivedRequest) -> dict[str, Any]:
    """The documents held out of the index, by source then path, paged."""

    kb_id = context.knowledge_base(request.knowledge_base)
    rows = _readable(context, _held(context, request.source_name), request)
    prefix = str(request.path_prefix or "").strip("/")
    if prefix:
        rows = [
            r
            for r in rows
            if r["relative_path"] == prefix or str(r["relative_path"]).startswith(f"{prefix}/")
        ]
    limit = max(1, min(int(request.limit or 200), 500))
    offset = max(0, int(request.offset or 0))
    page = rows[offset : offset + limit]
    return {
        "knowledge_base": kb_id,
        "source_name": request.source_name,
        "path_prefix": prefix or None,
        "documents": [
            {
                **_document(
                    str(r["source_id"]), str(r["relative_path"]), r.get("artifact_id"), r["kind"]
                ),
                "archived_at": r.get("archived_at"),
                "archived_by": r.get("archived_by"),
            }
            for r in page
        ],
        "total": len(rows),
        "page": {
            "offset": offset,
            "limit": limit,
            "returned": len(page),
            "has_more": offset + len(page) < len(rows),
        },
    }


def _apply_removal(context: ServiceContext, source_name: str) -> dict[str, Any]:
    indexing = _indexing(context)
    if not indexing.local:
        return {**indexing.start_sync(source_name), "status": "removal_queued"}
    from pheasant.sync import archived as archived_step

    try:
        removed = archived_step.apply(context.engine, source_name)
    except RuntimeError as exc:
        # A sync of this source holds its lock; it removes the document as it
        # finishes, because its prune step reads the archive afresh.
        return {"status": "removal_pending", "detail": str(exc)}
    return {"status": "removed", "removed_artifacts": removed}


def _indexing(context: ServiceContext) -> Indexing:
    if context.indexing is not None:
        return context.indexing

    def sync_here(source_name: str) -> dict[str, Any]:
        result = context.engine.sync_source(source_name, "incremental")
        return {
            "status": "synced",
            "indexed": result.indexed_artifacts,
            "result_status": result.status,
        }

    return Indexing(local=True, start_sync=sync_here)


def _requested(raw: list[str] | str | None) -> list[str]:
    from pheasant.services import scope

    names = scope.names(raw)
    if not names:
        raise InvalidRequest("name at least one document, by id, source/path or path")
    if len(names) > MAX_DOCUMENTS:
        raise InvalidRequest(f"{len(names)} documents named; at most {MAX_DOCUMENTS} per call")
    return names


def _held(context: ServiceContext, source_name: str | None) -> list[dict[str, Any]]:
    if source_name:
        return archive.for_source(context.state, source_name)
    return archive.everything(context.state)


def _readable(context: ServiceContext, rows: list[dict[str, Any]], request: Any) -> list[dict]:
    from pheasant.services.inventory import reader

    admit = reader(context, request.principal, request.principal_groups)
    return list(admit(rows)) if admit is not None else rows


def _names_row(row: dict[str, Any], name: str) -> bool:
    if name.startswith("file:"):
        return row.get("artifact_id") == name
    wanted = name.lower().strip("/")
    relative = str(row["relative_path"]).lower()
    qualified = f"{str(row['source_id']).lower()}/{relative}"
    return wanted in {relative, qualified} or relative.endswith(f"/{wanted}")


def _is_upload(context: ServiceContext, source_name: str) -> bool:
    """Whether the source is an upload landing zone this region owns."""

    row = context.state.get_source(source_name)
    if not row:
        return False
    state_path = getattr(getattr(context.config, "pheasant", None), "state_path", None)
    if state_path is None:
        return False
    owned = owned_upload_directory(state_path, str(row["name"]), str(row["type"]), str(row["path"]))
    return owned is not None


def _acl_of(context: ServiceContext, artifact_id: str) -> str | None:
    rows = context.state.rows("SELECT acl FROM artifacts WHERE id=? LIMIT 1", (artifact_id,))
    value = rows[0]["acl"] if rows else None
    return value if isinstance(value, str) or value is None else json.dumps(value)


def _document(source: str, path: str, artifact_id: Any, disposition: str) -> dict[str, Any]:
    return {
        "source_name": source,
        "path": path,
        "name": PurePosixPath(path).name,
        "id": artifact_id,
        "disposition": disposition,
        "reinstatable": disposition == "archived",
    }


def _qualified(row: Any) -> str:
    return f"{row['source_id']}/{row['relative_path']}"


def _overall(results: Any, done: str) -> str:
    """One word for the call: done here, or handed to whoever indexes."""

    statuses = {str(result.get("status")) for result in results}
    if statuses <= {"removed", "synced", "syncing"}:
        return done
    return "queued" if statuses & {"removal_queued", "queued"} else "pending"


def _now() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
