"""The run journal and its fenced lease.

One logical coordinator owns a run at a time. It owns it by holding the run's
lease, and every write it makes names the *fence* it was granted: a claim
increments the fence, so a worker that lost its lease (it paused, its machine
was partitioned, its lease expired and another worker took over) finds that
its writes match no row and stops. Appending to the journal is a single
``UPDATE ... RETURNING last_seq`` on the run row, conditional on that fence,
followed by the event insert and any projection writes in the same
transaction: an observation, a state change and the next work item are
committed together or not at all.
"""

from __future__ import annotations

import json
from typing import Any

from pheasant_harness.common import NotFound, StaleFence, canonical, loads, new_id, now

ACTIVE_STATES = ("accepted", "running", "verifying", "delivering", "reconciling")
WAITING_STATES = ("waiting",)
TERMINAL_STATES = ("succeeded", "partial", "failed", "cancelled")

_RUN_COLUMNS = (
    "state",
    "wait_json",
    "result_json",
    "usage_json",
    "versions_json",
    "cancel_requested",
    "request_json",
    "workload",
    "workflow_version",
)


class RunStore:
    def __init__(self, state: Any, kb_id: str):
        self.state = state
        self.kb = kb_id

    # -- creation and reads ---------------------------------------------------

    def find_by_key(self, actor: str, client_key: str) -> dict | None:
        rows = self.state.rows(
            "SELECT * FROM harness_runs WHERE kb_id=? AND actor=? AND client_key=?",
            (self.kb, actor, client_key),
        )
        return self._decode(rows[0]) if rows else None

    def create(self, row: dict, first_event: tuple[str, dict]) -> dict:
        stamp = now()
        columns = {
            "kb_id": self.kb,
            "revision": 1,
            "last_seq": 1,
            "fence": 0,
            "cancel_requested": 0,
            "created_at": stamp,
            "updated_at": stamp,
            **row,
        }
        names = ",".join(columns)
        marks = ",".join("?" for _ in columns)
        with self.state.conn:
            self.state.conn.execute(
                f"INSERT INTO harness_runs({names}) VALUES({marks}) "
                "ON CONFLICT (kb_id, actor, client_key) DO NOTHING",
                tuple(columns.values()),
            )
            inserted = self.state.conn.execute(
                "SELECT id FROM harness_runs WHERE kb_id=? AND actor=? AND client_key=?",
                (self.kb, row["actor"], row["client_key"]),
            ).fetchall()
            if inserted and inserted[0]["id"] == row["id"]:
                self.state.conn.execute(
                    "INSERT INTO harness_events(run_id,seq,type,data_json,visibility,created_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT (run_id, seq) DO NOTHING",
                    (row["id"], 1, first_event[0], canonical(first_event[1]), "user", stamp),
                )
        return self.get(inserted[0]["id"])

    def get(self, run_id: str) -> dict:
        rows = self.state.rows(
            "SELECT * FROM harness_runs WHERE kb_id=? AND id=?", (self.kb, run_id)
        )
        if not rows:
            raise NotFound(f"Unknown harness run: {run_id}")
        return self._decode(rows[0])

    def list(self, *, actor: str | None = None, limit: int = 50, states=None) -> list[dict]:
        sql = "SELECT * FROM harness_runs WHERE kb_id=?"
        params: list = [self.kb]
        if actor is not None:
            sql += " AND actor=?"
            params.append(actor)
        if states:
            sql += " AND state IN (" + ",".join("?" for _ in states) + ")"
            params.extend(states)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        return [self._decode(r) for r in self.state.rows(sql, tuple(params))]

    @staticmethod
    def _decode(row: Any) -> dict:
        run = dict(row)
        run["request"] = loads(run.pop("request_json"), {})
        run["limits"] = loads(run.pop("budget_json"), {})
        run["usage"] = loads(run.pop("usage_json"), {})
        run["versions"] = loads(run.pop("versions_json"), {})
        run["wait"] = loads(run.pop("wait_json"), None)
        run["result"] = loads(run.pop("result_json"), None)
        return run

    # -- the lease ------------------------------------------------------------

    def claim(self, run_id: str, worker: str, lease_seconds: float) -> int | None:
        """Take a free or expired lease. The fence moves only when the holder changes.

        A worker re-claiming a run it released itself keeps its fence, so its
        attempts still in flight stay current. Any other worker -- or the same
        id after another held the run in between -- gets a new fence, which is
        what makes every older attempt stale.
        """
        stamp = now()
        rows = self.state.execute_returning(
            "UPDATE harness_runs SET fence=CASE WHEN lease_owner=? THEN fence ELSE fence+1 END, "
            "lease_owner=?, lease_token=?, lease_until=? "
            "WHERE kb_id=? AND id=? AND (lease_until IS NULL OR lease_until<?) RETURNING fence",
            (worker, worker, new_id("lease"), stamp + lease_seconds, self.kb, run_id, stamp),
        )
        return int(rows[0]["fence"]) if rows else None

    def renew(self, run_id: str, fence: int, lease_seconds: float) -> bool:
        rows = self.state.execute_returning(
            "UPDATE harness_runs SET lease_until=? WHERE kb_id=? AND id=? AND fence=? RETURNING id",
            (now() + lease_seconds, self.kb, run_id, fence),
        )
        return bool(rows)

    def release(self, run_id: str, fence: int) -> None:
        # The owner is kept: it is how a re-claim by the same worker keeps its fence.
        self.state.execute(
            "UPDATE harness_runs SET lease_token=NULL, lease_until=NULL "
            "WHERE kb_id=? AND id=? AND fence=?",
            (self.kb, run_id, fence),
        )

    def expire_lease(self, run_id: str) -> None:
        """Operator/drill action: treat the current holder as dead."""
        self.state.execute(
            "UPDATE harness_runs SET lease_until=0, lease_owner='expired' WHERE kb_id=? AND id=?",
            (self.kb, run_id),
        )

    # -- the journal ----------------------------------------------------------

    def append(
        self,
        run_id: str,
        event_type: str,
        data: dict,
        *,
        fence: int | None = None,
        updates: dict | None = None,
        statements: tuple = (),
        visibility: str = "user",
        require_state: tuple[str, ...] | None = None,
    ) -> int:
        """Append one event, apply ``updates`` and ``statements``, atomically.

        With ``fence`` the append succeeds only for the current lease holder.
        Without it (cancellation, operator actions) it is ordered by the same
        counter and needs no lease.
        """

        assignments = ["last_seq=last_seq+1", "revision=revision+1", "updated_at=?"]
        values: list = [now()]
        for key, value in (updates or {}).items():
            if key not in _RUN_COLUMNS:
                raise ValueError(f"not a run column: {key}")
            assignments.append(f"{key}=?")
            values.append(
                canonical(value) if key.endswith("_json") and value is not None else value
            )
        where = "kb_id=? AND id=?"
        values += [self.kb, run_id]
        if fence is not None:
            where += " AND fence=?"
            values.append(fence)
        if require_state:
            where += " AND state IN (" + ",".join("?" for _ in require_state) + ")"
            values.extend(require_state)
        with self.state.conn:
            rows = self.state.conn.execute(
                f"UPDATE harness_runs SET {', '.join(assignments)} WHERE {where} RETURNING "
                "last_seq",
                tuple(values),
            ).fetchall()
            if not rows:
                if fence is not None:
                    raise StaleFence(f"run {run_id} is no longer held at fence {fence}")
                if require_state:
                    raise StaleFence(f"run {run_id} is not in state {list(require_state)}")
                raise NotFound(f"Unknown harness run: {run_id}")
            seq = int(rows[0]["last_seq"])
            self.state.conn.execute(
                "INSERT INTO harness_events(run_id,seq,type,data_json,visibility,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, seq, event_type, canonical(data), visibility, now()),
            )
            for statement in statements:
                sql, params = statement[0], statement[1]
                changed = self.state.conn.execute(sql, params).rowcount
                if len(statement) > 2 and statement[2] and not changed:
                    # A guarded projection write matched nothing: the step moved on
                    # (a newer attempt, a superseded candidate). Roll everything back.
                    raise StaleFence(f"guarded write found no row: {sql.split(' WHERE ')[0][:60]}")
        return seq

    def events(
        self, run_id: str, after_seq: int = 0, limit: int = 500, operator: bool = False
    ) -> list[dict]:
        rows = self.state.rows(
            "SELECT seq, type, data_json, visibility, created_at FROM harness_events "
            "WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
            (run_id, int(after_seq), int(limit)),
        )
        out = []
        for row in rows:
            visible = operator or row["visibility"] == "user"
            out.append(
                {
                    "seq": row["seq"],
                    "type": row["type"] if visible else "redacted",
                    "data": json.loads(row["data_json"]) if visible else {},
                    "created_at": row["created_at"],
                }
            )
        return out
