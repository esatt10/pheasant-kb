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

import hashlib
import threading
import weakref
from collections import Counter, OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pheasant.ingestion.content_types import MEMORY_ARTIFACT_TYPE
from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest, SourceNotFound
from pheasant.services.inventory import reader
from pheasant.sync.health import (
    current_failures,
    file_quality,
    percent,
    script_shells,
    status_for,
)

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
        files.extend(source_files(context, source, request.principal, request.principal_groups))
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

    files = source_files(context, source, principal, principal_groups)
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


def source_files(
    context: ServiceContext,
    source: dict[str, Any],
    principal: str | None = None,
    principal_groups: list[str] | None = None,
    *,
    folder: str = "",
) -> list[dict[str, Any]]:
    """Every file of one source (or of one ``folder`` of it, any depth), with its quality.

    Indexed files and files listed but never read alike; the taxonomy view
    rolls these up into each folder's health (:func:`summarize`).
    """

    snapshot = _snapshot(context, source)
    prefix = _prefix(folder)
    rows = folder_rows(context, source, principal, principal_groups, folder=folder)
    files = [snapshot.by_id[str(row["id"])] for row in rows]
    # Listed and never read: the source's own URL or path and the error. The
    # index holds nothing for them, so there is no ACL to judge them by, and
    # the source's listing already shows its URLs to the same callers.
    files += [f for f in snapshot.never_read if not prefix or f["path"].lower().startswith(prefix)]
    return files


def folder_rows(
    context: ServiceContext,
    source: dict[str, Any],
    principal: str | None = None,
    principal_groups: list[str] | None = None,
    *,
    folder: str = "",
) -> list[Any]:
    """The indexed files of ``folder`` (any depth) the caller may read: id and path."""

    prefix = _prefix(folder)
    rows = _snapshot(context, source).rows
    if prefix:
        rows = [row for row in rows if str(row["relative_path"]).lower().startswith(prefix)]
    admit = reader(context, principal, principal_groups)
    return list(admit(rows)) if admit is not None else rows


@dataclass(frozen=True)
class _Snapshot:
    """One source's files and their quality, as of one version of the source."""

    rows: list[dict[str, Any]]
    by_id: dict[str, dict[str, Any]]
    never_read: list[dict[str, Any]]


#: Sources whose snapshot one process keeps. Navigating the taxonomy reads
#: every level of one source, so the few being browsed are what matters.
SNAPSHOTS_KEPT = 16

_SNAPSHOTS_LOCK = threading.Lock()

#: Per state store: (snapshots, levels). Held weakly, so a store's caches go
#: with it and no later store can be handed them -- an object id is reused
#: once its object is collected.
_CACHES: weakref.WeakKeyDictionary[Any, tuple[OrderedDict, OrderedDict]] = (
    weakref.WeakKeyDictionary()
)


def _caches(state: Any) -> tuple[OrderedDict, OrderedDict]:
    with _SNAPSHOTS_LOCK:
        caches = _CACHES.get(state)
        if caches is None:
            caches = _CACHES[state] = (OrderedDict(), OrderedDict())
        return caches


def _snapshot(context: ServiceContext, source: dict[str, Any]) -> _Snapshot:
    """The source's files with their quality, computed once per version of the source.

    Every taxonomy level used to aggregate the chunk table over the whole
    folder and re-read the connector's checkpoint: 1.5-2.1 s a level on a
    25,000-page collection, for an answer that changes only when the source
    does. The version is what can change it -- the artifact count and newest
    index time (a commit), the latest sync event, the archive and the source's
    URL list -- read in four indexed statements, so a stale health cannot be
    served after any of them moves. ACLs are applied per request, after.
    """

    key = _snapshot_key(context, source)
    snapshots, _levels = _caches(context.state)
    with _SNAPSHOTS_LOCK:
        if key in snapshots:
            snapshots.move_to_end(key)
            return snapshots[key]
    snapshot = _build_snapshot(context, source)
    with _SNAPSHOTS_LOCK:
        for stale in [k for k in snapshots if k[0] == key[0]]:
            del snapshots[stale]
        snapshots[key] = snapshot
        while len(snapshots) > SNAPSHOTS_KEPT:
            snapshots.popitem(last=False)
    return snapshot


def _snapshot_key(context: ServiceContext, source: dict[str, Any]) -> tuple:
    """Which version of the source a snapshot (or a level built on one) is of."""

    from pheasant.persistence import archive

    name = str(source["name"])
    state = context.state
    held = state.rows(
        "SELECT COUNT(*) AS n, MAX(last_indexed_at) AS latest FROM artifacts WHERE source_id=?",
        (name,),
    )[0]
    event = state.list_sync_events(name, limit=1)
    archived = archive.for_source(state, name)
    raw = str(source.get("config_json") or "")
    version = (
        int(held["n"] or 0),
        str(held["latest"] or ""),
        str(event[0]["id"]) if event else "",
        len(archived),
        hashlib.sha256(raw.encode()).hexdigest() if raw else "",
        bool(context.config.security.acl_enforced),
    )
    return (name, version)


#: Taxonomy levels one process keeps: a level is a folder seen by one caller.
LEVELS_KEPT = 256


def cached_level(
    context: ServiceContext,
    source: dict[str, Any],
    principal: str | None,
    principal_groups: list[str] | None,
    folder: str,
    build: Callable[[], Any],
) -> Any:
    """``build()`` for one folder of one version of a source, as one caller sees it.

    Grouping a level and rolling up its folders' health walks every file
    beneath it -- 20,000 on a large collection's root, ~150 ms in Python --
    and the answer moves only with the source's version, so a level comes back
    from memory until the source changes. The caller is part of the key
    because an ACL can show two callers two different folders.
    """

    key = (
        _snapshot_key(context, source),
        folder.strip("/").lower(),
        principal,
        tuple(sorted(principal_groups or [])),
    )
    _snapshots, levels = _caches(context.state)
    with _SNAPSHOTS_LOCK:
        if key in levels:
            levels.move_to_end(key)
            return levels[key]
    value = build()
    with _SNAPSHOTS_LOCK:
        levels[key] = value
        while len(levels) > LEVELS_KEPT:
            levels.popitem(last=False)
    return value


def _build_snapshot(context: ServiceContext, source: dict[str, Any]) -> _Snapshot:
    name = str(source["name"])
    acl = ", a.acl" if context.config.security.acl_enforced else ""
    rows = [
        dict(row)
        for row in context.state.rows(
            "SELECT a.id, a.source_id, a.path, a.relative_path, a.last_indexed_at" + acl + ", "
            f"COUNT(c.id) AS chunks, {_TOKENS} AS tokens "
            "FROM artifacts a LEFT JOIN chunks c ON c.artifact_id = a.id "
            "WHERE a.source_id = ? AND a.type <> ? "
            "GROUP BY a.id, a.source_id, a.path, a.relative_path, a.last_indexed_at" + acl,
            (name, MEMORY_ARTIFACT_TYPE),
        )
    ]
    rows.sort(key=lambda row: str(row["relative_path"]))
    failures = current_failures(context.state, source)
    failed = dict(failures.items) if failures else {}
    shells = script_shells(context.state, name)
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = str(row["relative_path"])
        quality = file_quality(
            held=True,
            chunks=int(row["chunks"] or 0),
            tokens=int(row["tokens"] or 0),
            read_error=_error(failed.get(path)),
            script_shell=str(row["path"]) in shells,
        )
        by_id[str(row["id"])] = _file(row["id"], name, path, row["last_indexed_at"], quality)
    # A failing file the index holds is judged by its own ACL; only the ones
    # it holds nothing for are listed as never read.
    indexed = _paths(rows)
    never_read = [
        {
            **_file(
                None,
                name,
                path,
                None,
                file_quality(held=False, read_error=error or "no error was recorded"),
            ),
            "url": uri,
        }
        for path, (uri, error) in failed.items()
        if path not in indexed
    ]
    return _Snapshot(rows=rows, by_id=by_id, never_read=never_read)


def _prefix(folder: str) -> str:
    clean = folder.strip("/").lower()
    return f"{clean}/" if clean else ""


@dataclass(frozen=True)
class Level:
    """One folder of the taxonomy: what is beneath each sub-folder, and what is here."""

    #: Sub-folder -> documents beneath it that the index holds.
    folders: dict[str, int]
    #: Sub-folder -> every file beneath it, indexed or never read, with its quality.
    beneath: dict[str, list[dict[str, Any]]]
    #: The files directly here, in name order: (name, "indexed", id) or
    #: (name, "never_read", file).
    here: list[tuple[str, str, Any]]
    by_id: dict[str, dict[str, Any]]

    def directories(self, prefix: str) -> list[dict[str, Any]]:
        """Each sub-folder with its document count and its health."""

        return [
            {
                "name": name,
                "path": f"{prefix}/{name}" if prefix else name,
                "documents": count,
                "health": summarize(self.beneath.get(name, [])),
            }
            for name, count in sorted(self.folders.items(), key=lambda item: item[0].lower())
        ]

    def state(self, artifact_id: str) -> dict[str, Any]:
        file = self.by_id.get(artifact_id) or UNSCORED
        return {"status": file["status"], "flags": file["flags"]}


#: A file the quality pass did not score (it arrived between the two reads).
UNSCORED: dict[str, Any] = {"status": "ok", "flags": []}


def place(rows: list[Any], files: list[dict[str, Any]], depth: int) -> Level:
    """Group a folder's indexed ``rows`` and its never-read ``files`` one level down.

    A never-read file belongs in the folder its path names, like any other:
    it counts in that folder's health and is listed among its files.
    """

    by_id = {str(file["id"]): file for file in files if file["id"] is not None}
    folders: dict[str, int] = {}
    beneath: dict[str, list[dict[str, Any]]] = {}
    here: list[tuple[str, str, Any]] = []
    entries = [(row["relative_path"], "indexed", str(row["id"])) for row in rows] + [
        (file["path"], "never_read", file) for file in files if file["id"] is None
    ]
    for path, kind, key in entries:
        rest = [part for part in str(path or "").split("/") if part][depth:]
        if len(rest) > 1:
            folders[rest[0]] = folders.get(rest[0], 0) + (kind == "indexed")
            beneath.setdefault(rest[0], []).append(
                (by_id.get(key) or UNSCORED) if kind == "indexed" else key
            )
        elif rest:
            here.append((rest[0].lower(), kind, key))
    # Name order, so a page is stable from one call to the next.
    here.sort(key=lambda entry: (entry[0], entry[1]))
    return Level(folders=folders, beneath=beneath, here=here, by_id=by_id)


def never_read_row(file: dict[str, Any]) -> dict[str, Any]:
    """A file the source lists and the index holds nothing for: no id, no viewer."""

    from pathlib import PurePosixPath

    from pheasant.services.inventory import extension_of

    relative = str(file["path"])
    return {
        "id": None,
        "name": PurePosixPath(relative).name or relative,
        "path": relative,
        "type": None,
        "extension": extension_of(relative),
        "size_bytes": None,
        "last_indexed_at": None,
        "viewer": None,
        "language": None,
        "url": file.get("url"),
        "quality": {"status": file["status"], "flags": file["flags"]},
    }


def source_score(context: ServiceContext, row: Any, name: str) -> dict[str, Any] | None:
    """A source's percent and status, for the taxonomy's top level."""

    from pheasant.sync.health import source_health

    health = source_health(context.state, dict(row) if row is not None else {"name": name})
    return {"percent": health["percent"], "status": health["status"]} if health else None


def summarize(files: list[dict[str, Any]]) -> dict[str, Any]:
    """The health of a set of files -- a folder's -- in a source's own terms.

    ``percent`` is the share that reads (a file whose last read failed does
    not, whether or not older text is held), and ``status`` follows the same
    majority rule as a source's; ``ok`` / ``warning`` / ``error`` count the
    files by state. ``percent`` is ``None`` for a folder with no files.
    """

    statuses = Counter(file["status"] for file in files)
    failing = sum(1 for file in files if any(f["code"] in _READ_FAILURES for f in file["flags"]))
    score = percent(len(files) - failing, failing)
    return {
        "percent": score,
        "status": status_for(score),
        "ok": statuses.get("ok", 0),
        "warning": statuses.get("warning", 0),
        "error": statuses.get("error", 0),
    }


#: The flags that mean a file did not read, which is what a folder's percent counts.
_READ_FAILURES = frozenset({"unreadable", "stale"})


def _paths(rows: list[Any]) -> set[str]:
    return {str(row["relative_path"]) for row in rows}


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
