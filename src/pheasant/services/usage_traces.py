"""Traces behind the usage totals: per person, organization and model, by session.

``services/usage.py`` answers "how much" from ``usage_daily``, which holds
totals and never a question. An operator who sees a person, an organization
or a model there next asks "show me what happened", and the answer is the
interaction ledger (``interaction_events``), one row per call, keyed by the
same W3C trace id the call's OpenTelemetry spans carry
(``telemetry.interactions.observe`` adopts the SDK's ids). So the drill-down
reads the ledger and points at Log Analytics for the spans:

* :func:`sessions` -- the sessions a person, organization or model took part
  in over the selected window. A session is a conversation (the web UI's
  ``conversation_id``, MCP's ``session``); a call made outside one is its own
  session, keyed by its trace.
* :func:`session` -- one session's calls in order, each with the steps the
  answer took, its models and tokens, and a KQL query for its spans.
* :func:`export_lines` -- the same rows as JSON lines, for an export.

**Nothing is materialized ahead of a question.** Each is one bounded query
over the ledger, run when the operator selects something; the window is the
depth of the drill-down. The ledger keeps ``hot_retention_days``, so a window
longer than that shows what remains, and the payload says so.

Admin only (``security.manage``), like the totals: these rows carry what
people asked.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from pheasant.security.authorization import require_permission
from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest, ServiceError

MAX_DAYS = 366
MAX_SESSIONS = 200
MAX_SESSION_CALLS = 500
MAX_EXPORT_ROWS = 50_000
#: Trace ids a KQL query names inline; past this it filters by time and model.
MAX_KQL_TRACES = 200
#: Steps kept per answer on its ledger row, and characters per step detail.
MAX_STEPS = 40
MAX_STEP_DETAIL = 280
#: The App Insights tables a region's spans and logs land in (workspace-based).
SPAN_TABLES = ("AppRequests", "AppDependencies", "AppTraces", "AppExceptions")


class TracesNotKept(ServiceError):
    status = 404
    code = "TRACES_NOT_KEPT"

    def __init__(self) -> None:
        super().__init__(
            "This region keeps no interaction ledger, so there are no traces to drill into. "
            "Its operator turns it on with observability.interactions.enabled."
        )


class SessionNotFound(ServiceError):
    status = 404
    code = "SESSION_NOT_FOUND"

    def __init__(self, key: str) -> None:
        super().__init__(f"No calls for session {key} in the selected window")


# --------------------------------------------------------------------- writing


def annotate_answer(request: Any, payload: dict[str, Any], *, event: Any = None) -> None:
    """Put an answer's shape on the ledger row its call is filling in.

    What the drill-down shows and filters on: the models that ran, the routing
    outcome, the route, tokens and the steps. Never content beyond what the
    row already holds (``query_text`` and ``answer_text``, governed by
    ``redact_text``). Best-effort: observation never fails an answer.

    ``event`` names the row explicitly: a streamed answer's own child row,
    written after the request's row was handed over. Without it, the row the
    call is filling in -- unless that was already handed to the buffer, when
    writing to it would race the flush (``interactions.child_event``).
    """

    from pheasant.telemetry.interactions import current_event

    if event is None:
        event = current_event()
        if event is None or getattr(event, "handed_over", False):
            return
    try:
        if not event.session_id and getattr(request, "conversation_id", None):
            # The web UI sends its conversation in the body, not a header.
            event.session_id = str(request.conversation_id)[:200]
        chosen = payload.get("models") or {}
        routing = payload.get("model_routing") or {}
        route = payload.get("route") or {}
        steps = []
        totals = {"input_tokens": 0, "output_tokens": 0}
        for step in (payload.get("steps") or [])[:MAX_STEPS]:
            row = step if isinstance(step, dict) else getattr(step, "__dict__", {})
            detail = str(row.get("detail") or "")
            steps.append(
                {
                    "name": row.get("name"),
                    "detail": detail[:MAX_STEP_DETAIL],
                    "model": row.get("model"),
                    "passages": row.get("passages"),
                    "duration_seconds": row.get("duration_seconds"),
                    "input_tokens": row.get("input_tokens"),
                    "output_tokens": row.get("output_tokens"),
                }
            )
            for key in totals:
                totals[key] += int(row.get(key) or 0)
        if chosen.get("chat"):
            event.attributes["chat_model"] = chosen["chat"]
        if chosen.get("decision"):
            event.attributes["decision_model"] = chosen["decision"]
        event.attributes["answer"] = {
            "models": {"chat": chosen.get("chat"), "decision": chosen.get("decision")},
            "routing": {
                key: routing.get(key)
                for key in ("mode", "outcome", "selected", "decision_model")
                if routing
            }
            or None,
            "route": {key: route.get(key) for key in ("intent", "depth", "visual")} or None,
            "workflow": payload.get("workflow"),
            "mode": payload.get("mode"),
            "status": payload.get("status"),
            "degraded": bool(payload.get("degraded")),
            "citations": len(payload.get("citations") or []),
            "tokens": totals,
            "server_ms": payload.get("server_processing_ms"),
            "steps": steps,
        }
    except Exception:  # noqa: BLE001 - observation must never fail an answer
        return


# --------------------------------------------------------------------- reading


def _settings(context: ServiceContext) -> Any:
    require_permission("security.manage")
    settings = context.config.observability.interactions
    if not settings.enabled:
        raise TracesNotKept()
    return settings


def _window(days: Any) -> tuple[int, str]:
    try:
        days = int(days)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(f"days must be a whole number; got {days!r}") from exc
    if not 1 <= days <= MAX_DAYS:
        raise InvalidRequest(f"days must be between 1 and {MAX_DAYS}")
    since = datetime.now(UTC) - timedelta(days=days)
    return days, since.isoformat()


def _attribute(state: Any, key: str) -> str:
    """One top-level key of ``attributes_json``, in this backend's SQL."""

    if state.dialect.is_postgres:
        return f"(NULLIF(attributes_json, '')::jsonb ->> '{key}')"
    return f"json_extract(attributes_json, '$.{key}')"


def _where(
    context: ServiceContext,
    since: str,
    *,
    principal: str | None,
    organization: str | None,
    model: str | None,
) -> tuple[str, list[Any]]:
    state = context.state
    clauses = ["kb_id = ?", "started_at >= ?"]
    params: list[Any] = [context.config.pheasant.name, since]
    if principal:
        clauses.append("principal = ?")
        params.append(principal)
    if organization is not None:
        # "" is the region's own indexing and shared credentials, as in usage.
        clauses.append(f"COALESCE({_attribute(state, 'tenant_id')}, '') = ?")
        params.append(organization)
    if model:
        clauses.append(
            f"({_attribute(state, 'chat_model')} = ? OR {_attribute(state, 'decision_model')} = ?)"
        )
        params += [model, model]
    return " AND ".join(clauses), params


def _retention(settings: Any, days: int) -> dict[str, Any]:
    kept = int(getattr(settings, "hot_retention_days", 0) or 0)
    return {
        "ledger_days": kept or None,
        "complete": not kept or days <= kept,
        "note": None
        if not kept or days <= kept
        else f"The ledger keeps {kept} days; older calls in this window have expired.",
    }


def _session_key(row: dict[str, Any]) -> str:
    return str(row.get("session_id") or f"trace:{row['trace_id']}")


def sessions(
    context: ServiceContext,
    *,
    days: int = 7,
    principal: str | None = None,
    organization: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """The sessions a person, organization or model took part in, newest first."""

    settings = _settings(context)
    days, since = _window(days)
    where, params = _where(
        context, since, principal=principal, organization=organization, model=model
    )
    key = "COALESCE(session_id, 'trace:' || trace_id)"
    rows = context.state.rows(
        f"SELECT {key} AS session_key, MIN(started_at) AS first_at, MAX(started_at) AS last_at, "
        # A streamed answer is two rows of one trace (the request and its
        # answer): a call is a trace.
        "COUNT(DISTINCT trace_id) AS calls, "
        "SUM(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END) AS errors, "
        "SUM(COALESCE(duration_ms, 0)) AS duration_ms, MIN(principal) AS principal, "
        "MIN(modality) AS modality "
        f"FROM interaction_events WHERE {where} GROUP BY {key} "
        "ORDER BY MAX(started_at) DESC LIMIT ?",
        (*params, MAX_SESSIONS + 1),
    )
    listed = [
        {
            "session": row["session_key"],
            "first_at": row["first_at"],
            "last_at": row["last_at"],
            "calls": int(row["calls"] or 0),
            "errors": int(row["errors"] or 0),
            "duration_ms": round(float(row["duration_ms"] or 0), 1),
            "principal": row["principal"],
            "modality": row["modality"],
        }
        for row in rows[:MAX_SESSIONS]
    ]
    # The spans behind this selection. Spans carry no principal (by design,
    # `interactions._annotate_span`), so a person's or an organization's are
    # named by their trace ids; past MAX_KQL_TRACES the query filters by time
    # (and model) and says so.
    traces = [
        row["trace_id"]
        for row in context.state.rows(
            f"SELECT trace_id FROM interaction_events WHERE {where} "
            "ORDER BY started_at DESC LIMIT ?",
            (*params, MAX_KQL_TRACES + 1),
        )
    ]
    named = traces if len(traces) <= MAX_KQL_TRACES else None
    return {
        "window": {"days": days, "from": since},
        "filter": {"principal": principal, "organization": organization, "model": model},
        "sessions": listed,
        "truncated": len(rows) > MAX_SESSIONS,
        "retention": _retention(settings, days),
        "kql": kql(days=days, trace_ids=named, model=None if named else model) if traces else None,
        "kql_note": None
        if named is not None or not traces
        else f"More than {MAX_KQL_TRACES} calls: the query filters by time"
        + (" and model" if model else "")
        + " rather than naming each trace.",
    }


def _call(row: Any) -> dict[str, Any]:
    row = dict(row)  # sqlite3.Row has no .get()
    try:
        attributes = json.loads(row.get("attributes_json") or "{}")
    except ValueError:
        attributes = {}
    return {
        "trace_id": row["trace_id"],
        "span_id": row["span_id"],
        "operation": row["operation"],
        "modality": row["modality"],
        "principal": row["principal"],
        "organization": attributes.get("tenant_id"),
        "started_at": row["started_at"],
        "duration_ms": row["duration_ms"],
        "status": row["status"],
        "question": row["query_text"],
        "answer": row["answer_text"],
        "result_count": row["result_count"],
        "answer_trace": attributes.get("answer"),
        "http_status": attributes.get("http_status"),
    }


_COLUMNS = (
    "id, trace_id, span_id, parent_span_id, modality, operation, principal, session_id, "
    "client_id, started_at, duration_ms, status, query_text, answer_text, result_count, "
    "top_score, attributes_json"
)


def _session_rows(context: ServiceContext, key: str, since: str) -> list[dict[str, Any]]:
    if key.startswith("trace:"):
        clause, value = "trace_id = ? AND session_id IS NULL", key[len("trace:") :]
    else:
        clause, value = "session_id = ?", key
    return context.state.rows(
        f"SELECT {_COLUMNS} FROM interaction_events WHERE kb_id = ? AND started_at >= ? "
        f"AND {clause} ORDER BY started_at LIMIT ?",
        (context.config.pheasant.name, since, value, MAX_SESSION_CALLS),
    )


def session(context: ServiceContext, key: str, *, days: int = 7) -> dict[str, Any]:
    """One session's calls in order, with each answer's steps and a KQL query."""

    settings = _settings(context)
    days, since = _window(days)
    key = str(key or "").strip()
    if not key:
        raise InvalidRequest("session is required")
    rows = _session_rows(context, key, since)
    if not rows:
        raise SessionNotFound(key)
    # A request whose work is a child row of the same trace (a stream's answer)
    # says nothing its child does not: show the child, keep both in exports.
    parents = {row["parent_span_id"] for row in rows if row["parent_span_id"]}
    calls = [_call(row) for row in rows if row["span_id"] not in parents]
    trace_ids = list(dict.fromkeys(call["trace_id"] for call in calls))
    return {
        "session": key,
        "window": {"days": days, "from": since},
        "first_at": calls[0]["started_at"],
        "last_at": calls[-1]["started_at"],
        "calls": calls,
        "truncated": len(rows) >= MAX_SESSION_CALLS,
        "retention": _retention(settings, days),
        "kql": kql(
            trace_ids=trace_ids,
            start=calls[0]["started_at"],
            end=calls[-1]["started_at"],
        ),
    }


def export_lines(
    context: ServiceContext,
    *,
    days: int = 7,
    principal: str | None = None,
    organization: str | None = None,
    model: str | None = None,
    session_key: str | None = None,
) -> Iterator[str]:
    """The selected ledger rows as JSON lines, oldest first, for a download."""

    _settings(context)
    days, since = _window(days)
    if session_key:
        rows = _session_rows(context, session_key, since)
    else:
        where, params = _where(
            context, since, principal=principal, organization=organization, model=model
        )
        rows = context.state.rows(
            f"SELECT {_COLUMNS} FROM interaction_events WHERE {where} ORDER BY started_at LIMIT ?",
            (*params, MAX_EXPORT_ROWS),
        )
    for row in rows:
        record = dict(row)
        try:
            record["attributes"] = json.loads(record.pop("attributes_json") or "{}")
        except ValueError:
            record["attributes"] = {}
        yield json.dumps(record, default=str, ensure_ascii=False) + "\n"


# ------------------------------------------------------------------------ KQL


def _kql_string(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _kql_time(value: str, delta: timedelta) -> str:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00")) + delta
    except ValueError:
        return f"datetime({value})"
    return f"datetime({moment.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')})"


def kql(
    *,
    days: int | None = None,
    trace_ids: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    model: str | None = None,
) -> str:
    """A Log Analytics query for the spans and logs behind a selection.

    Spans land in the workspace's App Insights tables with ``OperationId``
    equal to the trace id. The time filter comes first because it is what
    makes the query cheap; a session's is its own span plus a few minutes.
    """

    if start and end:
        when = (
            f"| where TimeGenerated between ({_kql_time(start, timedelta(minutes=-5))} .. "
            f"{_kql_time(end, timedelta(minutes=15))})"
        )
    else:
        when = f"| where TimeGenerated > ago({int(days or 7)}d)"
    lines = []
    ids = list(trace_ids or [])[:MAX_KQL_TRACES]
    if ids:
        lines.append(f"let traces = dynamic([{', '.join(_kql_string(i) for i in ids)}]);")
    lines.append(f"union isfuzzy=true {', '.join(SPAN_TABLES)}")
    lines.append(when)
    if ids:
        lines.append("| where OperationId in (traces)")
    if model:
        lines.append(
            f"| where tostring(Properties['pheasant.chat_model']) == {_kql_string(model)} "
            f"or tostring(Properties['pheasant.decision_model']) == {_kql_string(model)}"
        )
    lines.append(
        "| project TimeGenerated, OperationId, Type, Name, DurationMs, Success, ResultCode, "
        "Message, SeverityLevel, AppRoleName, Properties"
    )
    lines.append("| order by TimeGenerated asc")
    return "\n".join(lines)
