"""Coalesced job snapshots on a dedicated writer, outside task completion."""

from __future__ import annotations

import logging
import threading
import time
import weakref
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
            except Exception:  # Database stores can also fail transiently.
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
                        if hasattr(self.store, "path"):
                            (self.store.path / f"{key}.json").unlink(missing_ok=True)
                        else:
                            self.store.clear_finished(key)
                    except Exception:
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
    registry._heartbeat = None
    if store is None:
        return
    reference = weakref.ref(registry)
    closed = registry._closed

    def heartbeat() -> None:
        while not closed.wait(interval):
            current = reference()
            if current is None:
                return
            with current._lock:
                running = [job for job in current._jobs.values() if job.status not in terminal]
            for job in running:
                current._publish(job)
            try:
                store.reap_stale({job.id for job in running})
            except Exception:
                logger.debug("could not reap stale job snapshots", exc_info=True)
            finally:
                del current

    registry._heartbeat = threading.Thread(
        target=heartbeat, name="pheasant-job-heartbeat", daemon=True
    )
    registry._heartbeat.start()


def lookup(registry: Any, job_id: str) -> dict | None:
    with registry._lock:
        job = registry._jobs.get(job_id)
        local = job.as_dict() if job else None
    store = registry._shared
    if local is not None and (
        local.get("active") or not getattr(store, "authoritative_terminal", False)
    ):
        return local
    return store.get(job_id) if store is not None else None


def merge_visible(store: Any, local: dict, shared: list[dict]) -> dict:
    merged = {str(row["id"]): row for row in shared}
    for key, row in local.items():
        if (
            row.get("active")
            or key in merged
            or not getattr(store, "authoritative_terminal", False)
        ):
            merged[key] = row
    return merged


def shared_listing(store: Any, limit: int) -> list[dict]:
    if store is None:
        return []
    return (
        store.list(limit=limit) if getattr(store, "authoritative_terminal", False) else store.list()
    )
