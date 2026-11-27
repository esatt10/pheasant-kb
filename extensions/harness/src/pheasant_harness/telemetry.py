"""What the harness tells an operator, and every place it tells it.

Three signals, each from the durable record rather than from a process, so an
operator sees the same thing whichever replica answers and after a restart:

* **The overview** (:func:`overview`): runs by state and workload, how long
  they took, what they spent against their budgets, what the verifier decided,
  what was delivered where, every connection call by provider and outcome, the
  approvals waiting, and the effects nobody can confirm. Computed from the
  harness tables over a window; this is what the Admin page's Harness tab
  renders, and what ``get_harness_telemetry`` / ``GET /harness/telemetry``
  return.
* **One trace per finished run** (:func:`run_spans`, :func:`emit_pending`):
  ``pheasant.harness.run`` with a child span per phase the run went through,
  per step attempt, per connection call, per effect and per verification,
  rebuilt from the journal once the run is terminal. Emitted exactly as sync
  traces are (`pheasant.telemetry.sync_trace.emit`): one ``otel.span`` JSON
  line each on stdout -- which Container Apps ships to Log Analytics -- and
  over OTLP when a collector is configured. The ids are derived from the run
  id, so the trace a collector holds and the one this page names are one
  trace. A run is claimed in ``harness_telemetry_traces`` before it is
  emitted, so two replicas emit it once. Spans carry the workload, state,
  usage and the models' spend, never the person who asked.
* **Prometheus** (``/metrics``): ``pheasant_harness_*`` counters for runs,
  steps, connection calls and verifications as their traces are emitted, and
  gauges for what is live now.

Where each signal goes is listed by :func:`destinations` -- configured or not
-- and :func:`test_destination` sends one test span to a destination and says
what happened, so an export path can be proven before anything depends on it.
With no collector configured (the standalone default, and any region not yet
connected to a cloud) the OTLP and Azure Monitor rows say so and their test
reports ``not_configured``; the stdout and Prometheus paths are always on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from pheasant_harness.common import loads, now

logger = logging.getLogger(__name__)

SPAN_PREFIX = "pheasant.harness"
TERMINAL = ("succeeded", "partial", "failed", "cancelled")
LIVE = ("accepted", "running", "verifying", "delivering", "reconciling", "waiting")
_PHASES = {
    "run.accepted": "accepted",
    "run.running": "running",
    "run.resumed": "running",
    "run.waiting": "waiting",
    "run.verifying": "verifying",
    "run.delivering": "delivering",
    "run.reconciling": "reconciling",
}
_METRICS = {
    "pheasant_harness_runs_total": ("counter", "Harness runs finished", ("workload", "state")),
    "pheasant_harness_run_seconds": ("histogram", "Harness run wall time", ("workload",)),
    "pheasant_harness_steps_total": (
        "counter",
        "Harness step attempts finished",
        ("action", "status"),
    ),
    "pheasant_harness_connection_calls_total": (
        "counter",
        "Requests the harness made through connected accounts",
        ("provider", "action", "outcome"),
    ),
    "pheasant_harness_verifications_total": (
        "counter",
        "Checks the verifier ran",
        ("outcome",),
    ),
    "pheasant_harness_traces_emitted_total": (
        "counter",
        "Run traces written to stdout and offered to the OTLP exporter",
        ("exported",),
    ),
    "pheasant_harness_runs_live": ("gauge", "Harness runs not yet finished", ("state",)),
    "pheasant_harness_approvals_pending": ("gauge", "Writes waiting for an approval", ()),
    "pheasant_harness_effects_uncertain": (
        "gauge",
        "Effects that may have reached their target and cannot be confirmed",
        (),
    ),
}
_REGISTERED = False


# -- metrics -------------------------------------------------------------------------


def ensure_metrics() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    from pheasant.telemetry.metrics import REGISTRY

    for name, (kind, help_text, labels) in _METRICS.items():
        try:
            if kind == "histogram":
                REGISTRY.histogram(name, help_text, labels)
            else:
                getattr(REGISTRY, kind)(name, help_text, labels)
        except (ValueError, KeyError):  # already registered by an earlier import
            pass
    _REGISTERED = True


def _metric(method: str, name: str, value: float = 1.0, **labels: str) -> None:
    try:
        from pheasant.telemetry.metrics import REGISTRY

        ensure_metrics()
        getattr(REGISTRY, method)(name, value, **labels)
    except Exception:  # noqa: BLE001 - a metric is never load-bearing
        logger.debug("harness metric %s failed", name, exc_info=True)


def metric_values() -> dict[str, list[dict]]:
    """The ``pheasant_harness_*`` series as ``/metrics`` renders them, parsed."""
    from pheasant.telemetry.metrics import REGISTRY

    ensure_metrics()
    found: dict[str, list[dict]] = {name: [] for name in _METRICS}
    for line in REGISTRY.render().splitlines():
        if not line.startswith("pheasant_harness_") or line.startswith("#"):
            continue
        series, _, value = line.rpartition(" ")
        name, _, labels = series.partition("{")
        base = next((m for m in _METRICS if name == m or name.startswith(m + "_")), None)
        if base is None:
            continue
        found[base].append({"series": series, "value": float(value)})
    return found


def publish_gauges(harness: Any) -> None:
    states = {
        row["state"]: int(row["n"])
        for row in harness.state.rows(
            "SELECT state, COUNT(*) AS n FROM harness_runs WHERE kb_id=? GROUP BY state",
            (harness.kb_id,),
        )
    }
    for state in LIVE:
        _metric("set", "pheasant_harness_runs_live", float(states.get(state, 0)), state=state)
    _metric("set", "pheasant_harness_approvals_pending", float(_count_waiting_approvals(harness)))
    _metric("set", "pheasant_harness_effects_uncertain", float(_count_uncertain(harness)))


def _count_waiting_approvals(harness: Any) -> int:
    rows = harness.state.rows(
        "SELECT wait_json FROM harness_runs WHERE kb_id=? AND state='waiting'", (harness.kb_id,)
    )
    return sum(1 for row in rows if (loads(row["wait_json"], {}) or {}).get("kind") == "approval")


def _count_uncertain(harness: Any) -> int:
    rows = harness.state.rows(
        "SELECT COUNT(*) AS n FROM harness_effects e JOIN harness_runs r ON r.id=e.run_id "
        "WHERE r.kb_id=? AND e.state='uncertain'",
        (harness.kb_id,),
    )
    return int(rows[0]["n"]) if rows else 0


# -- traces --------------------------------------------------------------------------


def trace_id(run_id: str) -> str:
    return hashlib.sha256(f"harness-trace\0{run_id}".encode()).hexdigest()[:32]


def _span_id(trace: str, name: str) -> str:
    return hashlib.sha256(f"{trace}\0{name}".encode()).hexdigest()[:16]


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _span(
    trace: str,
    key: str,
    name: str,
    parent: str | None,
    start: float,
    end: float,
    ok: bool,
    attributes: dict,
    message: str | None = None,
) -> dict:
    from pheasant.telemetry.sync_trace import SPAN_TYPE

    end = max(float(end), float(start))
    return {
        "type": SPAN_TYPE,
        "name": name,
        "kind": "INTERNAL",
        "trace_id": trace,
        "span_id": _span_id(trace, key),
        "parent_span_id": parent,
        "start_time": _iso(start),
        "end_time": _iso(end),
        "duration_ms": round((end - float(start)) * 1000, 1),
        "status": {"code": "OK" if ok else "ERROR", **({"message": message} if message else {})},
        "resource": {
            "service.name": "pheasant",
            "service.instance.id": socket.gethostname(),
            **(
                {"pheasant.role": os.environ["PHEASANT_ROLE"]}
                if os.environ.get("PHEASANT_ROLE")
                else {}
            ),
        },
        "attributes": {k: v for k, v in attributes.items() if v is not None},
    }


def spent_by_run(harness: Any, run_ids: list[str]) -> dict[str, dict[str, float]]:
    """What each run spent, summed over its step attempts (where spend is recorded)."""
    found: dict[str, dict[str, float]] = {}
    for start in range(0, len(run_ids), 500):
        chunk = run_ids[start : start + 500]
        if not chunk:
            continue
        marks = ",".join("?" for _ in chunk)
        for row in harness.state.rows(
            f"SELECT run_id, spent_json FROM harness_attempts WHERE run_id IN ({marks})",
            tuple(chunk),
        ):
            totals = found.setdefault(row["run_id"], {})
            for key, value in (loads(row["spent_json"], {}) or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] = totals.get(key, 0) + value
    return found


def run_spans(harness: Any, run: dict) -> list[dict]:
    """The run's trace, rebuilt from its journal: a root and one child per thing it did."""
    trace = trace_id(run["id"])
    usage = {**(run.get("usage") or {}), **spent_by_run(harness, [run["id"]]).get(run["id"], {})}
    result = run.get("result") or {}
    root_key = "run"
    root_id = _span_id(trace, root_key)
    start, end = float(run["created_at"]), float(run["updated_at"])
    spans = [
        _span(
            trace,
            root_key,
            f"{SPAN_PREFIX}.run",
            None,
            start,
            end,
            run["state"] == "succeeded",
            {
                "pheasant.harness.run_id": run["id"],
                "pheasant.harness.workload": run["workload"],
                "pheasant.harness.state": run["state"],
                # A tenant defaults to the asker; a span never names the person.
                "pheasant.harness.tenant": run.get("tenant")
                if run.get("tenant") != run.get("actor")
                else None,
                "pheasant.harness.result": result.get("status"),
                "pheasant.harness.destination": (
                    (run.get("request") or {}).get("destination") or {}
                ).get("adapter"),
                **{
                    f"pheasant.harness.usage.{k}": v
                    for k, v in usage.items()
                    if isinstance(v, (int, float))
                },
            },
            message=result.get("reason") if run["state"] != "succeeded" else None,
        )
    ]
    events = harness.state.rows(
        "SELECT seq, type, created_at FROM harness_events WHERE run_id=? ORDER BY seq",
        (run["id"],),
    )
    phase, since = None, start
    for event in events:
        kind = event["type"]
        nxt = _PHASES.get(kind) or (
            kind[4:] if kind.startswith("run.") and kind[4:] in TERMINAL else None
        )
        if nxt is None or nxt == phase:
            continue
        if phase is not None:
            spans.append(
                _span(
                    trace,
                    f"phase:{phase}:{since}",
                    f"{SPAN_PREFIX}.phase.{phase}",
                    root_id,
                    since,
                    event["created_at"],
                    True,
                    {"pheasant.harness.phase": phase},
                )
            )
        phase, since = nxt, float(event["created_at"])
        if nxt in TERMINAL:
            break
    for row in harness.state.rows(
        "SELECT a.attempt_id, a.step_id, a.status, a.error_class, a.started_at, a.finished_at, "
        "a.spent_json, s.action, s.capability FROM harness_attempts a JOIN harness_steps s "
        "ON s.run_id=a.run_id AND s.step_id=a.step_id WHERE a.run_id=? ORDER BY a.started_at",
        (run["id"],),
    ):
        spent = loads(row["spent_json"], {}) or {}
        spans.append(
            _span(
                trace,
                f"attempt:{row['attempt_id']}",
                f"{SPAN_PREFIX}.step.{row['action']}",
                root_id,
                row["started_at"],
                row["finished_at"] or row["started_at"],
                row["status"] in ("completed", "succeeded"),
                {
                    "pheasant.harness.step_id": row["step_id"],
                    "pheasant.harness.attempt_status": row["status"],
                    "pheasant.harness.capability": row["capability"],
                    "pheasant.harness.error_class": row["error_class"],
                    **{
                        f"pheasant.harness.spent.{k}": v
                        for k, v in spent.items()
                        if isinstance(v, (int, float))
                    },
                },
            )
        )
    for row in harness.state.rows(
        "SELECT id, connection, provider, action, method, host, outcome, status_code, bytes, "
        "policy_version, reason, at FROM harness_connection_calls WHERE run_id=? ORDER BY at",
        (run["id"],),
    ):
        spans.append(
            _span(
                trace,
                f"call:{row['id']}",
                f"{SPAN_PREFIX}.connection.{row['action']}",
                root_id,
                row["at"],
                row["at"],
                row["outcome"] == "allowed",
                {
                    "pheasant.connection.name": row["connection"],
                    "pheasant.connection.provider": row["provider"],
                    "http.request.method": row["method"],
                    "server.address": row["host"],
                    "pheasant.connection.outcome": row["outcome"],
                    "http.response.status_code": row["status_code"],
                    "pheasant.connection.bytes": row["bytes"],
                    "pheasant.connection.policy_version": row["policy_version"],
                },
                message=row["reason"] if row["outcome"] != "allowed" else None,
            )
        )
    for row in harness.state.rows(
        "SELECT operation_id, adapter, state, disposition, dispatch_count, created_at, updated_at "
        "FROM harness_effects WHERE run_id=? ORDER BY created_at",
        (run["id"],),
    ):
        spans.append(
            _span(
                trace,
                f"effect:{row['operation_id']}",
                f"{SPAN_PREFIX}.effect.{row['adapter']}",
                root_id,
                row["created_at"],
                row["updated_at"],
                row["state"] in ("committed", "reconciled"),
                {
                    "pheasant.harness.operation_id": row["operation_id"],
                    "pheasant.harness.effect_state": row["state"],
                    "pheasant.harness.disposition": row["disposition"],
                    "pheasant.harness.dispatches": row["dispatch_count"],
                },
            )
        )
    for row in harness.state.rows(
        "SELECT id, criterion_id, grader, outcome, created_at FROM harness_verifications "
        "WHERE run_id=? ORDER BY created_at",
        (run["id"],),
    ):
        spans.append(
            _span(
                trace,
                f"verify:{row['id']}",
                f"{SPAN_PREFIX}.verify",
                root_id,
                row["created_at"],
                row["created_at"],
                row["outcome"] == "pass",
                {
                    "pheasant.harness.criterion": row["criterion_id"],
                    "pheasant.harness.grader": row["grader"],
                    "pheasant.harness.outcome": row["outcome"],
                },
            )
        )
    return spans


def _exporting() -> bool:
    """Whether this process's OTLP exporter is attached (spans reach a collector)."""
    try:
        from pheasant.telemetry.interactions import TRACING

        return bool(TRACING.enabled)
    except Exception:  # noqa: BLE001
        return False


def _readable(span: dict) -> Any:
    """One span dict as the SDK's ``ReadableSpan``, for a synchronous test export."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

    def ns(value: str) -> int:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1e9)

    return ReadableSpan(
        name=span["name"],
        context=SpanContext(
            int(span["trace_id"], 16), int(span["span_id"], 16), False, TraceFlags(1)
        ),
        parent=None,
        kind=SpanKind.INTERNAL,
        resource=Resource.create(span["resource"]),
        attributes=span["attributes"],
        start_time=ns(span["start_time"]),
        end_time=ns(span["end_time"]),
        status=Status(StatusCode.OK),
    )


def emit_pending(harness: Any, limit: int = 25) -> int:
    """Emit the trace of every finished run not yet emitted. Returns how many."""
    marks = ",".join("?" for _ in TERMINAL)
    rows = harness.state.rows(
        f"SELECT r.id FROM harness_runs r LEFT JOIN harness_telemetry_traces t "
        f"ON t.kb_id=r.kb_id AND t.run_id=r.id WHERE r.kb_id=? AND r.state IN ({marks}) "
        f"AND t.run_id IS NULL ORDER BY r.updated_at LIMIT ?",
        (harness.kb_id, *TERMINAL, int(limit)),
    )
    emitted = 0
    for row in rows:
        run = harness.runs.get(row["id"])
        claimed = harness.state.execute_returning(
            "INSERT INTO harness_telemetry_traces(kb_id,run_id,trace_id,state,spans,duration_ms,"
            "exported,emitted_at) VALUES(?,?,?,?,0,?,0,?) ON CONFLICT (kb_id,run_id) DO NOTHING "
            "RETURNING run_id",
            (
                harness.kb_id,
                run["id"],
                trace_id(run["id"]),
                run["state"],
                (float(run["updated_at"]) - float(run["created_at"])) * 1000,
                now(),
            ),
        )
        if not claimed:
            continue  # another replica emitted it
        error = None
        try:
            spans = run_spans(harness, run)
            from pheasant.telemetry.sync_trace import emit

            emit(spans)
        except Exception as exc:  # noqa: BLE001 - telemetry never fails a run
            spans, error = [], f"{type(exc).__name__}: {exc}"
            logger.warning("harness trace for %s could not be emitted", run["id"], exc_info=True)
        exported = _exporting() and error is None
        harness.state.execute(
            "UPDATE harness_telemetry_traces SET spans=?, exported=?, export_error=? "
            "WHERE kb_id=? AND run_id=?",
            (len(spans), int(exported), error, harness.kb_id, run["id"]),
        )
        _count_run(run, spans, exported)
        emitted += 1
    if emitted:
        publish_gauges(harness)
    return emitted


def _count_run(run: dict, spans: list[dict], exported: bool) -> None:
    _metric("inc", "pheasant_harness_runs_total", workload=run["workload"], state=run["state"])
    _metric(
        "observe",
        "pheasant_harness_run_seconds",
        max(0.0, float(run["updated_at"]) - float(run["created_at"])),
        workload=run["workload"],
    )
    for span in spans:
        name, attrs = span["name"], span["attributes"]
        if name.startswith(f"{SPAN_PREFIX}.step."):
            _metric(
                "inc",
                "pheasant_harness_steps_total",
                action=name.rsplit(".", 1)[-1],
                status=str(attrs.get("pheasant.harness.attempt_status")),
            )
        elif name.startswith(f"{SPAN_PREFIX}.connection."):
            _metric(
                "inc",
                "pheasant_harness_connection_calls_total",
                provider=str(attrs.get("pheasant.connection.provider")),
                action=name.rsplit(".", 1)[-1],
                outcome=str(attrs.get("pheasant.connection.outcome")),
            )
        elif name == f"{SPAN_PREFIX}.verify":
            _metric(
                "inc",
                "pheasant_harness_verifications_total",
                outcome=str(attrs.get("pheasant.harness.outcome")),
            )
    _metric("inc", "pheasant_harness_traces_emitted_total", exported=str(exported).lower())


# -- destinations --------------------------------------------------------------------


def _observability(harness: Any) -> Any:
    return getattr(harness.config, "observability", None)


def destinations(harness: Any) -> list[dict]:
    """Every place harness telemetry goes, whether it is configured, and its last test."""
    from pheasant.telemetry.tracing import destination

    settings = _observability(harness)
    try:
        endpoint, protocol, headers = destination(settings, "traces")
        problem = None
    except ValueError as exc:
        endpoint, protocol, headers, problem = "", "", {}, str(exc)
    try:
        import opentelemetry.sdk  # noqa: F401

        sdk = True
    except ImportError:
        sdk = False
    azure = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    tests = _last_tests(harness)
    rows = [
        {
            "id": "prometheus",
            "label": "Prometheus metrics",
            "kind": "metrics",
            "configured": True,
            "where": "GET /metrics on every Pheasant process (pheasant_harness_*)",
            "detail": "Always on. Scrape it, or let Container Apps' Prometheus add-on do so.",
        },
        {
            "id": "stdout",
            "label": "Span lines on stdout",
            "kind": "traces",
            "configured": True,
            "where": "one otel.span JSON line per span in the process log",
            "detail": "Always on. Container Apps and Kubernetes ship stdout to Log Analytics / "
            "your log pipeline, so a run's trace is searchable there by its trace id.",
        },
        {
            "id": "otlp",
            "label": "OpenTelemetry collector (OTLP)",
            "kind": "traces",
            "configured": bool(endpoint) and problem is None,
            "where": endpoint or "not configured",
            "protocol": protocol or None,
            "headers": sorted(headers),
            "sdk_installed": sdk,
            "exporting": _exporting(),
            "detail": problem
            or (
                "Set observability.otlp_endpoint (or OTEL_EXPORTER_OTLP_ENDPOINT) to send run "
                "traces to a collector."
                if not endpoint
                else "Run traces are exported here as they finish."
                if sdk
                else "Configured, but the OpenTelemetry SDK is not installed (pheasant[otel]); "
                "a test is sent as OTLP/JSON instead."
            ),
        },
        {
            "id": "azure_monitor",
            "label": "Azure Monitor / Application Insights",
            "kind": "traces",
            "configured": bool(azure),
            "where": _ingestion_host(azure) or "not configured",
            "detail": "Reached through the OTLP collector (the Container Apps managed "
            "OpenTelemetry agent forwards to Application Insights). "
            + (
                "A connection string is present."
                if azure
                else "Set APPLICATIONINSIGHTS_CONNECTION_STRING on the environment to connect."
            ),
        },
    ]
    for row in rows:
        row["last_test"] = tests.get(row["id"])
    return rows


def _ingestion_host(connection_string: str) -> str | None:
    for part in connection_string.split(";"):
        key, _, value = part.partition("=")
        if key.strip().lower() == "ingestionendpoint" and value:
            return value.strip()
    return None


def _last_tests(harness: Any) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for row in harness.state.rows(
        "SELECT id, destination, ok, detail_json, actor, at FROM harness_telemetry_tests "
        "WHERE kb_id=? ORDER BY at DESC LIMIT 50",
        (harness.kb_id,),
    ):
        found.setdefault(
            row["destination"],
            {
                "id": row["id"],
                "ok": bool(row["ok"]),
                "at": row["at"],
                "actor": row["actor"],
                **loads(row["detail_json"], {}),
            },
        )
    return found


def _test_span(trace: str) -> dict:
    start = time.time()
    return _span(
        trace,
        "test",
        f"{SPAN_PREFIX}.telemetry_test",
        None,
        start,
        start + 0.001,
        True,
        {"pheasant.harness.test": True},
    )


def _otlp_json(span: dict) -> bytes:
    def ns(iso: str) -> str:
        return str(
            int(
                datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).timestamp()
                * 1e9
            )
        )

    def value(item: Any) -> dict:
        if isinstance(item, bool):
            return {"boolValue": item}
        if isinstance(item, int):
            return {"intValue": str(item)}
        if isinstance(item, float):
            return {"doubleValue": item}
        return {"stringValue": str(item)}

    return json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": k, "value": value(v)} for k, v in span["resource"].items()
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "pheasant.harness"},
                            "spans": [
                                {
                                    "traceId": span["trace_id"],
                                    "spanId": span["span_id"],
                                    "name": span["name"],
                                    "kind": 1,
                                    "startTimeUnixNano": ns(span["start_time"]),
                                    "endTimeUnixNano": ns(span["end_time"]),
                                    "attributes": [
                                        {"key": k, "value": value(v)}
                                        for k, v in span["attributes"].items()
                                    ],
                                    "status": {"code": 1},
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    ).encode()


def _send_otlp(settings: Any, span: dict) -> dict:
    from pheasant.telemetry.tracing import destination, exporter

    endpoint, protocol, headers = destination(settings, "traces")
    if not endpoint:
        return {"outcome": "not_configured", "message": "no OTLP endpoint is configured"}
    started = time.perf_counter()
    try:
        from opentelemetry.sdk.trace.export import SpanExportResult

        sdk = True
    except ImportError:
        sdk = False
    if sdk:
        sender = exporter(settings, "traces")
        try:
            result = sender.export([_readable(span)])
        except Exception as exc:  # noqa: BLE001 - said, not raised
            return {
                "outcome": "unreachable",
                "via": f"OpenTelemetry SDK ({protocol})",
                "endpoint": endpoint,
                "message": f"{type(exc).__name__}: {exc}",
            }
        finally:
            sender.shutdown()
        ok = result == SpanExportResult.SUCCESS
        return {
            "outcome": "sent" if ok else "refused",
            "via": f"OpenTelemetry SDK ({protocol})",
            "endpoint": endpoint,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "message": "the collector accepted the test span"
            if ok
            else "the exporter reported a failure (see the process log)",
        }
    if protocol != "http/protobuf":
        return {
            "outcome": "unavailable",
            "endpoint": endpoint,
            "message": "a gRPC endpoint needs the OpenTelemetry SDK (pip install pheasant[otel])",
        }
    request = urllib.request.Request(
        endpoint,
        data=_otlp_json(span),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - operator's URL
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return {
            "outcome": "unreachable",
            "via": "OTLP/JSON",
            "endpoint": endpoint,
            "message": str(getattr(exc, "reason", exc)),
        }
    return {
        "outcome": "sent" if 200 <= code < 300 else "refused",
        "via": "OTLP/JSON",
        "endpoint": endpoint,
        "status_code": code,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "message": "the collector accepted the test span"
        if 200 <= code < 300
        else f"the collector answered HTTP {code}",
    }


def test_destination(harness: Any, destination_id: str, actor: str) -> dict:
    """Send one test signal to ``destination_id`` and record what happened."""
    trace = hashlib.sha256(f"harness-test\0{time.time_ns()}".encode()).hexdigest()[:32]
    span = _test_span(trace)
    if destination_id == "prometheus":
        _metric("inc", "pheasant_harness_traces_emitted_total", 0.0, exported="test")
        values = metric_values()
        detail = {
            "outcome": "sent",
            "message": f"{sum(len(v) for v in values.values())} pheasant_harness_* series on "
            "/metrics in this process",
        }
    elif destination_id == "stdout":
        from pheasant.telemetry.sync_trace import emit

        emit([span])
        detail = {
            "outcome": "sent",
            "message": "one otel.span line written to this process's stdout",
        }
    elif destination_id == "otlp":
        detail = _send_otlp(_observability(harness), span)
    elif destination_id == "azure_monitor":
        if not os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
            detail = {
                "outcome": "not_configured",
                "message": "no APPLICATIONINSIGHTS_CONNECTION_STRING; Azure Monitor is reached "
                "through the OTLP collector once one is configured",
            }
        else:
            detail = {**_send_otlp(_observability(harness), span), "through": "otlp"}
    else:
        raise ValueError(f"unknown telemetry destination: {destination_id}")
    detail["trace_id"] = trace
    ok = detail["outcome"] == "sent"
    test_id = "tt_" + trace[:16]
    harness.state.execute(
        "INSERT INTO harness_telemetry_tests(kb_id,id,destination,ok,detail_json,actor,at) "
        "VALUES(?,?,?,?,?,?,?)",
        (harness.kb_id, test_id, destination_id, int(ok), json.dumps(detail), actor, now()),
    )
    return {"destination": destination_id, "ok": ok, "id": test_id, **detail}


# -- overview ------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    """A database aggregate as a JSON number (PostgreSQL's AVG is a decimal)."""
    return None if value is None else round(float(value), 3)


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[index], 1)


def overview(harness: Any, *, window_hours: float = 24 * 7, recent: int = 25) -> dict:
    """Everything an operator needs to see about the harness, over a window."""
    emit_pending(harness)
    publish_gauges(harness)
    since = now() - window_hours * 3600
    kb = harness.kb_id
    runs = [
        {
            **dict(row),
            "usage": loads(row["usage_json"], {}),
            "result": loads(row["result_json"], {}) or {},
            "budget": loads(row["budget_json"], {}),
        }
        for row in harness.state.rows(
            "SELECT id, workload, state, tenant, created_at, updated_at, usage_json, "
            "result_json, budget_json FROM harness_runs WHERE kb_id=? AND created_at>=? "
            "ORDER BY created_at DESC",
            (kb, since),
        )
    ]
    spent = spent_by_run(harness, [run["id"] for run in runs])
    for run in runs:
        run["usage"] = {**run["usage"], **spent.get(run["id"], {})}
    by_state: dict[str, int] = {}
    by_workload: dict[str, dict] = {}
    totals = {
        "model_calls": 0,
        "model_tokens": 0,
        "tool_calls": 0,
        "bytes_scanned": 0,
        "wall_seconds": 0.0,
    }
    durations: list[float] = []
    budget_stops = 0
    for run in runs:
        by_state[run["state"]] = by_state.get(run["state"], 0) + 1
        bucket = by_workload.setdefault(
            run["workload"], {"runs": 0, "succeeded": 0, "partial": 0, "failed": 0, "seconds": []}
        )
        bucket["runs"] += 1
        if run["state"] in ("succeeded", "partial", "failed"):
            bucket[run["state"]] += 1
        if run["state"] in TERMINAL:
            seconds = float(run["updated_at"]) - float(run["created_at"])
            durations.append(seconds)
            bucket["seconds"].append(seconds)
        for key in totals:
            value = run["usage"].get(key)
            if isinstance(value, (int, float)):
                totals[key] += value
        if str(run["result"].get("reason") or "").startswith("budget"):
            budget_stops += 1
    finished = sum(by_state.get(s, 0) for s in TERMINAL)
    workloads = {
        name: {
            "runs": b["runs"],
            "succeeded": b["succeeded"],
            "partial": b["partial"],
            "failed": b["failed"],
            "p50_seconds": _percentile(b["seconds"], 0.5),
            "p95_seconds": _percentile(b["seconds"], 0.95),
        }
        for name, b in sorted(by_workload.items())
    }
    steps = [
        {
            "action": row["action"],
            "status": row["status"],
            "n": int(row["n"]),
            "mean_seconds": _number(row["mean_seconds"]),
        }
        for row in harness.state.rows(
            "SELECT s.action, a.status, COUNT(*) AS n, "
            "AVG(COALESCE(a.finished_at, a.started_at) - a.started_at) AS mean_seconds "
            "FROM harness_attempts a JOIN harness_steps s ON s.run_id=a.run_id "
            "AND s.step_id=a.step_id JOIN harness_runs r ON r.id=a.run_id "
            "WHERE r.kb_id=? AND a.started_at>=? GROUP BY s.action, a.status "
            "ORDER BY s.action, a.status",
            (kb, since),
        )
    ]
    verifications = {
        row["outcome"]: int(row["n"])
        for row in harness.state.rows(
            "SELECT v.outcome, COUNT(*) AS n FROM harness_verifications v JOIN harness_runs r "
            "ON r.id=v.run_id WHERE r.kb_id=? AND v.created_at>=? GROUP BY v.outcome",
            (kb, since),
        )
    }
    deliveries = [
        {"adapter": row["adapter"], "state": row["state"], "n": int(row["n"])}
        for row in harness.state.rows(
            "SELECT e.adapter, e.state, COUNT(*) AS n FROM harness_effects e JOIN harness_runs r "
            "ON r.id=e.run_id WHERE r.kb_id=? AND e.created_at>=? GROUP BY e.adapter, e.state "
            "ORDER BY e.adapter, e.state",
            (kb, since),
        )
    ]
    calls = [
        {
            "provider": row["provider"],
            "action": row["action"],
            "outcome": row["outcome"],
            "n": int(row["n"]),
            "bytes": int(row["bytes"] or 0),
        }
        for row in harness.state.rows(
            "SELECT provider, action, outcome, COUNT(*) AS n, SUM(bytes) AS bytes "
            "FROM harness_connection_calls WHERE kb_id=? AND at>=? "
            "GROUP BY provider, action, outcome ORDER BY provider, action, outcome",
            (kb, since),
        )
    ]
    denials = [
        dict(row)
        for row in harness.state.rows(
            "SELECT connection, provider, action, resource, reason, run_id, at "
            "FROM harness_connection_calls WHERE kb_id=? AND at>=? AND outcome<>'allowed' "
            "ORDER BY at DESC LIMIT 10",
            (kb, since),
        )
    ]
    traces = {
        row["run_id"]: dict(row)
        for row in harness.state.rows(
            "SELECT run_id, trace_id, spans, exported, export_error, emitted_at "
            "FROM harness_telemetry_traces WHERE kb_id=? AND emitted_at>=?",
            (kb, since),
        )
    }
    recent_runs = []
    for run in runs[:recent]:
        trace = traces.get(run["id"])
        recent_runs.append(
            {
                "run_id": run["id"],
                "workload": run["workload"],
                "state": run["state"],
                "created_at": run["created_at"],
                "seconds": round(float(run["updated_at"]) - float(run["created_at"]), 1),
                "usage": run["usage"],
                "result": run["result"].get("status"),
                "reason": run["result"].get("reason"),
                "trace_id": (trace or {}).get("trace_id") or trace_id(run["id"]),
                "spans": (trace or {}).get("spans"),
                "trace": "emitted"
                if trace
                else ("pending" if run["state"] in TERMINAL else "live"),
                "exported": bool((trace or {}).get("exported")),
            }
        )
    emitted = len(traces)
    return {
        "window_hours": window_hours,
        "generated_at": now(),
        "runs": {
            "total": len(runs),
            "finished": finished,
            "by_state": by_state,
            "success_rate": round(by_state.get("succeeded", 0) / finished, 3) if finished else None,
            "p50_seconds": _percentile(durations, 0.5),
            "p95_seconds": _percentile(durations, 0.95),
            "budget_stops": budget_stops,
        },
        "workloads": workloads,
        "usage": totals,
        "steps": steps,
        "verifications": verifications,
        "deliveries": deliveries,
        "connection_calls": calls,
        "recent_denials": denials,
        "approvals_pending": _count_waiting_approvals(harness),
        "effects_uncertain": _count_uncertain(harness),
        "traces": {
            "emitted": emitted,
            "exported": sum(1 for t in traces.values() if t["exported"]),
            "errors": sum(1 for t in traces.values() if t["export_error"]),
        },
        "recent_runs": recent_runs,
        "destinations": destinations(harness),
        "metrics": metric_values(),
    }
