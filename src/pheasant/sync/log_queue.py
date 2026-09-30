"""The log tier's durable queue, and the worker that drains it.

Why a second queue rather than a ``kind`` column on ``index_tasks``: an
observation arrives per request, against a corpus that changes hourly at most.
Sharing a table would put request-rate churn on the very index the indexer
claims from -- ``idx_index_tasks_claim`` -- and the vacuum pressure that comes
with it under PostgreSQL. That is precisely the burden this tier exists to
remove, so sharing would defeat it.

Almost nothing is duplicated to get that isolation. :class:`LogQueue` subclasses
:class:`pheasant.sync.queue.LocalQueue` and changes two class attributes, so the
conditional-``UPDATE`` claim -- the piece with the twenty-line race argument
about which ``WHERE`` clause PostgreSQL re-evaluates under READ COMMITTED --
stays a single implementation. ``drain()`` is already task-agnostic and is
reused verbatim. ``tests/test_log_queue.py`` asserts the index queue's own
generated SQL did not move.

The worker is the other half of the answer. Persisting a batch, rolling hot rows
to Parquet and dropping expired partitions all happen here, on a process that is
neither serving a request nor holding the indexer's ``sync_lock`` -- which is
where an earlier draft of this put the roll, and would have stalled incremental
sync for every source in the region.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pheasant.sync.queue import (
    DEFAULT_VISIBILITY_SECONDS,
    IndexTask,
    LocalQueue,
    NatsQueue,
    TaskQueue,
)
from pheasant.telemetry.interactions import (
    COLUMNS,
    INSERT_SQL,
    InteractionEvent,
    events_from_batch,
    read_spool,
)
from pheasant.telemetry.metrics import REGISTRY

logger = logging.getLogger(__name__)

#: Lower than the index queue's 300s: a batch is an insert, not a sync. A long
#: visibility on short work just delays redelivery after a worker dies.
DEFAULT_LOG_VISIBILITY_SECONDS = 120.0

#: A log batch is best-effort by construction. Retrying a poisoned one three
#: times costs more than the data is worth, which is why this is below the
#: index queue's default.
DEFAULT_LOG_MAX_ATTEMPTS = 2


@dataclass
class LogTask:
    """One batch of observations.

    No ``source_id`` and no ``mode``: those are indexing vocabulary. The payload
    is the whole task.
    """

    id: str
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    max_attempts: int = DEFAULT_LOG_MAX_ATTEMPTS
    publish_id: str = field(default_factory=lambda: uuid.uuid4().hex, repr=False, compare=False)
    handle: Any = None

    @property
    def kind(self) -> str:
        return str(self.payload.get("kind") or "interactions")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
        }


class LogQueue(LocalQueue):
    """``log_tasks``, claimed by exactly the mechanism ``index_tasks`` is.

    Everything inherited is deliberate. In particular ``publish``'s
    ``ON CONFLICT ... WHERE status='done'`` re-arm behaves correctly here for a
    different reason than it does upstream: a batch id is content-addressed on
    the span ids it carries, so a republished batch is the *same* batch, and
    re-arming a completed one simply re-applies an insert that
    ``ON CONFLICT (id) DO NOTHING`` will absorb.
    """

    TABLE = "log_tasks"
    EXTRA_COLUMNS: tuple[str, ...] = ()

    #: Set from config by :func:`log_queue_from_config`. A class default so a
    #: queue built directly in a test still has one.
    max_attempts: int = DEFAULT_LOG_MAX_ATTEMPTS

    def _extra_values(self, task: Any) -> tuple[Any, ...]:
        return ()

    def _build_task(self, row: Any) -> LogTask:
        return LogTask(
            id=str(row["id"]),
            payload=json.loads(row["payload"] or "{}"),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
        )

    def publish_batch(self, task_id: str, payload: dict[str, Any]) -> LogTask:
        """What :class:`~pheasant.telemetry.interactions.QueueSink` calls."""

        return self.publish(LogTask(id=task_id, payload=payload, max_attempts=self.max_attempts))


class NatsLogQueue(NatsQueue):
    """JetStream for the log tier.

    :class:`~pheasant.sync.queue.NatsQueue` is reused whole rather than
    subclassed deeply: its wire format already carries an opaque ``payload``,
    which is all a batch is. The one thing it requires that a batch does not
    naturally have is a ``source``, so the knowledge base fills that slot --
    honest rather than clever, and it keeps the requeue path (which also
    reconstructs a task from a message) working unchanged.

    :func:`handle_batch` reads only ``.payload``, so it does not care whether a
    task arrived as a :class:`LogTask` from the local queue or an ``IndexTask``
    from here.
    """

    def __init__(self, *args: Any, max_attempts: int = DEFAULT_LOG_MAX_ATTEMPTS, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.max_attempts = int(max_attempts)

    def publish_batch(self, task_id: str, payload: dict[str, Any]) -> Any:
        return self.publish(
            IndexTask(
                id=task_id,
                source_id=str(payload.get("kb_id") or "logs"),
                mode="logs",
                payload=payload,
                max_attempts=self.max_attempts,
            )
        )


def log_queue_from_config(config: Any, state: Any) -> TaskQueue | None:
    """Build the log queue, or ``None`` when the tier is off.

    ``None`` is not a degraded mode -- it is the default, and it means whoever
    produced a batch writes it. A single container has nothing to gain from a
    queue between two halves of one process.
    """

    settings = getattr(getattr(config, "observability", None), "interactions", None)
    queue_settings = getattr(settings, "queue", None)
    if settings is None or queue_settings is None:
        return None
    if not getattr(settings, "enabled", False) or not getattr(queue_settings, "enabled", False):
        return None

    attempts = int(getattr(queue_settings, "max_attempts", DEFAULT_LOG_MAX_ATTEMPTS))
    backend = str(getattr(queue_settings, "backend", "local") or "local").lower()
    if backend == "nats":
        # Its own stream, subject and durable, so the two tiers cannot consume
        # each other's work even when they share one NATS cluster. The server
        # list is shared with the index queue deliberately: it says where the
        # cluster is, not what it carries.
        return NatsLogQueue(
            servers=list(getattr(config.sync.queue, "nats_servers", []) or []),
            stream=str(getattr(queue_settings, "nats_stream", "PHEASANT_LOGS")),
            subject=str(getattr(queue_settings, "nats_subject", "pheasant.logs.batches")),
            durable=str(getattr(queue_settings, "nats_durable", "pheasant-loggers")),
            max_attempts=attempts,
        )
    if backend not in ("local", ""):
        logger.warning("Unknown log queue backend %r; falling back to 'local'.", backend)
    queue = LogQueue(state)
    queue.max_attempts = attempts
    return queue


# --------------------------------------------------------------------------
# Persistence and the hot -> cold roll
# --------------------------------------------------------------------------


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def write_events(state: Any, events: list[InteractionEvent]) -> int:
    """Insert a batch into the hot store, idempotently.

    ``ON CONFLICT (id) DO NOTHING`` plus a content-addressed id is what makes
    at-least-once redelivery a no-op rather than a double-count -- and a
    double-count here would inflate a formation threshold, which is a wrong
    memory rather than merely a wrong number.

    Returns how many events were *submitted*, not how many rows were new: the
    difference is exactly the redelivery this absorbs, and counting inserts
    would mean a rowcount round trip per row to learn something no caller
    acts on.
    """

    writable = [event for event in events if event.is_writable]
    if not writable:
        return 0
    with state.conn:
        for event in writable:
            state.conn.execute(INSERT_SQL, event.as_row())
    return len(writable)


def hot_row_count(state: Any) -> int:
    rows = state.rows("SELECT COUNT(*) AS c FROM interaction_events", ())
    return int(rows[0]["c"]) if rows else 0


def ingest_spool(state: Any, root: Path) -> int:
    """Drain NDJSON spool files written by a replica that could not write.

    Each file is deleted only after its rows are committed, so a crash between
    the two re-ingests rather than loses -- and re-ingesting is free, because
    the insert is idempotent.
    """

    total = 0
    for path, events in read_spool(root):
        try:
            total += write_events(state, events)
            path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 - one bad spool file must not stop the rest
            logger.warning("Could not ingest interaction spool %s", path, exc_info=True)
    return total


def cold_partition_dir(exports_path: Path | str, day: str) -> Path:
    return Path(exports_path) / "interactions" / f"dt={day}"


def roll(
    state: Any,
    settings: Any,
    *,
    exports_path: Path | str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Move hot rows past their retention out of ``/state``.

    With ``cold_enabled`` they are written to a Parquet partition first;
    without it they are simply deleted. Either way the row count moved is
    bounded by ``max_rows_per_pass``, which is load-bearing in a single
    container where this runs on the scheduler beat under ``sync_lock``.

    Returns a report rather than logging one, so a caller can put it on an
    endpoint or a metric without re-deriving it.

    **A day is deleted only once its partition exists.** The pass groups rows
    by day, writes each day's partition, and deletes exactly the days that were
    written -- so the two failures a real ``/exports`` volume produces stay
    survivable:

    * *the write fails for one day.* The other days still roll; that day's rows
      stay hot and are retried next pass. Deleting the whole batch on a partial
      write would lose the failed day, and refusing to delete any of it would
      re-write every day that had already succeeded -- which is what produced
      duplicate rows in cold storage, in the one place an outside reader reads
      (``docs/reference/export-schema.md``). Same shape as the interaction
      batch insert: one bad row must not cost the rest.
    * *no partition can be written at all* (``cold_enabled`` with the
      ``analytics`` extra absent). The rows are still dropped -- an
      un-drainable hot store is the unbounded growth the retention policy
      exists to prevent -- but the report says ``dropped``, not ``cold``, and
      names why. Reporting ``cold`` for rows that reached no partition told an
      operator their observations were in ``/exports`` when they were gone.
    """

    now = now or datetime.now(UTC)
    retention = int(getattr(settings, "hot_retention_days", 7) or 0)
    limit = max(1, int(getattr(settings, "max_rows_per_pass", 50_000) or 50_000))
    cold = bool(getattr(settings, "cold_enabled", False))
    report: dict[str, Any] = {
        "rolled": 0,
        "partitions": [],
        "disposition": "cold" if cold else "dropped",
    }

    cutoff = _iso(now - timedelta(days=retention))
    rows = state.rows(
        f"SELECT {','.join(COLUMNS)} FROM interaction_events "
        "WHERE started_at < ? ORDER BY started_at LIMIT ?",
        (cutoff, limit),
    )
    if not rows:
        return report

    by_day: dict[str, list[Any]] = {}
    for row in rows:
        day = str(row["started_at"])[:10] or "unknown"
        by_day.setdefault(day, []).append(row)

    ids: list[str] = []
    if not cold:
        ids = [str(row["id"]) for row in rows]
    else:
        archived_days: list[str] = []
        deferred: dict[str, str] = {}
        for day, day_rows in sorted(by_day.items()):
            try:
                written = _write_partition(exports_path, day, day_rows)
            except Exception as exc:  # noqa: BLE001 - one bad day must not cost the rest
                logger.warning(
                    "Could not archive interaction partition dt=%s; its rows stay in "
                    "the hot store and will be retried",
                    day,
                    exc_info=True,
                )
                deferred[day] = f"{type(exc).__name__}: {exc}"
                continue
            if written is None:
                # Nothing to write it with. `_write_partition` has already said
                # so; the rows are dropped, and the disposition below says that.
                continue
            report["partitions"].append(str(written))
            archived_days.append(day)
        if deferred:
            report["deferred_partitions"] = deferred
        # A day that neither archived nor deferred is one nothing could write
        # it with -- `_write_partition` returned None because the analytics
        # extra is absent. Those rows are dropped, because a hot store that can
        # never drain is the unbounded growth the retention policy exists to
        # prevent; a deferred day is not dropped, because it will be retried.
        droppable = [day for day in by_day if day not in archived_days and day not in deferred]
        ids = [str(row["id"]) for day in (*archived_days, *droppable) for row in by_day[day]]
        if droppable:
            # The report must not call these cold: no partition holds them.
            report["cold_unavailable"] = True
            report["disposition"] = "cold_partial" if archived_days else "dropped"
        elif deferred and archived_days:
            report["disposition"] = "cold_partial"
        # Everything deferred and nothing else: `disposition` stays "cold" and
        # `rolled` stays 0, which is the truth -- nothing was archived *and*
        # nothing was dropped, and the next pass tries again.

    if not ids:
        return report
    _delete_ids(state, ids)
    report["rolled"] = len(ids)
    REGISTRY.inc(
        "pheasant_log_rolled_rows_total",
        float(len(ids)),
        disposition=report["disposition"],
    )
    return report


def _delete_ids(state: Any, ids: list[str]) -> None:
    """Delete in chunks so one pass cannot build a parameter list of 50k."""

    CHUNK = 500
    with state.conn:
        for start in range(0, len(ids), CHUNK):
            batch = ids[start : start + CHUNK]
            placeholders = ",".join("?" for _ in batch)
            state.conn.execute(
                f"DELETE FROM interaction_events WHERE id IN ({placeholders})", tuple(batch)
            )


def _write_partition(exports_path: Path | str, day: str, rows: list[Any]) -> Path | None:
    """Append one day's rows to its Parquet partition.

    Uses the analytics module's DuckDB writer, which is already tuned for flat
    memory (a bounded row-group size and a spill limit measured across a 10x
    row range). This does **not** make DuckDB a storage backend: the
    destination is ``/exports``, the caller is the log tier rather than the
    sync path, and nothing operational lives here.

    A file per pass rather than one file per day rewritten: appending to
    Parquet is not a thing, and rewriting a day would make the pass O(day)
    instead of O(batch).
    """

    from pheasant import analytics

    try:
        duckdb = analytics.duckdb_module()
    except analytics.AnalyticsUnavailable:
        logger.warning(
            "observability.interactions.cold_enabled is set but the [analytics] extra "
            "is not installed; expired rows will be dropped instead of archived."
        )
        return None

    directory = cold_partition_dir(exports_path, day)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    target = directory / f"part-{stamp}.parquet"

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"SET memory_limit='{analytics._MEMORY_LIMIT}'")
        connection.execute(f"CREATE TABLE part({','.join(f'{name} VARCHAR' for name in COLUMNS)})")
        placeholders = ",".join("?" for _ in COLUMNS)
        connection.executemany(
            f"INSERT INTO part VALUES({placeholders})",
            [
                tuple(None if row[name] is None else str(row[name]) for name in COLUMNS)
                for row in rows
            ],
        )
        connection.execute(
            f"COPY part TO '{target.as_posix()}' "
            f"(FORMAT PARQUET, ROW_GROUP_SIZE {analytics._ROW_GROUP_SIZE})"
        )
    finally:
        connection.close()
    return target


def drop_expired_partitions(
    exports_path: Path | str, settings: Any, *, now: datetime | None = None
) -> list[str]:
    """Remove whole ``dt=`` directories past ``cold_retention_days``.

    Whole days, never individual rows: a partition is the unit cold storage is
    written in, and rewriting one to drop a row would cost more than keeping
    it. ``None`` retention keeps everything forever, which is the default.
    """

    retention = getattr(settings, "cold_retention_days", None)
    if retention is None:
        return []
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=int(retention))).strftime("%Y-%m-%d")
    root = Path(exports_path) / "interactions"
    if not root.is_dir():
        return []
    dropped: list[str] = []
    for directory in sorted(root.glob("dt=*")):
        day = directory.name[3:]
        if day < cutoff:
            shutil.rmtree(directory, ignore_errors=True)
            dropped.append(day)
    return dropped


def handle_batch(state: Any, task: LogTask) -> int:
    """The queue handler: persist one claimed batch."""

    return write_events(state, events_from_batch(task.payload))


def publish_depth(queue: TaskQueue | None) -> None:
    """Refresh the gauges a ``--role logger`` tier scales on."""

    if queue is None:
        return
    try:
        depth = queue.depth() or {}
    except Exception:  # noqa: BLE001 - a scrape must not fail on a queue hiccup
        return
    for status, count in depth.items():
        REGISTRY.set("pheasant_log_queue_depth", float(count), status=str(status))
    REGISTRY.set("pheasant_log_dead_letters", float(depth.get("dead", 0)))


#: How long a written batch's ``log_tasks`` row outlives it. Its only use
#: afterwards is deduplicating a republish of the same batch, and the event
#: insert is ``ON CONFLICT (id) DO NOTHING`` anyway, so a day is generous.
COMPLETED_BATCH_RETENTION_SECONDS = 86_400.0


def _purge_completed_batches(state: Any, config: Any) -> int:
    """Delete ``log_tasks`` rows for batches already written.

    Every batch id is content-addressed over its events, so unlike an index
    task (one id per source, re-armed) no id repeats: each observation batch
    was a new row, and each row keeps its whole payload after it is done.
    `LocalQueue.purge_completed` existed for this and nothing called it, so
    the table grew with request traffic and stored every observation a
    second time. JetStream needs nothing here: the broker removes an acked
    message itself.
    """

    try:
        queue = log_queue_from_config(config, state)
        if not isinstance(queue, LogQueue):
            return 0
        return queue.purge_completed(COMPLETED_BATCH_RETENTION_SECONDS)
    except Exception:  # noqa: BLE001 - the beat's contract: never raise
        logger.warning("Could not purge completed log batches", exc_info=True)
        return 0


def run_log_maintenance(
    state: Any,
    config: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """One bounded upkeep pass: ingest spools, roll, drop expired partitions.

    Returns ``None`` fast when observation is off, so it can sit on the
    scheduler beat next to ``run_memory_maintenance`` and cost a disabled
    region nothing. It follows that beat's contract exactly: import inline,
    no-op fast, never raise.
    """

    settings = getattr(getattr(config, "observability", None), "interactions", None)
    if settings is None or not getattr(settings, "enabled", False):
        return None

    report: dict[str, Any] = {}
    spool = getattr(settings, "spool_path", None)
    if spool:
        report["spooled"] = ingest_spool(state, Path(spool))

    purged = _purge_completed_batches(state, config)
    if purged:
        report["purged_batches"] = purged
    exports_path = getattr(config.pheasant, "exports_path", None) or Path("exports")
    report.update(roll(state, settings, exports_path=exports_path, now=now))
    dropped = drop_expired_partitions(exports_path, settings, now=now)
    if dropped:
        report["dropped_partitions"] = dropped
    REGISTRY.set("pheasant_interaction_rows", float(hot_row_count(state)))
    return report


__all__ = [
    "DEFAULT_LOG_MAX_ATTEMPTS",
    "DEFAULT_LOG_VISIBILITY_SECONDS",
    "DEFAULT_VISIBILITY_SECONDS",
    "LogQueue",
    "LogTask",
    "cold_partition_dir",
    "drop_expired_partitions",
    "handle_batch",
    "hot_row_count",
    "ingest_spool",
    "log_queue_from_config",
    "publish_depth",
    "roll",
    "run_log_maintenance",
    "write_events",
]
