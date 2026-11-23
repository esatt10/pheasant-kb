"""What the process supervising a sync child does when the child will not stop.

Layer 3 of `sync/cancellation.py`'s design. A sync stops by itself between
items and, through its stop signal, inside a long one. This module is for the
child that cannot -- stuck in a socket read with no timeout, a native call
over one enormous page, a provider that never answers -- and for what such a
child leaves behind:

* :class:`CancelWatch` is the supervisor's view of one child's cancel, read
  in the loop that already reads the child's output (`sync/writer.py`,
  `sync/worker.py`), and says when its grace period is over;
* :func:`record_forced_stop` writes down, once the child is gone, what it
  could not: its run no longer live, its lease free, enrichment owed, its
  request closed, the source ``cancelled``, a ``sync.cancelled`` event;
* :func:`heal_vectors` is the next run's repair of the one thing a killed run
  can leave that no incremental sync would ever notice: chunks committed
  before their vectors were written.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from pheasant.sync import request_ledger as requests
from pheasant.sync.cancellation import (
    EXPIRED_HEARTBEAT,
    SUPERVISOR_ENV,
    WATCH_INTERVAL_SECONDS,
    _now,
)

logger = logging.getLogger(__name__)

#: How long a supervising process waits, after it first sees a cancel, for a
#: sync child to stop by itself before ending it
#: (``sync.concurrency.cancel_grace_seconds`` overrides it).
DEFAULT_CANCEL_GRACE_SECONDS = 60.0

#: Progress phases in which a child is already stopping or finishing. A
#: supervisor stops counting the grace period once it sees one: killing a
#: child that is flushing its last vectors costs more than waiting for it.
ACKNOWLEDGED_PHASES = frozenset({"cancelling", "saving"})


class CancelWatch:
    """A supervisor's view of one sync child's cancel, and when to end the child.

    The child is expected to stop by itself (layers 1 and 2 of
    `sync/cancellation.py`). This is the backstop for a child that cannot: stuck in a
    socket read with no timeout, a native call over one enormous page, a
    provider that never answers. The clock starts when *this* process first
    sees the request -- its own monotonic clock, so no two machines' wall
    clocks are ever compared -- and stops for good once the child reports a
    phase that says it is stopping or finishing (:data:`ACKNOWLEDGED_PHASES`).

    Only the child's own run is watched: the ``sync_runs`` row whose run id
    carries this watch's :attr:`token` -- handed to the child in its
    environment (:data:`SUPERVISOR_ENV`), so it names the run whatever
    process tree a launcher put between the two -- or the ledger request the
    child was handed. A run of the same source in some other process is not
    the one this child is doing, and must not end it. A process id would
    not do: on Windows a virtual environment's ``python.exe`` is a launcher
    whose child is the interpreter, so ``Popen.pid`` names neither the run's
    owner nor its lease.
    """

    def __init__(
        self,
        state: Any,
        source_id: str,
        request_id: str | None = None,
        *,
        grace_s: float = DEFAULT_CANCEL_GRACE_SECONDS,
        poll_interval_s: float = WATCH_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state = state
        self.source_id = source_id
        self.request_id = request_id
        self.grace_s = float(grace_s)
        self.poll_interval_s = float(poll_interval_s)
        self._clock = clock
        #: Put in the child's environment, so its runs carry it. A reusable
        #: writer interpreter keeps one token for its life, and hands it here.
        self.token = uuid.uuid4().hex
        #: The child's run's owner (``host:pid``), read off its row once seen:
        #: what its source lease is held under too.
        self.owner: str | None = None
        self.seen_at: float | None = None
        self.acknowledged = False
        self.last_phase: str | None = None
        self.requested_by = ""
        self.reason = ""
        self._next_poll = 0.0

    @property
    def enabled(self) -> bool:
        return self.grace_s > 0 and self.state is not None

    def child_env(self) -> dict[str, str]:
        """What to add to a sync child's environment so its runs name this watch."""

        return {SUPERVISOR_ENV: self.token}

    def find_owner(self) -> str | None:
        """The child's run's owner, read again if it was never seen. Never raises."""

        if self.owner is None:
            try:
                run = self.state.rows(
                    "SELECT owner FROM sync_runs WHERE source_id=? AND run_id LIKE ?",
                    (self.source_id, f"{self.token}.%"),
                )
                self.owner = str(run[0]["owner"]) if run else None
            except Exception:  # noqa: BLE001
                logger.debug("could not read the run of %s", self.source_id, exc_info=True)
        return self.owner

    def note_phase(self, phase: Any) -> None:
        if phase:
            self.last_phase = str(phase)
            if self.last_phase in ACKNOWLEDGED_PHASES:
                self.acknowledged = True

    def overdue(self) -> bool:
        """Whether the child had its grace period and is still running."""

        if not self.enabled or self.acknowledged:
            return False
        now = self._clock()
        if self.seen_at is None:
            if now < self._next_poll:
                return False
            self._next_poll = now + self.poll_interval_s
            if not self._requested():
                return False
            self.seen_at = now
            logger.info(
                "Sync of %s was cancelled by %s; ending its worker in %.0fs unless it stops",
                self.source_id,
                self.requested_by,
                self.grace_s,
            )
        return now - self.seen_at >= self.grace_s

    def _requested(self) -> bool:
        try:
            rows: list[Any] = []
            run = self.state.rows(
                "SELECT owner, cancel_requested_at, cancel_requested_by, cancel_reason "
                "FROM sync_runs WHERE source_id=? AND run_id LIKE ?",
                (self.source_id, f"{self.token}.%"),
            )
            if run:
                self.owner = str(run[0]["owner"])
                if run[0]["cancel_requested_at"]:
                    rows = run
            if not rows and self.request_id:
                ledger = requests.cancel_of(self.state, self.request_id)
                rows = [ledger] if ledger else []
        except Exception:  # noqa: BLE001 - an unreadable request is no request
            logger.debug("cancel watch for %s failed", self.source_id, exc_info=True)
            return False
        if not rows:
            return False
        self.requested_by = str(rows[0]["cancel_requested_by"] or "a caller")
        self.reason = str(rows[0]["cancel_reason"] or "")
        return True


class SyncForcedStop(RuntimeError):  # noqa: N818 - named for the event
    """A supervisor ended a sync child that did not stop when it was cancelled."""

    def __init__(self, watch: CancelWatch) -> None:
        self.watch = watch
        super().__init__(
            f"cancelled by {watch.requested_by}; the sync worker did not stop within "
            f"{watch.grace_s:.0f}s and was ended"
        )


def record_forced_stop(
    state: Any,
    watch: CancelWatch,
    *,
    mode: str,
    started_at: str | None = None,
    dirty_scope: str | None = None,
) -> dict[str, Any]:
    """Write down what a sync child that was ended could not.

    Run by the supervisor once the child is gone. Every step is best-effort
    and independent, because the point is that as much as possible lands:

    * the run's heartbeat is expired, not its row deleted -- nothing reads it
      as running any more, and the next run of the source still learns that
      its predecessor died and heals what that leaves (`SyncEngine`);
    * the child's source lease is released, so that next run need not wait
      out the staleness window first;
    * enrichment is marked owed, because the whole-graph passes the child may
      have been inside never committed;
    * the ledger request is closed as cancelled, so a redelivery of its task
      is acknowledged rather than run;
    * the source says ``cancelled`` and a ``sync.cancelled`` event says how.
    """

    now = _now()
    outcome = {
        "requested_by": watch.requested_by or "a caller",
        "reason": watch.reason or None,
        "forced": True,
        "grace_seconds": watch.grace_s,
        "last_phase": watch.last_phase,
    }

    def attempt(what: str, step: Callable[[], Any]) -> None:
        try:
            step()
        except Exception:  # noqa: BLE001 - see the docstring
            logger.warning(
                "after ending the sync of %s, could not %s", watch.source_id, what, exc_info=True
            )

    if watch.find_owner():
        attempt(
            "expire its run",
            lambda: state.execute(
                "UPDATE sync_runs SET heartbeat_at=? WHERE source_id=? AND owner=?",
                (EXPIRED_HEARTBEAT, watch.source_id, watch.owner),
            ),
        )
        attempt(
            "release its source lease",
            lambda: state.execute(
                "DELETE FROM source_leases WHERE source_id=? AND owner=?",
                (watch.source_id, watch.owner),
            ),
        )
    if dirty_scope:
        attempt(
            "mark enrichment owed",
            lambda: state.set_fingerprint(dirty_scope.format(name=watch.source_id), "1", now),
        )
    attempt(
        "close its request",
        lambda: requests.close(
            state, watch.request_id, requests.CANCELLED, outcome="ended after its grace period"
        ),
    )
    attempt("mark it cancelled", lambda: state.mark_source_status(watch.source_id, "cancelled"))
    attempt(
        "record the event",
        lambda: state.append_sync_event(
            state.new_sync_event_id(),
            watch.source_id,
            "sync.cancelled",
            "cancelled",
            started_at or now,
            now,
            {"mode": mode, "cancelled": outcome},
        ),
    )
    logger.warning(
        "Ended the sync worker of %s: cancelled by %s and still running after %.0fs (%s)",
        watch.source_id,
        outcome["requested_by"],
        watch.grace_s,
        watch.last_phase or "no phase reported",
    )
    return outcome


def heal_vectors(engine: Any, source_name: str) -> int:
    """Embed the chunks of one source the vector store is missing. Never raises.

    Run when a sync of the source finds that the previous one never exited
    (`SyncRun.predecessor_died`): a run that was ended -- by a supervisor
    after a cancel, by the kernel for memory -- can have committed chunks
    whose vectors were still queued, and an incremental sync skips their
    files by checksum forever after.

    Cheap when there is nothing to heal, which is nearly always: one statement
    reads the source's chunk ids (no text), the store answers membership from
    its id cache, and only documents with a missing chunk have their text read
    and queued. A first version read every chunk's text, one statement per
    document -- minutes, on a 25,000-page source, to find nothing.
    """

    if engine.vectors is None:
        return 0
    embedded = 0
    try:
        rows = engine.state.rows(
            "SELECT id, artifact_id FROM chunks WHERE source_id=?", (source_name,)
        )
        held = engine.vectors.store.existing_ids([str(row["id"]) for row in rows])
        lacking = sorted({str(row["artifact_id"]) for row in rows if str(row["id"]) not in held})
        for artifact_id in lacking:
            chunks = engine.state.rows(
                "SELECT id, text, text_hash FROM chunks WHERE artifact_id=? ORDER BY chunk_index",
                (artifact_id,),
            )
            embedded += engine.vectors.index_artifact(
                source_name, artifact_id, [dict(chunk) for chunk in chunks]
            )
        if embedded:
            engine.vectors.flush()
            logger.warning(
                "%s: embedded %d chunk(s) an interrupted sync committed without vectors",
                source_name,
                embedded,
            )
    except Exception:  # noqa: BLE001 - a repair must not fail the sync it precedes
        logger.warning("could not heal the vectors of %s", source_name, exc_info=True)
    return embedded
