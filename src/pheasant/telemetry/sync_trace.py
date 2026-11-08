"""Every sync run as an OpenTelemetry-shaped trace: a run span and one span per stage.

A sync is minutes to hours of work on an indexer nobody watches, and the only
record it left was a ``sync_events`` row and whatever its child process
logged. This makes each run a trace in the shape the OpenTelemetry trace data
model gives a span -- ``trace_id``, ``span_id``, ``parent_span_id``, ``name``,
``kind``, start and end time, ``status``, ``resource`` and ``attributes`` --
so a run can be followed in whatever collects the region's logs:

* **One JSON line per span on stdout** (``"type": "otel.span"``), written
  directly rather than through the console log formatter, which wraps long
  lines. On Azure Container Apps that is Log Analytics:
  ``ContainerAppConsoleLogs_CL | where Log_s startswith '{"type":"otel.span"'
  | extend s = parse_json(Log_s)``.
* **The same trace over OTLP** when ``observability.otlp_endpoint`` is set and
  the ``[otel]`` extra is installed: same trace id, so the two join.

The run joins the trace of whatever queued it (`telemetry.interactions`:
``POST /sync`` carries a ``traceparent`` into the task), so "who asked for
this sync, and what did it do" is one trace; a scheduled run starts its own.
Its id is on the sync event as ``trace_id`` and on the Syncs page.

A sync run in a child process (`sync/worker.py`) cannot reach the container's
log -- its stdout is the report -- so it hands its spans back in the report
(``details.otel_spans``) and the indexer writes them. ``PHEASANT_SYNC_CHILD``
tells the two apart.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: Set in a sync child's environment: spans go back in the report, not to stdout.
CHILD_ENV = "PHEASANT_SYNC_CHILD"
SPAN_TYPE = "otel.span"

_CURRENT: contextvars.ContextVar[SyncTrace | None] = contextvars.ContextVar(
    "pheasant_sync_trace", default=None
)
_WRITE_LOCK = threading.Lock()


class SyncTrace:
    """One sync run: its trace, its run span, and a span per stage it entered."""

    def __init__(self, source_name: str, mode: str, *, role: str | None = None) -> None:
        from pheasant.telemetry.interactions import current_trace

        ambient = current_trace()
        self.trace_id = ambient[0] if ambient else secrets.token_hex(16)
        self.parent_span_id = ambient[1] if ambient else None
        self.span_id = secrets.token_hex(8)
        self.source_name = source_name
        self.mode = mode
        self.role = role or os.environ.get("PHEASANT_ROLE") or None
        self.start = time.time()
        self._lock = threading.Lock()
        self._phase: str | None = None
        self._phase_start = self.start
        self._stages: list[tuple[str, float, float]] = []
        self._token: contextvars.Token | None = None

    def __enter__(self) -> SyncTrace:
        self._token = _CURRENT.set(self)
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._token is not None:
            _CURRENT.reset(self._token)
            self._token = None

    def phase(self, name: str) -> None:
        """The run entered stage ``name``; the stage before it ends now."""

        with self._lock:
            if not name or name == self._phase:
                return
            now = time.time()
            if self._phase is not None:
                self._stages.append((self._phase, self._phase_start, now))
            self._phase, self._phase_start = name, now

    def stage_seconds(self) -> list[list[Any]]:
        """``[stage, seconds]`` in the order entered, the current one up to now.

        A list, not a mapping: the sync event is stored as JSON with sorted
        keys, which would put a run's stages in alphabetical order.
        """

        now = time.time()
        with self._lock:
            stages = [*self._stages]
            if self._phase is not None:
                stages.append((self._phase, self._phase_start, now))
        seconds: dict[str, float] = {}
        for name, start, stop in stages:
            seconds[name] = round(seconds.get(name, 0.0) + (stop - start), 3)
        return [[name, value] for name, value in seconds.items()]

    def hook(self, forward: Any) -> Any:
        """A progress hook that records stage changes, then calls ``forward`` (if any)."""

        from pheasant.sync.engine import _hook_accepts_meta

        wants_meta = forward is not None and _hook_accepts_meta(forward)

        def traced(
            phase: str, current: int = 0, total: Any = None, detail: str = "", meta: Any = None
        ) -> None:
            self.phase(phase)
            if forward is None:
                return
            if wants_meta:
                forward(phase, current, total, detail, meta or {})
            else:
                forward(phase, current, total, detail)

        return traced

    def finish(
        self, status: str, attributes: dict[str, Any], error: str | None = None
    ) -> list[dict[str, Any]]:
        """Close the run and return its spans (run first, then stages in order)."""

        end = time.time()
        with self._lock:
            if self._phase is not None:
                self._stages.append((self._phase, self._phase_start, end))
                self._phase = None
            stages = list(self._stages)
        failed = status in ("failed", "timeout", "limit_exceeded") or error is not None
        run = _span(
            name="pheasant.sync",
            trace_id=self.trace_id,
            span_id=self.span_id,
            parent=self.parent_span_id,
            start=self.start,
            end=end,
            ok=not failed,
            message=error,
            role=self.role,
            attributes={
                "pheasant.source": self.source_name,
                "pheasant.sync.mode": self.mode,
                "pheasant.sync.status": status,
                **{f"pheasant.sync.{k}": v for k, v in attributes.items() if v is not None},
            },
        )
        spans = [run]
        for name, start, stop in stages:
            spans.append(
                _span(
                    name=f"pheasant.sync.{name}",
                    trace_id=self.trace_id,
                    span_id=secrets.token_hex(8),
                    parent=self.span_id,
                    start=start,
                    end=stop,
                    ok=True,
                    message=None,
                    role=self.role,
                    attributes={"pheasant.source": self.source_name, "pheasant.sync.stage": name},
                )
            )
        return spans


def traced(source_name: str, mode: str, on_progress: Any, run: Any) -> Any:
    """``run(hook)`` -- one sync -- inside its own trace, unless already inside one.

    ``hook`` is ``on_progress`` wrapped to record each stage. The result gains
    ``trace_id``; its spans are written here, or handed back in the report
    from a sync child (`sync/worker.py` writes them).
    """

    if current() is not None:  # a pass inside a run (a resumed full sync): one trace
        return run(on_progress)
    trace = SyncTrace(source_name, mode)
    with trace:
        try:
            result = run(trace.hook(on_progress))
        except Exception as exc:
            emit(trace.finish("failed", {}, error=str(exc)[:500]))
            raise
    details = result.details
    spans = trace.finish(
        result.status,
        {
            "fetched": details.get("fetched"),
            "indexed": result.indexed_artifacts,
            "unchanged": result.skipped_artifacts,
            "removed": details.get("removed_artifacts"),
            "unreadable": details.get("unreadable_items"),
            "readable_percent": details.get("readable_percent"),
        },
    )
    details["trace_id"] = trace.trace_id
    if os.environ.get(CHILD_ENV):
        details["otel_spans"] = spans
    else:
        emit(spans)
    return result


def current() -> SyncTrace | None:
    """The sync trace this thread is inside, if any."""

    return _CURRENT.get()


def emit(spans: list[dict[str, Any]]) -> None:
    """Write ``spans`` where this process's logs go, and export them if OTLP is on.

    In a sync child this does nothing: the spans travel back in its report
    and the indexer that ran it calls this.
    """

    if not spans or os.environ.get(CHILD_ENV):
        return
    lines = "".join(json.dumps(span, separators=(",", ":"), default=str) + "\n" for span in spans)
    with _WRITE_LOCK:
        try:
            sys.stdout.write(lines)
            sys.stdout.flush()
        except (OSError, ValueError):  # a closed stdout must never fail a sync
            pass
    _export(spans)


def _span(
    *,
    name: str,
    trace_id: str,
    span_id: str,
    parent: str | None,
    start: float,
    end: float,
    ok: bool,
    message: str | None,
    role: str | None,
    attributes: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": SPAN_TYPE,
        "name": name,
        "kind": "INTERNAL",
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent,
        "start_time": _iso(start),
        "end_time": _iso(end),
        "duration_ms": round((end - start) * 1000, 1),
        "status": {"code": "OK" if ok else "ERROR", **({"message": message} if message else {})},
        "resource": {
            "service.name": "pheasant",
            "service.instance.id": socket.gethostname(),
            **({"pheasant.role": role} if role else {}),
        },
        "attributes": attributes,
    }


def _export(spans: list[dict[str, Any]]) -> None:
    """The same spans over OTLP, when a tracer provider is configured (best effort)."""

    try:
        from opentelemetry import trace as ot_trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            Status,
            StatusCode,
            TraceFlags,
        )
    except ImportError:
        return
    provider = ot_trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        return
    tracer = provider.get_tracer("pheasant.sync")
    try:
        run = spans[0]
        parent_id = run.get("parent_span_id")
        context = None
        if parent_id:
            parent = SpanContext(
                int(run["trace_id"], 16), int(parent_id, 16), True, TraceFlags(TraceFlags.SAMPLED)
            )
            context = ot_trace.set_span_in_context(NonRecordingSpan(parent))
        root = tracer.start_span(
            run["name"],
            context=context,
            start_time=_ns(run["start_time"]),
            attributes=run["attributes"],
        )
        if run["status"]["code"] == "ERROR":
            root.set_status(Status(StatusCode.ERROR, run["status"].get("message")))
        stage_context = ot_trace.set_span_in_context(root)
        for stage in spans[1:]:
            span = tracer.start_span(
                stage["name"],
                context=stage_context,
                start_time=_ns(stage["start_time"]),
                attributes=stage["attributes"],
            )
            span.end(end_time=_ns(stage["end_time"]))
        root.end(end_time=_ns(run["end_time"]))
    except Exception:  # noqa: BLE001 - export is a mirror, never a failure
        logger.debug("OTLP export of a sync trace failed", exc_info=True)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ns(iso: str) -> int:
    return int(
        datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).timestamp() * 1e9
    )
