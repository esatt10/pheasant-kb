"""Coalesced job snapshots on a dedicated writer, outside task completion."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


class SnapshotPublisher:
    def __init__(self, store: Any, interval: float = 0.1):
        self.store, self.interval = store, interval
        self._condition = threading.Condition()
        self._pending: dict[str, dict] = {}
        self._closing = False
        self._writing = False
        self._current: str | None = None
        self._discarded: set[str] = set()
        self._thread = threading.Thread(
            target=self._run, name="pheasant-job-snapshots", daemon=True
        )
        self._thread.start()

    def submit(self, payload: dict) -> None:
        with self._condition:
            if not self._closing:
                self._pending[payload["id"]] = payload
                self._condition.notify_all()

    def flush(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._pending or self._writing:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._condition.wait(left)
        return True

    def discard(self, job_ids: set[str]) -> None:
        with self._condition:
            for key in job_ids:
                self._pending.pop(key, None)
                if key == self._current:
                    self._discarded.add(key)
            self._condition.notify_all()

    def close(self, timeout: float = 2.0) -> None:
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closing)
                if not self._pending:
                    return
                # Prefer terminal states. Bursts of progress for a running job
                # replace its pending snapshot; they do not queue file writes.
                terminal = next((k for k, v in self._pending.items() if not v.get("active")), None)
                if terminal is None and not self._closing:
                    self._condition.wait_for(
                        lambda: (
                            self._closing
                            or any(not v.get("active") for v in self._pending.values())
                        ),
                        timeout=self.interval,
                    )
                    if not self._pending:
                        continue
                    terminal = next(
                        (k for k, v in self._pending.items() if not v.get("active")), None
                    )
                key = terminal or next(iter(self._pending))
                payload = self._pending.pop(key)
                self._writing = True
                self._current = key
            failed = False
            try:
                self.store.write(payload)
            except OSError:
                failed = True
                logger.debug("could not publish shared job snapshot; retrying", exc_info=True)
            finally:
                with self._condition:
                    discarded = key in self._discarded
                    if not discarded:
                        if failed and not self._closing:
                            self._pending.setdefault(key, payload)
                        self._writing = False
                        self._current = None
                        self._condition.notify_all()
                if discarded:
                    # Cleanup happens outside the condition: a slow share
                    # must not block producers submitting other jobs either.
                    try:
                        (self.store.path / f"{key}.json").unlink(missing_ok=True)
                    except OSError:
                        logger.debug("could not discard retired job snapshot", exc_info=True)
                with self._condition:
                    if discarded:
                        self._discarded.discard(key)
                    self._writing = False
                    self._current = None
                    self._condition.notify_all()
                if failed and not self._closing and not discarded:
                    time.sleep(0.25)


def configure(
    registry: Any, store: Any, asynchronous: bool, interval: float, terminal: set
) -> None:
    registry._shared = store
    registry._publisher = SnapshotPublisher(store) if store is not None and asynchronous else None
    registry._closed = threading.Event()
    if store is None:
        return

    def heartbeat() -> None:
        while not registry._closed.wait(interval):
            with registry._lock:
                running = [job for job in registry._jobs.values() if job.status not in terminal]
            for job in running:
                registry._publish(job)
            try:
                store.reap_stale({job.id for job in running})
            except Exception:
                logger.debug("could not reap stale job snapshots", exc_info=True)

    threading.Thread(target=heartbeat, name="pheasant-job-heartbeat", daemon=True).start()
