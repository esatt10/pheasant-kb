"""The observation plane: what was asked, on which surface, by whom, and what
came back.

An **observation is not a memory record and must never become one.** A row here
is never a file, never chunked, never indexed, and never returned by a search --
a UI session's chat does not become knowledge because it was observed. The only
path from here into memory is a *candidate* that something admits, and admission
goes through :meth:`pheasant.artifacts.store.MemoryStore.append` like every other
write, so memory's first invariant ("records are files, no second ingestion
path") never bends. See ``docs/memory-formation.md``.

Three things in this module are load-bearing and easy to get wrong:

**The request path does a bounded in-memory handoff and nothing else.** Writing
one row per request into the region's own database puts a write on the same
PostgreSQL the lexical search arm contends on, and ``docs/architecture.md``
names high-frequency PostgreSQL text ranking as the dominant measured search
bottleneck. Making that write fail-soft protects correctness and does nothing
for latency. So a request appends one dataclass to a bounded deque; a flusher
thread batches and hands the batch on.

**Under pressure this loses data, not latency.** The buffer is bounded and the
queue depth is bounded, and crossing either drops observations rather than
blocking or growing without limit -- the same posture ``bound_concurrency``
takes when it answers 429 under saturation. It has a real consequence worth
stating where someone will read it: every memory-formation threshold counts a
stream that is thinned under load, so a busy region forms memory *more slowly*,
not incorrectly.

**Trace and span ids exist with or without OpenTelemetry.** They are the event's
primary key, so they cannot be optional. Without the ``[otel]`` extra this module
mints W3C-shaped ids itself and the ledger works unchanged; with it, the ids come
from the real span context, so a row here and a span in the operator's collector
name the same call. That keeps CLAUDE.md rule 7 -- an infrastructure-free
pheasant is unaffected either way -- and it is why ``pytest`` stays network-free
by construction rather than by mocking: an exporter is attached only when
``observability.otlp_endpoint`` is set, and the default is ``None``.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import blake2b
from pathlib import Path
from typing import Any, Protocol

from pheasant.telemetry.metrics import REGISTRY

logger = logging.getLogger(__name__)

#: Bumped when a column is removed, renamed or retyped -- never when one is
#: added, so a reader written against version 1 keeps working as the ledger
#: grows. Same contract the Parquet export's ``format_version`` makes.
INTERACTION_SCHEMA_VERSION = 1

#: Column order for the hot-store insert. The single source of truth for it:
#: :data:`INSERT_SQL`, the Parquet roll and :meth:`InteractionEvent.as_row` all
#: derive from this tuple rather than repeating it, because three hand-written
#: column lists is three chances for one to drift.
COLUMNS: tuple[str, ...] = (
    "id",
    "kb_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "modality",
    "operation",
    "principal",
    "session_id",
    "client_id",
    "started_at",
    "duration_ms",
    "status",
    "query_text",
    "answer_text",
    "criteria_json",
    "result_ids_json",
    "result_paths_json",
    "result_count",
    "top_score",
    "attributes_json",
    "schema_version",
)

#: ``ON CONFLICT DO NOTHING``, not ``INSERT OR IGNORE``: the second is
#: SQLite-only, and this exact substitution is a bug this codebase has already
#: shipped and fixed once (see CLAUDE.md's backend-parity note). It is also what
#: makes at-least-once redelivery of a batch free rather than duplicating rows,
#: since :func:`event_id` is content-addressed.
INSERT_SQL = (
    f"INSERT INTO interaction_events({','.join(COLUMNS)}) "
    f"VALUES({','.join('?' for _ in COLUMNS)}) "
    "ON CONFLICT (id) DO NOTHING"
)


#: What a caller may claim it is talking to us over. Bounded on purpose: this
#: becomes a Prometheus label, and it is the ``access modality`` dimension the
#: formation rules slice on.
class Modality(StrEnum):
    UI = "ui"
    MCP = "mcp"
    A2A = "a2a"
    CLI = "cli"


#: How a call ended. ``shed`` is distinct from ``error`` because a 429 under
#: saturation is the system working as designed, and lumping the two together
#: would make an overload look like a fault.
VALID_STATUS = ("ok", "error", "shed")

#: ``00-<32 hex trace>-<16 hex span>-<2 hex flags>``. Parsed leniently on
#: purpose -- an unparseable inbound header means "start a new trace", never
#: "fail the request".
_TRACEPARENT_RE = re.compile(
    r"^[0-9a-f]{2}-(?P<trace>[0-9a-f]{32})-(?P<span>[0-9a-f]{16})-[0-9a-f]{2}$"
)

_ALL_ZERO_TRACE = "0" * 32
_ALL_ZERO_SPAN = "0" * 16


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def parse_traceparent(header: str | None) -> tuple[str, str] | None:
    """``(trace_id, parent_span_id)`` from a W3C ``traceparent``, or ``None``.

    Lets an agent's own trace stitch to ours rather than starting a second,
    unrelated one for the same logical call. All-zero ids are rejected: the
    spec reserves them as invalid, and accepting one would collapse every
    such caller into a single trace.
    """

    if not header:
        return None
    match = _TRACEPARENT_RE.match(header.strip().lower())
    if match is None:
        return None
    trace, span = match.group("trace"), match.group("span")
    if trace == _ALL_ZERO_TRACE or span == _ALL_ZERO_SPAN:
        return None
    return trace, span


#: The trace the calling thread is inside, if any.
#:
#: A context variable rather than an argument because the alternative is
#: threading `(trace_id, span_id)` through every call between a request
#: handler and an outbound HTTP hop -- a signature change on code that has no
#: other reason to know about tracing, which is how propagation gets dropped
#: the first time someone adds a call site.
#:
#: Note it does **not** cross a bare `threading.Thread`: contextvars follow
#: asyncio tasks, not raw threads. That is why the sync path (which prepares
#: files on an executor) is not propagated through here -- a sync has no
#: ambient interaction trace to begin with, so the helpers below return None
#: there and every call site is a harmless no-op until syncs are traced too.
_CURRENT_TRACE: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "pheasant_current_trace", default=None
)


#: The event being filled in for the call running on this context, when one
#: is. Ambient for the same reason the trace is: retrieval sits several frames
#: below the handler that owns the row, and threading an event object through
#: `search_context` would put a telemetry parameter on the signature every
#: caller — HTTP, MCP, CLI, the assistant, the tuning replay — has to pass.
#:
#: Follows the same rule as the trace and inherits the same limitation: it does
#: not cross a bare `threading.Thread`, so a handler that hands retrieval to a
#: raw thread annotates nothing rather than annotating the wrong row.
_CURRENT_EVENT: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "pheasant_current_interaction_event", default=None
)


def current_event() -> Any:
    """The interaction event this call is filling in, or ``None``."""

    return _CURRENT_EVENT.get()


def annotate_current(key: str, value: Any) -> bool:
    """Add one attribute to the ambient event. ``True`` if there was one.

    Best-effort and deliberately quiet: observation being off, or a caller
    running outside an observed handler, is the ordinary case rather than an
    anomaly — and a retrieval path that raised because nothing was listening
    would be a diagnostic that costs queries.
    """

    event = _CURRENT_EVENT.get()
    if event is None:
        return False
    try:
        event.attributes[key] = value
    except Exception:  # noqa: BLE001 - annotation must never fail a call
        return False
    return True


def current_trace() -> tuple[str, str] | None:
    """``(trace_id, span_id)`` for the call in progress, or ``None``."""

    return _CURRENT_TRACE.get()


def traceparent_header(sampled: bool = True) -> str | None:
    """The W3C ``traceparent`` for the call in progress, or ``None``.

    What makes a trace survive pheasant's own service hops. Without it a
    trace stops dead at the region's boundary: an operator sees "search took
    four seconds" and cannot see that most of it was one graph-query call to
    another pod.
    """

    trace = current_trace()
    if trace is None:
        return None
    trace_id, span_id = trace
    return f"00-{trace_id}-{span_id}-{'01' if sampled else '00'}"


def inject_traceparent(headers: dict[str, str]) -> dict[str, str]:
    """Add ``traceparent`` to an outbound header dict, when there is one.

    Mutates and returns, so a call site reads as one line beside the other
    headers it already sets.
    """

    header = traceparent_header()
    if header:
        headers["traceparent"] = header
    return headers


@contextmanager
def adopt_trace(traceparent: str | None) -> Iterator[None]:
    """Run a block inside a trace that arrived from somewhere else.

    What links a request to the work it queued. `POST /sync` publishes a task
    carrying its `traceparent`; the indexer that claims it -- a different
    process, possibly minutes later -- runs the sync inside that trace, so the
    preparation calls it makes onward carry it too. Without this the chain
    breaks at the queue and an operator sees two unrelated traces for one
    thing a person asked for.

    An unparseable value is not an error: it means "no trace", and a sync must
    never fail because a header was malformed.
    """

    parsed = parse_traceparent(traceparent)
    if parsed is None:
        yield
        return
    trace_id, span_id = parsed
    token = _CURRENT_TRACE.set((trace_id, span_id))
    try:
        yield
    finally:
        _CURRENT_TRACE.reset(token)


def event_id(trace_id: str, span_id: str) -> str:
    """The row's primary key: content-addressed on the span it describes.

    Making the id a function of the span rather than a random value is what
    turns at-least-once batch redelivery from a duplicate-row problem into a
    no-op, and it means the same call observed twice (a retry inside one
    process, say) collapses instead of double-counting a formation threshold.
    """

    return blake2b(f"{trace_id}|{span_id}".encode(), digest_size=16).hexdigest()


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class InteractionContext:
    """Who is calling, over what, in which session.

    Every field is **asserted by the caller**, exactly as ``principal`` already
    is everywhere else in pheasant -- there is no authentication here and this
    module does not pretend otherwise. The consequence is recorded as a known
    limit in ``docs/memory-formation.md``: one caller can claim another's
    session, the same category as "``supersedes`` is not authorization-checked".
    """

    modality: Modality = Modality.UI
    principal: str | None = None
    session_id: str | None = None
    client_id: str | None = None
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str | None = None

    @classmethod
    def create(
        cls,
        modality: Modality | str = Modality.UI,
        *,
        principal: str | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        traceparent: str | None = None,
    ) -> InteractionContext:
        inbound = parse_traceparent(traceparent)
        trace_id, parent = inbound if inbound else (new_trace_id(), None)
        try:
            resolved = Modality(str(modality))
        except ValueError:
            # An unknown surface name is data, not a crash: this is reached
            # from a request header on an unauthenticated API.
            resolved = Modality.UI
        return cls(
            modality=resolved,
            principal=principal or None,
            session_id=session_id or None,
            client_id=client_id or None,
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=parent,
        )


@dataclass
class InteractionEvent:
    """One observed call. Mutable while a handler is still filling it in."""

    kb_id: str
    operation: str
    modality: str = Modality.UI.value
    principal: str | None = None
    session_id: str | None = None
    client_id: str | None = None
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str | None = None
    started_at: str = ""
    duration_ms: float | None = None
    status: str = "ok"
    query_text: str | None = None
    #: The assistant's generated answer, when this call produced one. Capped
    #: by `observability.interactions.max_answer_chars` (0 = never recorded),
    #: because model output runs 10-50x a question's bytes and would
    #: otherwise let chat traffic dominate a ledger sized for search.
    answer_text: str | None = None
    criteria: dict[str, Any] | None = None
    #: Stable node ids -- what joins to `graph_nodes` and, through them, to
    #: `chunks`. Deliberately homogeneous: a rule that has to sniff whether a
    #: value is an id or a path is a rule that behaves differently per
    #: surface, which is the opposite of deterministic.
    result_ids: list[str] = field(default_factory=list)
    #: Source-relative paths, in the same grammar `steering` matches against
    #: (`relative_path`, not the absolute path). `path-affinity-v1` mints a
    #: `preference` rule from these, so the two must agree or the rule it
    #: writes cannot fire.
    result_paths: list[str] = field(default_factory=list)
    result_count: int | None = None
    top_score: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    schema_version: int = INTERACTION_SCHEMA_VERSION

    @property
    def id(self) -> str:
        return event_id(self.trace_id, self.span_id)

    @property
    def is_writable(self) -> bool:
        """Can this become a row without violating a NOT NULL column?

        Load-bearing, and found by a test rather than by reading: a batch is
        inserted inside one transaction, so a single event carrying a null
        ``trace_id`` -- which is what a truncated spool line or a garbled
        queue payload produces -- raises ``IntegrityError`` and rolls back
        **every other event in the batch**. The batch then nacks, retries,
        fails identically, and dead-letters. One bad line would cost hundreds
        of good observations.

        Checked here rather than caught at the insert because the fix has to
        drop the one bad event and keep the rest, which a rolled-back
        transaction cannot do.
        """

        return all(
            isinstance(value, str) and value
            for value in (
                self.kb_id,
                self.operation,
                self.trace_id,
                self.span_id,
                self.started_at,
                self.status,
                str(self.modality),
            )
        )

    def as_row(self) -> tuple[Any, ...]:
        return (
            self.id,
            self.kb_id,
            self.trace_id,
            self.span_id,
            self.parent_span_id,
            str(self.modality),
            self.operation,
            self.principal,
            self.session_id,
            self.client_id,
            self.started_at,
            self.duration_ms,
            self.status,
            self.query_text,
            self.answer_text,
            json.dumps(self.criteria, sort_keys=True) if self.criteria else None,
            json.dumps(self.result_ids) if self.result_ids else None,
            json.dumps(self.result_paths) if self.result_paths else None,
            self.result_count,
            self.top_score,
            json.dumps(self.attributes, sort_keys=True) if self.attributes else None,
            int(self.schema_version),
        )

    def as_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> InteractionEvent:
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        known.setdefault("kb_id", "")
        known.setdefault("operation", "")
        return cls(**known)


# --------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------


class InteractionSink(Protocol):
    """Where a flushed batch goes.

    Which implementation is used is decided by a **capability probe, not a
    config switch** (:func:`resolve_sink`): an API replica in the shipped fleet
    can write directly because the fleet runs PostgreSQL, while the same code
    on a read-only ``/state`` under SQLite must not try. Asking the operator to
    configure that correctly per role would be asking them to know which
    deployment shape they are in.
    """

    name: str

    def write(self, events: Sequence[InteractionEvent]) -> int:
        """Persist a batch. Returns how many were accepted."""

    def close(self) -> None: ...


class NullSink:
    """Nowhere writable and no spool. Counts what it drops, warns once."""

    name = "null"

    def __init__(self, reason: str = "no_sink") -> None:
        self._reason = reason
        self._warned = False

    def write(self, events: Sequence[InteractionEvent]) -> int:
        if not self._warned and events:
            logger.warning(
                "Interaction observation is enabled but nothing here can persist it "
                "(%s). Events are being dropped. In a fleet, either run PostgreSQL "
                "so every replica can write, enable observability.interactions."
                "queue, or set observability.interactions.spool_path.",
                self._reason,
            )
            self._warned = True
        _dropped(self._reason, len(events))
        return 0

    def close(self) -> None:
        return None


class StateSink:
    """Write straight to the hot store. The single-container path, and the
    fleet path whenever the backend is PostgreSQL."""

    name = "state"

    def __init__(self, state: Any) -> None:
        self._state = state

    def write(self, events: Sequence[InteractionEvent]) -> int:
        if not events:
            return 0
        rows = [event.as_row() for event in events]
        with self._state.conn:
            for row in rows:
                self._state.conn.execute(INSERT_SQL, row)
        return len(rows)

    def close(self) -> None:
        return None


class QueueSink:
    """Publish the batch and let a ``--role logger`` persist it.

    The reason the tier exists: persistence, rolling and cold compaction all
    move off whatever process served the request, and off the indexer's
    ``sync_lock``.
    """

    name = "queue"

    def __init__(self, queue: Any, *, kb_id: str, max_depth: int = 0) -> None:
        self._queue = queue
        self._kb_id = kb_id
        self._max_depth = max_depth

    def _over_depth(self) -> bool:
        """Refuse to publish into an already-drowning queue.

        Without this, a stalled log tier turns a bounded memory buffer into an
        unbounded table -- the same failure wearing a different hat. Depth is
        read best-effort: a backend that cannot answer is not a reason to stop
        recording.
        """

        if self._max_depth <= 0:
            return False
        try:
            depth = self._queue.depth() or {}
        except Exception:  # noqa: BLE001 - depth is advisory, never load-bearing
            return False
        pending = int(depth.get("pending", 0)) + int(depth.get("inflight", 0))
        return pending >= self._max_depth

    def write(self, events: Sequence[InteractionEvent]) -> int:
        if not events:
            return 0
        if self._over_depth():
            _dropped("queue_full", len(events))
            return 0
        payload = {
            "kind": "interactions",
            "kb_id": self._kb_id,
            "events": [event.as_json() for event in events],
        }
        # Content-addressed so a republished batch dedups in the queue, the
        # same property `worker_pool`'s idempotency key gives remote
        # preparation.
        digest = blake2b(
            "|".join(sorted(event.id for event in events)).encode(), digest_size=12
        ).hexdigest()
        self._queue.publish_batch(f"log-{digest}", payload)
        return len(events)

    def close(self) -> None:
        return None


class SpoolSink:
    """Append NDJSON for the indexer to ingest later.

    The degraded path for a custom SQLite multi-process deployment, where
    ``/state`` is read-only on a replica and no queue is configured. Reuses the
    shape :mod:`pheasant.synapse.events` already uses -- one JSON object per
    line, a file per day, and a write failure that is swallowed rather than
    raised, because an observation must never fail a request.

    The shipped fleet needs none of this: it runs PostgreSQL, so
    :class:`StateSink` covers every replica.
    """

    name = "spool"

    def __init__(self, root: Path, *, owner: str) -> None:
        self._root = Path(root)
        self._owner = re.sub(r"[^A-Za-z0-9_.-]", "-", owner)[:64] or "replica"

    def _path(self, when: datetime) -> Path:
        return self._root / self._owner / f"{when:%Y-%m-%d}.ndjson"

    def write(self, events: Sequence[InteractionEvent]) -> int:
        if not events:
            return 0
        path = self._path(utc_now())
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event.as_json(), sort_keys=True) + "\n")
        return len(events)

    def close(self) -> None:
        return None


def read_spool(root: Path) -> Iterator[tuple[Path, list[InteractionEvent]]]:
    """Every spool file under ``root``, oldest first, with its parsed events.

    A malformed line is skipped rather than raised: a spool is written by a
    process that may have been killed mid-line, and one truncated record must
    not strand every batch behind it.
    """

    root = Path(root)
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.ndjson")):
        events: list[InteractionEvent] = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = InteractionEvent.from_json(json.loads(line))
            except (ValueError, TypeError):
                continue
            if event.is_writable:
                events.append(event)
        yield path, events


def resolve_sink(
    settings: Any,
    *,
    state: Any = None,
    queue: Any = None,
    kb_id: str = "",
    state_writable: bool = True,
    owner: str = "replica",
) -> InteractionSink:
    """Pick a sink by what this process can actually do.

    Order is deliberate. A configured queue wins, because handing work to the
    log tier is the whole point of having one. Then a writable state store.
    Then a spool. Then nothing, loudly.
    """

    if queue is not None:
        return QueueSink(queue, kb_id=kb_id, max_depth=int(getattr(settings, "max_queue_depth", 0)))
    if state is not None and state_writable:
        return StateSink(state)
    spool = getattr(settings, "spool_path", None)
    if spool:
        return SpoolSink(Path(spool), owner=owner)
    return NullSink("state_read_only" if state is not None else "no_state")


def _dropped(reason: str, count: int = 1) -> None:
    if count > 0:
        REGISTRY.inc("pheasant_interaction_events_dropped_total", float(count), reason=reason)


# --------------------------------------------------------------------------
# The buffer
# --------------------------------------------------------------------------


class InteractionBuffer:
    """The bounded ring every observation passes through.

    :meth:`record` is the only thing a request thread ever calls, and it does
    one bounded append. Everything else -- batching, persistence, retries --
    happens on the flusher thread or further down the tier.
    """

    def __init__(
        self,
        sink: InteractionSink,
        *,
        capacity: int = 10_000,
        batch_size: int = 500,
        interval_seconds: float = 5.0,
    ) -> None:
        self._sink = sink
        self._capacity = max(1, int(capacity))
        self._batch_size = max(1, int(batch_size))
        self._interval = max(0.1, float(interval_seconds))
        self._events: deque[InteractionEvent] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- the request path --------------------------------------------------

    def record(self, event: InteractionEvent) -> bool:
        """Buffer one observation. ``False`` means it was dropped.

        Never raises and never blocks. A full buffer drops the *oldest* event:
        under sustained overload the recent past is the more useful half, and
        dropping the newest would make the ledger's tail systematically stale
        exactly when something is going wrong.
        """

        try:
            with self._lock:
                overflow = len(self._events) >= self._capacity
                if overflow:
                    self._events.popleft()
                self._events.append(event)
                depth = len(self._events)
                ready = depth >= self._batch_size
            REGISTRY.set("pheasant_interaction_buffer_depth", float(depth))
            REGISTRY.inc(
                "pheasant_interaction_events_total",
                modality=str(event.modality),
                operation=event.operation,
                status=event.status,
            )
            if overflow:
                _dropped("buffer_full")
            if ready:
                # Wake the flusher rather than flushing here: a request thread
                # must not pay for a batch write.
                self._wake()
            return not overflow
        except Exception:  # noqa: BLE001 - an observation must never fail a request
            _dropped("error")
            return False

    # -- the flusher -------------------------------------------------------

    def _drain(self, limit: int) -> list[InteractionEvent]:
        with self._lock:
            batch = [self._events.popleft() for _ in range(min(limit, len(self._events)))]
            depth = len(self._events)
        REGISTRY.set("pheasant_interaction_buffer_depth", float(depth))
        return batch

    def flush(self) -> int:
        """Hand everything buffered to the sink. Returns how many were written.

        A sink failure costs the batch. That is the deliberate trade: retrying
        in here would mean either blocking the flusher (which backs up the ring
        and drops *more*) or an unbounded retry list (which is the unbounded
        growth the ring exists to prevent). Durability past this point is the
        log queue's job, and it has real retries and a dead-letter.
        """

        written = 0
        while True:
            batch = self._drain(self._batch_size)
            if not batch:
                break
            try:
                written += self._sink.write(batch)
            except Exception:  # noqa: BLE001 - fail-soft by design, see above
                logger.debug("Interaction flush failed; dropping %d events", len(batch))
                _dropped("error", len(batch))
        return written

    def _wake(self) -> None:
        # `_stop` doubles as the flusher's timer: setting it would stop the
        # thread, so a nudge is a no-op here and the next tick picks the batch
        # up. Kept as a seam so a future eager flush has one place to live.
        return None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.flush()
            except Exception:  # noqa: BLE001 - the flusher must outlive one bad pass
                logger.debug("Interaction flusher pass failed", exc_info=True)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="pheasant-interactions", daemon=True)
        self._thread.start()

    def stop(self, *, flush: bool = True) -> None:
        """Stop the flusher, draining what is buffered by default.

        Called from the API's SIGTERM drain, so it must be quick and must not
        raise: a shutdown that hangs on the log tier is worse than a shutdown
        that loses a few observations.
        """

        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        if flush:
            try:
                self.flush()
            except Exception:  # noqa: BLE001
                logger.debug("Final interaction flush failed", exc_info=True)
        self._sink.close()

    # -- introspection -----------------------------------------------------

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._events)

    @property
    def sink_name(self) -> str:
        return self._sink.name


# --------------------------------------------------------------------------
# Tracing
# --------------------------------------------------------------------------


class _Tracing:
    """Holds whatever OpenTelemetry we managed to configure, if any.

    Deliberately a small object rather than module globals so a test can build
    one, and so "is tracing on" is a question with one answer instead of three
    module-level flags that can disagree.
    """

    def __init__(self) -> None:
        self.tracer: Any = None
        self.enabled = False

    def configure(self, settings: Any) -> bool:
        endpoint = getattr(settings, "otlp_endpoint", None)
        if not endpoint:
            # No exporter, and therefore no SDK needed. Spans still exist --
            # they are just ours, and they still feed the ledger.
            self.tracer, self.enabled = None, False
            return False
        try:
            from opentelemetry import trace as ot_trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
        except ImportError:
            logger.warning(
                "observability.otlp_endpoint is set but the [otel] extra is not "
                "installed; spans will not be exported. Install with: "
                'pip install "pheasant[otel]"'
            )
            self.tracer, self.enabled = None, False
            return False

        headers = _parse_headers(os.environ.get(getattr(settings, "otlp_headers_env", "") or ""))
        from pheasant.telemetry.sync_trace import ROLE_ENV

        role = os.environ.get(ROLE_ENV)
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": str(getattr(settings, "service_name", "pheasant")),
                    # Which tier emitted a span: the stdout spans say so too.
                    **({"pheasant.role": role} if role else {}),
                }
            ),
            sampler=TraceIdRatioBased(float(getattr(settings, "sample_ratio", 1.0) or 1.0)),
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=str(endpoint), headers=headers or None))
        )
        ot_trace.set_tracer_provider(provider)
        self.tracer = ot_trace.get_tracer("pheasant")
        self.enabled = True
        return True

    def parent_context(self, context: InteractionContext) -> Any:
        """Rebuild an OTel context from the inbound ``traceparent``.

        Without this the SDK starts its own root trace for a call the caller
        already had a trace for, and the operator's collector shows two
        unrelated traces where there was one request.
        """

        if not context.parent_span_id or not context.trace_id:
            return None
        try:
            from opentelemetry.trace.propagation.tracecontext import (
                TraceContextTextMapPropagator,
            )

            header = f"00-{context.trace_id}-{context.parent_span_id}-01"
            return TraceContextTextMapPropagator().extract({"traceparent": header})
        except Exception:  # noqa: BLE001 - propagation is best-effort
            return None

    def current_ids(self) -> tuple[str, str] | None:
        """``(trace_id, span_id)`` of the active span, in W3C hex.

        This is what keeps a ledger row and an exported span naming the *same*
        call. Without it the two are minted independently and an operator
        correlating a slow span in their collector to a row here finds
        nothing -- which is most of the reason to export spans at all.
        """

        try:
            from opentelemetry import trace as ot_trace

            span_context = ot_trace.get_current_span().get_span_context()
            if not span_context.is_valid:
                return None
            return (
                format(span_context.trace_id, "032x"),
                format(span_context.span_id, "016x"),
            )
        except Exception:  # noqa: BLE001 - never break a call over an id
            return None


def _parse_headers(raw: str) -> dict[str, str]:
    """``key=value,key=value`` from an environment variable.

    The variable's **name** comes from config, never its value -- the same rule
    ``storage.dsn_env`` follows, so a config file that gets committed cannot
    leak a collector credential.
    """

    headers: dict[str, str] = {}
    for part in (raw or "").split(","):
        if "=" in part:
            key, _, value = part.partition("=")
            key, value = key.strip(), value.strip()
            if key:
                headers[key] = value
    return headers


TRACING = _Tracing()


def configure_tracing(settings: Any) -> bool:
    """Wire the exporter, if one is configured and installed. Idempotent."""

    try:
        return TRACING.configure(settings)
    except Exception:  # noqa: BLE001 - telemetry must never break startup
        logger.warning("Could not configure OTLP tracing; continuing without it", exc_info=True)
        return False


#: The process's buffer, so a surface that is not handed one can still find it.
#:
#: Process-wide for the same reason :data:`pheasant.telemetry.metrics.REGISTRY`
#: is: the MCP server is mounted *inside* the API app in one deployment shape
#: and runs alone over stdio in another, and it must observe through the same
#: buffer in the first case rather than opening a second one that would
#: double-count every ``/mcp`` call.
_PROCESS_BUFFER: InteractionBuffer | None = None
_PROCESS_LOCK = threading.Lock()


def set_process_buffer(buffer: InteractionBuffer | None) -> None:
    global _PROCESS_BUFFER
    with _PROCESS_LOCK:
        _PROCESS_BUFFER = buffer


def process_buffer() -> InteractionBuffer | None:
    with _PROCESS_LOCK:
        return _PROCESS_BUFFER


@contextmanager
def observe(
    buffer: InteractionBuffer | None,
    context: InteractionContext,
    *,
    kb_id: str,
    operation: str,
) -> Iterator[InteractionEvent]:
    """Time one call, fill in an event, and hand it to the buffer.

    The handler mutates the yielded event to say what came back (``result_ids``,
    ``top_score``, ``criteria``). An exception marks the event ``error`` and is
    **re-raised unchanged** -- observation never swallows a caller's failure,
    and never invents one.
    """

    started = utc_now()
    # Wall clock for `started_at` (it has to be comparable across processes and
    # sortable in SQL), monotonic for the duration. Subtracting two wall-clock
    # readings makes an NTP step mid-request emit a negative or wildly inflated
    # duration -- a nonsense row in the one column an operator reads to find
    # slow calls.
    ticked = time.perf_counter()
    event = InteractionEvent(
        kb_id=kb_id,
        operation=operation,
        modality=str(context.modality),
        principal=context.principal,
        session_id=context.session_id,
        client_id=context.client_id,
        trace_id=context.trace_id or new_trace_id(),
        span_id=context.span_id or new_span_id(),
        parent_span_id=context.parent_span_id,
        started_at=_iso(started),
    )
    token = _CURRENT_TRACE.set((event.trace_id, event.span_id))
    event_token = _CURRENT_EVENT.set(event)
    span_cm = None
    if TRACING.enabled and TRACING.tracer is not None:
        span_cm = TRACING.tracer.start_as_current_span(
            operation, context=TRACING.parent_context(context)
        )
        span_cm.__enter__()
        # Adopt the SDK's ids rather than our own. The event was built with
        # locally minted ones because they must exist with or without the
        # extra; when a real span is running, *its* ids are the ones the
        # operator's collector will show, and the row has to match.
        adopted = TRACING.current_ids()
        if adopted is not None:
            event.trace_id, event.span_id = adopted
            # Re-publish: an outbound hop must carry the ids the collector
            # will show, not the ones we minted before the SDK spoke.
            _CURRENT_TRACE.reset(token)
            token = _CURRENT_TRACE.set((event.trace_id, event.span_id))
    try:
        yield event
    except Exception:
        event.status = "error"
        raise
    finally:
        event.duration_ms = round((time.perf_counter() - ticked) * 1000.0, 3)
        if event.result_count is None and event.result_ids:
            event.result_count = len(event.result_ids)
        if span_cm is not None:
            try:
                _annotate_span(event)
                span_cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 - a span must not break a request
                logger.debug("Span exit failed", exc_info=True)
        _CURRENT_TRACE.reset(token)
        _CURRENT_EVENT.reset(event_token)
        if buffer is not None:
            buffer.record(event)


def _annotate_span(event: InteractionEvent) -> None:
    """Put the shape of the call on the exported span, never its content.

    Modality, operation and result count are what an operator reads a trace
    for. Query text and principal are deliberately absent: they are the
    ledger's business, they are governed by `redact_query_text` there, and a
    collector is a different system with different retention.
    """

    from opentelemetry import trace as ot_trace

    span = ot_trace.get_current_span()
    if not span.is_recording():
        return
    span.set_attribute("pheasant.kb", event.kb_id)
    span.set_attribute("pheasant.modality", str(event.modality))
    span.set_attribute("pheasant.operation", event.operation)
    if event.result_count is not None:
        span.set_attribute("pheasant.result_count", int(event.result_count))
    if event.status != "ok":
        span.set_status(ot_trace.Status(ot_trace.StatusCode.ERROR, event.status))


def redact(event: InteractionEvent, *, enabled: bool) -> InteractionEvent:
    """Drop every free-text field, keeping what the structural rules need.

    Question **and** answer together: redacting the question while keeping an
    answer that quotes the corpus back at it would be incoherent, which is why
    the setting is `redact_text` rather than `redact_query_text`.

    Identity, modality, criteria, result ids and result paths survive, so
    ``path-affinity-v1`` and ``retrieval-gap-v1`` still work; only the lexical
    rule ``alias-cooccurrence-v1`` goes quiet. That asymmetry is the point: a
    region can keep learning its own shape without keeping what anyone typed.
    """

    if enabled and (event.query_text is not None or event.answer_text is not None):
        event.query_text = None
        event.answer_text = None
        event.attributes = {**event.attributes, "text_redacted": True}
    return event


def cap_answer(event: InteractionEvent, *, max_chars: int) -> InteractionEvent:
    """Bound a recorded answer, marking it when it was cut.

    ``0`` drops answers entirely. Truncation is recorded rather than silent,
    because a rule counting phrases in an answer must be able to tell a short
    answer from a clipped one.
    """

    if event.answer_text is None:
        return event
    if max_chars <= 0:
        event.answer_text = None
        return event
    if len(event.answer_text) > max_chars:
        event.answer_text = event.answer_text[:max_chars]
        event.attributes = {**event.attributes, "answer_truncated": True}
    return event


#: How many hits one row records. A cap, because a `max_results: 500` query
#: would otherwise put 500 ids and 500 paths in a single ledger row, and the
#: rules only ever look at the head of a ranked list.
MAX_RESULTS_RECORDED = 50


def child_event(parent: InteractionEvent, operation: str) -> InteractionEvent:
    """A second observation for work that outlives the request that started it.

    A streaming chat returns its response object immediately and produces the
    answer on a worker thread afterwards, so the request's own event has
    already been handed to the buffer by the time there is anything to say
    about the answer. Mutating it then is a race whose outcome depends on
    flush timing.

    So the answer gets its own event instead, sharing the parent's trace and
    naming the parent's span -- the request span says "a stream was opened",
    this one says "an answer was produced", and the trace joins them. Which is
    what a parent/child span relationship is for.
    """

    return InteractionEvent(
        kb_id=parent.kb_id,
        operation=operation,
        modality=parent.modality,
        principal=parent.principal,
        session_id=parent.session_id,
        client_id=parent.client_id,
        trace_id=parent.trace_id,
        span_id=new_span_id(),
        parent_span_id=parent.span_id,
        started_at=_iso(utc_now()),
    )


def extract_results(payload: Any) -> tuple[list[str], list[str], float | None, int]:
    """``(stable ids, source-relative paths, top score, count)`` from a result
    payload, whatever surface produced it.

    One implementation, two callers, on purpose. A formation rule that saw
    ids from MCP and paths from HTTP would mine different evidence depending
    on which surface a user happened to be on -- which is the opposite of the
    determinism every rule downstream of this is built on.

    Ids join to `graph_nodes` and, through them, to `chunks`, which is how a
    rule asks whether a retrieved document actually contains the token that
    found it. Paths are `relative_path`: the same grammar `steering` matches
    against, so a `preference` rule minted from these can actually fire.

    Shape-tolerant because the surfaces genuinely differ -- `/search` answers
    with `results`, `/relevant-files` with `files`, chat with `citations` --
    and a rule that covers the shapes it knows beats a crash on the rest.
    """

    if not isinstance(payload, dict):
        return [], [], None, 0
    items = payload.get("results") or payload.get("files") or payload.get("citations")
    if not isinstance(items, list):
        return [], [], None, 0

    ids: list[str] = []
    paths: list[str] = []
    scores: list[float] = []
    for item in items[:MAX_RESULTS_RECORDED]:
        if isinstance(item, str):
            # `get_relevant_files` and friends answer with bare paths.
            paths.append(item)
            continue
        if not isinstance(item, dict):
            continue
        identifier = item.get("node_id") or item.get("chunk_id") or item.get("stable_id")
        if identifier:
            ids.append(str(identifier))
        path = item.get("relative_path") or item.get("path")
        if path:
            paths.append(str(path))
        score = item.get("score")
        if isinstance(score, (int, float)):
            scores.append(float(score))
    # `count` is the full result set; the lists are capped. Keeping the real
    # count is what lets `retrieval-gap-v1` tell "nothing matched" from
    # "matched more than we bothered to record".
    return ids, paths, (max(scores) if scores else None), len(items)


def events_from_batch(payload: dict[str, Any]) -> list[InteractionEvent]:
    """Parse a queued batch back into events, skipping anything malformed."""

    out: list[InteractionEvent] = []
    malformed = 0
    for raw in payload.get("events") or []:
        if not isinstance(raw, dict):
            malformed += 1
            continue
        try:
            event = InteractionEvent.from_json(raw)
        except (TypeError, ValueError):
            malformed += 1
            continue
        if event.is_writable:
            out.append(event)
        else:
            malformed += 1
    # Counted, not silent. A trace id and a timestamp are the two things every
    # row must have, so an event arriving without them is a defect somewhere
    # upstream -- and a defect that only ever manifests as a slightly smaller
    # ledger is a defect nobody finds.
    _dropped("malformed", malformed)
    return out


def rows_for(events: Iterable[InteractionEvent]) -> list[tuple[Any, ...]]:
    return [event.as_row() for event in events]
