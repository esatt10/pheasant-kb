"""Every source's sync pipeline: what it does, when it runs, and how it went.

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

The drill-down, each pipeline's document ledger (``documents``, ``document``,
``recheck``), is `services/sync_ledger.py`.
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


# -- shared reads (cached per checkpoint version) ------------------------------------

_CACHE_LOCK = threading.Lock()
_SCHEDULES: weakref.WeakKeyDictionary[Any, dict[str, tuple[str, Any]]] = weakref.WeakKeyDictionary()


def _checkpoint_version(context: ServiceContext, name: str) -> str:
    """When the source's checkpoint was last written ("" when it has none)."""

    rows = context.state.rows(
        "SELECT updated_at FROM source_checkpoints WHERE source_id=?", (name,)
    )
    return str(rows[0]["updated_at"]) if rows else ""


def _web_schedule(context: ServiceContext, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A web pipeline's per-page validators and its last watermark.

    The checkpoint of a 25,000-page collection is megabytes of JSON; it is
    parsed once per checkpoint write, not once per request.
    """

    version = _checkpoint_version(context, name)
    if not version:
        return {}, {}
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
