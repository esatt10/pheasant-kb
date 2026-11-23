"""Stopping a sync that is running, from whichever process somebody asked.

A sync of a large web collection is hours of fetching, and before this the
only way to stop one was to restart the container doing it -- which also
stopped every other source that container was indexing, and which a person
talking to an api replica cannot do at all. The job registry could not help:
on a fleet the job a caller sees says ``succeeded`` the moment the sync is
*published*, while the work runs in an indexer that claimed the task minutes
later and that the caller never addresses.

So the request is a **row**, the same shape the tuning plane's cancel already
has and for the same reason: the process doing the work and the process being
asked are usually different, and a flag in one process's memory is a cancel
that lands only when they happen to be the same.

* While a source syncs, :class:`SyncRun` holds one ``sync_runs`` row for it,
  keyed by source and carrying a per-run id, and heartbeats it from a thread.
  The heartbeat is a thread rather than the item loop because one slow item --
  a large PDF, a page whose server is timing out -- would otherwise make a
  running sync look dead and its cancel be refused.
* :func:`request_cancel` is one ``UPDATE ... RETURNING`` against a row whose
  heartbeat is fresh. The statement is the whole answer to "is anything
  running": a row comes back only if a live run accepted the request, so there
  is no read-then-write window, and no request is ever written for a sync that
  is not running -- which is what keeps a cancel from stopping the *next* sync
  somebody starts.
* The run reads the request between items (:meth:`SyncRun.checkpoint`, at most
  once per :data:`CHECK_INTERVAL_SECONDS`) and raises :class:`SyncCancelled`.
  A new run replaces the row, so a request left over from an earlier run can
  never reach a later one.

What a cancelled sync keeps is the engine's business (``SyncEngine.sync_source``):
every item it committed stays committed, and nothing that claims the pass was
complete -- the source fingerprint, deletion of vanished files, the "healthy"
status -- is recorded.

**Ending a sync is layered, because no one layer survives every failure:**

1. *Between items* -- :meth:`SyncRun.checkpoint`, above. Exact, and it keeps
   every committed item.
2. *Inside an item* -- the run carries a :class:`~pheasant.cancel_scope.StopSignal`
   that its watch thread sets within :data:`WATCH_INTERVAL_SECONDS` of the
   request, on the heartbeat's own connection, and the code that waits inside
   one item (a pacing slot, a backoff, the next PDF page range or page) stops
   on it. Whatever an item computed after the signal was set is never
   committed: the commit loop reads the same signal first.
3. *Past a stuck item* -- the process that supervises a sync child
   (`sync/writer.py`, `sync/worker.py`) watches the request too
   (`sync/supervision.py`) and, when the child has not answered within
   ``sync.concurrency.cancel_grace_seconds``, ends it. Commits are
   transactions, so a killed child loses only the item it was in; the parent
   records what the child could not, and the next
   run of the source, finding the row of a run that never exited, heals what
   a kill can leave behind: the node index's pending delta, and chunks
   committed before their vectors.
4. *Past a dead process* -- a queued sync is a row in `sync/request_ledger.py`, and a
   cancel marks it. A broker redelivers the task of an indexer that died; the
   next claim finds the cancel and acknowledges the task instead of running it
   again from the start. The same row is how a sync still waiting for an
   indexer is cancelled at all.
"""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from pheasant import cancel_scope
from pheasant.persistence.heartbeat import Heartbeat, HeartbeatWriter
from pheasant.sync import request_ledger as requests
from pheasant.sync.locks import SOURCE_HEARTBEAT_SECONDS, SOURCE_STALE_SECONDS

logger = logging.getLogger(__name__)

#: How often a running sync looks for a cancel request. A read of one row by
#: primary key, so it is cheap; throttled anyway because the loop runs once per
#: item and a source of 50,000 unchanged files would otherwise issue 50,000 of
#: them in a minute. A second is well inside what a person waits for.
CHECK_INTERVAL_SECONDS = 1.0

#: How often the run's watch thread reads the request, on its own connection,
#: and sets the stop signal the code inside an item waits on. Independent of
#: the commit loop's throttle above, which tests set to zero.
WATCH_INTERVAL_SECONDS = 1.0

#: Older than any staleness window. Written over the heartbeat of a run whose
#: process was ended: the row stays, because the next run must learn that its
#: predecessor died, but nothing reads it as running any more.
EXPIRED_HEARTBEAT = "1970-01-01T00:00:00+00:00"

#: Set by a supervising process in a sync child's environment; the child's
#: runs prefix their run id with it, which is how the supervisor finds the run
#: of *its* child (`sync/supervision.py`).
SUPERVISOR_ENV = "PHEASANT_SYNC_SUPERVISOR"


class SyncCancelled(cancel_scope.Stopped):
    """Somebody asked this sync to stop.

    Raised between items by :meth:`SyncRun.checkpoint`. Its base,
    :class:`~pheasant.cancel_scope.Stopped`, is what code inside an item
    raises once the run's stop signal is set.
    """


#: The ledger request (`sync/request_ledger.py`) the sync running in this context
#: answers, bound by ``SyncEngine.apply_index_task`` around one queued task.
_REQUEST: ContextVar[str | None] = ContextVar("pheasant_sync_request", default=None)


def current_request() -> str | None:
    return _REQUEST.get()


class bound_request:  # noqa: N801 - a context manager, named like contextlib's
    """Run the body as the sync of ledger request ``request_id``."""

    def __init__(self, request_id: str | None) -> None:
        self.request_id = request_id
        self._token: Any = None

    def __enter__(self) -> str | None:
        self._token = _REQUEST.set(self.request_id)
        return self.request_id

    def __exit__(self, *_exc: Any) -> None:
        _REQUEST.reset(self._token)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _stale_before(stale_after_s: float = SOURCE_STALE_SECONDS) -> str:
    return (datetime.now(UTC) - timedelta(seconds=stale_after_s)).isoformat()


class SyncRun:
    """The ``sync_runs`` row for one sync of one source, while it runs.

    Entered once the source's write lock is held, so two runs of one source
    never overlap and the row has exactly one writer. Bookkeeping, never a
    reason to fail a sync: every write is best-effort, and a run whose row could
    not be written simply cannot be cancelled -- which is how every sync behaved
    before this existed.
    """

    def __init__(
        self,
        state: Any,
        source_id: str,
        *,
        heartbeat_interval_s: float = SOURCE_HEARTBEAT_SECONDS,
        check_interval_s: float | None = None,
        watch_interval_s: float | None = None,
        request_id: str | None = None,
    ) -> None:
        self.state = state
        self.source_id = source_id
        #: The ledger request this run answers, when it came off the queue: a
        #: cancel can land there before this run has a row of its own.
        self.request_id = request_id if request_id is not None else current_request()
        self.watch_interval_s = (
            WATCH_INTERVAL_SECONDS if watch_interval_s is None else watch_interval_s
        )
        #: Set once, by the watch thread or a checkpoint, when a cancel is seen.
        self.signal = cancel_scope.StopSignal()
        self._scope: Any = None
        self._next_beat = 0.0
        supervisor = os.environ.get(SUPERVISOR_ENV)
        self.run_id = f"{supervisor}.{uuid.uuid4().hex}" if supervisor else uuid.uuid4().hex
        self.owner = f"{socket.gethostname()}:{os.getpid()}"
        self.heartbeat_interval_s = heartbeat_interval_s
        self.check_interval_s = (
            CHECK_INTERVAL_SECONDS if check_interval_s is None else check_interval_s
        )
        self._registered = False
        #: The previous run of this source left its row behind: it never
        #: reached ``__exit__``, so it was killed. Anything it held only in
        #: memory -- the node index's pending delta -- was lost with it.
        self.predecessor_died = False
        self._next_check = 0.0
        self._heartbeat: Heartbeat | None = None

    def __enter__(self) -> SyncRun:
        now = _now()
        try:
            # The write lock is held, so a row here belongs to a run that ended
            # without clearing it -- a killed process, never a live one.
            self.predecessor_died = bool(
                self.state.rows("SELECT 1 FROM sync_runs WHERE source_id=?", (self.source_id,))
            )
            # Replacing the row clears any request a previous run left behind:
            # that request was for a sync that has ended.
            self.state.execute(
                "INSERT INTO sync_runs(source_id, run_id, owner, started_at, heartbeat_at, "
                "cancel_requested_at, cancel_requested_by, cancel_reason) "
                "VALUES(?,?,?,?,?,NULL,NULL,NULL) ON CONFLICT(source_id) DO UPDATE SET "
                "run_id=excluded.run_id, owner=excluded.owner, "
                "started_at=excluded.started_at, heartbeat_at=excluded.heartbeat_at, "
                "cancel_requested_at=NULL, cancel_requested_by=NULL, cancel_reason=NULL",
                (self.source_id, self.run_id, self.owner, now, now),
            )
            self._registered = True
        except Exception:  # noqa: BLE001 - see the class docstring
            logger.warning("could not register the sync of %s as running", self.source_id)
            logger.debug("sync_runs registration failed", exc_info=True)
            return self
        # Code inside an item reads the signal through the context; the
        # engine's executors copy the context into their threads.
        self._scope = cancel_scope.bound(self.signal)
        self._scope.__enter__()
        # Its own thread and its own connection (`persistence.heartbeat`): a
        # beat that shared the sync's pool stopped at 00:41 on the Azure fleet
        # while the sync committed for another hour, so every reader called it
        # dead. The same thread watches for a cancel every `watch_interval_s`
        # and writes the heartbeat every `heartbeat_interval_s`.
        self._next_beat = time.monotonic() + self.heartbeat_interval_s
        self._heartbeat = Heartbeat(
            self.state,
            f"sync-run-{self.source_id}",
            self._beat,
            max(0.05, min(self.watch_interval_s, self.heartbeat_interval_s)),
        ).start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._scope is not None:
            self._scope.__exit__(None, None, None)
            self._scope = None
        if self._heartbeat is not None:
            self._heartbeat.stop()
            self._heartbeat = None
        if not self._registered:
            return
        try:
            # Only our own run: a row with another run id belongs to a sync
            # that started after this one, and is not ours to remove.
            self.state.execute(
                "DELETE FROM sync_runs WHERE source_id=? AND run_id=?",
                (self.source_id, self.run_id),
            )
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.debug("could not clear the sync_runs row for %s", self.source_id, exc_info=True)

    _CANCEL_COLUMNS = "cancel_requested_at, cancel_requested_by, cancel_reason"

    def _beat(self, writer: HeartbeatWriter) -> None:
        now = time.monotonic()
        if now >= self._next_beat:
            self._next_beat = now + self.heartbeat_interval_s
            rows = writer.execute(
                "UPDATE sync_runs SET heartbeat_at=? WHERE source_id=? AND run_id=? "
                f"RETURNING {self._CANCEL_COLUMNS}",
                (_now(), self.source_id, self.run_id),
            )
        elif self.signal.is_set():
            return  # seen already; only the heartbeat is still owed
        else:
            rows = writer.execute(
                f"SELECT {self._CANCEL_COLUMNS} FROM sync_runs WHERE source_id=? AND run_id=?",
                (self.source_id, self.run_id),
            )
        if self._observe(rows) or self.signal.is_set() or not self.request_id:
            return
        self._observe(
            writer.execute(
                f"SELECT {self._CANCEL_COLUMNS} FROM sync_requests "
                "WHERE id=? AND cancel_requested_at IS NOT NULL",
                (self.request_id,),
            )
        )

    def _observe(self, rows: list[Any]) -> bool:
        """Set the signal from a row carrying a cancel. Whether one did."""

        if rows and rows[0]["cancel_requested_at"]:
            self.signal.set(
                str(rows[0]["cancel_requested_by"] or ""), str(rows[0]["cancel_reason"] or "")
            )
            return True
        return False

    def _cancelled(self) -> SyncCancelled:
        return SyncCancelled(self.signal.requested_by, self.signal.reason)

    def poll(self, *, force: bool = False) -> SyncCancelled | None:
        """The cancel this run was asked for, or ``None``. Never raises.

        The signal first, unthrottled: once anything has seen a cancel, no
        further item may be committed. Then, at most once per
        ``check_interval_s`` (at once with ``force``), the rows themselves --
        the run's and its ledger request's -- through the state store's pool,
        for a run whose watch thread is behind or could not connect.
        """

        if not self._registered:
            return None
        if self.signal.is_set():
            return self._cancelled()
        now = time.monotonic()
        if not force and now < self._next_check:
            return None
        self._next_check = now + self.check_interval_s
        try:
            rows = self.state.rows(
                f"SELECT {self._CANCEL_COLUMNS} FROM sync_runs WHERE source_id=? AND run_id=?",
                (self.source_id, self.run_id),
            )
            if not self._observe(rows) and self.request_id:
                ledger = requests.cancel_of(self.state, self.request_id)
                self._observe([ledger] if ledger else [])
        except Exception:  # noqa: BLE001 - a failed check must not stop work
            logger.debug("sync_runs cancel check failed for %s", self.source_id, exc_info=True)
            return None
        return self._cancelled() if self.signal.is_set() else None

    def checkpoint(self) -> None:
        """Raise :class:`SyncCancelled` if this run was asked to stop.

        Raises rather than returning a flag, like the tuning executor's
        checkpoint, so a caller cannot forget to look at the answer.
        """

        cancelled = self.poll()
        if cancelled is not None:
            raise cancelled


def request_cancel(
    state: Any,
    source_id: str,
    *,
    requested_by: str,
    reason: str = "",
    stale_after_s: float = SOURCE_STALE_SECONDS,
) -> dict[str, Any] | None:
    """Ask the running sync of ``source_id`` to stop. ``None`` if none is running.

    A run whose heartbeat is older than ``stale_after_s`` is not running: its
    process died without clearing its row, and accepting a cancel for it would
    report success for work that had already stopped. Asking twice is
    harmless -- the first request's time and author are kept, because they are
    the answer to "who stopped this".
    """

    now = _now()
    rows = state.execute_returning(
        "UPDATE sync_runs SET "
        "cancel_requested_by=CASE WHEN cancel_requested_at IS NULL THEN ? "
        "ELSE cancel_requested_by END, "
        "cancel_reason=CASE WHEN cancel_requested_at IS NULL THEN ? ELSE cancel_reason END, "
        "cancel_requested_at=COALESCE(cancel_requested_at, ?) "
        "WHERE source_id=? AND heartbeat_at >= ? "
        "RETURNING run_id, owner, started_at, cancel_requested_at, cancel_requested_by, "
        "cancel_reason",
        (requested_by, reason or None, now, source_id, _stale_before(stale_after_s)),
    )
    if not rows:
        return None
    accepted = dict(rows[0])
    accepted["first_request"] = accepted.get("cancel_requested_at") == now
    return accepted


def running(
    state: Any, source_id: str, *, stale_after_s: float = SOURCE_STALE_SECONDS
) -> dict[str, Any] | None:
    """The live run of ``source_id``, or ``None``. Read-only."""

    rows = state.rows(
        "SELECT run_id, owner, started_at, heartbeat_at, cancel_requested_at, "
        "cancel_requested_by FROM sync_runs WHERE source_id=? AND heartbeat_at >= ?",
        (source_id, _stale_before(stale_after_s)),
    )
    return dict(rows[0]) if rows else None


def cancel_source(
    state: Any,
    source_id: str,
    *,
    requested_by: str,
    reason: str = "",
) -> dict[str, Any] | None:
    """Ask every sync of ``source_id`` that is running or waiting to stop.

    ``{"run": <the live run, or None>, "requests": [<ledger rows reached>]}``,
    or ``None`` when there is neither -- the only answer that means "nothing
    to stop". The run's row first: a run that registers between the two
    statements still sees the ledger request, because its checks read both.
    The ledger is best-effort, as everywhere: a failure there must not refuse
    the cancel of a sync that is visibly running.
    """

    run = request_cancel(state, source_id, requested_by=requested_by, reason=reason)
    # One time for the whole request, so every row it reached says the same.
    stamp = str((run or {}).get("cancel_requested_at") or _now())
    try:
        reached = requests.request_cancel(
            state, source_id, requested_by=requested_by, reason=reason, now=stamp
        )
    except Exception:  # noqa: BLE001 - see the docstring
        logger.warning("could not record the cancel of %s's queued syncs", source_id, exc_info=True)
        reached = []
    if run is None and not reached:
        return None
    first = bool(run and run.get("first_request")) or any(
        row.get("cancel_requested_at") == stamp for row in reached
    )
    return {"run": run, "requests": reached, "first_request": first}


def record_cancelled_before_reading(
    state: Any,
    source_id: str,
    cancelled: cancel_scope.Stopped,
    *,
    mode: str,
    stage: str,
    started_at: str | None = None,
) -> dict[str, Any]:
    """The event for a sync stopped before it read anything: queued, or listing.

    The source's status is left as it was. Nothing in the index changed, so
    whatever the last sync that ran said about it is still true; "cancelled"
    is for a source a stopped pass left partly indexed.
    """

    now = _now()
    outcome = {
        "requested_by": cancelled.requested_by,
        "reason": cancelled.reason or None,
        "stage": stage,
        "processed_items": 0,
    }
    try:
        state.append_sync_event(
            state.new_sync_event_id(),
            source_id,
            "sync.cancelled",
            "cancelled",
            started_at or now,
            now,
            {"mode": mode, "cancelled": outcome},
        )
    except Exception:  # noqa: BLE001 - the stop itself already happened
        logger.warning("could not record the cancel of %s", source_id, exc_info=True)
    return outcome


def admit_index_task(engine: Any, task: Any) -> Any:
    """The result for a queued sync that must not run on this delivery, or ``None``.

    ``SyncEngine.apply_index_task`` asks this of every delivery: checked
    against its ledger row (`sync/request_ledger.py`), a sync cancelled while
    it waited, or one whose indexer died after a cancel, is acknowledged
    instead of run; one that already ran to the end -- only its
    acknowledgement was lost -- is not run twice.
    """

    from pheasant.sync.engine import SyncResult

    admission = requests.admit(engine.state, task, f"{socket.gethostname()}:{os.getpid()}")
    if admission.runs:
        return None
    source_id = str(task.source_id)
    if admission.verdict == "finished":
        logger.info("%s: sync request already finished; not running it again", source_id)
        nodes, edges = engine._graph_counts()
        return SyncResult(source_id, 0, 0, nodes, edges, "skipped", {"reason": "already finished"})
    cancelled = SyncCancelled(admission.requested_by or "", admission.reason or "")
    logger.info("%s: sync %s before it started", source_id, cancelled)
    return cancelled_before_reading(engine, source_id, cancelled, str(task.mode), "queued")


def cancelled_before_reading(
    engine: Any, source_name: str, cancelled: Any, mode: str, stage: str
) -> Any:
    """A sync stopped before it read anything: nothing to keep, one event to write."""

    from pheasant.sync.engine import SyncResult

    outcome = record_cancelled_before_reading(
        engine.state, source_name, cancelled, mode=str(mode), stage=stage
    )
    nodes, edges = engine._graph_counts()
    return SyncResult(source_name, 0, 0, nodes, edges, "cancelled", {"cancelled": outcome})


def cancellable(
    prepared_items: Any,
    run: SyncRun,
    processed: set[str],
    stopped: list[SyncCancelled],
):
    """Yield ``prepared_items`` until the run is cancelled, then stop.

    A generator rather than a ``try`` around the commit loop, so the loop body
    is unchanged: the run is checked before each item is handed out, and a
    cancel ends the iteration with the request left in ``stopped`` for the
    caller to act on. An item counts as ``processed`` once the loop asks for
    the next one, which is after its body ran to the end or to a ``continue``
    -- never for an item that was only read ahead. (Counting it before the
    ``yield`` instead would be equivalent on the cancel path, which checks
    before an item is handed out; the order matters only to a body that
    raises, and then the sync fails and ``processed`` is never read.)

    Closing ``prepared_items`` is what stops the look-ahead: its executor is
    told to drop work it has not started rather than prepare a window of items
    nobody will commit.
    """

    iterator = iter(prepared_items)
    try:
        while True:
            try:
                prepared = next(iterator)
            except StopIteration:
                return
            except Exception:
                # Preparing an item failed. Once the run was asked to stop,
                # that failure *is* the stop -- a page range abandoned, a
                # backoff interrupted, whatever the code in between made of
                # the `Stopped` it was handed -- so the pass ends as a cancel,
                # not as a failed sync. Before that, it is a real failure.
                if (cancelled := run.poll(force=True)) is not None:
                    stopped.append(cancelled)
                    return
                raise
            if (cancelled := run.poll()) is not None:
                stopped.append(cancelled)
                return
            yield prepared
            processed.add(prepared.item.identity)
    finally:
        close = getattr(prepared_items, "close", None)
        if close is not None:
            close()


def finish_cancelled(
    engine: Any,
    cancelled: SyncCancelled,
    *,
    source: Any,
    connector: Any,
    mode: str,
    manifest: dict[str, Any],
    items: list[Any],
    processed: set[str],
    counts: dict[str, int],
    started_at: str,
    report: Callable[..., None],
    embedding_progress: Callable[[int], None],
    details: dict[str, Any],
) -> Any:
    """Stop a pass somebody cancelled, keeping everything it committed.

    ``SyncEngine.sync_source`` calls this with itself as ``engine`` when
    :func:`cancellable` ends its loop early, beside `vanished.prune_vanished`.

    Each item the loop committed is already in the state store and the
    working graph; this makes the rest of that durable -- vectors flushed,
    graph published, manifest saved -- so the next incremental sync skips
    those items by checksum and reads only what this pass never reached.

    What it deliberately does **not** record is anything that claims the
    pass was complete. Not the source fingerprint (a full sync escalated
    by a config change stays escalated). Not the deletion of files missing
    from the listing, nor the whole-graph enrichment: both run on the next
    pass, which the dirty flag below makes enrich even if it finds nothing
    new. Not "healthy": the source says ``cancelled`` until a sync of it
    finishes. And not the connector's checkpoint, unless the connector can
    build one that is true of a partial pass (``partial_checkpoint_from_items``).
    """

    from pheasant.ingestion.pipeline import utc_now
    from pheasant.sync.engine import SyncResult

    indexed = int(counts.get("indexed") or 0)
    skipped = int(counts.get("skipped") or 0)
    total_items = len(items)
    logger.info(
        "Sync of %s %s after %d of %d item(s)",
        source.name,
        cancelled,
        len(processed),
        total_items,
    )
    report("cancelling", len(processed), total_items, str(cancelled))
    if engine.vectors is not None:
        # Before the commit mutex, as in `_finalize_index_state`: this
        # waits on a provider, and committed chunks without their vectors
        # are exactly what no later incremental sync would re-embed.
        engine.vectors.flush(on_progress=embedding_progress)
    with engine._sync_mutex:
        if indexed:
            engine.flush_node_index()
            engine.graph_store.save(engine.config.knowledge_base_id, engine.graph_builder.graph)
            engine._graph_dirty = False
            engine._last_checkpoint = time.monotonic()
            engine.state.set_fingerprint(
                engine._ENRICH_DIRTY_SCOPE.format(name=source.name), "1", utc_now()
            )
        engine.manifests.save(source.name, manifest)
        engine.reconcile_node_index(source.name)
    partial = connector.partial_checkpoint_from_items(items, processed)
    if partial is not None:
        cursor, high_watermark = partial
        high_watermark.update(
            {"indexed_artifacts": indexed, "skipped_artifacts": skipped, "cancelled": True}
        )
        connector.set_checkpoint(cursor, high_watermark, "cancelled")
    engine.state.mark_source_status(source.name, "cancelled")
    outcome = {
        "requested_by": cancelled.requested_by,
        "reason": cancelled.reason or None,
        "processed_items": len(processed),
        "total_items": total_items,
        "checkpoint_saved": partial is not None,
    }
    now = utc_now()
    engine.state.append_sync_event(
        engine.state.new_sync_event_id(),
        source.name,
        "sync.cancelled",
        "cancelled",
        started_at,
        now,
        {
            "mode": mode,
            "connector_type": connector.connector_type,
            "fetched": int(counts.get("fetched") or 0),
            "skipped": int(counts.get("transfer_skipped") or 0),
            "indexed_artifacts": indexed,
            "skipped_artifacts": skipped,
            "cancelled": outcome,
            **details,
        },
    )
    graph_nodes, graph_edges = engine._graph_counts()
    return SyncResult(
        source.name,
        indexed,
        skipped,
        graph_nodes,
        graph_edges,
        "cancelled",
        {
            "connector_type": connector.connector_type,
            "fetched": int(counts.get("fetched") or 0),
            "skipped": int(counts.get("transfer_skipped") or 0),
            "cancelled": outcome,
            **details,
        },
    )


def skip_disabled(engine: Any, source_id: str) -> Any:
    """The result of a queued sync for a disabled source, or ``None`` to run it.

    A task published before the source was disabled is still in the queue, and
    an indexer restarted mid-sync has it redelivered -- which used to resume the
    source anyway, so disabling a sync that was looping did not stop it. Such a
    task is acknowledged, not failed: there is nothing to retry until somebody
    enables the source again.
    """

    registered = engine.state.get_source(source_id)
    if registered is None or int(registered["enabled"] or 0):
        return None
    from pheasant.sync.engine import SyncResult

    logger.info("Skipping queued sync of %s: the source is disabled", source_id)
    nodes, edges = engine._graph_counts()
    return SyncResult(source_id, 0, 0, nodes, edges, "disabled", {"skipped": "source disabled"})
