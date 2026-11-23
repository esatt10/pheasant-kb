"""The syncs somebody asked for through the index queue, as rows.

A JetStream work queue holds a published sync as a message, and a message can
be counted but not listed, and cannot be taken back once a consumer may hold
it. So the three questions a person asks after pressing *Run now* on a fleet
had no answer: what is waiting, can I stop it before an indexer starts, and --
the one that matters for fault tolerance -- will it come back? A cancel only
reached a *running* sync (`sync/cancellation.py`), and the broker redelivers a
task whose indexer died, so a sync stopped mid-flight on an indexer that then
crashed was run again from the start by the next one.

This ledger answers all three with one row per sync request, written by the
publishing process and closed by the queue verbs:

* ``publish`` opens a row (``queued``) and puts its id in the task payload.
* An indexer's claim *admits* the task against its row (:func:`admit`): one
  conditional ``UPDATE`` moves it to ``claimed`` unless a cancel is recorded,
  and a cancelled or already-finished request is acknowledged without running.
  That check runs on **every** delivery, so a redelivery after a crash -- the
  case a broker cannot be told about -- meets the same row and the same answer.
* ``ack`` closes it (``finished``), ``nack`` gives it back (``queued`` again, or
  ``dead`` when the attempts are spent), and replaying a dead letter reopens it.
* :func:`request_cancel` marks every open row of a source in one statement: a
  ``queued`` row is ``cancelled`` at once, a ``claimed`` one keeps its state and
  carries the request to the run that holds it.

The ledger is bookkeeping beside the broker, never in front of it. Every write
is best-effort: a task whose row could not be written runs exactly as every
task did before this existed (it simply cannot be cancelled while queued), and
a task with no ``request_id`` -- published by an older release, or a control
task such as ``prune_documents`` -- is admitted unconditionally.

Rows nobody closes (a message the broker lost, a publisher killed between its
insert and its publish) stop counting as open after :data:`OPEN_HORIZON_SECONDS`
and are removed by :func:`prune`, which the scheduler beat runs.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

QUEUED = "queued"
CLAIMED = "claimed"
FINISHED = "finished"
CANCELLED = "cancelled"
DEAD = "dead"
EXPIRED = "expired"
#: States a request is still waiting or running in.
OPEN = (QUEUED, CLAIMED)

#: An open row older than this is presumed lost: no cancel reaches it, no
#: listing shows it. Two days is far past any backlog a healthy region holds
#: and short enough that a ghost does not sit on the Syncs page for a week.
OPEN_HORIZON_SECONDS = 2 * 24 * 60 * 60
#: How long a closed row is kept, for "what happened to the sync I asked for".
CLOSED_RETENTION_SECONDS = 7 * 24 * 60 * 60

#: The payload key a task carries its request id in.
PAYLOAD_KEY = "request_id"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _horizon(now: datetime | None = None) -> str:
    return _iso((now or _now()) - timedelta(seconds=OPEN_HORIZON_SECONDS))


def tracked(task: Any) -> bool:
    """Whether ``task`` is a sync a person or a schedule asked for.

    A control task (``operation``: prune, artifact projection, a source
    replacement) is not a sync of the source and must never be stopped by a
    cancel of one.
    """

    payload = getattr(task, "payload", None) or {}
    return not payload.get("operation")


def request_id_of(task: Any) -> str | None:
    value = (getattr(task, "payload", None) or {}).get(PAYLOAD_KEY)
    return str(value) if value else None


def record(state: Any, task: Any) -> str | None:
    """Open (or reopen) the row for ``task`` and stamp its id into the payload.

    A task already carrying an id -- a dead letter being replayed, a publish
    retried with the same object -- keeps it: a dead row is reopened, any
    other row is left exactly as it is. ``None`` when the row could not be
    written; the task is published regardless.
    """

    if state is None or not tracked(task):
        return None
    request_id = request_id_of(task) or uuid.uuid4().hex
    try:
        state.execute(
            "INSERT INTO sync_requests(id, source_id, task_id, mode, state, requested_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state, "
            "requested_at=excluded.requested_at, claimed_at=NULL, claimed_by=NULL, "
            "finished_at=NULL, outcome=NULL WHERE sync_requests.state=?",
            (
                request_id,
                str(task.source_id),
                str(task.id),
                str(getattr(task, "mode", "incremental") or "incremental"),
                QUEUED,
                _iso(_now()),
                DEAD,
            ),
        )
    except Exception:  # noqa: BLE001 - bookkeeping, never a reason to drop a sync
        logger.warning("could not record the sync request for %s", task.source_id, exc_info=True)
        return None
    task.payload[PAYLOAD_KEY] = request_id
    return request_id


def discard(state: Any, request_id: str | None) -> None:
    """Forget a row whose publish failed: nothing will ever claim it."""

    if state is None or not request_id:
        return
    try:
        state.execute(
            "DELETE FROM sync_requests WHERE id=? AND state=?",
            (request_id, QUEUED),
        )
    except Exception:  # noqa: BLE001
        logger.debug("could not discard sync request %s", request_id, exc_info=True)


@dataclass(frozen=True)
class Admission:
    """What a claimed delivery of a task should do."""

    #: ``run``; ``cancelled`` (a cancel reached it first); ``finished`` (it
    #: already ran to the end and only its acknowledgement was lost).
    verdict: str
    request_id: str | None = None
    requested_by: str | None = None
    requested_at: str | None = None
    reason: str | None = None

    @property
    def runs(self) -> bool:
        return self.verdict == "run"


RUN = Admission("run")


def admit(state: Any, task: Any, owner: str) -> Admission:
    """Decide whether this delivery of ``task`` runs.

    One ``UPDATE`` whose ``WHERE`` the cancel's own write falsifies
    (``cancel_requested_at IS NULL``), so under READ COMMITTED a cancel and a
    claim racing on one row resolve in one order or the other and never both
    "won". A lookup that fails admits the task: an unreachable ledger must
    not stop every sync in the region.
    """

    request_id = request_id_of(task)
    if state is None or not request_id or not tracked(task):
        return RUN
    try:
        claimed = state.execute_returning(
            "UPDATE sync_requests SET state=?, claimed_at=?, claimed_by=? "
            "WHERE id=? AND state IN (?,?,?) AND cancel_requested_at IS NULL RETURNING id",
            (CLAIMED, _iso(_now()), owner, request_id, QUEUED, CLAIMED, DEAD),
        )
        if claimed:
            return Admission("run", request_id)
        rows = state.rows(
            "SELECT state, cancel_requested_at, cancel_requested_by, cancel_reason "
            "FROM sync_requests WHERE id=?",
            (request_id,),
        )
    except Exception:  # noqa: BLE001 - see the docstring
        logger.warning("could not admit sync request %s; running it", request_id, exc_info=True)
        return Admission("run", request_id)
    if not rows:
        # Pruned, or written by a ledger that is gone: nothing says otherwise.
        return Admission("run", request_id)
    row = rows[0]
    if row["cancel_requested_at"]:
        close(state, request_id, CANCELLED, outcome="cancelled before it started")
        return Admission(
            "cancelled",
            request_id,
            requested_by=row["cancel_requested_by"],
            requested_at=row["cancel_requested_at"],
            reason=row["cancel_reason"],
        )
    if row["state"] in (FINISHED, CANCELLED):
        return Admission("finished", request_id)
    return Admission("run", request_id)


def close(state: Any, request_id: str | None, final: str, *, outcome: str | None = None) -> None:
    """Move an open row to ``final``. A row already closed is left alone."""

    if state is None or not request_id:
        return
    try:
        state.execute(
            "UPDATE sync_requests SET state=?, finished_at=?, outcome=COALESCE(?, outcome) "
            "WHERE id=? AND state IN (?,?)",
            (final, _iso(_now()), outcome, request_id, *OPEN),
        )
    except Exception:  # noqa: BLE001
        logger.debug("could not close sync request %s", request_id, exc_info=True)


def release(state: Any, request_id: str | None, *, dead: bool) -> None:
    """A delivery gave its task back: waiting again, or out of attempts."""

    if state is None or not request_id:
        return
    try:
        state.execute(
            "UPDATE sync_requests SET state=?, claimed_by=NULL WHERE id=? AND state=?",
            (DEAD if dead else QUEUED, request_id, CLAIMED),
        )
    except Exception:  # noqa: BLE001
        logger.debug("could not release sync request %s", request_id, exc_info=True)


def cancel_of(state: Any, request_id: str | None) -> dict[str, Any] | None:
    """The cancel recorded against one request, or ``None``. Read-only."""

    if state is None or not request_id:
        return None
    rows = state.rows(
        "SELECT cancel_requested_at, cancel_requested_by, cancel_reason "
        "FROM sync_requests WHERE id=? AND cancel_requested_at IS NOT NULL",
        (request_id,),
    )
    return dict(rows[0]) if rows else None


def request_cancel(
    state: Any,
    source_id: str,
    *,
    requested_by: str,
    reason: str = "",
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Record a cancel on every open request of ``source_id``; the rows it reached.

    A ``queued`` row is closed as ``cancelled`` in the same statement, so it
    leaves every listing at once even though its message waits on the broker
    until an indexer claims and acknowledges it. A ``claimed`` row stays
    claimed: the run holding it reads the request and stops, and a
    redelivery after that run's process died is refused by :func:`admit`.
    The first request's author and time are kept, as on a run.
    """

    stamp = now or _iso(_now())
    return [
        dict(row)
        for row in state.execute_returning(
            "UPDATE sync_requests SET "
            "cancel_requested_by=CASE WHEN cancel_requested_at IS NULL THEN ? "
            "ELSE cancel_requested_by END, "
            "cancel_reason=CASE WHEN cancel_requested_at IS NULL THEN ? ELSE cancel_reason END, "
            "cancel_requested_at=COALESCE(cancel_requested_at, ?), "
            "outcome=CASE WHEN state=? THEN 'cancelled before it started' ELSE outcome END, "
            "finished_at=CASE WHEN state=? THEN ? ELSE finished_at END, "
            "state=CASE WHEN state=? THEN ? ELSE state END "
            "WHERE source_id=? AND state IN (?,?) AND requested_at >= ? "
            "RETURNING id, task_id, mode, state, requested_at, claimed_by, "
            "cancel_requested_at, cancel_requested_by, cancel_reason",
            (
                requested_by,
                reason or None,
                stamp,
                QUEUED,
                QUEUED,
                stamp,
                QUEUED,
                CANCELLED,
                source_id,
                *OPEN,
                _horizon(),
            ),
        )
    ]


def open_requests(
    state: Any, source_id: str | None = None, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Requests still waiting or running, oldest first."""

    where = "state IN (?,?) AND requested_at >= ?"
    params: list[Any] = [*OPEN, _horizon()]
    if source_id is not None:
        where += " AND source_id=?"
        params.append(source_id)
    params.append(max(1, int(limit)))
    return [
        dict(row)
        for row in state.rows(
            "SELECT id, source_id, task_id, mode, state, requested_at, claimed_at, claimed_by, "
            f"cancel_requested_at FROM sync_requests WHERE {where} "
            "ORDER BY requested_at LIMIT ?",
            tuple(params),
        )
    ]


def prune(state: Any, *, now: datetime | None = None) -> int:
    """Expire open rows past the horizon and delete closed rows past retention."""

    moment = now or _now()
    expired = state.execute_returning(
        "UPDATE sync_requests SET state=?, finished_at=?, outcome='never closed' "
        "WHERE state IN (?,?) AND requested_at < ? RETURNING id",
        (EXPIRED, _iso(moment), *OPEN, _horizon(moment)),
    )
    deleted = state.execute_returning(
        "DELETE FROM sync_requests WHERE state NOT IN (?,?) AND "
        "COALESCE(finished_at, requested_at) < ? RETURNING id",
        (*OPEN, _iso(moment - timedelta(seconds=CLOSED_RETENTION_SECONDS))),
    )
    return len(expired) + len(deleted)
