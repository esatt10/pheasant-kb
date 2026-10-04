"""Live filesystem watching that triggers per-source incremental syncs.

One watchdog observer is created per enabled filesystem-type source. File
events are mapped back to the owning source (respecting the source's
include/exclude/max_depth config where cheap), debounced per
``sync.watcher.debounce_ms``, batched up to ``sync.watcher.batch_window_ms``,
and dispatched as a single ``SyncEngine.sync_source(name, mode="incremental")``
per affected source per batch window.

Watcher- and scheduler-triggered syncs are serialized through a shared
``sync_lock`` because ``SyncEngine`` is not safe for concurrent syncs within
one process (full crash/concurrency safety lands in Step 21.2).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from pheasant.config.schema import PheasantConfig, SourceConfig
from pheasant.ingestion.pipeline import _match_any, within_max_depth

if TYPE_CHECKING:
    from pheasant.sync.engine import SyncEngine

logger = logging.getLogger(__name__)

FILESYSTEM_SOURCE_TYPES = frozenset(
    {"repository", "markdown_folder", "document_folder", "single_file"}
)


class _SourceEventHandler(FileSystemEventHandler):
    """Maps raw watchdog events for one source onto the watcher's pending set."""

    def __init__(self, watcher: WatcherService, source: SourceConfig) -> None:
        self._watcher = watcher
        self._source = source
        single_file = source.path.is_file()
        root = source.path.parent if single_file else source.path
        self._root = root.resolve()
        self._target = source.path.resolve() if single_file else None

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        for raw in (event.src_path, getattr(event, "dest_path", None)):
            if raw and self._matches(os.fsdecode(raw)):
                self._watcher.record_event(self._source.name)
                return

    def _matches(self, raw_path: str) -> bool:
        try:
            resolved = Path(raw_path).resolve()
            relative = resolved.relative_to(self._root).as_posix()
        except (OSError, ValueError):
            return False
        if self._target is not None:
            return resolved == self._target
        if not within_max_depth(relative, self._source.max_depth):
            return False
        if _match_any(relative, self._source.exclude):
            return False
        return not self._source.include or _match_any(relative, self._source.include)


class WatcherService:
    """Watches enabled filesystem sources and syncs them incrementally."""

    def __init__(
        self,
        engine: SyncEngine,
        config: PheasantConfig | None = None,
        *,
        sync_lock: threading.Lock | None = None,
    ) -> None:
        self.engine = engine
        self.config = config if config is not None else engine.config
        self.settings = self.config.sync.watcher
        self._sync_lock = sync_lock or threading.Lock()
        self._observers: list[Observer] = []
        self._dispatch_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._pending: dict[str, tuple[float, float]] = {}
        self._pending_lock = threading.Lock()
        self._started = False

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    @property
    def running(self) -> bool:
        return self._started

    def watchable_sources(self) -> list[SourceConfig]:
        sources = [
            source
            for source in self.config.sources
            if source.enabled
            and source.type.value in FILESYSTEM_SOURCE_TYPES
            and source.path.exists()
        ]
        return sources[: max(0, self.settings.max_watch_paths)]

    def start(self) -> None:
        if self._started or not self.enabled:
            return
        sources = self.watchable_sources()
        if not sources:
            return
        self._stop.clear()
        self._wake.clear()
        for source in sources:
            root = source.path if source.path.is_dir() else source.path.parent
            observer = Observer()
            observer.daemon = True
            observer.schedule(_SourceEventHandler(self, source), str(root), recursive=True)
            observer.start()
            self._observers.append(observer)
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop,
            name="pheasant-watcher-dispatch",
            daemon=True,
        )
        self._dispatch_thread.start()
        self._started = True
        logger.info(
            "Watcher started for %s source(s): %s",
            len(sources),
            ", ".join(source.name for source in sources),
        )

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        self._wake.set()
        if self._dispatch_thread is not None:
            self._dispatch_thread.join(timeout=10)
            self._dispatch_thread = None
        for observer in self._observers:
            observer.stop()
        for observer in self._observers:
            observer.join(timeout=10)
        self._observers.clear()
        with self._pending_lock:
            self._pending.clear()
        self._started = False
        logger.info("Watcher stopped")

    def record_event(self, source_name: str) -> None:
        """Note a filesystem change for a source (called from observer threads)."""
        now = time.monotonic()
        with self._pending_lock:
            first, _ = self._pending.get(source_name, (now, now))
            self._pending[source_name] = (first, now)
        self._wake.set()

    def _dispatch_loop(self) -> None:
        debounce_s = max(self.settings.debounce_ms, 0) / 1000.0
        batch_s = max(self.settings.batch_window_ms / 1000.0, debounce_s)
        while not self._stop.is_set():
            self._wake.wait(timeout=self._next_timeout(debounce_s, batch_s))
            self._wake.clear()
            if self._stop.is_set():
                return
            for source_name in self._take_due(debounce_s, batch_s):
                self._sync_one(source_name)

    def _next_timeout(self, debounce_s: float, batch_s: float) -> float | None:
        with self._pending_lock:
            if not self._pending:
                return None
            deadlines = [
                min(last + debounce_s, first + batch_s) for first, last in self._pending.values()
            ]
        return max(0.0, min(deadlines) - time.monotonic()) or 0.01

    def _take_due(self, debounce_s: float, batch_s: float) -> list[str]:
        now = time.monotonic()
        due: list[str] = []
        with self._pending_lock:
            for source_name, (first, last) in list(self._pending.items()):
                if (now - last) >= debounce_s or (now - first) >= batch_s:
                    due.append(source_name)
                    del self._pending[source_name]
        return due

    def _sync_one(self, source_name: str) -> None:
        try:
            with self._sync_lock:
                result = self.engine.sync_source(source_name, "incremental")
        except Exception:
            logger.exception("Watcher-triggered sync failed for source %s", source_name)
            return
        logger.info(
            "Watcher sync for %s: indexed=%s skipped=%s",
            source_name,
            result.indexed_artifacts,
            result.skipped_artifacts,
        )
