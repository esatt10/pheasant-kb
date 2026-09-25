"""Background work you can watch: one registry for every long-running job.

A first index of a real repository takes minutes; of a large collection, hours.
Before this, the only thing the UI could say about that was a boolean —
``syncing: true`` on a source row, flipping to nothing when the sync finished —
which is indistinguishable from a hang for as long as it runs. That is the gap
this module closes: every background job gets an id, a phase, counters, a tail
of what it last did, and a terminal outcome that survives the job itself.

**Per source, not per job (Phase 35.1).** A ``sync_all`` over eight sources used
to be one job with one counter, so the one source that was stuck was invisible
behind the seven that were fine — and the counter's denominator changed under
your feet as each source discovered its own file list. Each source now gets its
own :class:`SourceProgress` record; the job-level :class:`JobProgress` is an
aggregate derived from them, kept so existing callers and the UI keep working.

**Throughput and ETA are observed, not reported.** The indexing child process
sends phase/counter updates over the existing progress wire; this module stamps
its own clock on arrival and derives files-per-second over a sliding window and
an ETA from that. Deriving them here rather than sending them keeps the wire
unchanged and means a caller that never emits a rate still gets one.

**"Slow" and "stuck" are different answers.** ``seconds_since_progress`` is
always reported, so a UI can say "last update 4s ago" during healthy work.
``stalled`` only trips after :data:`STALL_AFTER_SECONDS`, which is deliberately
generous: a single large PDF, or an embedding provider serving a retry with
backoff, is slow but fine, and a progress bar that cries wolf gets ignored.

The registry remains in memory for single-process use, and can also publish
atomic JSON snapshots to a shared state directory. API replicas and indexer
processes therefore observe the same job state without adding another service
or making the progress path depend on the queue transport.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from pheasant.ingestion.pipeline import utc_now
from pheasant.telemetry.interactions import inject_traceparent

#: Terminal states. A job in any of these will never change again.
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})

#: How many completed jobs to keep. Running jobs are never evicted.
DEFAULT_MAX_RECORDS = 200

#: Lines of a job's output kept for the UI. The full output goes to the log.
LOG_TAIL = 40

#: Per-source failures retained for display. Enough to see a pattern ("every
#: .docx in this folder"), bounded so a source that fails on all 50,000 files
#: cannot turn a progress record into a memory leak.
FAILURE_TAIL = 25

#: Progress samples kept per source for the rate calculation. At roughly one
#: update per second (the CLI emitter's throttle) this is a ~30s window: long
#: enough not to swing wildly on one slow file, short enough that the ETA
#: reacts when a sync genuinely slows down. A whole-run average would keep
#: quoting the fast early minutes of a pass that has since crawled.
RATE_SAMPLES = 30

#: How long a running source may report nothing before it is called stalled.
#: Generous on purpose — see the module docstring. A big PDF or a rate-limited
#: embedding batch is slow, not stuck.
STALL_AFTER_SECONDS = 300.0

logger = logging.getLogger(__name__)


# The state-writing indexer owns the shared job snapshots in a fleet.  Keep
# this route private and separate from the browser-facing ``/jobs`` surface:
# an API replica has no write permission on the state volume by design.
JOBS_CLEAR_PATH = "/internal/jobs/clear"


class JobClearServiceError(RuntimeError):
    """The indexer could not clear its persisted job notifications."""


class JobClearServiceClient:
    """Authenticated client for the indexer's finished-job clear endpoint."""

    def __init__(
        self,
        base_url: str,
        token_env: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_env = token_env
        self.timeout = max(0.1, float(timeout_seconds))
        # Internal traffic must not be sent through an ambient HTTP proxy.
        self._opener = build_opener(ProxyHandler({}))

    def clear(self, job_id: str | None = None) -> int:
        token = os.environ.get(self.token_env or "", "")
        if not token:
            raise JobClearServiceError(
                f"The job service requires a token in environment variable {self.token_env!r}"
            )
        query = f"?{urlencode({'job_id': job_id})}" if job_id else ""
        request = Request(
            f"{self.base_url}{JOBS_CLEAR_PATH}{query}",
            method="DELETE",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        inject_traceparent(request.headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail")
            except Exception:  # pragma: no cover - a malformed upstream response
                detail = None
            raise JobClearServiceError(detail or exc.reason) from exc
        except (OSError, TimeoutError, URLError, json.JSONDecodeError) as exc:
            raise JobClearServiceError(
                f"job service at {self.base_url!r} could not clear finished jobs: {exc}"
            ) from exc
        try:
            return int(payload["cleared"])
        except (KeyError, TypeError, ValueError) as exc:
            raise JobClearServiceError("job service returned an invalid response") from exc


def _fraction(current: int, total: int | None) -> float | None:
    if not total:
        return None
    return min(1.0, current / total)


@dataclass
class SourceProgress:
    """Where one source has got to, and how fast it is getting there.

    ``total`` is ``None`` until it is known — a sync does not know how many
    files it will index until the connector has finished listing them, and
    inventing a denominator so the bar looks nicer would make it lie.
    """

    source: str
    phase: str = "queued"
    current: int = 0
    total: int | None = None
    detail: str = ""
    status: str = "running"
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_done: int = 0
    started_at: str = field(default_factory=utc_now)
    last_progress_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    #: Cumulative wall time per phase, so "where did the hour go" is answerable.
    phase_seconds: dict[str, float] = field(default_factory=dict)
    failures: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=FAILURE_TAIL))

    # Monotonic bookkeeping. Kept off the wire and out of ``as_dict``: these
    # are for arithmetic, and ``time.monotonic`` values are meaningless to a
    # reader (and incomparable across processes).
    _samples: deque[tuple[float, int]] = field(
        default_factory=lambda: deque(maxlen=RATE_SAMPLES), repr=False
    )
    _phase_started: float = field(default_factory=time.monotonic, repr=False)
    _last_monotonic: float = field(default_factory=time.monotonic, repr=False)

    def observe(
        self,
        *,
        phase: str | None,
        current: int | None,
        total: int | None,
        detail: str | None,
        stats: dict[str, Any] | None,
        now: float,
    ) -> None:
        """Fold one update in. Caller holds the registry lock."""

        if phase is not None and phase != self.phase:
            # Bank the time the outgoing phase took before switching, so a
            # phase that ran twice accumulates rather than overwrites.
            self.phase_seconds[self.phase] = self.phase_seconds.get(self.phase, 0.0) + (
                now - self._phase_started
            )
            self._phase_started = now
            self.phase = phase
            # Counters are phase-local (files while preparing, chunks while
            # embedding). Carrying the old denominator into a new phase whose
            # total is not yet known produces a plausible but false bar, and
            # the rate samples would be comparing different units.
            if total is None:
                self.total = None
            self._samples.clear()
        if total is not None:
            self.total = total
        if current is not None:
            self.current = current
            self._samples.append((now, current))
        if detail is not None:
            self.detail = detail
        for key in ("indexed", "skipped", "failed", "bytes_done"):
            value = (stats or {}).get(key)
            if value is not None:
                setattr(self, key, int(value))
        for failure in (stats or {}).get("failures") or []:
            if isinstance(failure, dict):
                self.failures.append(
                    {"path": str(failure.get("path", "")), "error": str(failure.get("error", ""))}
                )
        self.last_progress_at = utc_now()
        self._last_monotonic = now

    def finish(self, status: str, now: float) -> None:
        self.phase_seconds[self.phase] = self.phase_seconds.get(self.phase, 0.0) + (
            now - self._phase_started
        )
        self._phase_started = now
        self.status = status
        self.finished_at = utc_now()
        if self.total:
            # A source that succeeded with its counter at 812/1000 (because the
            # last files were skipped, not indexed) reads as "stopped early".
            self.current = self.total

    @property
    def active(self) -> bool:
        return self.status not in TERMINAL

    def files_per_second(self, now: float) -> float | None:
        """Throughput over the sample window, or None with too little data."""

        if len(self._samples) < 2:
            return None
        (first_at, first_count), (last_at, last_count) = self._samples[0], self._samples[-1]
        elapsed = last_at - first_at
        advanced = last_count - first_count
        if elapsed <= 0 or advanced <= 0:
            return None
        return advanced / elapsed

    def eta_seconds(self, now: float) -> float | None:
        rate = self.files_per_second(now)
        if not rate or not self.total:
            return None
        remaining = max(0, self.total - self.current)
        return remaining / rate

    def seconds_since_progress(self, now: float) -> float:
        return max(0.0, now - self._last_monotonic)

    def stalled(self, now: float) -> bool:
        return self.active and self.seconds_since_progress(now) > STALL_AFTER_SECONDS

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        elapsed_phase = self.phase_seconds.get(self.phase, 0.0)
        if self.active:
            elapsed_phase += now - self._phase_started
        return {
            "source": self.source,
            "phase": self.phase,
            "current": self.current,
            "total": self.total,
            "detail": self.detail,
            "status": self.status,
            "active": self.active,
            "fraction": _fraction(self.current, self.total),
            "indexed": self.indexed,
            "skipped": self.skipped,
            "failed": self.failed,
            "bytes_done": self.bytes_done,
            "files_per_second": self.files_per_second(now),
            "eta_seconds": self.eta_seconds(now),
            "seconds_since_progress": self.seconds_since_progress(now),
            "stalled": self.stalled(now),
            "started_at": self.started_at,
            "last_progress_at": self.last_progress_at,
            "finished_at": self.finished_at,
            "phase_seconds": {
                **self.phase_seconds,
                **({self.phase: elapsed_phase} if self.active else {}),
            },
            "failures": list(self.failures),
        }


@dataclass
class JobProgress:
    """Job-level rollup. Derived from the per-source records, never authored.

    Kept because it is what the pre-35.1 API and UI read. ``total`` stays
    ``None`` until *every* running source knows its own — a partial sum would
    shrink the denominator as sources report in and run the bar backwards.
    """

    phase: str = "starting"
    current: int = 0
    total: int | None = None
    detail: str = ""

    @property
    def fraction(self) -> float | None:
        return _fraction(self.current, self.total)


@dataclass
class Job:
    id: str
    kind: str
    label: str
    targets: list[str] = field(default_factory=list)
    status: str = "queued"
    progress: JobProgress = field(default_factory=JobProgress)
    started_at: str = ""
    finished_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_TAIL))
    sources: dict[str, SourceProgress] = field(default_factory=dict)

    def source_record(self, name: str) -> SourceProgress:
        record = self.sources.get(name)
        if record is None:
            record = SourceProgress(source=name)
            self.sources[name] = record
        return record

    def recompute(self, now: float) -> None:
        """Refresh the rollup from the per-source records."""

        records = list(self.sources.values())
        if not records:
            return
        active = [r for r in records if r.active] or records
        self.progress.current = sum(r.current for r in records)
        totals = [r.total for r in records]
        self.progress.total = sum(t for t in totals if t) if all(t for t in totals) else None
        # The least-advanced running phase is the honest headline: a job is not
        # "saving" because one of its eight sources is.
        newest = max(active, key=lambda r: r._last_monotonic)
        self.progress.phase = newest.phase
        self.progress.detail = (
            f"[{newest.source}] {newest.detail}" if len(records) > 1 else newest.detail
        )

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        payload = asdict(
            self,
            dict_factory=lambda items: {k: v for k, v in items if k not in {"log", "sources"}},
        )
        payload["log"] = list(self.log)
        payload["progress"]["fraction"] = self.progress.fraction
        payload["active"] = self.status not in TERMINAL
        payload["sources"] = [
            record.as_dict(now) for record in sorted(self.sources.values(), key=lambda r: r.source)
        ]
        payload["stalled"] = any(record.stalled(now) for record in self.sources.values())
        payload["failed_files"] = sum(record.failed for record in self.sources.values())
        return payload


class JobSnapshotStore:
    """Atomic cross-process snapshots for role-split deployments.

    Indexers own the writable state mount and publish one JSON file per job;
    API replicas mount the same directory read-only and only list/read it.
    Atomic replacement means a reader sees either the previous complete
    update or the next complete update, never a partially written payload.
    """

    def __init__(self, path: str | Path, max_records: int = DEFAULT_MAX_RECORDS) -> None:
        self.path = Path(path)
        self.max_records = max(1, int(max_records))

    def write(self, payload: dict[str, Any]) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        job_id = str(payload.get("id") or "")
        if not job_id:
            return
        temporary = self.path / (
            f".{job_id}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
        )
        target = self.path / f"{job_id}.json"
        try:
            temporary.write_text(
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        self._evict()

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._read(self.path / f"{job_id}.json")

    def list(self) -> list[dict[str, Any]]:
        if not self.path.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for path in self.path.glob("*.json"):
            record = self._read(path)
            if record is not None:
                records.append(record)
        return records

    def clear_finished(self, job_id: str | None = None) -> set[str]:
        removed: set[str] = set()
        candidates = [self.path / f"{job_id}.json"] if job_id else list(self.path.glob("*.json"))
        for path in candidates:
            record = self._read(path)
            if record is None or bool(record.get("active")):
                continue
            try:
                path.unlink()
            except OSError:
                # A read-only API replica can still display shared jobs; only
                # the writable indexer can physically clear their snapshots.
                continue
            removed.add(str(record.get("id") or path.stem))
        return removed

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict) or not payload.get("id"):
            return None
        return _refresh_snapshot(payload)

    def _evict(self) -> None:
        records = self.list()
        if len(records) <= self.max_records:
            return
        finished = sorted(
            (record for record in records if not record.get("active")),
            key=lambda record: str(record.get("finished_at") or record.get("started_at") or ""),
        )
        for record in finished[: max(0, len(records) - self.max_records)]:
            try:
                (self.path / f"{record['id']}.json").unlink()
            except OSError:
                return


def _refresh_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Advance wall-clock fields after a snapshot crosses process boundaries."""

    now = datetime.now(UTC)
    stalled = False
    for source in payload.get("sources") or []:
        if not isinstance(source, dict):
            continue
        timestamp = source.get("last_progress_at")
        seconds = float(source.get("seconds_since_progress") or 0.0)
        if timestamp:
            try:
                then = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                seconds = max(0.0, (now - then).total_seconds())
            except ValueError:
                pass
        source["seconds_since_progress"] = seconds
        source["stalled"] = bool(source.get("active")) and seconds > STALL_AFTER_SECONDS
        stalled = stalled or bool(source["stalled"])
    payload["stalled"] = stalled
    return payload


class JobRegistry:
    """Thread-safe registry of background jobs, with a subscription stream.

    Every mutation is under one lock and every reader gets a snapshot: jobs are
    written from worker threads and read from request threads, and handing out
    a live object would let a response serialize a half-updated record.
    """

    def __init__(
        self,
        max_records: int = DEFAULT_MAX_RECORDS,
        *,
        shared_path: str | Path | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._max_records = max_records
        self._subscribers: list[Queue] = []
        self._shared = JobSnapshotStore(shared_path, max_records) if shared_path else None

    # -- lifecycle --------------------------------------------------------

    def create(
        self,
        kind: str,
        label: str,
        targets: list[str] | None = None,
        *,
        job_id: str | None = None,
    ) -> Job:
        targets = list(targets or [])
        job = Job(
            id=job_id or uuid.uuid4().hex[:12],
            kind=kind,
            label=label,
            targets=targets,
            status="running",
            started_at=utc_now(),
        )
        # Seed a record per target so a source that has not reported yet still
        # shows as queued rather than being missing from the UI entirely —
        # "nothing here" and "waiting its turn" are different answers.
        for target in targets:
            job.source_record(target)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._evict()
        self._publish(job)
        return job

    def progress(
        self,
        job_id: str,
        *,
        phase: str | None = None,
        current: int | None = None,
        total: int | None = None,
        detail: str | None = None,
        source: str | None = None,
        stats: dict[str, Any] | None = None,
    ) -> None:
        """Record forward movement. Unknown job ids are ignored, not raised.

        A progress callback fires from deep inside the indexing loop; making it
        capable of failing a sync — because a job was evicted, or the registry
        was swapped in a test — would be a poor trade for a cosmetic feature.

        ``source`` names which source the update is about. When it is absent
        (an older caller, or a job with a single target) the update is applied
        to the job's only source record, so nothing regresses.
        """
        now = time.monotonic()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in TERMINAL:
                return
            name = source
            if name is None:
                names = list(job.sources)
                name = names[0] if len(names) == 1 else "*"
            elif name != "*":
                # A source-less bootstrap update on a multi-source job is
                # represented by a synthetic ``*`` record.  Once the worker
                # reports a real source, that placeholder must disappear;
                # otherwise the UI keeps showing "waiting_for_indexer" next
                # to sources that are visibly indexing, and the placeholder
                # also prevents a complete job total from being calculated.
                job.sources.pop("*", None)
            record = job.source_record(name)
            record.observe(
                phase=phase,
                current=current,
                total=total,
                detail=detail,
                stats=stats,
                now=now,
            )
            if detail is not None:
                prefix = f"[{name}] " if name != "*" and len(job.sources) > 1 else ""
                job.log.append(f"{utc_now()} {prefix}{detail}")
            job.recompute(now)
            snapshot = job
        self._publish(snapshot)

    def finish(
        self,
        job_id: str,
        status: str = "succeeded",
        *,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        now = time.monotonic()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = status
            job.error = error
            job.result = result
            job.finished_at = utc_now()
            for record in job.sources.values():
                if record.active:
                    record.finish(status, now)
            job.progress.phase = status
            job.recompute(now)
            if job.progress.total:
                job.progress.current = job.progress.total
            snapshot = job
        self._publish(snapshot)

    def finish_source(self, job_id: str, source: str, status: str = "succeeded") -> None:
        """Mark one source terminal while the job carries on.

        Without this a `sync_all` shows all eight sources running until the
        slowest finishes, which hides exactly the information the per-source
        breakdown exists to give.
        """
        now = time.monotonic()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in TERMINAL:
                return
            record = job.sources.get(source)
            if record is None or not record.active:
                return
            record.finish(status, now)
            job.recompute(now)
            snapshot = job
        self._publish(snapshot)

    # -- reading ----------------------------------------------------------

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            local = job.as_dict() if job else None
        if local is not None:
            return local
        return self._shared.get(job_id) if self._shared is not None else None

    def list(self, *, active_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first. Running jobs always sort ahead of finished ones.

        Serialization happens **inside** the lock, like :meth:`get`. Releasing
        it first handed out live `Job` objects and rendered them afterwards, so
        `as_dict` walked `job.sources` while an indexing thread was adding
        entries — `RuntimeError: dictionary changed size during iteration`, on
        a route the UI polls once a second during exactly the multi-source sync
        that makes it happen. Found by the concurrency test written for the
        identical defect in `_publish`; the two shared a cause, not a line.
        """
        with self._lock:
            local = {
                job_id: self._jobs[job_id].as_dict()
                for job_id in self._order
                if job_id in self._jobs
            }
        shared = self._shared.list() if self._shared is not None else []
        merged = {str(record["id"]): record for record in shared}
        merged.update(local)
        records = list(merged.values())
        if active_only:
            records = [record for record in records if bool(record.get("active"))]
        active = sorted(
            (record for record in records if bool(record.get("active"))),
            key=lambda record: str(record.get("started_at") or ""),
            reverse=True,
        )
        done = sorted(
            (record for record in records if not bool(record.get("active"))),
            key=lambda record: str(record.get("finished_at") or record.get("started_at") or ""),
            reverse=True,
        )
        return (active + done)[:limit]

    def active_for(self, target: str) -> Job | None:
        with self._lock:
            for job_id in reversed(self._order):
                job = self._jobs.get(job_id)
                if job and job.status not in TERMINAL and target in job.targets:
                    return job
        return None

    def last_outcome_for(self, target: str) -> Job | None:
        """The most recent *finished* job that touched ``target``.

        The UI needs this because ``active`` alone flickers to nothing the
        instant a job completes — which is precisely when a caller most wants
        to know whether it succeeded.
        """
        with self._lock:
            for job_id in reversed(self._order):
                job = self._jobs.get(job_id)
                if job and job.status in TERMINAL and target in job.targets:
                    return job
        return None

    def active_snapshot_for(self, target: str) -> dict[str, Any] | None:
        """Newest active local or shared job touching ``target``."""

        return next(
            (
                record
                for record in self.list(active_only=True, limit=self._max_records)
                if target in (record.get("targets") or [])
            ),
            None,
        )

    def last_outcome_snapshot_for(self, target: str) -> dict[str, Any] | None:
        return next(
            (
                record
                for record in self.list(limit=self._max_records)
                if not record.get("active") and target in (record.get("targets") or [])
            ),
            None,
        )

    def source_progress(self, target: str) -> dict[str, Any] | None:
        """The live per-source record for ``target``, if one is running."""

        now = time.monotonic()
        with self._lock:
            for job_id in reversed(self._order):
                job = self._jobs.get(job_id)
                if job is None or job.status in TERMINAL:
                    continue
                record = job.sources.get(target)
                if record is not None:
                    return record.as_dict(now)
        active = self.active_snapshot_for(target)
        if active is None:
            return None
        return next(
            (
                record
                for record in (active.get("sources") or [])
                if record.get("source") == target and record.get("active")
            ),
            None,
        )

    def metrics_sample(self) -> dict[str, Any]:
        """Gauge values for the metrics endpoint, sampled at scrape time.

        Returns plain numbers and ``{source: value}`` maps, which is the shape
        :func:`pheasant.telemetry.metrics.render_with` consumes — this module
        stays free of any dependency on the telemetry package.
        """

        ratio: dict[str, float] = {}
        rate: dict[str, float] = {}
        eta: dict[str, float] = {}
        stalled: dict[str, float] = {}
        preparation_backlog = 0
        queued = 0
        inflight = 0
        for job in self.list(active_only=True, limit=self._max_records):
            inflight += 1
            for record in job.get("sources") or []:
                if not record.get("active"):
                    continue
                queued += 1
                name = str(record.get("source") or "")
                fraction = record.get("fraction")
                if fraction is not None:
                    ratio[name] = float(fraction)
                observed = record.get("files_per_second")
                if observed is not None:
                    rate[name] = float(observed)
                remaining = record.get("eta_seconds")
                if remaining is not None:
                    eta[name] = float(remaining)
                stalled[name] = 1.0 if record.get("stalled") else 0.0
                if record.get("phase") == "preparing":
                    current = int(record.get("current") or 0)
                    total = record.get("total")
                    if total is not None:
                        preparation_backlog += max(0, int(total) - current)
        return {
            "pheasant_index_queue_depth": queued,
            "pheasant_index_inflight": inflight,
            "pheasant_index_progress_ratio": ratio,
            "pheasant_index_files_per_second": rate,
            "pheasant_index_eta_seconds": eta,
            "pheasant_index_stalled": stalled,
            "pheasant_index_preparation_backlog": preparation_backlog,
        }

    def clear(self, job_id: str | None = None) -> int:
        """Remove finished notifications; active work is never cancelled."""
        removed_ids: set[str] = set()
        with self._lock:
            selected = [job_id] if job_id is not None else list(self._order)
            for candidate in selected:
                job = self._jobs.get(candidate)
                if job is not None and job.status in TERMINAL:
                    self._jobs.pop(candidate, None)
                    removed_ids.add(candidate)
            if removed_ids:
                self._order = [candidate for candidate in self._order if candidate in self._jobs]
        if self._shared is not None:
            removed_ids.update(self._shared.clear_finished(job_id))
        return len(removed_ids)

    # -- streaming --------------------------------------------------------

    def subscribe(self) -> Queue:
        queue: Queue = Queue(maxsize=256)
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: Queue) -> None:
        with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    def drain(self, queue: Queue) -> list[dict[str, Any]]:
        """Everything waiting on a subscriber's queue, without blocking.

        Deliberately non-blocking. A ``queue.get(timeout=…)`` helper used to
        live here and it was a trap: Starlette runs a sync generator in a
        threadpool it cannot interrupt, so an SSE client that disconnected
        left a thread blocked on a queue nobody would ever write to again —
        one leaked thread per dropped connection. Consumers poll this from an
        async loop instead, which cancels cleanly.
        """
        items: list[dict[str, Any]] = []
        while True:
            try:
                items.append(queue.get_nowait())
            except Empty:
                return items

    def _publish(self, job: Job) -> None:
        # Serialized **under** the lock. `snapshot = job` in the callers above
        # is a reference, not a copy, so the job keeps mutating while it is
        # being rendered: `as_dict` walks `job.sources` and `job.log`, both of
        # which the indexing thread writes to on every artifact. That is a
        # torn payload at best and `RuntimeError: dictionary changed size
        # during iteration` — raised from inside a progress callback, on the
        # sync path — at worst. Every call site releases the lock before
        # calling this, so taking it here cannot deadlock.
        with self._lock:
            payload = job.as_dict()
            subscribers = list(self._subscribers)
        if self._shared is not None:
            try:
                self._shared.write(payload)
            except OSError:
                # Progress is observability, never a reason to fail indexing.
                logger.debug("could not persist shared job snapshot", exc_info=True)
        for queue in subscribers:
            try:
                queue.put_nowait(payload)
            except Exception:
                # A subscriber that cannot keep up loses updates rather than
                # blocking the job that is producing them. The next update, and
                # the terminal one, still arrive.
                pass

    def _evict(self) -> None:
        """Drop the oldest finished jobs past the cap. Lock held."""
        if len(self._order) <= self._max_records:
            return
        keep: list[str] = []
        removable = len(self._order) - self._max_records
        for job_id in self._order:
            job = self._jobs.get(job_id)
            if removable > 0 and job is not None and job.status in TERMINAL:
                self._jobs.pop(job_id, None)
                removable -= 1
                continue
            keep.append(job_id)
        self._order = keep
