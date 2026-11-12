"""A supervised, reusable writer interpreter; no graph residency in the API.

Requests and progress are NDJSON. One parent gate serializes callers across
the scheduler and queue drainer. A failed request discards the interpreter:
durable queue delivery retries it against committed state, never a half-built
in-memory graph. There is no automatic replay of an ambiguous write.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pheasant.telemetry.sync_trace import CHILD_ENV

logger = logging.getLogger(__name__)
_sessions: dict[str, Session] = {}
_gate = threading.Lock()


def run(
    config_path: str | Path,
    source_name: str | None = None,
    mode: str = "incremental",
    *,
    timeout: float = 21600,
    full_scan: bool = False,
    depth: int | None = None,
    on_progress: Any = None,
    task_payload: dict | None = None,
) -> dict:
    key = str(Path(config_path).resolve())
    with _gate:
        session = _sessions.setdefault(key, Session(key))
    return session.run(
        {
            "source": source_name,
            "mode": mode,
            "full_scan": full_scan,
            "depth": depth,
            "payload": task_payload,
        },
        timeout,
        on_progress,
    )


def close() -> None:
    with _gate:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        session.stop()


atexit.register(close)


class Session:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.events: queue.Queue = queue.Queue()
        self.stamp: bytes | None = None

    def start(self) -> None:
        stamp = Path(self.path).read_bytes()
        if self.process is not None and self.process.poll() is None and stamp == self.stamp:
            return
        self.stop()
        self.stamp = stamp
        self.events = queue.Queue()
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "pheasant.sync.writer", self.path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env={**os.environ, CHILD_ENV: "1"},
        )

        def read(stream: Any, kind: str, events: queue.Queue) -> None:
            try:
                for line in stream:
                    events.put((kind, line))
            finally:
                stream.close()
                events.put((kind, None))

        for kind in ("stdout", "stderr"):
            threading.Thread(
                target=read,
                args=(getattr(self.process, kind), kind, self.events),
                name=f"pheasant-writer-{kind}",
                daemon=True,
            ).start()

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        # stdin EOF asks for an orderly checkpoint and lease release.
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass  # A killed child has no reading end left to flush to.
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def run(self, request: dict, timeout: float, on_progress: Any) -> dict:
        from pheasant.sync.worker import DEFAULT_INACTIVITY_TIMEOUT_S, PROGRESS_MARKER

        with self.lock:
            errors: list[str] = []
            started = time.monotonic()
            last = started
            try:
                self.start()
                assert self.process is not None and self.process.stdin is not None
                self.process.stdin.write(json.dumps(request) + "\n")
                self.process.stdin.flush()
                while True:
                    now = time.monotonic()
                    if now - started >= timeout or now - last >= DEFAULT_INACTIVITY_TIMEOUT_S:
                        raise TimeoutError("writer exceeded its total or inactivity deadline")
                    try:
                        kind, line = self.events.get(
                            timeout=min(0.5, max(0.01, timeout - (now - started)))
                        )
                    except queue.Empty:
                        continue
                    if line is None and kind == "stderr":
                        continue
                    if line is None:
                        raise RuntimeError("writer exited before acknowledging the task")
                    if kind == "stderr":
                        errors.append(line.strip())
                        errors = errors[-10:]
                        logger.info("writer: %s", line.strip())
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("marker") == PROGRESS_MARKER:
                        last = time.monotonic()
                        if on_progress is not None:
                            try:
                                on_progress(event)
                            except Exception:
                                logger.debug("progress callback failed", exc_info=True)
                    elif event.get("marker") == "pheasant.writer.result":
                        report = event["report"]
                        if report.get("status") != "ok":
                            self.stop()
                        return report
            except Exception as exc:
                logger.exception("writer request failed")
                self.stop()
                return {
                    "status": "timeout" if isinstance(exc, TimeoutError) else "failed",
                    "error": "\n".join([str(exc), *errors[-3:]]),
                    "results": [],
                }


def serve(path: str) -> None:
    from pheasant.config.loader import load_config
    from pheasant.sync.engine import SyncEngine
    from pheasant.sync.queue import IndexTask
    from pheasant.sync.worker import DEFAULT_LEASE_WAIT_S, PROGRESS_MARKER

    engine = SyncEngine(load_config(Path(path)), defer_persisted_graph_load=True)
    engine.lease.wait_timeout_s = DEFAULT_LEASE_WAIT_S

    def progress(
        phase: str, current: int, total: int | None, detail: str, meta: dict | None = None
    ) -> None:
        print(
            json.dumps(
                {
                    "marker": PROGRESS_MARKER,
                    "phase": phase,
                    "current": current,
                    "total": total,
                    "detail": detail,
                    "meta": meta,
                }
            ),
            flush=True,
        )

    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                # The writer can lose its source lease between requests. If
                # another process committed, cached graph state is invalid.
                if engine._graph_loaded:
                    published = engine.graph_store.published_generation(
                        engine.config.knowledge_base_id
                    )
                    if published and published["generation_id"] != engine._loaded_graph_generation:
                        engine.reload_graph()
                source = request["source"]
                payload = request.get("payload")
                if payload is not None:
                    payload = dict(payload)
                    attempt = int(payload.pop("_delivery_attempt", 0) or 0)
                    payload.update(max_depth=request.get("depth"), full_scan=request["full_scan"])
                    result = engine.apply_index_task(
                        IndexTask(
                            id="writer-task",
                            source_id=source,
                            mode=request["mode"],
                            payload=payload,
                            attempts=attempt,
                        ),
                        on_progress=progress,
                    )
                    results = [result]
                elif source is None:
                    engine.ensure_node_index()
                    results = engine.sync_all(
                        request["mode"],
                        max_depth=request.get("depth"),
                        full_scan=request["full_scan"],
                        on_progress=progress,
                    )
                else:
                    engine.ensure_node_index()
                    results = [
                        engine.sync_source(
                            source,
                            request["mode"],
                            max_depth=request.get("depth"),
                            full_scan=request["full_scan"],
                            on_progress=progress,
                        )
                    ]
                from pheasant.job_snapshots import FAILED_SYNC_STATUSES

                report = {
                    "status": "failed"
                    if any(r.status in FAILED_SYNC_STATUSES for r in results)
                    else "ok",
                    "results": [asdict(r) for r in results],
                    "writer_pid": os.getpid(),
                }
                release = getattr(engine.graph_builder.graph, "release_clean_cache", None)
                if release is not None and report["status"] == "ok":
                    release()
            except Exception as exc:
                # Do not reuse partially-mutated RAM after a failed request.
                logger.exception("writer task failed")
                report = {"status": "failed", "error": str(exc), "results": []}
            print(json.dumps({"marker": "pheasant.writer.result", "report": report}), flush=True)
            if report["status"] != "ok":
                break
    finally:
        engine.close()


if __name__ == "__main__":
    serve(sys.argv[1])
