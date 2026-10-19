"""The thing that runs a batch without the region noticing.

The requirement is "an executor that does not burden the rest of the
framework", and burden here has three separate shapes, each needing its own
answer.

**Contention for the database.** A tuning batch is thousands of reads against
the same SQLite or Postgres the lexical arm serves from. So the executor holds
**one slot**. Not a pool: one. Parallelism would multiply exactly the
contention this is trying to avoid, and the batch is not latency-sensitive --
nobody is waiting for it.

**Contention with indexing.** It takes the ``__tuning__`` lease and **never**
takes ``sync_lock``. The scheduler holds ``sync_lock`` across all its work, and
a thousand-query replay inside it would stall incremental sync for every source
in the region -- the same mistake the observation plane's hot-to-cold roll was
moved outside the lock to avoid, and the same rule the evaluation plane
follows.

**Contention for the machine.** :class:`BackpressureGate` stands the batch down
while the index queue is backed up or a sync is holding a source lease, and
checks *between* units rather than once at the start: a batch that began on an
idle region and is still running when a large re-index starts must yield, not
finish what it started. Under sustained pressure it stops and the experiment is
resumable, which is the whole reason trials are checkpointed.

On platforms that support it the worker thread also drops its niceness, so the
kernel prefers the request path when both want CPU. Best-effort: it is an
optimisation, and a container without ``CAP_SYS_NICE`` simply keeps its default
priority rather than failing to start.

**Why a thread and not a role.** ``serve --role tuner`` exists as a config
posture (``tuning.auto.enabled`` on the indexer, off on the API replicas), but
the executor itself is a thread because the work is I/O-bound against the state
store and a separate process would need its own copy of the graph -- which is
the largest thing in memory and the reason the indexer coordinator does not
load one either.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: The lease a tuning batch claims, so N replicas produce one batch. Named in
#: the same `__name__` style as `__evaluation__`, and for the same reason: it
#: is a pseudo-source in `source_leases`, and the underscores keep it from
#: colliding with a real source called "tuning".
TUNING_LEASE = "__tuning__"

#: Standing down is checked this often while a batch runs.
BACKPRESSURE_INTERVAL_SECONDS = 5.0

#: How deep the index queue may be before the batch yields. Low: an index
#: queue with work in it means somebody is waiting for content to become
#: searchable, and that is unambiguously more important than a measurement.
DEFAULT_MAX_QUEUE_DEPTH = 1


def _owner() -> str:
    """A lease owner that is unique per *holder*, not per process.

    ``host:pid`` is not enough. Two threads in one replica share it, and
    ``SourceLease`` grants a lease its current holder already owns — so a
    process-scoped owner makes an in-process race look like a re-entrant
    acquire and lets both threads through. The tuning executor is exactly that
    shape: a scheduled batch and a hand-started one live in the same process.
    """

    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"


@dataclass
class BackpressureGate:
    """Whether the region can spare the effort right now.

    Cheap by construction -- it is consulted between every unit of work, so it
    caches its answer for :data:`BACKPRESSURE_INTERVAL_SECONDS` and never does
    more than two small indexed reads.
    """

    state: Any
    max_queue_depth: int = DEFAULT_MAX_QUEUE_DEPTH
    respect_sync: bool = True
    interval: float = BACKPRESSURE_INTERVAL_SECONDS
    _last_checked: float = field(default=0.0, repr=False)
    _last_answer: tuple[bool, str] = field(default=(True, ""), repr=False)

    def check(self) -> tuple[bool, str]:
        """``(may_continue, reason_if_not)``."""

        now = time.monotonic()
        if now - self._last_checked < self.interval:
            return self._last_answer
        self._last_checked = now
        self._last_answer = self._probe()
        return self._last_answer

    def _probe(self) -> tuple[bool, str]:
        try:
            rows = self.state.rows(
                "SELECT COUNT(*) AS depth FROM index_tasks WHERE status IN ('pending','claimed')",
                (),
            )
            depth = int(rows[0]["depth"]) if rows else 0
            if depth >= self.max_queue_depth:
                return False, f"the index queue has {depth} task(s) waiting"
        except Exception:  # noqa: BLE001 - no queue table is the ordinary case
            logger.debug("tuning: index queue depth unavailable", exc_info=True)
        if self.respect_sync:
            try:
                rows = self.state.rows(
                    "SELECT source_id FROM source_leases WHERE source_id NOT LIKE '\\_\\_%' "
                    "ESCAPE '\\'",
                    (),
                )
                if rows:
                    held = ", ".join(str(row["source_id"]) for row in rows[:3])
                    return False, f"a sync holds the lease for {held}"
            except Exception:  # noqa: BLE001
                logger.debug("tuning: source leases unavailable", exc_info=True)
        return True, ""


class TuningLease:
    """The batch's exclusion, or a no-op where there is nothing to exclude.

    The same shape and the same degradation as the evaluation plane's lease: a
    single-process region running one CLI command has nothing to exclude, and
    failing a tuning pass because a lease table could not be written would make
    it depend on a fleet feature it does not need.
    """

    def __init__(self, state: Any, *, owner: str | None = None, stale_after_seconds: float = 90.0):
        self._lease: Any = None
        try:
            from pheasant.sync.locks import SourceLease

            self._lease = SourceLease(
                state,
                TUNING_LEASE,
                owner=owner or _owner(),
                # Beat faster than the staleness window by a comfortable
                # margin. The evaluation plane shipped a version where the
                # lease's window and the run's window were different clocks,
                # and a live batch's own lease looked abandoned.
                heartbeat_interval_s=max(2.0, stale_after_seconds / 6.0),
                stale_after_s=stale_after_seconds,
            )
        except Exception:  # noqa: BLE001
            logger.debug("tuning: lease unavailable; running unguarded", exc_info=True)

    def __enter__(self) -> bool:
        if self._lease is None:
            return True
        try:
            return bool(self._lease.try_acquire())
        except Exception:  # noqa: BLE001
            logger.warning(
                "tuning: could not claim the %s lease; running unguarded, so concurrent "
                "replicas may each start a batch",
                TUNING_LEASE,
                exc_info=True,
            )
            return True

    def __exit__(self, exc_type: Any, exc: BaseException | None, _tb: Any) -> None:
        if self._lease is None:
            return
        try:
            self._lease.release()
        except Exception:  # noqa: BLE001
            logger.debug("tuning: lease release failed", exc_info=True)


class Cancelled(RuntimeError):
    """Somebody asked this batch to stop.

    Distinct from :class:`StoodDown` because the two mean opposite things
    about intent. Standing down is the region protecting itself and should
    resume unprompted; a cancel is a person saying "not now", and resuming it
    automatically would be ignoring them. The runner records them as different
    terminal states for that reason.
    """


class StoodDown(RuntimeError):
    """The batch yielded to more important work. Resumable, not failed.

    A distinct type rather than a flag because the two outcomes want opposite
    handling: a failure is an error to report, and a stand-down is a normal
    event whose right response is to leave the experiment resumable and try
    again later.
    """


@dataclass
class TuningExecutor:
    """One slot, its own lease, and a gate it consults between units.

    Deliberately not a ``ThreadPoolExecutor``. What is needed here is the
    opposite of a pool: a single worker that yields readily, can be asked to
    stop, and holds no queue of its own.
    """

    state: Any
    gate: BackpressureGate | None = None
    #: Returns who asked to cancel, or "" — read between units. A callable
    #: rather than an id so the executor stays ignorant of the tuning schema,
    #: and so a test can cancel without a database.
    cancel_check: Callable[[], str] | None = None
    nice_increment: int = 10
    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def submit(self, name: str, work: Callable[[], Any]) -> bool:
        """Start the batch in the background. ``False`` if a slot is already busy.

        Refusing rather than queueing is the point: two batches queued behind
        each other is two batches' worth of load arriving eventually, and the
        second is almost always redundant by the time it runs -- the state it
        would measure has moved on.
        """

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, args=(name, work), name=f"pheasant-tuning-{name}", daemon=True
            )
            self._thread.start()
            return True

    def _run(self, name: str, work: Callable[[], Any]) -> None:
        self._lower_priority()
        try:
            work()
        except Cancelled as cancelled:
            logger.info("tuning: %s %s", name, cancelled)
        except StoodDown as stood:
            logger.info("tuning: %s stood down (%s); it will resume", name, stood)
        except Exception:  # noqa: BLE001 - a batch must not take the process down
            logger.exception("tuning: %s failed", name)

    def _lower_priority(self) -> None:
        """Prefer the request path when both want CPU. Best-effort."""

        try:
            os.nice(self.nice_increment)
        except Exception:  # noqa: BLE001 - not permitted everywhere, and fine
            logger.debug("tuning: could not lower thread priority", exc_info=True)

    def stop(self) -> None:
        self._stop.set()

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def checkpoint(self) -> None:
        """Yield if asked to. Called between units of work.

        Raises :class:`StoodDown` rather than returning a flag, so a caller
        cannot forget to check it. Every call site that can be interrupted is
        already inside a loop whose partial results are checkpointed, which is
        what makes raising here safe.
        """

        if self.cancel_check is not None:
            try:
                requested_by = self.cancel_check()
            except Exception:  # noqa: BLE001 - a failed check must not stop work
                logger.debug("tuning: cancel check failed", exc_info=True)
                requested_by = ""
            if requested_by:
                raise Cancelled(f"cancelled by {requested_by}")
        if self._stop.is_set():
            raise StoodDown("stop was requested")
        if self.gate is not None:
            ok, reason = self.gate.check()
            if not ok:
                raise StoodDown(reason)
