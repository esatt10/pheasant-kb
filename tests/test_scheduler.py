"""Tests for the interval-based catch-all sync scheduler (Synapse step 21.1)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from pheasant.config.loader import load_config
from pheasant.sync.scheduler import SchedulerService

POLL_TIMEOUT_S = 5.0


def _wait_for(
    predicate: Callable[[], bool],
    timeout: float = POLL_TIMEOUT_S,
    interval: float = 0.02,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _SpyEngine:
    """Counts sync_all invocations without doing any real indexing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.modes: list[str] = []

    def sync_all(self, mode: str = "incremental", *, scheduled: bool = False) -> list:
        # The beat says it is the beat, so paused and manual pipelines sit it out.
        assert scheduled, "the scheduler must call sync_all(scheduled=True)"
        with self._lock:
            self.calls += 1
            self.modes.append(mode)
        return []


def test_scheduler_fires_repeatedly_and_stops_promptly() -> None:
    spy = _SpyEngine()
    baseline_threads = set(threading.enumerate())
    scheduler = SchedulerService(spy, interval_seconds=0.2, enabled=True)
    scheduler.start()
    assert scheduler.running
    assert _wait_for(lambda: spy.calls >= 3), "scheduler did not fire at least 3 times"
    stop_started = time.monotonic()
    scheduler.stop()
    assert time.monotonic() - stop_started < 2.0, "stop() did not return promptly"
    assert not scheduler.running
    assert not (set(threading.enumerate()) - baseline_threads)
    assert all(mode == "incremental" for mode in spy.modes)


def test_scheduler_disabled_explicitly_is_noop() -> None:
    spy = _SpyEngine()
    baseline_threads = set(threading.enumerate())
    scheduler = SchedulerService(spy, interval_seconds=0.05, enabled=False)
    scheduler.start()
    assert not scheduler.running
    assert set(threading.enumerate()) == baseline_threads
    time.sleep(0.15)
    assert spy.calls == 0
    scheduler.stop()


def test_scheduler_disabled_in_config_is_noop(config_path: Path) -> None:
    cfg = load_config(config_path)
    cfg.sync.scheduler.enabled = False
    scheduler = SchedulerService(_SpyEngine(), cfg)
    assert scheduler.interval_seconds == float(cfg.sync.scheduler.interval_seconds)
    scheduler.start()
    assert not scheduler.running
    scheduler.stop()


def test_scheduler_reads_interval_from_config(config_path: Path) -> None:
    cfg = load_config(config_path)
    cfg.sync.scheduler.interval_seconds = 123
    scheduler = SchedulerService(_SpyEngine(), cfg)
    assert scheduler.interval_seconds == 123.0
    assert scheduler.enabled
