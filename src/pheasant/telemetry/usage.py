"""Metering: what a region spent and did, counted where it happens.

The operator of a hosted region has to answer four questions that nothing
else here answers durably: how many tokens each model was sent and wrote, who
caused them, how much the region is used, and how long an answer takes. The
interaction ledger holds a row per call for a week; the Prometheus counters
hold nothing per person and nothing across a restart. This keeps **daily
totals** (`persistence/usage_ledger.py`, table ``usage_daily``), which are
small enough to keep for years and are what a monthly statement is made of.

Two kinds of thing are counted:

* a **model call** -- every reported chat, judgement and embedding request,
  at the one place each wire reads its provider's ``usage`` -- by model, with
  input, cached-input, output and reasoning tokens as the provider billed them;
* an **operation** -- an answer or a search, with its duration in a fixed
  histogram, so a percentile can be read back from sums.

Each is attributed to whoever is calling (the verified identity, when there is
one) and added to an in-memory total. **Nothing here writes on the request
path**: totals are flushed in one additive upsert at most every
:data:`FLUSH_SECONDS`, from a short-lived thread, on shutdown, and before a
read. A process that dies loses at most that interval, which is the trade the
observation plane already makes and says so.

Off unless ``observability.usage`` is set: a process that never calls
:meth:`Meter.bind` returns from every ``record`` at its first line.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: Upper bounds of the latency histogram, in milliseconds; one more bucket
#: holds everything slower. Fixed, because sums of counts are only meaningful
#: across days and replicas when every one of them cut at the same places.
BUCKETS_MS = (250, 500, 1000, 2000, 4000, 8000, 16000, 32000, 64000)
FLUSH_SECONDS = 30.0
#: Kinds of row. Public vocabulary: they are stored, so added to, not renamed.
CHAT, DECISION, EMBEDDING, ANSWER, SEARCH = "chat", "decision", "embedding", "answer", "search"
MODEL_KINDS = (CHAT, DECISION, EMBEDDING)

_CHANNEL: ContextVar[str] = ContextVar("pheasant_usage_channel", default="")
Key = tuple[str, str, str, str, str, str]  # day, kind, model, tenant, principal, channel


@contextmanager
def channel(name: str | None):
    """Attribute what happens inside to a surface (``api``, ``mcp``)."""

    token = _CHANNEL.set(str(name or ""))
    try:
        yield
    finally:
        _CHANNEL.reset(token)


def enter_channel(name: str | None) -> Any:
    """:func:`channel` for a caller that already has a ``try``/``finally``."""

    return _CHANNEL.set(str(name or ""))


def leave_channel(token: Any) -> None:
    _CHANNEL.reset(token)


@dataclass
class Totals:
    requests: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    duration_ms: float = 0.0
    buckets: list[int] = field(default_factory=lambda: [0] * (len(BUCKETS_MS) + 1))


def bucket_of(milliseconds: float) -> int:
    for index, bound in enumerate(BUCKETS_MS):
        if milliseconds <= bound:
            return index
    return len(BUCKETS_MS)


def _int(value: Any) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


class Meter:
    """One per process. Thread-safe; cheap when nothing is bound."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[Key, Totals] = {}
        self._target: tuple[Any, str] | None = None
        #: A second ledger the same totals are added to: a fleet's directory,
        #: so the operator reads every knowledge base in one place.
        self._mirror: Any = None
        self._flushed_at = time.monotonic()
        self._flushing = False

    @property
    def bound(self) -> bool:
        return self._target is not None

    def bind(self, state: Any, kb_id: str, mirror: Any = None) -> None:
        """Start metering into ``state``. What was pending goes where it was counted."""

        if self._target is not None and self._target[0] is not state:
            self.flush()
        with self._lock:
            self._target = (state, str(kb_id))
            self._mirror = mirror

    def bind_for(self, config: Any, state: Any) -> None:
        """:meth:`bind` when ``observability.usage`` is on, mirrored into the
        fleet directory when this stack is part of one."""

        if not config.observability.usage:
            return
        mirror = None
        try:
            from pheasant.persistence import fleet_store

            mirror = fleet_store.open_store(config)
        except Exception:  # noqa: BLE001 - a directory that is down costs the mirror only
            logger.debug("fleet directory unavailable for usage mirroring", exc_info=True)
        self.bind(state, config.knowledge_base_id, mirror)

    def unbind(self, state: Any = None) -> None:
        """Stop metering (flushing first); with ``state``, only if it is the target."""

        if self._target is None or (state is not None and self._target[0] is not state):
            return
        self.flush()
        with self._lock:
            self._target = None
            self._mirror = None
            self._rows.clear()

    def _key(self, kind: str, model: str) -> Key:
        from pheasant.security.identity import current_identity

        identity = current_identity()
        person = identity is not None and identity.is_person
        return (
            datetime.now(UTC).date().isoformat(),
            kind,
            model,
            (identity.tenant_id if person else "") or "",
            (identity.principal if identity is not None else "") or "",
            _CHANNEL.get(),
        )

    def model_call(  # noqa: PLR0913 - one argument per reported figure
        self,
        kind: str,
        model: str | None,
        input_tokens: Any = None,
        output_tokens: Any = None,
        cached_input_tokens: Any = None,
        reasoning_tokens: Any = None,
        *,
        cached_included: bool = False,
    ) -> None:
        """Count one reported provider call.

        ``cached_included`` says the provider's input figure already contains
        its cached tokens (OpenAI's wire); stored input is always the part
        that was *not* served from cache, so one price applies to it.
        """

        if self._target is None:
            return
        cached = _int(cached_input_tokens)
        fresh = _int(input_tokens) - (cached if cached_included else 0)
        with self._lock:
            row = self._rows.setdefault(self._key(kind, str(model or "")), Totals())
            row.requests += 1
            row.input_tokens += max(0, fresh)
            row.output_tokens += _int(output_tokens)
            row.cached_input_tokens += cached
            row.reasoning_tokens += _int(reasoning_tokens)
        self._maybe_flush()

    def operation(self, name: str, seconds: float, *, ok: bool = True) -> None:
        """Count one answer or search and where its duration falls."""

        if self._target is None:
            return
        milliseconds = max(0.0, float(seconds) * 1000.0)
        with self._lock:
            row = self._rows.setdefault(self._key(name, ""), Totals())
            row.requests += 1
            row.errors += 0 if ok else 1
            row.duration_ms += milliseconds
            row.buckets[bucket_of(milliseconds)] += 1
        self._maybe_flush()

    def _maybe_flush(self) -> None:
        if time.monotonic() - self._flushed_at < FLUSH_SECONDS:
            return
        with self._lock:
            if self._flushing:
                return
            self._flushing = True
        threading.Thread(target=self._background, name="pheasant-usage-flush", daemon=True).start()

    def _background(self) -> None:
        try:
            self.flush()
        finally:
            with self._lock:
                self._flushing = False

    def flush(self) -> int:
        """Write what is pending as one additive upsert; rows written."""

        with self._lock:
            target, mirror, rows = self._target, self._mirror, self._rows
            self._rows = {}
            self._flushed_at = time.monotonic()
        if target is None or not rows:
            return 0
        from pheasant.persistence import usage_ledger

        if mirror is not None:
            try:
                usage_ledger.add(mirror, target[1], rows)
            except Exception:  # noqa: BLE001 - the region's own ledger still gets them
                logger.debug("usage totals could not be mirrored to the fleet directory")
        try:
            usage_ledger.add(target[0], target[1], rows)
        except Exception:  # noqa: BLE001 - a lost interval of counts must never fail a request
            logger.debug("usage totals could not be written; dropped %d rows", len(rows))
            return 0
        return len(rows)


#: The process's meter. Module-level like `metrics.REGISTRY`, for the same
#: reason: the wires that report usage know nothing about apps or engines.
METER = Meter()
