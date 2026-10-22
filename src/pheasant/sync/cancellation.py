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
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pheasant.sync.locks import SOURCE_HEARTBEAT_SECONDS, SOURCE_STALE_SECONDS

logger = logging.getLogger(__name__)

#: How often a running sync looks for a cancel request. A read of one row by
#: primary key, so it is cheap; throttled anyway because the loop runs once per
#: item and a source of 50,000 unchanged files would otherwise issue 50,000 of
#: them in a minute. A second is well inside what a person waits for.
CHECK_INTERVAL_SECONDS = 1.0


class SyncCancelled(RuntimeError):  # noqa: N818 - named for the event, like Cancelled
    """Somebody asked this sync to stop. Raised between items, never inside one."""

    def __init__(self, requested_by: str, reason: str = "") -> None:
        self.requested_by = requested_by or "a caller"
        self.reason = reason or ""
        message = f"cancelled by {self.requested_by}"
        super().__init__(f"{message}: {self.reason}" if self.reason else message)


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
    ) -> None:
        self.state = state
        self.source_id = source_id
        self.run_id = uuid.uuid4().hex
        self.owner = f"{socket.gethostname()}:{os.getpid()}"
        self.heartbeat_interval_s = heartbeat_interval_s
        self.check_interval_s = (
            CHECK_INTERVAL_SECONDS if check_interval_s is None else check_interval_s
        )
        self._registered = False
        self._next_check = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> SyncRun:
        now = _now()
        try:
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
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"pheasant-sync-run-{self.source_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.heartbeat_interval_s * 2))
            self._thread = None
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

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval_s):
            try:
                self.state.execute(
                    "UPDATE sync_runs SET heartbeat_at=? WHERE source_id=? AND run_id=?",
                    (_now(), self.source_id, self.run_id),
                )
            except Exception:  # noqa: BLE001 - a missed beat costs a refused cancel
                logger.debug("sync_runs heartbeat failed for %s", self.source_id, exc_info=True)

    def checkpoint(self) -> None:
        """Raise :class:`SyncCancelled` if this run was asked to stop.

        Raises rather than returning a flag, like the tuning executor's
        checkpoint, so a caller cannot forget to look at the answer.
        """

        if not self._registered:
            return
        now = time.monotonic()
        if now < self._next_check:
            return
        self._next_check = now + self.check_interval_s
        try:
            rows = self.state.rows(
                "SELECT cancel_requested_at, cancel_requested_by, cancel_reason "
                "FROM sync_runs WHERE source_id=? AND run_id=?",
                (self.source_id, self.run_id),
            )
        except Exception:  # noqa: BLE001 - a failed check must not stop work
            logger.debug("sync_runs cancel check failed for %s", self.source_id, exc_info=True)
            return
        if rows and rows[0]["cancel_requested_at"]:
            raise SyncCancelled(
                str(rows[0]["cancel_requested_by"] or ""), str(rows[0]["cancel_reason"] or "")
            )


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
