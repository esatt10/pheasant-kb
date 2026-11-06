"""Each file's health and data quality, and the files that need cleaning up.

A source's health score says how much of it reads; this says which files are
the reason, and why. Every file is ``ok``, ``warning`` or ``error``, with one
flag per problem and a reason a person can act on (`sync/health.file_quality`):
a read that failed (``unreadable`` when the index holds nothing for it,
``stale`` when it serves text from an earlier read), a page whose server sent
only a JavaScript shell, a file that extracted no text or almost none.

Computed when asked, from what the region already records -- the last sync's
failures, the connector's checkpoint, the chunk rows -- so it is the same on
every replica and moves when the file does: a URL removed from a web
collection or a document archived is no longer flagged, before any sync.

``quality_issues`` (MCP ``list_quality_issues``, ``GET /quality``) lists the
flagged files for a cleanup pass; ``document_quality`` is the one file's
answer that ``describe_document`` carries.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from pheasant.ingestion.content_types import MEMORY_ARTIFACT_TYPE
from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest, SourceNotFound
from pheasant.services.inventory import reader
from pheasant.sync.health import current_failures, file_quality, script_shells

SEVERITIES = ("warning", "error")
MAX_ISSUES = 500


@dataclass(frozen=True)
class QualityRequest:
    """Which flagged files to list, in the vocabulary both surfaces speak."""

    knowledge_base: str | None = None
    source_name: str | None = None
    #: ``warning`` or ``error``; neither lists both.
    severity: str | None = None
    #: One flag code (``unreadable``, ``stale``, ``script_shell``, ``no_text``,
    #: ``little_text``); none lists every code.
    code: str | None = None
    limit: int = 50
    offset: int = 0
    principal: str | None = None
    principal_groups: list[str] | None = None


def quality_issues(context: ServiceContext, request: QualityRequest) -> dict[str, Any]:
    """Every flagged file, worst first, with the totals a cleanup pass starts from."""

    kb_id = context.knowledge_base(request.knowledge_base)
    if request.severity and request.severity not in SEVERITIES:
        raise InvalidRequest(
            f"severity must be one of {', '.join(SEVERITIES)}; got {request.severity!r}"
        )
    limit = max(1, min(int(request.limit), MAX_ISSUES))
    offset = max(0, int(request.offset))
    sources = _sources(context, request.source_name)

    files: list[dict[str, Any]] = []
    for source in sources:
        files.extend(_source_files(context, source, request.principal, request.principal_groups))
    status_totals = Counter(file["status"] for file in files)
    code_totals = Counter(flag["code"] for file in files for flag in file["flags"])

    flagged = [
        file
        for file in files
        if file["status"] != "ok"
        and (not request.severity or any(f["severity"] == request.severity for f in file["flags"]))
        and (not request.code or any(f["code"] == request.code for f in file["flags"]))
    ]
    flagged.sort(key=lambda f: (f["status"] != "error", str(f["source"]), str(f["path"])))
    page = flagged[offset : offset + limit]
    return {
        "knowledge_base": kb_id,
        "source_name": request.source_name,
        "issues": page,
        "totals": {
            "files": len(files),
            "ok": status_totals.get("ok", 0),
            "warning": status_totals.get("warning", 0),
            "error": status_totals.get("error", 0),
            "by_code": dict(sorted(code_totals.items())),
            "matching": len(flagged),
        },
        "pagination": {
            "limit": limit,
            "offset": offset,
            "returned": len(page),
            "next_offset": offset + limit if offset + limit < len(flagged) else None,
        },
    }


def source_quality(
    context: ServiceContext,
    source: dict[str, Any],
    principal: str | None = None,
    principal_groups: list[str] | None = None,
) -> dict[str, Any]:
    """How many of one source's files are ok, warning and error, by flag code."""

    files = _source_files(context, source, principal, principal_groups)
    statuses = Counter(file["status"] for file in files)
    return {
        "ok": statuses.get("ok", 0),
        "warning": statuses.get("warning", 0),
        "error": statuses.get("error", 0),
        "by_code": dict(
            sorted(Counter(f["code"] for file in files for f in file["flags"]).items())
        ),
    }


def document_quality(context: ServiceContext, row: Any) -> dict[str, Any]:
    """One indexed file's state and flags, with their reasons."""

    name = str(row["source_id"])
    source = context.state.get_source(name) or {"name": name}
    failures = current_failures(context.state, source)
    measured = context.state.rows(
        f"SELECT COUNT(c.id) AS chunks, {_TOKENS} AS tokens FROM chunks c WHERE c.artifact_id=?",
        (str(row["id"]),),
    )
    failure = failures.items.get(str(row["relative_path"])) if failures else None
    return file_quality(
        held=True,
        chunks=int(measured[0]["chunks"] or 0) if measured else 0,
        tokens=int(measured[0]["tokens"] or 0) if measured else 0,
        read_error=_error(failure),
        script_shell=str(row["path"]) in script_shells(context.state, name),
    )


#: Extracted text per file in tokens: the chunk's estimate, or its length / 4
#: for a row written without one.
_TOKENS = "COALESCE(SUM(COALESCE(c.token_estimate, LENGTH(c.text) / 4)), 0)"


def _sources(context: ServiceContext, name: str | None) -> list[dict[str, Any]]:
    from pheasant.registry.source_registry import SourceRegistry

    if name:
        row = context.state.get_source(name)
        if row is None or context.state.source_removed(name):
            raise SourceNotFound(name)
        return [row]
    return [
        row
        for row in SourceRegistry(context.config, context.state).list_sources(limit=10_000)
        if not str(row.get("name") or "").startswith("__")
    ]


def _source_files(
    context: ServiceContext,
    source: dict[str, Any],
    principal: str | None,
    principal_groups: list[str] | None,
) -> list[dict[str, Any]]:
    """Every file of one source -- indexed, or listed and never read -- with its quality."""

    name = str(source["name"])
    acl = ", a.acl" if context.config.security.acl_enforced else ""
    rows = context.state.rows(
        "SELECT a.id, a.source_id, a.path, a.relative_path, a.last_indexed_at" + acl + ", "
        f"COUNT(c.id) AS chunks, {_TOKENS} AS tokens "
        "FROM artifacts a LEFT JOIN chunks c ON c.artifact_id = a.id "
        "WHERE a.source_id = ? AND a.type <> ? "
        "GROUP BY a.id, a.source_id, a.path, a.relative_path, a.last_indexed_at" + acl,
        (name, MEMORY_ARTIFACT_TYPE),
    )
    admit = reader(context, principal, principal_groups)
    if admit is not None:
        rows = admit(rows)
    failures = current_failures(context.state, source)
    failed = dict(failures.items) if failures else {}
    shells = script_shells(context.state, name)

    files = []
    for row in rows:
        path = str(row["relative_path"])
        quality = file_quality(
            held=True,
            chunks=int(row["chunks"] or 0),
            tokens=int(row["tokens"] or 0),
            read_error=_error(failed.pop(path, None)),
            script_shell=str(row["path"]) in shells,
        )
        files.append(_file(row["id"], name, path, row["last_indexed_at"], quality))
    # Listed and never read: nothing in the index carries an ACL to judge them
    # by, so they are shown only where documents are not access-controlled.
    if admit is None:
        for path, (uri, error) in failed.items():
            quality = file_quality(held=False, read_error=error or "no error was recorded")
            files.append({**_file(None, name, path, None, quality), "url": uri})
    return files


def _file(
    artifact_id: Any, source: str, path: str, indexed: Any, quality: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": artifact_id,
        "source": source,
        "path": path,
        "last_indexed_at": indexed,
        **quality,
    }


def _error(failure: tuple[str | None, str | None] | None) -> str | None:
    if failure is None:
        return None
    return failure[1] or "no error was recorded"
