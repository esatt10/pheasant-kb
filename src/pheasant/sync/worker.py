"""Run a sync in a separate process, so indexing cannot starve serving.

Indexing is CPU-bound Python. In the server process that means the GIL: while
a sync runs, request threads barely get scheduled. Measured on a live index —
one model call took 0.57s idle and **107s** during a sync, and search latency
swung by 4x. Cooperative yielding (:mod:`pheasant.sync.pacing`) softened that;
it could not remove it, because the work and the serving still shared one
interpreter.

So the server does not index. It runs ``python -m pheasant sync`` as a child
process, which owns the CPU cost in its own interpreter, writes to ``/state``
the way any other client would, and reports its result on stdout. The parent
then reloads the graph from disk — cheap now that it is compressed — and keeps
serving throughout.

Nothing about correctness changes: the child takes the same single-writer
lease (:class:`~pheasant.sync.locks.EngineLease`) the in-process path took, so
two writers still cannot overlap. ``SyncEngine.sync_source`` remains the
in-process path, used by the CLI, the tests, and anyone embedding pheasant.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


#: Progress lines carry this marker so the parent can tell them apart from the
#: final report (which is also JSON on stdout) without guessing at shape.
#:
#: Defined here rather than in `cli.py` because it is a protocol between two
#: processes and this is the side that *parses* it — the CLI emits it, and a
#: domain module importing the CLI to learn its own wire format is the
#: dependency running the wrong way.
PROGRESS_MARKER = "pheasant.progress"

#: A sync that has produced no output at all for this long is presumed hung.
DEFAULT_TIMEOUT_S = 6 * 60 * 60
DEFAULT_INACTIVITY_TIMEOUT_S = 30 * 60
# Leave a little time for the child to report a useful lease-timeout error
# before the parent subprocess timeout expires.
DEFAULT_LEASE_WAIT_S = DEFAULT_TIMEOUT_S - 60


#: A history snapshot past this is abandoned; the next due window retries.
SNAPSHOT_TIMEOUT_S = 30 * 60

#: One background snapshot per server process at a time.
_snapshot_lock = threading.Lock()
_snapshot_job: threading.Thread | None = None


def run_snapshot(config_path: str | Path, *, timeout: float = SNAPSHOT_TIMEOUT_S) -> dict[str, Any]:
    """Take the interval's graph snapshot in a child process; return its report.

    A child for the same reason a sync is one: reading a region's rows back
    is a CPU-bound Python pass, and the server's interpreter answers queries.
    See :func:`pheasant.sync.graph_history.snapshot_published`.
    """

    command = [sys.executable, "-m", "pheasant.sync.graph_history", "--config", str(config_path)]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        logger.warning("Graph snapshot child timed out after %ss", timeout)
        return {"status": "timeout"}
    if completed.returncode != 0:
        tail = (completed.stderr or "").strip().splitlines()[-3:]
        logger.warning("Graph snapshot child failed: %s", " / ".join(tail))
        return {"status": "failed"}
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    return json.loads(lines[-1]) if lines else {"status": "ok"}


class SyncWorkerStalled(RuntimeError):
    """A child stayed alive but emitted no progress for the stall budget."""


def run_sync(
    config_path: str | Path,
    source_name: str | None = None,
    mode: str = "incremental",
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    full_scan: bool = False,
    depth: int | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    task_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Index in a child process and return its report.

    The report mirrors :class:`~pheasant.sync.engine.SyncResult` fields when
    the child succeeds. A non-zero exit is reported, not raised: a failed sync
    must not take down the process that is still serving queries.

    With ``on_progress`` the child is run with ``--progress`` and its stdout is
    **streamed** rather than buffered, so the parent sees each NDJSON progress
    line as it is written. Without it, nothing changes: the child is not asked
    for progress and the output is simply collected.
    """

    command = [
        sys.executable,
        "-m",
        "pheasant",
        "sync",
        "--config",
        str(config_path),
        "--mode",
        mode,
        "--json",
        "--worker-child",
    ]
    if source_name:
        command += ["--source", source_name]
    else:
        command += ["--all"]
    if full_scan:
        command += ["--full-scan"]
    if depth is not None:
        command += ["--depth", str(depth)]
    if on_progress is not None:
        command += ["--progress"]
    payload_file: str | None = None
    if task_payload:
        # A file, not an argument: Linux refuses any single argument over
        # 128 KiB (MAX_ARG_STRLEN), and a task carrying a source's whole
        # configuration -- a 4,698-URL web collection's is ~330 KB -- failed
        # every sync with `[Errno 7] Argument list too long` before the child
        # started. Private to this user (mkstemp), removed once the child exits.
        import tempfile

        fd, payload_file = tempfile.mkstemp(prefix="pheasant-task-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(task_payload, handle, sort_keys=True, separators=(",", ":"))
        command += ["--task-payload-file", payload_file]
    # Server-owned workers queue behind another legitimate writer. The public
    # CLI remains fail-fast, which is important for scripts and diagnostics.
    command += ["--wait-for-lease", str(DEFAULT_LEASE_WAIT_S)]

    logger.info("Starting sync worker: %s", " ".join(command[3:]))
    try:
        returncode, stdout, stderr = _run_streaming(command, timeout, on_progress)
    except SyncWorkerStalled as exc:
        logger.error("Sync worker stalled: %s", exc)
        return {
            "status": "timeout",
            "source_id": source_name,
            "error": str(exc),
            "results": [],
        }
    except subprocess.TimeoutExpired:
        logger.error("Sync worker timed out after %.0fs", timeout)
        return {"status": "timeout", "source_id": source_name, "results": []}
    finally:
        if payload_file is not None:
            Path(payload_file).unlink(missing_ok=True)

    if returncode != 0:
        # stderr carries the child's traceback; surface the tail, which is the
        # part that says what actually went wrong.
        tail = (stderr or "").strip().splitlines()[-5:]
        logger.error("Sync worker failed (exit %s): %s", returncode, " / ".join(tail))
        return {
            "status": "failed",
            "source_id": source_name,
            "error": "\n".join(tail),
            "results": [],
        }

    return _parse_report(stdout, source_name)


def _run_streaming(
    command: list[str],
    timeout: float,
    on_progress: Callable[[dict[str, Any]], None] | None,
) -> tuple[int, str, str]:
    """Run the child, forwarding progress lines as they arrive.

    Without a progress callback this is ``subprocess.run`` and behaves exactly
    as it always did. With one, stdout is read line by line — the only way to
    see progress *during* a multi-minute sync rather than after it — while
    stderr stays fully buffered, because it is only read on failure and
    draining two pipes by hand is how you deadlock on a full one.
    """
    # This process takes the child's history snapshot once it reports
    # (`WorkerBackedEngine._snapshot_in_background`), so the child need not.
    from pheasant.sync.graph_history import SNAPSHOT_BY_SUPERVISOR

    env = {**os.environ, SNAPSHOT_BY_SUPERVISOR: "1"}
    if on_progress is None:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False, env=env
        )
        return completed.returncode, completed.stdout, completed.stderr

    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    ) as process:
        assert process.stdout is not None
        assert process.stderr is not None
        return _monitor_streaming_process(
            process,
            command,
            timeout,
            on_progress,
            PROGRESS_MARKER,
        )


def _monitor_streaming_process(
    process: subprocess.Popen,
    command: list[str],
    timeout: float,
    on_progress: Callable[[dict[str, Any]], None],
    progress_marker: str,
) -> tuple[int, str, str]:
    """Drain both pipes while enforcing total and no-progress deadlines.

    Reading stdout in the caller thread made its later ``wait(timeout=...)``
    unreachable until the child exited. It also left stderr undrained, so a
    noisy failure could fill that pipe and deadlock both processes. Dedicated
    readers make the watchdog independent of either pipe's activity.
    """

    import json as _json

    assert process.stdout is not None
    assert process.stderr is not None
    events: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def read_stream(name: str, stream) -> None:  # type: ignore[no-untyped-def]
        try:
            for raw in stream:
                events.put((name, raw.rstrip("\n")))
        finally:
            events.put((name, None))

    readers = [
        threading.Thread(
            target=read_stream,
            args=("stdout", process.stdout),
            name="pheasant-sync-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=read_stream,
            args=("stderr", process.stderr),
            name="pheasant-sync-stderr",
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    collected: list[str] = []
    errors: list[str] = []
    started = time.monotonic()
    deadline = started + max(0.0, float(timeout))
    last_progress = started
    stall_budget = max(0.01, float(DEFAULT_INACTIVITY_TIMEOUT_S))
    ended: set[str] = set()

    while len(ended) < 2:
        now = time.monotonic()
        if now >= deadline:
            process.kill()
            process.wait(timeout=5)
            raise subprocess.TimeoutExpired(command, timeout)
        silent_for = now - last_progress
        if silent_for >= stall_budget:
            process.kill()
            process.wait(timeout=5)
            raise SyncWorkerStalled(
                f"no progress for {stall_budget:.0f}s; child was terminated so its "
                "queue task can be retried"
            )
        wait_for = min(0.5, deadline - now, stall_budget - silent_for)
        try:
            stream_name, line = events.get(timeout=max(0.01, wait_for))
        except queue.Empty:
            continue
        if line is None:
            ended.add(stream_name)
            continue
        if stream_name == "stderr":
            errors.append(line)
            continue

        # Only stdout is protocol progress. Repeated stderr warnings from a
        # stuck child must not keep the watchdog alive indefinitely.
        last_progress = time.monotonic()
        if not line:
            continue
        payload = None
        if line.startswith("{"):
            try:
                payload = _json.loads(line)
            except _json.JSONDecodeError:
                payload = None
        if isinstance(payload, dict) and payload.get("marker") == progress_marker:
            try:
                on_progress(payload)
            except Exception:  # pragma: no cover - never fail a sync for this
                logger.debug("progress forwarding failed", exc_info=True)
            continue
        collected.append(line)

    try:
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise
    for reader in readers:
        reader.join(timeout=1)
    return process.returncode, "\n".join(collected), "\n".join(errors)


class WorkerBackedEngine:
    """A :class:`SyncEngine` whose syncs happen in a child process.

    Everything else — the graph, the search index, the state store — is the
    real engine, reached by attribute proxying. Only the three methods that
    *index* are replaced, so the watcher, the scheduler and the startup pass
    keep calling exactly what they always called and simply stop competing
    with the server for CPU.
    """

    def __init__(self, engine: Any, config_path: str | Path) -> None:
        # Bypass __getattr__ for our own attributes.
        object.__setattr__(self, "_engine", engine)
        object.__setattr__(self, "_config_path", str(config_path))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)

    def _can_delegate(self) -> bool:
        """Can a child process reconstruct *this* engine from disk?

        Existence of the config file is not enough: it has to describe the
        same state directory. An engine built from an in-memory config —
        embedded use, `pheasant up`, and every test that calls
        ``create_app(config=...)`` — may point at a config file that is a stub
        or belongs to a different knowledge base, and handing that to a child
        would index the wrong directory entirely. When in doubt we index
        in-process: correct, just not isolated.

        The answer cannot change while the process runs, so it is computed
        once.
        """

        cached = getattr(self, "_delegate", None)
        if cached is not None:
            return cached

        decision = False
        try:
            path = Path(self._config_path)
            if path.is_file():
                from pheasant.config.loader import load_config
                from pheasant.persistence.paths import StatePaths

                on_disk = StatePaths.from_config(load_config(path)).sqlite
                decision = Path(on_disk).resolve() == Path(self._engine.paths.sqlite).resolve()
                if not decision:
                    logger.info(
                        "Config at %s describes a different state directory; "
                        "indexing in-process instead of in a worker",
                        path,
                    )
        except Exception as exc:  # pragma: no cover - unreadable config
            logger.debug("Cannot delegate indexing to a worker (%s)", exc)
            decision = False

        object.__setattr__(self, "_delegate", decision)
        return decision

    def sync_source(
        self,
        source_name: str,
        mode: str = "incremental",
        *,
        max_depth: int | None = None,
        full_scan: bool = False,
        on_progress: Any = None,
    ) -> Any:
        if not self._can_delegate():
            # In-process fallback: the engine's hook takes four positional
            # arguments, the worker's takes one dict. Adapt rather than make
            # callers care which path they landed on.
            return self._engine.sync_source(
                source_name,
                mode,
                max_depth=max_depth,
                full_scan=full_scan,
                on_progress=_as_engine_hook(on_progress),
            )
        report = run_sync(
            self._config_path,
            source_name,
            mode,
            depth=max_depth,
            full_scan=full_scan,
            on_progress=on_progress,
        )
        results = self._adopt(report)
        self._snapshot_in_background()
        return results[0] if results else self._empty(source_name, report)

    def apply_index_task(self, task: Any, *, on_progress: Any = None) -> Any:
        """Run an ordinary sync or queued graph control operation in a child."""

        if not self._can_delegate():
            return self._engine.apply_index_task(
                task,
                on_progress=_as_engine_hook(on_progress),
            )
        report = run_sync(
            self._config_path,
            str(task.source_id),
            str(task.mode),
            full_scan=task.full_scan,
            depth=task.max_depth,
            on_progress=on_progress,
            task_payload={
                **dict(task.payload or {}),
                "_delivery_attempt": int(getattr(task, "attempts", 0) or 0),
            },
        )
        results = self._adopt(report)
        self._snapshot_in_background()
        return results[0] if results else self._empty(str(task.source_id), report)

    def sync_all(
        self,
        mode: str = "incremental",
        *,
        max_depth: int | None = None,
        full_scan: bool = False,
        on_progress: Any = None,
    ) -> list[Any]:
        if not self._can_delegate():
            return self._engine.sync_all(
                mode,
                max_depth=max_depth,
                full_scan=full_scan,
                on_progress=_as_engine_hook(on_progress),
            )
        report = run_sync(
            self._config_path,
            None,
            mode,
            depth=max_depth,
            full_scan=full_scan,
            on_progress=on_progress,
        )
        results = self._adopt(report)
        self._snapshot_in_background()
        # A child-process failure has no per-source result payload. Returning
        # an empty list made callers conclude that there were zero failures
        # and mark the UI job succeeded even though the worker exited nonzero.
        # Preserve the failure as one synthetic result so every caller's
        # existing ``status in {failed, timeout}`` check sees it.
        if not results and report.get("status") != "ok":
            return [self._empty(None, report)]
        return results

    def startup(self) -> list[Any]:
        engine = self._engine
        engine.paths.ensure()
        engine.state.migrate()
        # Deliberately NOT rebuilding the node index here: that is hundreds of
        # thousands of rows of CPU work, and doing it in the serving process is
        # the exact starvation this class exists to avoid (it cost a 95s graph
        # search when it ran here). The worker builds it — see the CLI sync
        # path — and until it exists, graph search falls back to scanning.
        engine.reconcile_embeddings()
        enabled = engine.enabled_sources()
        names = [source.name for source in enabled if source.sync.on_startup]
        # The normal deployment starts every enabled source. Delegate that as
        # one sync-all child so the engine can apply its bounded source-level
        # parallelism and rebuild the shared graph index once at the end.
        if names and len(names) == len(enabled) and self._can_delegate():
            return self.sync_all("incremental")
        results = []
        for name in names:
            results.append(self.sync_source(name, "incremental"))
        return results

    def _snapshot_in_background(self) -> None:
        """Start the interval's history snapshot, if due, beside rather than in a sync.

        Only on the row backend, whose children defer it (their graph is not
        in memory, so the snapshot reads the rows back); the file backend's
        snapshot is a byte copy the child still takes inline. Single-flight:
        a sync finishing while one runs does not start a second.
        """

        global _snapshot_job
        from pheasant.ingestion.pipeline import utc_now
        from pheasant.sync.graph_history import snapshot_due

        engine = self._engine
        try:
            storage = engine.config.storage
            if not storage.graph_snapshots or engine.graph_store.rows is None:
                return
            if not snapshot_due(
                engine.graph_store,
                engine.config.knowledge_base_id,
                utc_now(),
                storage.graph_snapshot_interval_seconds,
            ):
                return
        except Exception:  # noqa: BLE001 - history is never load-bearing for a sync
            logger.debug("Could not tell whether a graph snapshot is due", exc_info=True)
            return
        with _snapshot_lock:
            if _snapshot_job is not None and _snapshot_job.is_alive():
                return
            _snapshot_job = threading.Thread(
                target=run_snapshot,
                args=(self._config_path,),
                name="pheasant-graph-snapshot",
                daemon=True,
            )
            _snapshot_job.start()

    def _adopt(self, report: dict[str, Any]) -> list[Any]:
        """Turn the worker's report into SyncResults and pick up its graph."""

        from pheasant.sync.engine import SyncResult

        results = [
            SyncResult(
                source_id=str(item.get("source_id") or ""),
                indexed_artifacts=int(item.get("indexed_artifacts") or 0),
                skipped_artifacts=int(item.get("skipped_artifacts") or 0),
                graph_nodes=int(item.get("graph_nodes") or 0),
                graph_edges=int(item.get("graph_edges") or 0),
                status=str(item.get("status") or "unknown"),
                details=item.get("details") or {},
            )
            for item in report.get("results") or []
        ]
        if any(r.indexed_artifacts for r in results) or report.get("status") == "ok":
            try:
                self._engine.reload_graph()
            except Exception as exc:  # pragma: no cover - reload is best effort
                logger.warning("Could not reload the graph after a worker sync: %s", exc)
        return results

    @staticmethod
    def _empty(source_name: str | None, report: dict[str, Any]) -> Any:
        from pheasant.sync.engine import SyncResult

        return SyncResult(
            source_id=source_name or "",
            indexed_artifacts=0,
            skipped_artifacts=0,
            graph_nodes=0,
            graph_edges=0,
            status=str(report.get("status") or "unknown"),
            details={"error": report.get("error")} if report.get("error") else {},
        )


def _as_engine_hook(on_progress: Callable[[dict[str, Any]], None] | None):
    """Adapt the worker's dict-shaped callback to the engine's positional one.

    The two differ because they cross different boundaries: the worker's
    arrives as parsed NDJSON from a child process, the engine's is a plain
    call inside one. Callers should not have to know which path ran.
    """
    if on_progress is None:
        return None

    def hook(phase: str, current: int, total: int | None, detail: str) -> None:
        on_progress({"phase": phase, "current": current, "total": total, "detail": detail})

    return hook


def _parse_report(stdout: str, source_name: str | None) -> dict[str, Any]:
    """Pull the JSON report out of the child's output.

    Anything the child logged before the report is ignored rather than fatal —
    a stray warning on stdout should not turn a good sync into a failed one.
    """

    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "results" in payload:
            return payload
    return {"status": "unknown", "source_id": source_name, "results": []}
