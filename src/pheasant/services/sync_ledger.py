"""A sync pipeline's document ledger: when each document was read, changed and is due.

The drill-down of the Syncs page (`services/sync_pipelines.py` is the page):
per document of one source, the last read, the last change, the next check
and its interval, what the server said, the validator, and the document's
quality with its reasons -- filterable by state and paged. Same operation
for ``GET /sync/pipelines/{name}/documents`` and MCP
``list_pipeline_documents``.

A ledger is built once per version of its source and caller and kept
(`quality.cached_view`), so a page of a 100,000-document source is a filter
over numbers rather than a rebuild of every row.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest
from pheasant.services.sync_pipelines import (
    RECENT_RUNS,
    WATCHED_TYPES,
    WEB_TYPES,
    _checkpoint_version,
    _epoch,
    _iso,
    _next_check,
    _registered,
    _url_path,
    _web_schedule,
)

DOCUMENT_STATES = (
    "all",
    "due_soon",
    "changed_week",
    "attention",
    "unreadable",
    "stale",
    "little_text",
)
MAX_DOCUMENTS = 500

# -- operations -------------------------------------------------------


@dataclass(frozen=True)
class DocumentsRequest:
    source_name: str
    knowledge_base: str | None = None
    state: str = "all"
    path_contains: str | None = None
    limit: int = 100
    offset: int = 0
    principal: str | None = None
    principal_groups: list[str] | None = None


def documents(context: ServiceContext, request: DocumentsRequest) -> dict[str, Any]:
    """Every document of one pipeline with its schedule and quality, filtered and paged."""

    kb_id = context.knowledge_base(request.knowledge_base)
    if request.state not in DOCUMENT_STATES:
        raise InvalidRequest(
            f"state must be one of {', '.join(DOCUMENT_STATES)}; got {request.state!r}"
        )
    source = _registered(context, request.source_name)
    ledger = _ledger(context, source, request.principal, request.principal_groups)
    now = time.time()
    counts = ledger.counts(now)
    needle = (request.path_contains or "").strip().lower()
    chosen = [
        ledger.rows[i]
        for i in ledger.members(request.state, now)
        if not needle or needle in ledger.lower_paths[i]
    ]
    limit = max(1, min(int(request.limit), MAX_DOCUMENTS))
    offset = max(0, int(request.offset))
    page = chosen[offset : offset + limit]
    return {
        "knowledge_base": kb_id,
        "source_name": request.source_name,
        "state": request.state,
        "documents": page,
        "totals": {**counts, "matching": len(chosen)},
        "pagination": {
            "limit": limit,
            "offset": offset,
            "returned": len(page),
            "next_offset": offset + limit if offset + limit < len(chosen) else None,
        },
    }


def document(
    context: ServiceContext,
    name: str,
    path: str,
    knowledge_base: str | None = None,
    principal: str | None = None,
    principal_groups: list[str] | None = None,
) -> dict[str, Any]:
    """One document's schedule, quality and the runs that could not read it."""

    kb_id = context.knowledge_base(knowledge_base)
    source = _registered(context, name)
    row = _ledger(context, source, principal, principal_groups).by_key.get(path.strip())
    if row is None:
        from pheasant.services.errors import DocumentNotFound

        raise DocumentNotFound(path)
    failed_runs = []
    for event in context.state.list_sync_events(name, limit=RECENT_RUNS):
        for item in (event.get("details") or {}).get("unreadable_all") or []:
            if item and item[0] == row["path"]:
                failed_runs.append(
                    {"at": event.get("finished_at"), "error": item[2] if len(item) > 2 else None}
                )
    return {
        "knowledge_base": kb_id,
        "source_name": name,
        "document": row,
        "failed_runs": failed_runs,
    }


def recheck(
    context: ServiceContext, name: str, paths: list[str], knowledge_base: str | None = None
) -> dict[str, Any]:
    """Make the named pages due now (a web pipeline) and start a sync."""

    context.knowledge_base(knowledge_base)
    source = _registered(context, name)
    if not paths:
        raise InvalidRequest("name at least one document to recheck")
    marked = 0
    if str(source.get("type")) in WEB_TYPES:
        checkpoint = context.state.get_source_checkpoint(name)
        if checkpoint:
            cursor = dict(checkpoint.get("cursor") or {})
            validators = dict(cursor.get("validators") or {})
            wanted = set(paths)
            for url, entry in validators.items():
                if url in wanted or _url_path(url) in wanted:
                    validators[url] = {**entry, "checked_at": 0}
                    marked += 1
            cursor["validators"] = validators
            context.state.set_source_checkpoint(
                name,
                checkpoint.get("connector_type") or str(source.get("type")),
                cursor,
                checkpoint.get("high_watermark") or {},
                datetime.now(UTC).isoformat(),
                checkpoint.get("status") or "healthy",
            )
    started = context.indexing.start_sync(name) if context.indexing is not None else None
    return {"source_name": name, "marked_due": marked, "sync": started}


# -- the ledger ----


#: Ledgers one process keeps: one per (source version, caller). A ledger of a
#: 100,000-document source is tens of megabytes, so few, and a new version of
#: a source replaces its old one at once (`quality.cached_view`).
LEDGERS_KEPT = 8

#: The states a document is in whatever the time is; ``due_soon`` and
#: ``changed_week`` move with the clock and are counted per request.
_FIXED_STATES = ("attention", "unreadable", "stale", "little_text")


@dataclass(frozen=True)
class _Ledger:
    """One pipeline's documents as one caller sees them, as of one version of the source.

    Built once per version: the rows (in the order a page shows them), each
    row's fixed states, its times as numbers, its path lowered for the filter,
    an index by path, URL and id, so a request only counts and filters by the
    clock and slices -- a pass over floats rather than rebuilding every row
    from the index, the checkpoint and the quality snapshot.
    """

    rows: list[dict[str, Any]]
    lower_paths: list[str]
    next_at: list[float | None]
    changed_at: list[float | None]
    fixed: dict[str, list[int]]
    by_key: dict[str, dict[str, Any]]

    def members(self, state: str, now: float) -> Any:
        """Row indexes in ``state`` now, in page order."""

        if state == "all":
            return range(len(self.rows))
        if state in self.fixed:
            return self.fixed[state]
        if state == "due_soon":
            return [i for i, at in enumerate(self.next_at) if at is not None and at <= now + 3600]
        if state == "changed_week":
            since = now - 7 * 86400
            return [i for i, at in enumerate(self.changed_at) if at is not None and at >= since]
        return []

    def counts(self, now: float) -> dict[str, int]:
        return {state: len(self.members(state, now)) for state in DOCUMENT_STATES}


def _ledger(
    context: ServiceContext,
    source: dict[str, Any],
    principal: str | None,
    principal_groups: list[str] | None,
) -> _Ledger:
    """The pipeline's ledger, from memory until the source or its checkpoint changes.

    The source's version is the taxonomy snapshot's (`quality._snapshot_key`:
    a commit, a sync, an archive, the URL list); the checkpoint's write time
    is added because a recheck moves a page's next check by writing only the
    checkpoint.
    """

    from pheasant.services.quality import cached_view

    return cached_view(
        context,
        source,
        "ledgers",
        (principal, tuple(sorted(principal_groups or []))),
        lambda: _build_ledger(context, source, principal, principal_groups),
        kept=LEDGERS_KEPT,
        version=(_checkpoint_version(context, str(source["name"])),),
    )


def _build_ledger(
    context: ServiceContext,
    source: dict[str, Any],
    principal: str | None,
    principal_groups: list[str] | None,
) -> _Ledger:
    rows = _ledger_rows(context, source, principal, principal_groups)
    rows.sort(key=lambda r: (_rank(r["status"]), r["next_check_at"] or "~", r["path"]))
    fixed: dict[str, list[int]] = {state: [] for state in _FIXED_STATES}
    by_key: dict[str, dict[str, Any]] = {}
    for i, row in enumerate(rows):
        for state in _FIXED_STATES:
            if _in_state(row, state, 0.0):
                fixed[state].append(i)
        for key in (row["path"], row["url"], row["id"]):
            if key is not None:
                by_key.setdefault(str(key), row)
    return _Ledger(
        rows=rows,
        lower_paths=[str(row["path"]).lower() for row in rows],
        next_at=[_epoch(row["next_check_at"]) for row in rows],
        changed_at=[_epoch(row["last_changed_at"]) for row in rows],
        fixed=fixed,
        by_key=by_key,
    )


def _ledger_rows(
    context: ServiceContext,
    source: dict[str, Any],
    principal: str | None,
    principal_groups: list[str] | None,
) -> list[dict[str, Any]]:
    from pheasant.services.quality import source_files

    name = str(source["name"])
    kind = str(source.get("type"))
    files = source_files(context, source, principal, principal_groups)
    validators, _ = _web_schedule(context, name) if kind in WEB_TYPES else ({}, {})
    indexed = _indexed_rows(context, name)
    rows = []
    for file in files:
        row = indexed.get(str(file["id"])) if file["id"] is not None else None
        url = (row or {}).get("path") or file.get("url")
        entry = validators.get(str(url)) if url else None
        last_read = entry.get("checked_at") if entry else None
        next_check = _next_check(entry) if entry else None
        changed = entry.get("changed_at") if entry else None
        indexed_at = (row or {}).get("last_indexed_at")
        rows.append(
            {
                "id": file["id"],
                "path": file["path"],
                "url": url if kind in WEB_TYPES else None,
                "status": file["status"],
                "flags": file["flags"],
                "last_read_at": _iso(last_read) if last_read else indexed_at,
                "last_changed_at": _iso(changed) if changed else indexed_at,
                "next_check_at": _iso(next_check) if next_check else None,
                "next_check": _iso(next_check) if next_check else _trigger_label(kind),
                "interval_seconds": entry.get("interval") if entry else None,
                "server_said": _server_said(entry, file),
                "validator": "etag"
                if entry and entry.get("etag")
                else ("last-modified" if entry and entry.get("last_modified") else None),
            }
        )
    return rows


def _indexed_rows(context: ServiceContext, name: str) -> dict[str, dict[str, Any]]:
    return {
        str(row["id"]): dict(row)
        for row in context.state.rows(
            "SELECT id, path, last_indexed_at FROM artifacts WHERE source_id=?", (name,)
        )
    }


def _server_said(entry: dict[str, Any] | None, file: dict[str, Any]) -> str | None:
    for flag in file.get("flags") or []:
        if flag.get("code") in ("unreadable", "stale"):
            match = re.search(r"HTTP Error (\d{3})", str(flag.get("reason") or ""))
            return match.group(1) if match else "error"
    if entry and entry.get("status"):
        return str(entry["status"])
    return None


def _trigger_label(kind: str) -> str:
    if kind in WATCHED_TYPES:
        return "on change"
    return "next beat"


def _in_state(row: dict[str, Any], state: str, now: float) -> bool:
    if state == "all":
        return True
    codes = {flag["code"] for flag in row["flags"]}
    if state == "attention":
        return row["status"] != "ok"
    if state == "unreadable":
        return "unreadable" in codes
    if state == "stale":
        return "stale" in codes
    if state == "little_text":
        return bool(codes & {"no_text", "little_text"})
    if state == "due_soon":
        at = _epoch(row["next_check_at"])
        return at is not None and at <= now + 3600
    if state == "changed_week":
        at = _epoch(row["last_changed_at"])
        return at is not None and at >= now - 7 * 86400
    return False


def _rank(status: str) -> int:
    return {"error": 0, "warning": 1}.get(status, 2)
