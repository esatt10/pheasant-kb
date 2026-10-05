"""The durable index work queue (Phase 35.5).

``sync_all`` used to hold its remaining sources in a Python list. That is
fine for one container indexing five folders and wrong for everything this
phase is about: a process killed nine sources into ten has silently lost the
tenth, nothing outside that process can see the backlog, and there is no
number for a scheduler to scale on.

A task is therefore a **row**, or a message on a broker:

* it survives the process that enqueued it, so a restart resumes;
* it is claimed with a **visibility timeout** rather than held, so a worker
  that dies releases its task by simply not finishing — no lock to clean up;
* it counts attempts and **dead-letters** rather than retrying forever, so a
  source that cannot be indexed stops consuming the fleet;
* its depth is a gauge, which is what an HPA or KEDA scales on.

**At-least-once delivery is safe here, and that is not a coincidence.**
Indexing is idempotent by design (content sha256 + stable IDs), which is
pillar 1 of this project — so a redelivered task re-indexes to the identical
state. The queue is cheap precisely because that guarantee was already paid
for.

**Off by default** (``sync.queue.enabled: false``). With it off, ``sync_all``
is byte-identical to what it always did; rule 7 is not negotiable, and a
single container indexing a folder should not need a queue to do it.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

PENDING = "pending"
INFLIGHT = "inflight"
DONE = "done"
DEAD = "dead"

#: How long a claim stays invisible before another worker may take it. Long
#: enough that a genuinely slow source is not stolen mid-index, short enough
#: that a killed worker's task is picked up in the same operational minute.
#: Extended by heartbeats while the claimer is alive, so the value bounds
#: *silence*, not work.
DEFAULT_VISIBILITY_SECONDS = 300.0

#: JetStream's default ``ack_wait`` is 30 seconds. The visibility setting is
#: also used by the local queue and is commonly much larger, so deriving the
#: first heartbeat from it alone can let NATS redeliver a healthy task before
#: the first ping. Ten seconds is below that broker default and negligible
#: overhead compared with a repository sync.
MAX_HEARTBEAT_INTERVAL_SECONDS = 10.0

#: Attempts before a task is dead-lettered. Three is the point at which a
#: failure has stopped looking transient; a dead task is kept, never deleted,
#: so `pheasant queue requeue-dead` can replay it after a fix.
DEFAULT_MAX_ATTEMPTS = 3

#: Bounded retries of the claim statement itself. Two workers can select the
#: same candidate row; the loser updates nothing and tries the next one.
CLAIM_ATTEMPTS = 8


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def owner_id() -> str:
    """Host + pid + a random suffix, so a recycled pid is not mistaken for us."""

    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass
class IndexTask:
    """One source to index, with everything a fresh process needs to do it."""

    id: str
    source_id: str
    mode: str = "incremental"
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    #: One publish invocation, distinct from the logical task id. JetStream's
    #: duplicate window suppresses this value; a fresh IndexTask with the same
    #: logical id is therefore a legitimate new run after the prior one ends.
    publish_id: str = field(default_factory=lambda: uuid.uuid4().hex, repr=False, compare=False)
    #: Backend-specific handle (a JetStream message, say). Never persisted.
    handle: Any = None

    @property
    def max_depth(self) -> int | None:
        value = self.payload.get("max_depth")
        return None if value is None else int(value)

    @property
    def full_scan(self) -> bool:
        return bool(self.payload.get("full_scan", False))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source_id,
            "mode": self.mode,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            **({"payload": self.payload} if self.payload else {}),
        }


class TaskQueue:
    """What every backend must provide. Deliberately small.

    Four verbs cover the whole lifecycle, and the visibility timeout means
    there is no fifth for "release" — a claimer that stops heartbeating is
    released by time.
    """

    def publish(self, task: IndexTask) -> IndexTask:
        raise NotImplementedError

    def claim(self, owner: str, *, visibility_seconds: float = DEFAULT_VISIBILITY_SECONDS) -> Any:
        raise NotImplementedError

    def ack(self, task: IndexTask) -> None:
        raise NotImplementedError

    def nack(self, task: IndexTask, error: str, *, retry_in_seconds: float = 30.0) -> None:
        raise NotImplementedError

    def heartbeat(
        self, task: IndexTask, *, visibility_seconds: float = DEFAULT_VISIBILITY_SECONDS
    ) -> None:
        """Extend a claim. Default no-op: not every broker needs it."""

    def depth(self) -> dict[str, int]:
        raise NotImplementedError

    def outstanding(self, limit: int = 50) -> list[dict[str, Any]] | None:
        """Unfinished tasks, oldest first, or ``None`` when this backend cannot list them.

        ``None`` is not "empty". A broker that can count its backlog but not
        enumerate it (JetStream, without consuming) has to say *unknown*, or a
        reader shows a queue with nothing waiting while three syncs sit in it.
        """

        return None

    def close(self) -> None:
        """Release backend resources. Default no-op."""


class LocalQueue(TaskQueue):
    """The state store *is* the queue.

    No broker to run, and it works identically on SQLite and Postgres — which
    is the whole reason it is the default: a single container gets crash
    resumption without operating anything, and the same code multiplexes
    across indexers once the state backend is Postgres.

    Claiming is one conditional ``UPDATE ... RETURNING``, so the database
    arbitrates races rather than a read-then-write in Python.

    **The outer ``visible_at<=?`` is the whole race-freedom argument**, and it
    is easy to get subtly wrong. Under READ COMMITTED, a Postgres UPDATE that
    blocks on a row another transaction is updating re-evaluates its WHERE
    against the *new* row version once that commits. Only the outer clause is
    re-evaluated — the subquery is not — so the outer clause has to be a
    predicate the winner's own write falsifies. ``status`` alone does not:
    this queue deliberately allows claiming an ``inflight`` row (that is how a
    dead worker's task is redelivered), so after the winner sets
    ``status='inflight'`` the guard is still true and both transactions claim
    the row. Repeating the visibility check outside is what makes the loser
    match nothing: the winner has just pushed ``visible_at`` into the future.

    This was found by a Postgres concurrency test, after mutation testing
    showed the SQLite suite could not tell the difference — SQLite serializes
    writers, so no arrangement of guards fails there.

    The outer ``status IN (...)`` beside it is **defence in depth and is not
    covered by a test**, recorded here rather than left to look load-bearing:
    it catches an ack landing between the subquery and the update, which
    leaves ``visible_at`` in the past (ack does not touch it) while the row is
    no longer claimable. Forcing that interleaving needs two hand-driven
    transactions, so mutation testing correctly reports the guard as
    unkilled; it is kept because the case is real and the clause is free.
    """

    #: The table this queue lives in, and the columns it carries beyond the
    #: generic lifecycle set. Both are class-level seams so the log tier can
    #: reuse **this** claim -- the one with the race argument above -- instead
    #: of copying it into a second table where it could drift. The index
    #: queue's own generated SQL is unchanged by the parameterization, and
    #: `tests/test_log_queue.py` asserts exactly that rather than trusting it.
    TABLE = "index_tasks"
    EXTRA_COLUMNS: tuple[str, ...] = ("source_id", "mode")

    def __init__(self, state: Any) -> None:
        self.state = state

    # -- subclass seams ----------------------------------------------------

    def _extra_values(self, task: Any) -> tuple[Any, ...]:
        return (task.source_id, task.mode)

    def _build_task(self, row: Any) -> Any:
        return IndexTask(
            id=str(row["id"]),
            source_id=str(row["source_id"]),
            mode=str(row["mode"]),
            payload=json.loads(row["payload"] or "{}"),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
        )

    def publish(self, task: IndexTask) -> IndexTask:
        """Enqueue a task, or re-arm one that has already run.

        The task id is content-addressed on (knowledge base, source, mode) so
        that two replicas answering one user's double-click enqueue one task.
        A plain ``INSERT`` turns that dedup into something much worse: once the
        row reaches ``done`` it can never be inserted again, and the caller
        swallows the primary-key error as "already queued". Every ``sync_all``
        after the first then found nothing claimable and indexed **nothing**,
        silently — including the scheduler beat, so a queue-enabled deployment
        stopped re-indexing entirely after its first pass.

        Dedup means "do not queue it twice while it is outstanding", not
        "never queue it again". So a ``done`` row is re-armed; ``pending`` and
        ``inflight`` rows are left alone (that is the dedup); and a ``dead``
        row stays dead, because dead-lettering exists to stop a poison task
        consuming the fleet and ``requeue-dead`` is how an operator overrides
        that deliberately.
        """

        now = _now()
        self.state.execute(self._publish_sql(), self._publish_params(task, now))
        return task

    def _publish_sql(self) -> str:
        extra = "".join(f"{name}, " for name in self.EXTRA_COLUMNS)
        # 10 lifecycle columns (id, payload, status, attempts, max_attempts,
        # owner, visible_at, enqueued_at, updated_at, last_error) plus whatever
        # this queue's rows carry of their own.
        slots = ",".join("?" for _ in range(10 + len(self.EXTRA_COLUMNS)))
        return (
            f"INSERT INTO {self.TABLE}("
            f"id, {extra}payload, status, attempts, max_attempts, owner, "
            "visible_at, enqueued_at, updated_at, last_error"
            f") VALUES({slots}) "
            "ON CONFLICT(id) DO UPDATE SET "
            "status=excluded.status, attempts=0, owner=NULL, "
            "payload=excluded.payload, max_attempts=excluded.max_attempts, "
            "visible_at=excluded.visible_at, enqueued_at=excluded.enqueued_at, "
            "updated_at=excluded.updated_at, last_error=NULL "
            f"WHERE {self.TABLE}.status=?"
        )

    def _publish_params(self, task: Any, now: datetime) -> tuple[Any, ...]:
        return (
            task.id,
            *self._extra_values(task),
            json.dumps(task.payload, sort_keys=True),
            PENDING,
            0,
            int(task.max_attempts),
            None,
            _iso(now),
            _iso(now),
            _iso(now),
            None,
            DONE,
        )

    def claim(
        self, owner: str, *, visibility_seconds: float = DEFAULT_VISIBILITY_SECONDS
    ) -> IndexTask | None:
        now = _now()
        deadline = _iso(now + timedelta(seconds=visibility_seconds))
        for _ in range(CLAIM_ATTEMPTS):
            # execute_returning, not rows: a claim is a *write*, and running
            # it through the read path leaves the transaction open — which on
            # SQLite holds the write lock against every other claimant and
            # makes the claim invisible to another process entirely.
            rows = self.state.execute_returning(
                self._claim_sql(),
                (
                    INFLIGHT,
                    owner,
                    deadline,
                    _iso(now),
                    PENDING,
                    INFLIGHT,
                    _iso(now),
                    PENDING,
                    INFLIGHT,
                    _iso(now),
                ),
            )
            if not rows:
                # Either the queue is empty or another worker won the row we
                # picked. Look again; a bounded loop distinguishes the two
                # without a second query.
                if not self._has_claimable(now):
                    return None
                continue
            task = self._build_task(rows[0])
            if task.attempts > task.max_attempts:
                self._dead_letter(task, "exceeded max_attempts before running")
                continue
            return task
        return None

    def _claim_sql(self) -> str:
        """The conditional UPDATE the class docstring's race argument is about.

        Built from :attr:`TABLE` and :attr:`EXTRA_COLUMNS` and nothing else --
        every predicate is a literal, so parameterizing the table cannot move a
        guard.
        """

        returning = "".join(f"{name}, " for name in self.EXTRA_COLUMNS)
        return (
            f"UPDATE {self.TABLE} SET status=?, owner=?, attempts=attempts+1, "
            "visible_at=?, updated_at=? WHERE id=("
            f"  SELECT id FROM {self.TABLE} WHERE status IN (?,?) AND visible_at<=? "
            "  ORDER BY enqueued_at, id LIMIT 1"
            ") AND status IN (?,?) AND visible_at<=? "
            f"RETURNING id, {returning}payload, attempts, max_attempts"
        )

    def _has_claimable(self, now: datetime) -> bool:
        rows = self.state.rows(
            f"SELECT COUNT(*) AS c FROM {self.TABLE} WHERE status IN (?,?) AND visible_at<=?",
            (PENDING, INFLIGHT, _iso(now)),
        )
        return bool(rows and int(rows[0]["c"]) > 0)

    def ack(self, task: IndexTask) -> None:
        self.state.execute(
            f"UPDATE {self.TABLE} SET status=?, owner=NULL, updated_at=?, last_error=NULL "
            "WHERE id=?",
            (DONE, _iso(_now()), task.id),
        )

    def nack(self, task: IndexTask, error: str, *, retry_in_seconds: float = 30.0) -> None:
        if task.attempts >= task.max_attempts:
            self._dead_letter(task, error)
            return
        now = _now()
        self.state.execute(
            f"UPDATE {self.TABLE} SET status=?, owner=NULL, visible_at=?, updated_at=?, "
            "last_error=? WHERE id=?",
            (
                PENDING,
                _iso(now + timedelta(seconds=max(0.0, retry_in_seconds))),
                _iso(now),
                error[:500],
                task.id,
            ),
        )

    def _dead_letter(self, task: IndexTask, error: str) -> None:
        logger.error(
            "%s task %s (%s) dead-lettered after %d attempt(s): %s",
            self.TABLE,
            task.id,
            getattr(task, "source_id", "-"),
            task.attempts,
            error,
        )
        self.state.execute(
            f"UPDATE {self.TABLE} SET status=?, owner=NULL, updated_at=?, last_error=? WHERE id=?",
            (DEAD, _iso(_now()), error[:500], task.id),
        )

    def heartbeat(
        self, task: IndexTask, *, visibility_seconds: float = DEFAULT_VISIBILITY_SECONDS
    ) -> None:
        now = _now()
        self.state.execute(
            f"UPDATE {self.TABLE} SET visible_at=?, updated_at=? WHERE id=? AND status=?",
            (_iso(now + timedelta(seconds=visibility_seconds)), _iso(now), task.id, INFLIGHT),
        )

    def depth(self) -> dict[str, int]:
        counts = {PENDING: 0, INFLIGHT: 0, DONE: 0, DEAD: 0}
        for row in self.state.rows(
            f"SELECT status, COUNT(*) AS c FROM {self.TABLE} GROUP BY status", ()
        ):
            counts[str(row["status"])] = int(row["c"])
        return counts

    def outstanding(self, limit: int = 50) -> list[dict[str, Any]] | None:
        """Pending, in-flight and dead rows, oldest first: the pre-claim backlog.

        Read straight off the claim index's leading column. Nothing here
        claims or touches a row, so a reader polling it cannot perturb the
        race argument above.
        """

        rows = self.state.rows(
            f"SELECT id, {', '.join(self.EXTRA_COLUMNS)}, status, attempts, max_attempts, "
            f"owner, visible_at, enqueued_at, updated_at, last_error FROM {self.TABLE} "
            "WHERE status IN (?,?,?) ORDER BY enqueued_at, id LIMIT ?",
            (PENDING, INFLIGHT, DEAD, max(1, int(limit))),
        )
        return [dict(row) for row in rows]

    def requeue_dead(self) -> int:
        """Replay dead-lettered tasks after the cause is fixed.

        One statement, not a read followed by an ``UPDATE`` per row: the same
        rows either way, in one commit rather than one per task.
        """

        now = _now()
        rows = self.state.execute_returning(
            f"UPDATE {self.TABLE} SET status=?, attempts=0, owner=NULL, visible_at=?, "
            "updated_at=? WHERE status=? RETURNING id",
            (PENDING, _iso(now), _iso(now), DEAD),
        )
        return len(rows)

    def purge_completed(self, older_than_seconds: float = 86_400.0) -> int:
        """Keep the table from growing without bound; failures are kept.

        One ``DELETE``, for the reason `requeue_dead` gives. The log tier's
        maintenance pass calls it (`log_queue._purge_completed_batches`),
        where every batch is a new row. Index tasks need no purge: their ids
        are content-addressed over (knowledge base, source, mode, payload), so
        a re-run re-arms an existing row rather than adding one.
        """

        cutoff = _iso(_now() - timedelta(seconds=max(0.0, older_than_seconds)))
        rows = self.state.execute_returning(
            f"DELETE FROM {self.TABLE} WHERE status=? AND updated_at<? RETURNING id",
            (DONE, cutoff),
        )
        return len(rows)


class NatsQueue(TaskQueue):
    """NATS JetStream, for a fleet that outgrows one database.

    Chosen over Redis Streams for durable acks, redelivery on ack timeout and
    a dead-letter path that is part of the product rather than a convention
    layered on top. The verbs map straight across — ``ack``/``nak``/
    ``in_progress`` are JetStream's own — which is why the seam is this small.

    A local queue is still the default. This buys fan-out across machines
    without a shared database; it does not buy correctness the local queue
    lacks, and it is one more thing to run.

    **A new stream uses work-queue retention**: a task is removed when it is
    acked, which is what a task is. The stream used to be created with the
    default ``limits`` retention and no limits, so every task ever published
    stayed on the broker's disk forever -- 50 of 50 acked tasks still stored,
    measured against a real server. ``max_age`` would have bounded it too and
    is the wrong tool: it expires a *pending* task whose backlog outlived it,
    and the dead letters, which are kept on purpose. JetStream cannot change
    an existing stream's retention, so a stream created by an earlier release
    keeps ``limits`` and says so once at connect.
    """

    #: Ceiling on establishing the connection. nats-py retries an initial
    #: connect ``max_reconnect_attempts`` times (60, two seconds apart), so
    #: with the broker unreachable a ``/metrics`` scrape or a ``claim`` sat in
    #: that loop for minutes -- measured: still blocked at 90s. Only the
    #: *initial* connect is bounded here; reconnecting an established
    #: connection keeps the client's own policy.
    CONNECT_DEADLINE_SECONDS = 10.0

    def __init__(
        self,
        servers: list[str],
        *,
        stream: str = "PHEASANT_INDEX",
        subject: str = "pheasant.index.tasks",
        durable: str = "pheasant-indexers",
        connect_timeout: float = 5.0,
    ) -> None:
        try:
            import nats  # noqa: F401
        except ImportError as exc:
            raise QueueUnavailable(
                "sync.queue.backend: nats needs the [queue] extra: pip install 'pheasant[queue]'"
            ) from exc
        self.servers = list(servers)
        self.stream = stream
        self.subject = subject
        self.dead_subject = f"{subject}.dead"
        self.durable = durable
        self.dead_durable = f"{durable}-dead"
        self.connect_timeout = float(connect_timeout)
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._js: Any = None
        self._subscription: Any = None
        self._dead_subscription: Any = None
        # ``sync_all`` may run several drain loops in a ThreadPoolExecutor.
        # asyncio event loops and nats-py subscriptions are not safe to drive
        # concurrently from those threads, so every loop/state transition is
        # marshalled through one re-entrant gate. Handlers still run outside
        # the gate, preserving source-level parallelism.
        self._loop_lock = threading.RLock()

    # JetStream's client is asyncio-only while the indexing engine is
    # threaded, so every call is marshalled onto one private event loop. A
    # thread per queue operation would be worse: this keeps exactly one — and
    # that one thread must actually keep running the loop between calls, not
    # just for the duration of each one.
    #
    # An earlier version spun the loop only via ``run_until_complete`` per
    # call, leaving it idle in between. nats-py's own background tasks —
    # the socket reader and its ping/keepalive coroutine, both created by
    # ``nats.connect()`` — only run while *something* is driving the loop,
    # so an indexer idle between ``claim()`` polls never processed the
    # server's own keepalive pings. Reproduced against a real broker
    # (``ping_interval: 1s, ping_max: 2``, a scaled-down analogue of
    # production's ``2min``/``2``): after 8s idle the connection was broken
    # server-side, and the client's own ``is_connected`` still incorrectly
    # reported ``True`` right up until the next call failed outright.
    #
    # The fix is to keep the loop itself running continuously on a
    # dedicated daemon thread (``run_forever``, not spun per call), and
    # marshal each call in with ``run_coroutine_threadsafe`` instead of
    # ``run_until_complete`` — verified against the same broker to survive
    # the identical idle window.
    def _run(self, coroutine: Any) -> Any:
        import asyncio

        with self._loop_lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever,
                    name="pheasant-nats-queue-loop",
                    daemon=True,
                )
                self._thread.start()
            loop = self._loop
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

    def _connect(self) -> Any:
        with self._loop_lock:
            if self._js is not None:
                if self._client is None or not getattr(self._client, "is_closed", False):
                    return self._js
                # The client gave up reconnecting (its own retry budget ran
                # out during a long outage). A cached handle to it would fail
                # every call until the process restarted; start again.
                self._js = None
                self._subscription = None
                self._dead_subscription = None

            async def setup() -> Any:
                import asyncio

                import nats

                try:
                    self._client = await asyncio.wait_for(
                        nats.connect(servers=self.servers, connect_timeout=self.connect_timeout),
                        timeout=max(self.connect_timeout, self.CONNECT_DEADLINE_SECONDS),
                    )
                except Exception as exc:
                    raise QueueUnavailable(
                        f"JetStream at {', '.join(self.servers)} is unreachable: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                js = self._client.jetstream()
                try:
                    from nats.js.api import RetentionPolicy

                    await js.add_stream(
                        name=self.stream,
                        subjects=[self.subject, self.dead_subject],
                        retention=RetentionPolicy.WORK_QUEUE,
                    )
                except Exception:
                    # Already provisioned by another indexer, which is the normal
                    # case in a fleet and not worth distinguishing.
                    logger.debug("JetStream stream %s already exists", self.stream)
                    try:
                        info = await js.stream_info(self.stream)
                        retention = str(getattr(info.config, "retention", "") or "")
                        if "limits" in retention.lower():
                            logger.info(
                                "JetStream stream %s uses limits retention, so acked "
                                "tasks are never removed; recreate it (drained) to "
                                "get work-queue retention",
                                self.stream,
                            )
                        subjects = set(getattr(info.config, "subjects", None) or [])
                        wanted = {self.subject, self.dead_subject}
                        if not wanted.issubset(subjects):
                            await js.update_stream(
                                name=self.stream,
                                subjects=sorted(subjects | wanted),
                            )
                    except Exception:
                        logger.warning(
                            "Could not add explicit dead-letter subject %s to stream %s",
                            self.dead_subject,
                            self.stream,
                            exc_info=True,
                        )
                return js

            self._js = self._run(setup())
            return self._js

    def publish(self, task: IndexTask) -> IndexTask:
        js = self._connect()
        body = json.dumps(task.as_dict(), sort_keys=True).encode("utf-8")

        async def send() -> None:
            # A retry of this task object is de-duplicated, while a new task
            # object with the same logical id is a new requested run. Using
            # ``task.id`` here made JetStream silently suppress every rapid
            # re-run for its default two-minute duplicate window.
            await js.publish(self.subject, body, headers={"Nats-Msg-Id": task.publish_id})

        self._run(send())
        return task

    def _subscribe(self) -> None:
        """Create the durable consumer if it does not exist yet.

        Called from :meth:`depth` as well as :meth:`claim`, because a
        consumer that only appears once someone claims makes the queue-depth
        gauge read zero while a backlog is sitting there — an autoscaler
        would never scale *up*, which is the one thing it exists to do.
        """

        with self._loop_lock:
            if self._subscription is not None:
                return
            js = self._connect()

            async def subscribe() -> Any:
                return await js.pull_subscribe(self.subject, durable=self.durable)

            self._subscription = self._run(subscribe())

    def _subscribe_dead(self) -> None:
        """Create the durable DLQ view used by depth and explicit replay."""

        with self._loop_lock:
            if self._dead_subscription is not None:
                return
            js = self._connect()

            async def subscribe() -> Any:
                return await js.pull_subscribe(
                    self.dead_subject,
                    durable=self.dead_durable,
                )

            self._dead_subscription = self._run(subscribe())

    def claim(
        self, owner: str, *, visibility_seconds: float = DEFAULT_VISIBILITY_SECONDS
    ) -> IndexTask | None:
        self._subscribe()

        async def pull() -> Any:
            try:
                messages = await self._subscription.fetch(1, timeout=1)
            except Exception:
                return None
            return messages[0] if messages else None

        for _ in range(CLAIM_ATTEMPTS):
            message = self._run(pull())
            if message is None:
                return None
            raw = json.loads(message.data.decode("utf-8"))
            task = IndexTask(
                id=str(raw.get("id") or uuid.uuid4().hex),
                source_id=str(raw["source"]),
                mode=str(raw.get("mode") or "incremental"),
                payload=dict(raw.get("payload") or {}),
                attempts=int(message.metadata.num_delivered or 1),
                max_attempts=int(raw.get("max_attempts") or DEFAULT_MAX_ATTEMPTS),
                handle=message,
            )
            if task.attempts > task.max_attempts:
                # Delivered more times than it may be attempted, so earlier
                # deliveries ended without a nack: the claimer died holding
                # it. The attempt cap lived only in `nack`, which a crash
                # never reaches, so a task that kills its worker was
                # redelivered forever -- the consumer's `max_deliver` is
                # unlimited. The same check `LocalQueue.claim` makes, and the
                # same outcome: a dead letter someone can replay, rather than
                # a consumer-side cap that stops delivering without one.
                self.nack(task, "exceeded max_attempts before running")
                continue
            return task
        return None

    def ack(self, task: IndexTask) -> None:
        if task.handle is not None:
            self._run(task.handle.ack())

    def nack(self, task: IndexTask, error: str, *, retry_in_seconds: float = 30.0) -> None:
        if task.handle is None:
            return
        if task.attempts >= task.max_attempts:
            # `term` stops redelivery for good — JetStream's dead letter.
            logger.error(
                "Index task %s for source %s terminated after %d delivery(s): %s",
                task.id,
                task.source_id,
                task.attempts,
                error,
            )
            sequence = getattr(
                getattr(getattr(task.handle, "metadata", None), "sequence", None),
                "stream",
                0,
            )
            dead = {
                **task.as_dict(),
                "attempts": task.attempts,
                "dead_letter": {
                    "error": str(error),
                    "failed_at": _iso(_now()),
                    "deliveries": task.attempts,
                },
            }

            async def publish_dead() -> None:
                await self._connect().publish(
                    self.dead_subject,
                    json.dumps(dead, sort_keys=True).encode("utf-8"),
                    headers={"Nats-Msg-Id": f"dlq:{self.stream}:{sequence or task.id}"},
                )

            # Never term first: if the broker cannot durably accept the DLQ
            # record, leave the main task eligible for recovery.
            self._run(publish_dead())
            self._run(task.handle.term())
            return
        self._run(task.handle.nak(delay=max(0.0, retry_in_seconds)))

    def heartbeat(
        self, task: IndexTask, *, visibility_seconds: float = DEFAULT_VISIBILITY_SECONDS
    ) -> None:
        if task.handle is not None:
            self._run(task.handle.in_progress())

    def depth(self) -> dict[str, int]:
        """Unprocessed work, from the *consumer* rather than the stream.

        A stream's message count does not drop on ack under the default
        ``limits`` retention, so scraping it would report a queue that only
        ever grows — an autoscaling signal that scales up and never down. The
        durable consumer's ``num_pending`` is the number actually waiting, and
        ``num_ack_pending`` is what is being worked on right now.
        """

        async def info() -> Any:
            js = self._js
            main = await js.consumer_info(self.stream, self.durable)
            dead = await js.consumer_info(self.stream, self.dead_durable)
            return main, dead

        try:
            self._connect()
            self._subscribe()
            self._subscribe_dead()
            consumer, dead_consumer = self._run(info())
        except Exception as exc:
            # Raised, never reported as zeros. An empty queue is a reading an
            # autoscaler acts on -- it scales the fleet down -- and it used to
            # be what an unreachable broker produced, so an outage looked like
            # a drained backlog. Every caller already treats an exception as
            # "unknown": `/metrics` leaves the gauge out of that scrape.
            raise QueueUnavailable(f"could not read JetStream depth: {exc}") from exc
        return {
            PENDING: int(getattr(consumer, "num_pending", 0) or 0),
            INFLIGHT: int(getattr(consumer, "num_ack_pending", 0) or 0),
            DONE: int(
                getattr(consumer, "ack_floor", None) and consumer.ack_floor.consumer_seq or 0
            ),
            DEAD: int(getattr(dead_consumer, "num_pending", 0) or 0)
            + int(getattr(dead_consumer, "num_ack_pending", 0) or 0),
        }

    def requeue_dead(self) -> int:
        """Replay every explicit DLQ record after its cause has been fixed."""

        self._subscribe_dead()
        replayed = 0
        while True:

            async def pull() -> list[Any]:
                try:
                    return await self._dead_subscription.fetch(32, timeout=0.25)
                except Exception:
                    return []

            messages = self._run(pull())
            if not messages:
                return replayed
            for message in messages:
                raw = json.loads(message.data.decode("utf-8"))
                sequence = getattr(
                    getattr(getattr(message, "metadata", None), "sequence", None),
                    "stream",
                    replayed,
                )
                task = IndexTask(
                    id=str(raw.get("id") or uuid.uuid4().hex),
                    source_id=str(raw["source"]),
                    mode=str(raw.get("mode") or "incremental"),
                    payload=dict(raw.get("payload") or {}),
                    attempts=0,
                    max_attempts=int(raw.get("max_attempts") or DEFAULT_MAX_ATTEMPTS),
                    publish_id=f"requeue:{self.stream}:{sequence}",
                )
                self.publish(task)
                self._run(message.ack())
                replayed += 1

    def close(self) -> None:
        if self._client is None:
            return

        async def shutdown() -> None:
            import asyncio

            await self._client.close()
            # nats-py leaves its ping/flusher/subscription tasks running on
            # the loop. Closing the loop under them raises "Event loop is
            # closed" from their own teardown — noise from a clean shutdown,
            # which is worse than useless in a log. Cancel and reap them.
            pending = [
                task for task in asyncio.all_tasks(self._loop) if task is not asyncio.current_task()
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        try:
            self._run(shutdown())
        except Exception:  # pragma: no cover - already closed
            logger.debug("NATS close failed", exc_info=True)
        finally:
            if self._loop is not None:
                # The loop is running continuously on its own thread now
                # (``run_forever``), not just for the duration of the last
                # call — so it must be stopped and that thread joined
                # *before* any further method is called on it directly.
                # ``run_until_complete``/``close()`` are only safe once
                # ``run_forever`` has actually returned; calling them while
                # another thread is still driving the loop raises.
                self._loop.call_soon_threadsafe(self._loop.stop)
                if self._thread is not None:
                    self._thread.join(timeout=5.0)
                try:
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                except Exception:  # pragma: no cover - loop already unusable
                    pass
                self._loop.close()
            self._client = None
            self._js = None
            self._loop = None
            self._thread = None
            self._subscription = None
            self._dead_subscription = None


class QueueUnavailable(RuntimeError):
    """A queue backend was selected whose dependency is not installed."""


def queue_from_config(config: Any, state: Any) -> TaskQueue | None:
    """Build the configured queue, or ``None`` when queuing is off.

    ``None`` is the default and means ``sync_all`` behaves exactly as it did
    before this module existed — the rule-7 escape hatch, checked in one
    place so no caller has to remember it.
    """

    settings = getattr(config.sync, "queue", None)
    if settings is None or not getattr(settings, "enabled", False):
        return None
    backend = str(getattr(settings, "backend", "local") or "local").lower()
    if backend == "nats":
        return NatsQueue(
            list(settings.nats_servers or ["nats://127.0.0.1:4222"]),
            stream=settings.nats_stream,
            subject=settings.nats_subject,
            durable=settings.nats_durable,
        )
    if backend != "local":
        logger.warning("Unknown sync.queue.backend=%r; using the local queue", backend)
    return LocalQueue(state)


def drain(
    queue: TaskQueue,
    handler: Any,
    *,
    owner: str | None = None,
    max_tasks: int | None = None,
    idle_timeout: float = 0.0,
    poll_interval: float = 0.5,
    visibility_seconds: float = DEFAULT_VISIBILITY_SECONDS,
) -> list[Any]:
    """Claim and run tasks until the queue is empty (or ``max_tasks``).

    One loop serves both callers: ``sync_all`` runs it with
    ``idle_timeout=0`` so it returns the moment the backlog is clear, and a
    long-lived indexer runs it with a timeout so it waits for more work. That
    is the whole difference between the two roles.

    A handler that raises nacks its task with backoff and the loop continues:
    one unindexable source must not stop the other nine, which is precisely
    what the in-memory list could not do.
    """

    claimant = owner or owner_id()
    results: list[Any] = []
    idle_since: float | None = None
    while max_tasks is None or len(results) < max_tasks:
        task = queue.claim(claimant, visibility_seconds=visibility_seconds)
        if task is None:
            if idle_timeout <= 0:
                break
            if idle_since is None:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= idle_timeout:
                break
            time.sleep(poll_interval)
            continue
        idle_since = None
        with _keepalive(queue, task, visibility_seconds):
            try:
                results.append(handler(task))
            except Exception as exc:  # noqa: BLE001 - one bad source must not stop the rest
                logger.exception("Task %s (%s) failed", task.id, getattr(task, "source_id", "-"))
                queue.nack(task, f"{type(exc).__name__}: {exc}")
                continue
        queue.ack(task)
    return results


@contextmanager
def _keepalive(queue: TaskQueue, task: IndexTask, visibility_seconds: float):
    """Extend the task's visibility for as long as the handler is running.

    ``heartbeat`` existed on every backend and had **no caller**, so a task
    stayed claimed for exactly ``visibility_seconds`` no matter how long the
    work took. Indexing a real source is minutes to hours and the default
    visibility is 300s, so the queue would hand the same source to a second
    worker while the first was still mid-sync — the redelivery path built for
    a *dead* worker, firing on a healthy one. Indexing is idempotent so the
    result would still be correct, but two writers on one source is the
    contention 35.4's per-source leases exist to prevent, and the wasted pass
    is the throughput the whole phase is about.

    A thread, because ``handler`` is an opaque blocking call. Failures are
    logged and swallowed: a missed ping costs a redelivery, whereas raising
    here would fail a sync that is going fine.
    """

    interval = _heartbeat_interval(visibility_seconds)
    done = threading.Event()

    def beat() -> None:
        while not done.wait(interval):
            try:
                queue.heartbeat(task, visibility_seconds=visibility_seconds)
            except Exception:  # noqa: BLE001 - a missed ping costs a redelivery, not a sync
                logger.debug("Heartbeat failed for index task %s", task.id, exc_info=True)

    thread = threading.Thread(target=beat, name=f"pheasant-queue-heartbeat-{task.id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join(timeout=5.0)


def _heartbeat_interval(visibility_seconds: float) -> float:
    return min(MAX_HEARTBEAT_INTERVAL_SECONDS, max(1.0, visibility_seconds / 3.0))
