"""Every source's sync pipeline: what it does, when it runs, how it went, per document.

A pipeline is made for a source when it is added -- adding a source stays one
field -- and this is where it is seen and changed in depth: the Syncs page,
``/sync/pipelines`` over HTTP, and the ``*_sync_pipeline`` MCP tools all call
these functions. Nothing here is a second store of truth: the settings are
``sources[].sync`` (`sync/pipeline.py`), runs are ``sync_events`` and the job
snapshots, health and quality are `sync/health.py` and `services/quality.py`,
and a web page's schedule is its entry in the connector's checkpoint.

* :func:`pipelines` -- one row per source: schedule, last and next run, what
  is running or queued, health, quality, pacing.
* :func:`pipeline` -- one in depth: the last run by stage, recent runs,
  failures grouped by cause, the pages due next, every setting.
* :func:`configure` -- change settings; :func:`delete` removes the schedule
  (manual runs only) or the pipeline (nothing syncs the source; its documents
  stay); :func:`restore` brings a deleted one back.
* :func:`documents` / :func:`document` -- the ledger: per document, when it
  was last read, last changed and is next checked, its interval, what the
  server said and its quality, filtered and paged.
* :func:`recheck` -- make pages due now and start a sync.
"""

from __future__ import annotations

import json
import re
import threading
import time
import weakref
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest, SourceNotFound
from pheasant.sync.pipeline import SCHEDULES, state_of

#: Source types whose items are web pages with their own schedules.
WEB_TYPES = frozenset({"web_collection", "api"})
WATCHED_TYPES = frozenset({"document_folder", "markdown_folder", "repository"})
#: A web page with no history and no configured interval: rechecked hourly.
DEFAULT_MIN_REFRESH = 3600

#: Settings `configure` accepts, by where they live on the source.
SYNC_FIELDS = (
    "schedule",
    "paused",
    "pipeline",
    "interval_seconds",
    "max_items_per_run",
    "min_request_interval_seconds",
    "respect_robots",
    "on_startup",
    "on_file_change",
    "on_git_commit",
)
CONNECTOR_FIELDS = ("max_refresh_seconds",)

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
RECENT_RUNS = 10


# -- pipelines ---------------------------------------------------------------


def pipelines(context: ServiceContext, knowledge_base: str | None = None) -> dict[str, Any]:
    """One row per source, the same shape `pipeline` opens."""

    kb_id = context.knowledge_base(knowledge_base)
    from pheasant.registry.source_registry import SourceRegistry
    from pheasant.services.index_queue import queue_status

    try:
        queue = queue_status(context, kb_id, limit=500)
    except Exception:  # noqa: BLE001 - an unreachable queue is a missing column, not an error
        queue = {"tasks": [], "listing": "unavailable", "depth": {}}
    rows = [
        _row(context, source, queue)
        for source in SourceRegistry(context.config, context.state).list_sources(limit=10_000)
        if not str(source.get("name") or "").startswith("__")
    ]
    counts = Counter(row["state"] for row in rows)
    return {
        "knowledge_base": kb_id,
        "pipelines": rows,
        "totals": {
            "pipelines": len(rows),
            "running": sum(1 for row in rows if row["running"]),
            "queued": sum(len(row["queued"]) for row in rows),
            "due_next_hour": sum(row["due_next_hour"] or 0 for row in rows),
            "needs_attention": sum(
                (row["quality"] or {}).get("warning", 0) + (row["quality"] or {}).get("error", 0)
                for row in rows
            ),
            **{
                state: counts.get(state, 0)
                for state in ("scheduled", "paused", "manual", "deleted")
            },
        },
        "beat_seconds": _beat(context),
    }


def pipeline(
    context: ServiceContext, name: str, knowledge_base: str | None = None
) -> dict[str, Any]:
    """One pipeline in depth."""

    kb_id = context.knowledge_base(knowledge_base)
    from pheasant.services.index_queue import queue_status

    source = _registered(context, name)
    try:
        queue = queue_status(context, kb_id, limit=500)
    except Exception:  # noqa: BLE001
        queue = {"tasks": [], "listing": "unavailable", "depth": {}}
    row = _row(context, source, queue)
    events = context.state.list_sync_events(name, limit=RECENT_RUNS)
    job = _job(context, name)
    validators, _ = _web_schedule(context, name)
    upcoming = sorted(
        (
            (_next_check(entry), url)
            for url, entry in validators.items()
            if _next_check(entry) is not None
        ),
    )[:10]
    return {
        "knowledge_base": kb_id,
        **row,
        "stages": _stages(job if row["running"] else None, events[0] if events else None),
        "job": _job_summary(job),
        "runs": [_run(event) for event in events],
        "failures": _failure_groups(events[0] if events else None),
        "next_due": [{"url": url, "at": _iso(at)} for at, url in upcoming],
    }


# -- changing a pipeline -------------------------------------------------------


@dataclass(frozen=True)
class PipelineChange:
    """Settings to change on one pipeline; ``None`` leaves a setting as it is."""

    source_name: str
    knowledge_base: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    sync: bool = False


def configure(context: ServiceContext, change: PipelineChange) -> dict[str, Any]:
    """Apply ``change.settings`` and return the pipeline as it now is."""

    context.knowledge_base(change.knowledge_base)
    source = _registered(context, change.source_name)
    payload = json.loads(source.get("config_json") or "{}") or {}
    unknown = sorted(set(change.settings) - set(SYNC_FIELDS) - set(CONNECTOR_FIELDS))
    if unknown:
        raise InvalidRequest(
            f"unknown pipeline setting(s): {', '.join(unknown)}; "
            f"settable: {', '.join(SYNC_FIELDS + CONNECTOR_FIELDS)}"
        )
    sync = dict(payload.get("sync") or {})
    connector = dict(payload.get("connector") or {})
    for key, value in change.settings.items():
        if value is None and key not in (
            "interval_seconds",
            "max_items_per_run",
            "min_request_interval_seconds",
        ):
            continue
        _check(key, value)
        (connector if key in CONNECTOR_FIELDS else sync)[key] = value
    payload["sync"] = sync
    payload["connector"] = connector
    from pheasant.registry.source_registry import SourceRegistry

    SourceRegistry(context.config, context.state).install(payload)
    if change.sync and context.indexing is not None and sync.get("pipeline", True):
        context.indexing.start_sync(change.source_name)
    return pipeline(context, change.source_name, change.knowledge_base)


def delete(
    context: ServiceContext, name: str, what: str, knowledge_base: str | None = None
) -> dict[str, Any]:
    """``what="schedule"``: manual runs only. ``what="pipeline"``: nothing syncs it."""

    if what == "schedule":
        settings: dict[str, Any] = {"schedule": "manual", "paused": False}
    elif what == "pipeline":
        settings = {"pipeline": False}
    else:
        raise InvalidRequest(f"what must be 'schedule' or 'pipeline'; got {what!r}")
    return configure(context, PipelineChange(name, knowledge_base, settings))


def restore(
    context: ServiceContext, name: str, knowledge_base: str | None = None
) -> dict[str, Any]:
    """A deleted pipeline back, on its automatic schedule."""

    settings = {"pipeline": True, "schedule": "auto", "paused": False}
    return configure(context, PipelineChange(name, knowledge_base, settings))


def _check(key: str, value: Any) -> None:
    if key == "schedule" and value not in SCHEDULES:
        raise InvalidRequest(f"schedule must be one of {', '.join(SCHEDULES)}; got {value!r}")
    if key in ("paused", "pipeline", "respect_robots", "on_startup", "on_git_commit"):
        if not isinstance(value, bool):
            raise InvalidRequest(f"{key} must be true or false")
    if (
        key in ("interval_seconds", "max_items_per_run", "max_refresh_seconds")
        and value is not None
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise InvalidRequest(f"{key} must be a whole number of 0 or more")
    if key == "min_request_interval_seconds" and value is not None:
        if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
            raise InvalidRequest(f"{key} must be a number of seconds, 0 or more")


# -- the document ledger -------------------------------------------------------


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
    rows = _ledger(context, source, request.principal, request.principal_groups)
    now = time.time()
    counts = {
        state: sum(1 for row in rows if _in_state(row, state, now)) for state in DOCUMENT_STATES
    }
    needle = (request.path_contains or "").strip().lower()
    chosen = [
        row
        for row in rows
        if _in_state(row, request.state, now) and (not needle or needle in row["path"].lower())
    ]
    chosen.sort(key=lambda r: (_rank(r["status"]), r["next_check_at"] or "~", r["path"]))
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
    wanted = path.strip()
    row = next(
        (
            r
            for r in _ledger(context, source, principal, principal_groups)
            if wanted in (r["path"], r["url"], r["id"])
        ),
        None,
    )
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


# -- rows -----------------------------------------------------------------------


def _row(context: ServiceContext, source: dict[str, Any], queue: dict[str, Any]) -> dict[str, Any]:
    from pheasant.services.quality import source_quality
    from pheasant.sync.cancellation import running
    from pheasant.sync.health import source_health

    name = str(source["name"])
    config = _source_config(context, name)
    kind = str(source.get("type"))
    health = source.get("health") if "health" in source else source_health(context.state, source)
    events = context.state.list_sync_events(name, limit=1)
    run = running(context.state, name)
    job = _job(context, name) if run else None
    validators, watermark = _web_schedule(context, name) if kind in WEB_TYPES else ({}, {})
    now = time.time()
    upcoming = [n for n in (_next_check(e) for e in validators.values()) if n is not None]
    due_next_hour = sum(1 for n in upcoming if n <= now + 3600) if kind in WEB_TYPES else None
    state = state_of(config) if config is not None else "scheduled"
    return {
        "name": name,
        "type": kind,
        "description": (json.loads(source.get("config_json") or "{}") or {}).get("description"),
        "state": state,
        "documents": (health or {}).get("readable"),
        "schedule": _schedule(context, config, kind),
        "settings": _settings(config),
        "last_run": _run(events[0]) if events else None,
        "running": (
            {
                "since": run.get("started_at"),
                "heartbeat_at": run.get("heartbeat_at"),
                "progress": (job or {}).get("progress"),
                "phase": ((job or {}).get("sources") or [{}])[0].get("phase") if job else None,
            }
            if run
            else None
        ),
        "queued": [
            {k: t.get(k) for k in ("task_id", "state", "waiting_seconds")}
            for t in queue.get("tasks") or []
            if t.get("source") == name and t.get("state") != "dead"
        ],
        "next_run": _next_run(context, state, run, upcoming, events, kind),
        "due_next_hour": due_next_hour,
        "health": health,
        "quality": source_quality(context, source),
        "pacing": _pacing(context, config, kind, watermark),
    }


def _schedule(context: ServiceContext, config: Any, kind: str) -> dict[str, Any]:
    beat = _beat(context)
    if config is None:
        return {"mode": "auto", "label": "every beat"}
    sync = config.sync
    state = state_of(config)
    if state != "scheduled":
        label = {"manual": "Only when asked", "paused": "Paused", "deleted": "Nothing syncs it"}
        return {"mode": state, "label": label[state], "beat_seconds": beat}
    if kind in WEB_TYPES:
        low = sync.interval_seconds if sync.interval_seconds is not None else DEFAULT_MIN_REFRESH
        high = max(int(config.connector.max_refresh_seconds), int(low))
        return {
            "mode": "adaptive",
            "label": f"Adaptive · {_span(low)} → {_span(high)}",
            "min_interval_seconds": low,
            "max_interval_seconds": high,
            "beat_seconds": beat,
        }
    triggers = []
    if kind == "repository" and sync.on_git_commit:
        triggers.append("on commit")
    if kind in WATCHED_TYPES and sync.on_file_change:
        triggers.append("on change")
    triggers.append(f"every {_span(beat)}")
    return {"mode": "triggers", "label": " · ".join(triggers).capitalize(), "beat_seconds": beat}


def _settings(config: Any) -> dict[str, Any]:
    if config is None:
        return {}
    sync = config.sync
    return {
        **{key: getattr(sync, key, None) for key in SYNC_FIELDS},
        "max_refresh_seconds": config.connector.max_refresh_seconds,
    }


def _pacing(
    context: ServiceContext, config: Any, kind: str, watermark: dict[str, Any]
) -> dict[str, Any]:
    parallel = int(getattr(context.config.sync.concurrency, "max_parallel_files", 1) or 1)
    if kind not in WEB_TYPES or config is None:
        return {"label": "—", "parallel": parallel}
    override = config.sync.min_request_interval_seconds
    delays = dict(watermark.get("crawl_delays") or {})
    if override is not None:
        return {
            "label": f"1 request / {override:g} s",
            "source": "configured",
            "delay_seconds": override,
        }
    if delays:
        delay = max(delays.values())
        return {
            "label": f"1 request / {delay:g} s",
            "source": "robots.txt",
            "delay_seconds": delay,
            "hosts": delays,
            "budget_deferred": watermark.get("budget_deferred"),
        }
    return {"label": f"{parallel} at a time", "source": "no crawl delay", "parallel": parallel}


def _next_run(
    context: ServiceContext,
    state: str,
    run: Any,
    upcoming: list[float],
    events: list[dict[str, Any]],
    kind: str,
) -> dict[str, Any] | None:
    if state in ("deleted", "manual", "paused"):
        return None
    if run:
        return {"label": "after this run", "at": None}
    beat = _beat(context)
    last = _epoch(events[0].get("finished_at")) if events else None
    next_beat = (last or time.time()) + beat
    if kind in WEB_TYPES and upcoming:
        first_due = min(upcoming)
        at = max(first_due, time.time()) if first_due > next_beat else next_beat
        return {"label": _iso(at), "at": _iso(at)}
    return {"label": _iso(next_beat), "at": _iso(next_beat)}


def _run(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details") or {}
    started, finished = _epoch(event.get("started_at")), _epoch(event.get("finished_at"))
    return {
        "id": event.get("id"),
        "status": event.get("status"),
        "mode": details.get("mode"),
        "started_at": event.get("started_at"),
        "finished_at": event.get("finished_at"),
        "duration_seconds": round(finished - started, 1) if started and finished else None,
        "fetched": details.get("fetched"),
        "indexed": details.get("indexed_artifacts"),
        "unchanged": details.get("skipped_artifacts"),
        "removed": details.get("removed_artifacts", 0),
        "unreadable": int(details.get("unreadable_items") or 0),
        "error": event.get("error") or details.get("error"),
        # The run's OpenTelemetry trace (`telemetry.sync_trace`): its spans are
        # in the region's logs under this id.
        "trace_id": details.get("trace_id"),
    }


def _stages(job: dict[str, Any] | None, event: dict[str, Any] | None) -> list[dict[str, Any]]:
    """A running sync's stages from its job; else the latest run's, from its event.

    The event's are what its trace timed, so a sync started by the beat, at
    startup or by a watcher -- none of which is a job -- has them too.
    """

    if job:
        seconds = ((job.get("sources") or [{}])[0] or {}).get("phase_seconds") or {}
    else:
        seconds = dict(((event or {}).get("details") or {}).get("stage_seconds") or [])
    return [{"phase": phase, "seconds": round(float(value), 1)} for phase, value in seconds.items()]


def _job_summary(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not job:
        return None
    return {k: job.get(k) for k in ("id", "status", "started_at", "finished_at", "progress")}


def _failure_groups(event: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not event:
        return []
    details = event.get("details") or {}
    groups: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    for item in details.get("unreadable_all") or []:
        cause = _cause(item[2] if len(item) > 2 else None)
        groups[cause] += 1
        examples.setdefault(cause, []).append(item[0])
    return [
        {"cause": cause, "documents": count, "examples": examples[cause][:3]}
        for cause, count in groups.most_common()
    ]


def _cause(error: Any) -> str:
    text = str(error or "unknown")
    match = re.search(r"HTTP Error (\d{3})[:\s]*([A-Za-z ]*)", text)
    if match:
        return f"HTTP {match.group(1)} {match.group(2).strip()}".strip()
    return text.split(":", 1)[0][:60]


def _ledger(
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


# -- shared reads (cached per checkpoint version) ------------------------------------

_CACHE_LOCK = threading.Lock()
_SCHEDULES: weakref.WeakKeyDictionary[Any, dict[str, tuple[str, Any]]] = weakref.WeakKeyDictionary()


def _web_schedule(context: ServiceContext, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A web pipeline's per-page validators and its last watermark.

    The checkpoint of a 25,000-page collection is megabytes of JSON; it is
    parsed once per checkpoint write, not once per request.
    """

    rows = context.state.rows(
        "SELECT updated_at FROM source_checkpoints WHERE source_id=?", (name,)
    )
    if not rows:
        return {}, {}
    version = str(rows[0]["updated_at"])
    with _CACHE_LOCK:
        cached = _SCHEDULES.setdefault(context.state, {}).get(name)
    if cached and cached[0] == version:
        return cached[1]
    checkpoint = context.state.get_source_checkpoint(name) or {}
    value = (
        dict((checkpoint.get("cursor") or {}).get("validators") or {}),
        dict(checkpoint.get("high_watermark") or {}),
    )
    with _CACHE_LOCK:
        _SCHEDULES.setdefault(context.state, {})[name] = (version, value)
    return value


def _job(context: ServiceContext, name: str) -> dict[str, Any] | None:
    """The newest job touching ``name``, from the snapshots the indexer publishes."""

    from pheasant.job_snapshots import JobSnapshotStore

    store = JobSnapshotStore(Path(context.config.pheasant.state_path) / "jobs", 200)
    try:
        records = store.list()
    except OSError:
        return None
    mine = [r for r in records if name in (r.get("targets") or [])]
    mine.sort(key=lambda r: (bool(r.get("active")), str(r.get("started_at") or "")), reverse=True)
    return mine[0] if mine else None


def _source_config(context: ServiceContext, name: str) -> Any:
    for source in context.config.sources:
        if source.name == name:
            return source
    return None


def _registered(context: ServiceContext, name: str) -> dict[str, Any]:
    from pheasant.registry.source_registry import SourceRegistry

    if not name or context.state.source_removed(name):
        raise SourceNotFound(name or "(no source named)")
    row = context.state.get_source(name)
    if row is None or str(row.get("name") or "").startswith("__"):
        raise SourceNotFound(name)
    SourceRegistry(context.config, context.state).adopt(name)
    return row


def _beat(context: ServiceContext) -> int:
    return int(getattr(context.config.sync.scheduler, "interval_seconds", 900) or 900)


def _next_check(entry: dict[str, Any] | None) -> float | None:
    if not entry:
        return None
    checked = entry.get("checked_at")
    if not isinstance(checked, int | float):
        return None
    return float(checked) + float(entry.get("interval") or DEFAULT_MIN_REFRESH)


def _url_path(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return f"{parsed.netloc}{parsed.path}"


def _epoch(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        text = str(value).replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.timestamp()
    except ValueError:
        return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return datetime.fromtimestamp(float(value), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _span(seconds: float) -> str:
    seconds = float(seconds)
    for unit, size in (("days", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            value = seconds / size
            label = f"{value:g}" if value != int(value) else str(int(value))
            return f"{label} {unit}" if unit != "days" or value != 1 else "1 day"
    return f"{int(seconds)} s"
