"""Shared job history with a time lifetime independent of published outputs."""

from __future__ import annotations

import errno
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pheasant.job_snapshots import (
    SNAPSHOT_STALE_SECONDS,
    JobSnapshotStore,
    _interrupted,
    _refresh_snapshot,
)


class DurableJobStore:
    authoritative_terminal = True

    def __init__(
        self,
        state: Any,
        kb_id: str,
        retention_days: int = 30,
        legacy: JobSnapshotStore | None = None,
    ):
        self.state, self.kb_id = state, kb_id
        self.retention_days = max(0, int(retention_days))
        self.legacy = legacy

    def write(self, payload: dict[str, Any]) -> None:
        finished = payload.get("finished_at")
        if finished:
            finished = datetime.fromisoformat(str(finished).replace("Z", "+00:00")).isoformat()
        self.state.execute(
            "INSERT INTO durable_jobs(kb_id,id,payload_json,active,updated_at,finished_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(kb_id,id) DO UPDATE SET "
            "payload_json=excluded.payload_json, active=excluded.active, "
            "updated_at=excluded.updated_at, finished_at=excluded.finished_at",
            (
                self.kb_id,
                payload["id"],
                json.dumps(payload, default=str),
                int(bool(payload.get("active"))),
                datetime.now(UTC).isoformat(),
                finished,
            ),
        )

    def _decode(self, row: Any) -> dict:
        payload = json.loads(row["payload_json"])
        written = datetime.fromisoformat(row["updated_at"])
        silent = (datetime.now(UTC) - written).total_seconds()
        if payload.get("active") and silent > SNAPSHOT_STALE_SECONDS:
            return _interrupted(payload, written.timestamp(), silent)
        return _refresh_snapshot(payload)

    def get(self, job_id: str) -> dict | None:
        if "/" in job_id or "\\" in job_id:
            return None
        old = self.legacy.get(job_id) if self.legacy is not None else None
        rows = self.state.rows(
            "SELECT * FROM durable_jobs WHERE kb_id=? AND id=?", (self.kb_id, job_id)
        )
        if old and (not rows or self._legacy_newer(job_id, rows[0]["updated_at"])):
            return old
        return self._decode(rows[0]) if rows else None

    def _legacy_newer(self, job_id: str, updated_at: str) -> bool:
        try:
            return (self.legacy.path / f"{job_id}.json").stat().st_mtime > datetime.fromisoformat(
                updated_at
            ).timestamp()
        except OSError:
            return False

    def list(self, *, limit: int | None = None) -> list[dict]:
        fence, params = "", ()
        if limit is not None:
            fence = (
                " AND (active=1 OR id IN (SELECT id FROM durable_jobs WHERE kb_id=? AND active=0 "
                "ORDER BY COALESCE(finished_at,updated_at) DESC,id DESC LIMIT ?))"
            )
            params = (self.kb_id, max(1, int(limit)))
        rows = self.state.rows(
            "SELECT * FROM durable_jobs WHERE kb_id=?" + fence + " ORDER BY updated_at DESC",
            (self.kb_id, *params),
        )
        records = {r["id"]: self._decode(r) for r in rows}
        updated = {r["id"]: r["updated_at"] for r in rows}
        for record in self.legacy.list() if self.legacy is not None else []:
            key = record["id"]
            if key not in records or self._legacy_newer(key, updated[key]):
                records[key] = record
        return list(records.values())

    def clear_finished(self, job_id: str | None = None) -> set[str]:
        suffix, values = (" AND id=?", (job_id,)) if job_id else ("", ())
        with self.state.conn:
            rows = self.state.conn.execute(
                "DELETE FROM durable_jobs WHERE kb_id=? AND active=0" + suffix + " RETURNING id",
                (self.kb_id, *values),
            ).fetchall()
        removed = {str(row["id"]) for row in rows}
        migrated = set(removed)
        if job_id and not self.state.rows(
            "SELECT id FROM durable_jobs WHERE kb_id=? AND id=?", (self.kb_id, job_id)
        ):
            # The serving replica may already have deleted the shared row.
            # Its notification still lets the writable indexer tidy the copy.
            migrated.add(job_id)
        self._remove_migrated(migrated)
        if self.legacy is not None:
            removed |= self.legacy.clear_finished(job_id)
        return removed

    def _remove_migrated(self, identifiers: set[str]) -> None:
        if self.legacy is None:
            return
        for identifier in identifiers:
            if "/" in identifier or "\\" in identifier:
                continue
            # These are redundant imported snapshots, never artifact originals.
            path = self.legacy.path / f"{identifier}.json.migrated"
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                if exc.errno not in (errno.EROFS, errno.EACCES, errno.EPERM):
                    raise
                # PostgreSQL is authoritative. NFS may report EROFS even for
                # an absent file; cleanup belongs to the writable indexer.

    def reap_stale(self, keep: set[str]) -> None:
        for row in self.state.rows(
            "SELECT * FROM durable_jobs WHERE kb_id=? AND active=1", (self.kb_id,)
        ):
            if row["id"] in keep:
                continue
            payload = self._decode(row)
            if not payload.get("active"):
                self.write(payload)
        self.prune()

    def prune(self, *, now: datetime | None = None) -> int:
        rows = self.state.rows(
            "SELECT data_json FROM region_settings WHERE kb_id=? AND section='artifacts'",
            (self.kb_id,),
        )
        overlay = json.loads(rows[0]["data_json"]) if rows else {}
        days = int(overlay.get("job_retention_days", self.retention_days))
        if not days:
            return 0
        cutoff = ((now or datetime.now(UTC)) - timedelta(days=days)).isoformat()
        with self.state.conn:
            rows = self.state.conn.execute(
                "DELETE FROM durable_jobs WHERE kb_id=? AND active=0 "
                "AND finished_at<? RETURNING id",
                (self.kb_id, cutoff),
            ).fetchall()
        self._remove_migrated({str(row["id"]) for row in rows})
        return len(rows)


def store_for(
    state: Any, kb_id: str, retention_days: int, shared_path: Any, max_records: int
) -> Any:
    legacy = JobSnapshotStore(shared_path, max_records) if shared_path else None
    if state is None:
        return legacy
    store = DurableJobStore(state, kb_id, retention_days, legacy=legacy)
    for snapshot in legacy.list() if legacy is not None else []:
        existing = state.rows(
            "SELECT id FROM durable_jobs WHERE kb_id=? AND id=?", (kb_id, snapshot["id"])
        )
        if not existing:
            store.write(snapshot)
        path = Path(shared_path) / f"{snapshot['id']}.json"
        try:
            path.rename(path.with_suffix(".json.migrated"))
        except OSError:
            pass
    return store
