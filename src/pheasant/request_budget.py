"""Monotonic request deadlines shared by blocking application boundaries."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class DeadlineExceeded(TimeoutError):
    """The request used its end-to-end time budget."""


_ACTIVE_DEADLINE: ContextVar[float | None] = ContextVar("pheasant_request_deadline", default=None)
_ACTIVE_BUDGET: ContextVar[RequestBudget | None] = ContextVar(
    "pheasant_request_budget", default=None
)


class RequestBudget:
    """A duration budget represented internally by a monotonic deadline."""

    def __init__(self, seconds: float | None) -> None:
        self.started = time.monotonic()
        self.deadline = self.started + max(0.0, float(seconds)) if seconds else None
        self._timings: list[dict[str, object]] = []
        self._timings_lock = threading.Lock()

    def remaining(self) -> float | None:
        return None if self.deadline is None else max(0.0, self.deadline - time.monotonic())

    def check(self) -> None:
        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            raise DeadlineExceeded("assistant request deadline exceeded")

    def record_timing(self, stage: str, seconds: float, **attributes: object) -> None:
        """Append one completed timing to this request's existing trace."""
        item: dict[str, object] = {
            "stage": stage,
            "duration_seconds": max(0.0, float(seconds)),
        }
        item.update(attributes)
        with self._timings_lock:
            self._timings.append(item)

    def timings(self) -> list[dict[str, object]]:
        with self._timings_lock:
            return [dict(item) for item in self._timings]


@contextmanager
def activate(deadline: float | RequestBudget | None) -> Iterator[None]:
    """Expose a request deadline to clients called in this context."""
    budget = deadline if isinstance(deadline, RequestBudget) else None
    monotonic_deadline = budget.deadline if budget is not None else deadline
    deadline_token = _ACTIVE_DEADLINE.set(monotonic_deadline)
    budget_token = _ACTIVE_BUDGET.set(budget)
    try:
        yield
    finally:
        _ACTIVE_BUDGET.reset(budget_token)
        _ACTIVE_DEADLINE.reset(deadline_token)


def remaining_seconds() -> float | None:
    """Return remaining duration, never a process-local timestamp."""
    deadline = _ACTIVE_DEADLINE.get()
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def check_active() -> None:
    remaining = remaining_seconds()
    if remaining is not None and remaining <= 0:
        raise DeadlineExceeded("assistant request deadline exceeded")


def record_active_timing(stage: str, seconds: float, **attributes: object) -> None:
    """Record a stage in the current answer budget, when one is active."""
    budget = _ACTIVE_BUDGET.get()
    if budget is not None:
        budget.record_timing(stage, seconds, **attributes)
