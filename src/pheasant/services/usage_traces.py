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

Each takes ``match``, a simple wildcard search over what a call said: its
question, its answer, its operation and its session. ``*`` is any run of
characters, ``?`` is one, everything else is literal, case is ignored, and
the pattern may match anywhere (``deploy*worker`` finds "how do I deploy a
worker"). A session list then holds the sessions with a matching call, counting
those calls, and a session holds only its matching calls.

**Nothing is materialized ahead of a question.** Each is one bounded query
over the ledger, run when the operator selects something; the window is the
depth of the drill-down. The ledger keeps ``hot_retention_days``, so a window
longer than that shows what remains, and the payload says so.

Admin only (``security.manage``), like the totals: these rows carry what
people asked.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
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

    def __init__(self, key: str, match: str | None = None) -> None:
        searched = f" matching {match!r}" if match else ""
        super().__init__(f"No calls{searched} for session {key} in the selected window")


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


class VisualTrace:
    """What :func:`visual_trace` measured of one diagram request."""

    def __init__(self, request: Any) -> None:
        self.request = request
        self.started = time.perf_counter()
        self.drawing_started: float | None = None
        self.searched_seconds: float | None = None
        self.usage: Any = None

    def finish(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Put the diagram's request and shape on the ledger row; return ``payload``."""

        annotate_visual(self.request, payload, self)
        return payload


@contextmanager
def visual_trace(request: Any) -> Iterator[VisualTrace]:
    """Measure a diagram request's drawing: the time it took and the tokens the
    model reported (``providers.collect_token_usage``), for :func:`annotate_visual`."""

    from pheasant.assistant.providers import collect_token_usage

    trace = VisualTrace(request)
    trace.drawing_started = time.perf_counter()
    trace.searched_seconds = trace.drawing_started - trace.started
    with collect_token_usage() as usage:
        trace.usage = usage
        yield trace


def annotate_visual(request: Any, payload: dict[str, Any], trace: Any = None) -> None:
    """Put a diagram request on the ledger row its call is filling in.

    A diagram asked for after an answer (the chat's "Draw a diagram", a
    redraw in another shape, MCP ``create_visual``) is a call of its own, and
    without this its row carried no question: the drill-down's Questions view
    hid it and nothing said what was drawn. The request becomes the row's
    ``query_text``, a one-line outcome its ``answer_text``, and the trace the
    same shape an answer's has (models, route, tokens, steps), so the Usage
    drill-down shows it beside the conversation's answers. Best-effort.
    """

    from pheasant.telemetry.interactions import current_event

    event = current_event()
    if event is None or getattr(event, "handed_over", False):
        return
    try:
        text = str(getattr(request, "request", "") or "")
        if not event.query_text:
            event.query_text = text
        if not event.session_id and getattr(request, "conversation_id", None):
            event.session_id = str(request.conversation_id)[:200]
        visual = payload.get("visual") or {}
        diagram = visual.get("diagram") or {}
        status = str(visual.get("status") or "none")
        drawn = diagram.get("kind") or ("images" if visual.get("type") == "images" else None)
        grounding = visual.get("grounding") or {}
        if status == "ok" and drawn == "images":
            outcome = f"Showed {len(visual.get('figures') or [])} image(s)"
        elif status == "ok":
            outcome = (
                f"Drew a {drawn} diagram, {diagram.get('title') or 'untitled'}: "
                f"{grounding.get('cited', 0)} cited and {grounding.get('inferred', 0)} "
                "inferred element(s)"
            )
        else:
            outcome = f"No diagram: {visual.get('reason') or 'nothing to draw from'}"
        if visual.get("fallback_from"):
            outcome += f" (drawn from the graph; the {visual['fallback_from']} could not)"
        if not event.answer_text:
            event.answer_text = outcome
        usage = getattr(trace, "usage", None)
        model = getattr(usage, "model", None) if usage is not None else None
        tokens = {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        }
        drawing = (
            time.perf_counter() - trace.drawing_started
            if trace is not None and trace.drawing_started is not None
            else None
        )
        named = len(getattr(request, "node_ids", None) or [])
        steps = [
            {
                "name": "passages",
                "detail": (f"{named} named passage(s)" if named else "searched for the request")
                + f"; {len(payload.get('citations') or [])} to draw from",
                "passages": len(payload.get("citations") or []),
                "duration_seconds": getattr(trace, "searched_seconds", None),
            },
            {
                "name": "draw",
                "detail": outcome[:MAX_STEP_DETAIL],
                "model": model,
                "duration_seconds": drawing,
                **tokens,
            },
        ]
        if model:
            event.attributes["chat_model"] = model.split(", ")[0]
        event.attributes["visual"] = {
            "requested_kind": getattr(request, "kind", None),
            "kind": drawn,
            "status": status,
            "reason": visual.get("reason"),
            "fallback_from": visual.get("fallback_from"),
            "grounding": grounding or None,
            "named_passages": named,
        }
        event.attributes["answer"] = {
            "models": {"chat": model, "decision": None},
            "route": {"intent": "visual", "depth": None, "visual": drawn or "none"},
            "workflow": "visual",
            "status": status,
            "citations": len(payload.get("citations") or []),
            "tokens": tokens,
            "steps": steps,
        }
    except Exception:  # noqa: BLE001 - observation must never fail a drawing
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
    calls: str = "questions",
    match: str | None = None,
) -> tuple[str, list[Any]]:
    state = context.state
    clauses = ["kb_id = ?", "started_at >= ?"]
    params: list[Any] = [context.config.pheasant.name, since]
    if calls == "questions":
        clauses.append(QUESTIONS)
    if match:
        clause, values = _match_clause(match)
        clauses.append(clause)
        params += values
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


#: Which calls a drill-down reads. The ledger observes every request, and on a
#: live region about 98% of them are the UI's polling (``/jobs``, ``/queue``,
#: ``/overview``) and the tiers' own ``/internal`` traffic: measured on
#: production, 179 questions among 11,645 rows in a week. The usage totals
#: count questions and searches, so the traces behind them are the calls that
#: carry one; ``all`` is every request, for chasing a fault.
CALLS = ("questions", "all")
QUESTIONS = "COALESCE(query_text, '') <> ''"


def _calls(value: Any) -> str:
    value = str(value or "questions").strip().lower()
    if value not in CALLS:
        raise InvalidRequest("calls must be questions or all")
    return value


#: The longest search pattern a drill-down takes.
MAX_MATCH = 200
#: What a search reads: what the call said, and where it was made.
MATCHED_COLUMNS = ("query_text", "answer_text", "operation", "session_id")


def _match(value: Any) -> str | None:
    """A search pattern, trimmed; ``None`` for no search."""

    value = str(value or "").strip()
    if not value.strip("*"):
        return None  # "" and "*" match everything: no search at all
    if len(value) > MAX_MATCH:
        raise InvalidRequest(f"match must be at most {MAX_MATCH} characters")
    return value


def _like_pattern(match: str) -> str:
    """A wildcard pattern as a LIKE pattern that matches anywhere.

    ``*`` and ``?`` become ``%`` and ``_``; LIKE's own ``%`` and ``_`` and the
    escape character (a backslash) are literal, so "50%" finds "50%".
    """

    out = []
    for char in match.lower():
        if char == "*":
            out.append("%")
        elif char == "?":
            out.append("_")
        elif char in "%_\\":
            out.append("\\" + char)
        else:
            out.append(char)
    return "%" + "".join(out) + "%"


def _match_clause(match: str) -> tuple[str, list[Any]]:
    # LOWER on both sides: SQLite's LIKE ignores ASCII case and Postgres's
    # does not, and a search should read the same on both.
    pattern = _like_pattern(match)
    branches = [f"LOWER(COALESCE({column}, '')) LIKE ? ESCAPE '\\'" for column in MATCHED_COLUMNS]
    return "(" + " OR ".join(branches) + ")", [pattern] * len(MATCHED_COLUMNS)


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
    calls: str = "questions",
    match: str | None = None,
) -> dict[str, Any]:
    """The sessions a person, organization or model took part in, newest first.

    ``calls`` is ``questions`` (the default: the questions and searches the
    usage totals count) or ``all`` (every request the ledger observed).
    ``match`` keeps the sessions with a call it finds, counting those calls.
    """

    settings = _settings(context)
    days, since = _window(days)
    calls = _calls(calls)
    match = _match(match)
    where, params = _where(
        context,
        since,
        principal=principal,
        organization=organization,
        model=model,
        calls=calls,
        match=match,
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
        "filter": {
            "principal": principal,
            "organization": organization,
            "model": model,
            "calls": calls,
            "match": match,
        },
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


def _session_rows(
    context: ServiceContext,
    key: str,
    since: str,
    calls: str = "questions",
    match: str | None = None,
) -> list[dict[str, Any]]:
    if key.startswith("trace:"):
        clause, params = "trace_id = ? AND session_id IS NULL", [key[len("trace:") :]]
    else:
        clause, params = "session_id = ?", [key]
    if calls == "questions":
        clause += f" AND {QUESTIONS}"
    if match:
        searched, values = _match_clause(match)
        clause += f" AND {searched}"
        params += values
    return context.state.rows(
        f"SELECT {_COLUMNS} FROM interaction_events WHERE kb_id = ? AND started_at >= ? "
        f"AND {clause} ORDER BY started_at LIMIT ?",
        (context.config.pheasant.name, since, *params, MAX_SESSION_CALLS),
    )


def session(
    context: ServiceContext,
    key: str,
    *,
    days: int = 7,
    calls: str = "questions",
    match: str | None = None,
) -> dict[str, Any]:
    """One session's calls in order, with each answer's steps and a KQL query.

    With ``match``, only the calls it finds; none is ``SESSION_NOT_FOUND``.
    """

    settings = _settings(context)
    days, since = _window(days)
    which = _calls(calls)
    match = _match(match)
    key = str(key or "").strip()
    if not key:
        raise InvalidRequest("session is required")
    rows = _session_rows(context, key, since, which, match)
    if not rows:
        raise SessionNotFound(key, match)
    # A request whose work is a child row of the same trace (a stream's answer)
    # says nothing its child does not: show the child, keep both in exports.
    parents = {row["parent_span_id"] for row in rows if row["parent_span_id"]}
    found = [_call(row) for row in rows if row["span_id"] not in parents]
    trace_ids = list(dict.fromkeys(call["trace_id"] for call in found))
    return {
        "session": key,
        "window": {"days": days, "from": since},
        "filter": {"calls": which, "match": match},
        "first_at": found[0]["started_at"],
        "last_at": found[-1]["started_at"],
        "calls": found,
        "truncated": len(rows) >= MAX_SESSION_CALLS,
        "retention": _retention(settings, days),
        "kql": kql(
            trace_ids=trace_ids,
            start=found[0]["started_at"],
            end=found[-1]["started_at"],
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
    calls: str = "questions",
    match: str | None = None,
) -> Iterator[str]:
    """The selected ledger rows as JSON lines, oldest first, for a download."""

    _settings(context)
    days, since = _window(days)
    calls = _calls(calls)
    match = _match(match)
    if session_key:
        rows = _session_rows(context, session_key, since, calls, match)
    else:
        where, params = _where(
            context,
            since,
            principal=principal,
            organization=organization,
            model=model,
            calls=calls,
            match=match,
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
