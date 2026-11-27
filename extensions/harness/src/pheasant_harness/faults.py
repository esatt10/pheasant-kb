"""Named crash points, armed only by tests and drills.

A worker "dies" at a point by raising :class:`SimulatedCrash`, which derives
from ``BaseException`` so no ``except Exception`` in the code under test can
swallow it -- a killed process runs no handlers either. Nothing is released on
the way out: leases stay held until they expire, sandboxes stay on disk until
the reaper finds them, exactly as after a real ``kill -9``.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

POINTS = (
    "before_dispatch",
    "before_effect_dispatch",
    "after_sink_commit",
    "after_receipt",
    "before_pull_request",
    "between_dispatch_and_commit",
    "before_checkpoint_commit",
    "after_checkpoint_commit",
    "during_cleanup",
)

_armed: dict[str, int] = {}
_lock = threading.Lock()


class SimulatedCrash(BaseException):
    def __init__(self, point: str):
        super().__init__(f"simulated worker death at {point}")
        self.point = point


def hit(point: str) -> None:
    with _lock:
        remaining = _armed.get(point, 0)
        if remaining <= 0:
            return
        _armed[point] = remaining - 1
    raise SimulatedCrash(point)


def arm(point: str, times: int = 1) -> None:
    if point not in POINTS:
        raise ValueError(f"unknown fault point {point}")
    with _lock:
        _armed[point] = times


def disarm() -> None:
    with _lock:
        _armed.clear()


@contextmanager
def armed(point: str, times: int = 1):
    arm(point, times)
    try:
        yield
    finally:
        disarm()
