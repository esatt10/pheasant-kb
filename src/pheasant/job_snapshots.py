"""Job snapshots shared across processes, and how a reader knows one is dead.

`jobs.JobRegistry` keeps jobs in memory and, in a role-split deployment,
publishes each as an atomic JSON file under ``<state>/jobs`` so API replicas
can show what an indexer is doing. Split from `jobs.py` when the snapshot
store learned to tell a running job from an orphaned one, which is the part
here worth reading on its own:

a snapshot is rewritten every :data:`SNAPSHOT_HEARTBEAT_SECONDS` while its job
runs, so the file's age is the evidence. A process that dies mid-job (a
container recreated during a sync) or a task dead-lettered at claim, never
run, writes nothing more, and its last snapshot said "running" forever:
nothing else ever ends it, and ``/jobs`` refuses to clear a running job. A
running snapshot silent past :data:`SNAPSHOT_STALE_SECONDS` therefore reads as
failed, and a process that can write the directory rewrites it so.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: How long a running source may report nothing before it is called stalled.
#: Generous on purpose — see the module docstring. A big PDF or a rate-limited
#: embedding batch is slow, not stuck.
STALL_AFTER_SECONDS = 300.0

#: A shared snapshot is rewritten this often while its job runs, progress or
#: not, so its file's age says whether the process behind it is alive. A
#: running snapshot older than SNAPSHOT_STALE_SECONDS lost its process (a
#: restarted container, a task dead-lettered at claim) and reads as failed;
#: without this it said "running" forever, because nothing else ends it.
SNAPSHOT_HEARTBEAT_SECONDS = 30.0
SNAPSHOT_STALE_SECONDS = 150.0


class JobSnapshotStore:
    """Atomic cross-process snapshots for role-split deployments.

    Indexers own the writable state mount and publish one JSON file per job;
    API replicas mount the same directory read-only and only list/read it.
    Atomic replacement means a reader sees either the previous complete
    update or the next complete update, never a partially written payload.
    """

    def __init__(self, path: str | Path, max_records: int) -> None:
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

    def reap_stale(self, keep: set[str]) -> None:
        """Rewrite running snapshots whose process went away as failed.

        Readers already show them failed (`_read`); writing it down is what lets
        them be cleared and evicted. Ids in ``keep`` are this process's own live
        jobs. A read-only replica cannot write and leaves this to the indexer.
        """

        for path in list(self.path.glob("*.json")) if self.path.is_dir() else []:
            if path.stem in keep:
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            record = self._read(path)
            if isinstance(raw, dict) and raw.get("active") and record and not record["active"]:
                try:
                    self.write(record)
                except OSError:
                    return

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
            written = path.stat().st_mtime
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict) or not payload.get("id"):
            return None
        silent = time.time() - written
        if payload.get("active") and silent > SNAPSHOT_STALE_SECONDS:
            return _interrupted(payload, written, silent)
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


def _interrupted(payload: dict[str, Any], written: float, silent: float) -> dict[str, Any]:
    """A running snapshot whose process stopped writing it, as the failure it is."""

    ended = datetime.fromtimestamp(written, UTC).replace(microsecond=0).isoformat()
    payload.update(
        status="failed",
        active=False,
        stalled=False,
        finished_at=ended.replace("+00:00", "Z"),
        error=f"interrupted: the process running this job stopped reporting {int(silent)}s ago "
        "(likely restarted); start the sync again",
    )
    for source in payload.get("sources") or []:
        if isinstance(source, dict) and source.get("active"):
            source.update(active=False, stalled=False, status="failed", phase="failed")
    return payload


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
