"""PostgreSQL checkpoints for resumable GTM research."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

LEASE_SECONDS = 150


class RunStore:
    def __init__(self, state: Any, kb_id: str, owner: str | None):
        self.state = state
        self.kb_id = kb_id
        self.owner = owner

    def _scope(self, run_id: str) -> tuple[str, tuple]:
        where = "kb_id=? AND id=?"
        params = (self.kb_id, run_id)
        if self.owner is not None:
            where += " AND owner=?"
            params += (self.owner,)
        return where, params

    def create(self, payload: dict) -> str:
        run_id, stamp = uuid4().hex, datetime.now(UTC).isoformat()
        self.state.execute(
            "INSERT INTO gtm_runs(kb_id,id,owner,payload_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (self.kb_id, run_id, self.owner or "", json.dumps(payload), stamp, stamp),
        )
        return run_id

    def get(self, run_id: str) -> dict | None:
        where, params = self._scope(run_id)
        rows = self.state.rows(
            "SELECT payload_json,lease_until,created_at,updated_at FROM gtm_runs WHERE " + where,
            params,
        )
        return dict(rows[0]) if rows else None

    def claim(self, run_id: str) -> tuple[str, dict] | None:
        where, params = self._scope(run_id)
        nonce = uuid4().hex
        now = datetime.now(UTC)
        rows = self.state.execute_returning(
            "UPDATE gtm_runs SET lease_token=?,lease_until=? WHERE "
            + where
            + " AND (lease_until IS NULL OR lease_until<=?) RETURNING payload_json",
            (nonce, (now + timedelta(seconds=LEASE_SECONDS)).isoformat(), *params, now.isoformat()),
        )
        return (nonce, json.loads(rows[0]["payload_json"])) if rows else None

    def commit(self, run_id: str, token: str, payload: dict | None) -> bool:
        where, params = self._scope(run_id)
        if payload is None:
            assignments, values = "", ()
        else:
            assignments = "payload_json=?,updated_at=?,"
            values = (json.dumps(payload), datetime.now(UTC).isoformat())
        rows = self.state.execute_returning(
            "UPDATE gtm_runs SET "
            + assignments
            + "lease_token=NULL,lease_until=NULL WHERE "
            + where
            + " AND lease_token=? RETURNING id",
            (*values, *params, token),
        )
        return bool(rows)
