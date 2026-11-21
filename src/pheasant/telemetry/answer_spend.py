"""What one deep-research answer has spent, against its own limit.

The monthly limit (``telemetry/spend.py``) bounds a knowledge base; this bounds
one answer. A deep-research answer (``@detailed`` / ``@deep``, or a pinned
``depth: "long"``) opens an :class:`AnswerSpend` for its duration and every
model call the meter prices while it is open is added to it, from any thread
the answer runs (the long form's section pool copies the caller's context, so
the same object is reached).

The limit is the operator's: ``models.deep_answer_spend_limit`` (default 5)
or the figure an operator admin set in the region controls row since. The
loop reads it at the places it decides something -- before another research
round, before writing, before each section -- rather than refusing calls one
by one, because a refused call part-way through an answer leaves nothing to
read, while a loop that stops searching still writes from what it found.

* **Wrapping up** is research stopping once what it spent plus what writing
  is projected to cost reaches ``WRAP_UP_SHARE`` (95%) of the limit.
* **The limit** is a section not yet started once spend reaches 100%; it is
  written from its passages instead, as a late section already is. Calls in
  flight at that moment finish, so an answer can pass the limit by what those
  calls cost, and never by a call started after it.

With usage metering off nothing is priced, so nothing here can be enforced;
:attr:`AnswerSpend.enforced` says so and the answer's steps repeat it. A call
to a model with no stated price counts nothing and is named.

Layer 1, beside the meter that feeds it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

#: The deep-answer limit when the config names none.
DEFAULT_DEEP_ANSWER_LIMIT = 5.0
#: The largest per-answer limit an operator may set: a typo guard.
MAX_DEEP_ANSWER_LIMIT = 10_000.0
#: Deep-research answers one process runs at once, when the config names none.
DEFAULT_DEEP_CONCURRENCY = 2
#: The most an operator may allow at once per process: a typo guard.
MAX_DEEP_CONCURRENCY = 64
#: The share of the limit at which research stops and the answer is written.
WRAP_UP_SHARE = 0.95

_ACTIVE: ContextVar[AnswerSpend | None] = ContextVar("pheasant_answer_spend", default=None)


class AnswerSpend:
    """One answer's spend, in micros of the catalog's currency. Thread-safe."""

    def __init__(self, limit: float | None, *, enforced: bool, currency: str = "USD") -> None:
        self.limit = limit
        self.enforced = bool(enforced and limit is not None)
        self.currency = currency
        self._lock = threading.Lock()
        self._micros = 0
        self.calls = 0
        self.unpriced: set[str] = set()

    def add(self, micros: int | None, model: str | None) -> None:
        with self._lock:
            self.calls += 1
            if micros is None:
                self.unpriced.add(str(model or "unknown"))
            else:
                self._micros += int(micros)

    @property
    def spent(self) -> float:
        with self._lock:
            return self._micros / 1_000_000

    def share(self, projected: float = 0.0) -> float | None:
        """Spend plus ``projected``, as a share of the limit; ``None`` unenforced."""

        if not self.enforced:
            return None
        if not self.limit:
            return 1.0  # a limit of 0 allows no research beyond the first round
        return (self.spent + max(0.0, projected)) / float(self.limit)

    def wrap_up(self, projected: float = 0.0) -> bool:
        """Whether research should stop now: spend + ``projected`` at 95% or more."""

        share = self.share(projected)
        return share is not None and share >= WRAP_UP_SHARE

    def reached(self) -> bool:
        """Whether nothing more should be started: spend at the limit."""

        share = self.share()
        return share is not None and share >= 1.0

    def money(self, amount: float | None = None) -> str:
        value = self.spent if amount is None else amount
        return f"{value:,.2f} {self.currency}" if self.currency != "USD" else f"${value:,.2f}"

    def summary(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "spent": round(self.spent, 4),
            "currency": self.currency,
            "enforced": self.enforced,
            "calls": self.calls,
            "unpriced_models": sorted(self.unpriced),
        }


def current() -> AnswerSpend | None:
    """The deep answer being written in this context, if any."""

    return _ACTIVE.get()


def record(micros: int | None, model: str | None) -> None:
    """Add one priced call to the open answer, if one is open. Called by the meter."""

    active = _ACTIVE.get()
    if active is not None:
        active.add(micros, model)


@contextmanager
def scope(spend: AnswerSpend | None) -> Iterator[AnswerSpend | None]:
    """Hold ``spend`` open for the calls made inside the block."""

    token = _ACTIVE.set(spend)
    try:
        yield spend
    finally:
        _ACTIVE.reset(token)


def configured_limit(config: Any) -> float | None:
    models = getattr(config, "models", None)
    if models is None:
        return DEFAULT_DEEP_ANSWER_LIMIT
    value = getattr(models, "deep_answer_spend_limit", DEFAULT_DEEP_ANSWER_LIMIT)
    return None if value is None else float(value)


def effective_limit(config: Any, saved: dict[str, Any]) -> float | None:
    """The per-answer limit in force: an operator's figure, else the deployment's."""

    if "deep_answer_spend_limit" in saved:
        value = saved["deep_answer_spend_limit"]
        return None if value is None else float(value)
    return configured_limit(config)


def configured_concurrency(config: Any) -> int:
    latency = getattr(getattr(config, "assistant", None), "latency", None)
    value = getattr(latency, "max_concurrent_deep_answers", DEFAULT_DEEP_CONCURRENCY)
    return int(DEFAULT_DEEP_CONCURRENCY if value is None else value)


def effective_concurrency(config: Any, saved: dict[str, Any]) -> int:
    """Deep answers one process may run at once; ``0`` is no separate cap."""

    if saved.get("max_concurrent_deep_answers") is not None:
        return int(saved["max_concurrent_deep_answers"])
    return configured_concurrency(config)
