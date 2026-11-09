"""A streamed answer outlives the connection that asked for it.

The web UI reads an answer as server-sent events over one long request, and a
phone that sleeps mid-answer drops that request. The answer was never the
casualty -- the worker finishes it regardless -- but it had nowhere to go, so
the reader saw a failure and asked again: a two-minute compose paid for twice,
found on the Azure fleet. Now a caller that names its answer (``answer_id``,
any 8-64 letters, digits, ``-`` or ``_``; the UI sends a random one) has it
kept here as it forms -- each finished step, the text, the visual that
follows, or the refusal -- for :data:`RETENTION_SECONDS`, and
``GET /assistant/chat/answers/{id}`` hands it back.

Deliberately not the drafts: provisional text arrives a few words at a time,
a row write per delta would put hundreds of writes on every answer, and the
verified text replaces it anyway. A reader who reconnects mid-answer sees the
steps so far and then the answer.

Kept in the state store, not in this process: a browser that reconnects may
reach another api replica, or this one after a restart. Read back only by the
principal that asked (an answer carries what that principal may read), and an
id held for someone else is the same 404 as one never held. Every write is
best-effort: a store that cannot hold the answer costs the resume, never the
answer itself.

HTTP only. MCP's ``ask_knowledge_base`` is one call with one result, and its
long-running shape is already a job (``compose_artifact`` + ``get_job``).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pheasant.services import ServiceContext
from pheasant.services.errors import AnswerNotFound, InvalidRequest

logger = logging.getLogger(__name__)

#: How long a finished answer waits to be collected. Long enough for a phone
#: that slept through a compose; short enough that the table stays small.
RETENTION_SECONDS = 3600

ANSWER_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def check_answer_id(answer_id: str | None) -> str | None:
    """``answer_id`` if well formed, ``None`` if absent; a malformed one is refused."""

    if answer_id is None or answer_id == "":
        return None
    if not ANSWER_ID.match(answer_id):
        raise InvalidRequest("answer_id must be 8-64 letters, digits, '-' or '_'")
    return answer_id


class AnswerRecorder:
    """Writes one streamed answer's progress as it happens. Never raises."""

    def __init__(self, context: ServiceContext, answer_id: str, owner: str | None) -> None:
        self.state = context.state
        self.answer_id = answer_id
        self.owner = owner
        self.kb_id = context.config.knowledge_base_id
        self.steps: list[dict[str, Any]] = []
        self._working = True
        self._answered = False
        now = _now()
        self._write(
            "DELETE FROM answer_streams WHERE created_at < ?",
            (_stamp(now - timedelta(seconds=RETENTION_SECONDS)),),
        )
        self._write(
            "INSERT INTO answer_streams(answer_id, kb_id, owner, status, steps_json, "
            "answer_json, error, created_at, updated_at) VALUES(?,?,?,?,?,NULL,NULL,?,?) "
            "ON CONFLICT(answer_id) DO NOTHING",
            (answer_id, self.kb_id, owner, "running", "[]", _stamp(now), _stamp(now)),
        )

    def step(self, step: dict[str, Any]) -> None:
        self.steps.append(step)
        self._update("running", steps=True)

    def answered(self, answer: dict[str, Any]) -> None:
        """The text exists; a pending visual may still follow."""

        pending = (answer.get("visual") or {}).get("status") == "pending"
        self._answered = True
        self._update("answered" if pending else "done", answer=answer)

    def visual(self, answer: dict[str, Any]) -> None:
        self._update("done", answer=answer)

    def failed(self, error: str) -> None:
        # After the text exists, a failure is the visual's: the answer stands.
        self._update("done" if self._answered else "failed", error=error)

    def finish(self) -> None:
        """The stream ended. An answer still waiting on its visual is done without it."""

        self._write(
            "UPDATE answer_streams SET status='done', updated_at=? "
            "WHERE answer_id=? AND status='answered'",
            (_stamp(_now()), self.answer_id),
        )

    def _update(
        self,
        status: str,
        *,
        steps: bool = False,
        answer: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        assignments = ["status=?", "updated_at=?"]
        params: list[Any] = [status, _stamp(_now())]
        if steps:
            assignments.append("steps_json=?")
            params.append(json.dumps(self.steps, default=str))
        if answer is not None:
            assignments.append("answer_json=?")
            params.append(json.dumps(answer, default=str))
        if error is not None:
            assignments.append("error=?")
            params.append(error)
        params.append(self.answer_id)
        self._write(
            f"UPDATE answer_streams SET {', '.join(assignments)} WHERE answer_id=?",
            tuple(params),
        )

    def _write(self, sql: str, params: tuple[Any, ...]) -> None:
        if not self._working:
            return
        try:
            self.state.execute(sql, params)
        except Exception:  # noqa: BLE001 - the resume is lost, never the answer
            # Once: a store that refused one write will refuse the rest, and
            # an answer has dozens of them.
            self._working = False
            logger.warning(
                "could not keep answer %s for a reconnecting reader", self.answer_id, exc_info=True
            )


def start(
    context: ServiceContext, answer_id: str | None, owner: str | None
) -> AnswerRecorder | None:
    """A recorder for ``answer_id``, or ``None`` when the caller named no answer."""

    answer_id = check_answer_id(answer_id)
    return AnswerRecorder(context, answer_id, owner) if answer_id else None


def read(context: ServiceContext, answer_id: str, owner: str | None) -> dict[str, Any]:
    """What the answer named ``answer_id`` has done so far, for the principal that asked it.

    ``status`` is ``running`` (``steps`` so far), ``answered`` (the text, its
    visual still being drawn), ``done`` or ``failed`` (with ``error``).
    """

    check_answer_id(answer_id)
    cutoff = _stamp(_now() - timedelta(seconds=RETENTION_SECONDS))
    rows = context.state.rows(
        "SELECT owner, status, steps_json, answer_json, error, created_at, updated_at "
        "FROM answer_streams WHERE answer_id=? AND kb_id=? AND created_at >= ?",
        (answer_id, context.config.knowledge_base_id, cutoff),
    )
    row = dict(rows[0]) if rows else None
    if row is None or (row.get("owner") or None) != (owner or None):
        raise AnswerNotFound(answer_id)
    return {
        "answer_id": answer_id,
        "status": row["status"],
        "steps": json.loads(row.get("steps_json") or "[]"),
        "answer": json.loads(row["answer_json"]) if row.get("answer_json") else None,
        "error": row.get("error"),
        "started_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
