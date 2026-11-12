"""Single-flight, bounded-memory graph history outside the mutation writer's gate."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from pheasant.ingestion.pipeline import utc_now
from pheasant.persistence.graph_store import GraphStore
from pheasant.persistence.snapshot_stream import SnapshotCancelled, write_snapshot
from pheasant.persistence.state_store import StateStore

logger = logging.getLogger(__name__)


class HistoryPublisher:
    def __init__(self, config: Any, paths: Any):
        self.config, self.paths = config, paths
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancelled = threading.Event()
        self._next_check = 0.0

    def request(self) -> None:
        with self._lock:
            if self._cancelled.is_set() or time.monotonic() < self._next_check:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="pheasant-graph-history", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        self._cancelled.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        from pheasant.sync.graph_history import snapshot_due

        config, paths = self.config, self.paths
        kb, storage = config.knowledge_base_id, config.storage
        state = None
        delay = 30.0
        try:
            now = utc_now()
            files = GraphStore(paths.graphs)
            if snapshot_due(files, kb, now, storage.graph_snapshot_interval_seconds):
                state = StateStore.from_config(config, paths.sqlite)
                store = GraphStore(paths.graphs, state=state, graph_format="rows")
                path = write_snapshot(store, kb, now, self._cancelled)
                logger.info("Graph history published: %s", path.name)
                delay = max(1.0, float(storage.graph_snapshot_interval_seconds))
            files.enforce_retention(kb, int(float(storage.max_state_size_gb) * 1024**3))
        except SnapshotCancelled:
            logger.info("Graph history interrupted; the previous snapshot remains available")
        except Exception:
            logger.warning("Graph history failed; a later mutation retries it", exc_info=True)
        finally:
            if state is not None:
                state.close()
            with self._lock:
                self._next_check = time.monotonic() + delay


def request(engine: Any) -> None:
    publisher = getattr(engine, "_history_publisher", None)
    if publisher is None:
        publisher = engine._history_publisher = HistoryPublisher(engine.config, engine.paths)
    publisher.request()


def close(engine: Any) -> None:
    publisher = getattr(engine, "_history_publisher", None)
    if publisher is not None:
        publisher.close()
