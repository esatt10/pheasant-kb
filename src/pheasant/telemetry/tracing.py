"""OTLP transport and lifecycle, separate from the local interaction ledger.

Azure's managed agent supplies standard OTEL_* variables and accepts gRPC.
Explicit Pheasant endpoints retain their existing HTTP/protobuf semantics.
No endpoint means no SDK, exporter, thread or network activity.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any

logger = logging.getLogger(__name__)


def parse_headers(raw: str | None) -> dict[str, str]:
    return {
        key.strip(): value.strip()
        for part in (raw or "").split(",")
        if "=" in part
        for key, _, value in [part.partition("=")]
        if key.strip()
    }


def destination(settings: Any, signal: str = "traces") -> tuple[str, str, dict[str, str]]:
    """Resolve explicit configuration and standard per-signal/base OTLP variables."""
    explicit = getattr(settings, "otlp_endpoint", None)
    prefix = f"OTEL_EXPORTER_OTLP_{signal.upper()}"
    specific = os.environ.get(f"{prefix}_ENDPOINT")
    base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    endpoint = explicit or specific or base
    if not endpoint:
        return "", "", {}
    protocol = (
        str(getattr(settings, "otlp_protocol", "http/protobuf"))
        if explicit
        else os.environ.get(f"{prefix}_PROTOCOL")
        or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
        or "http/protobuf"
    )
    if protocol not in {"grpc", "http/protobuf"}:
        raise ValueError("otlp_protocol must be grpc or http/protobuf")
    endpoint = str(endpoint)
    if protocol == "http/protobuf":
        if explicit and signal != "traces" and endpoint.rstrip("/").endswith("/v1/traces"):
            endpoint = endpoint.rstrip("/")[: -len("traces")] + signal
        elif not explicit and not specific and base:
            endpoint = endpoint.rstrip("/") + f"/v1/{signal}"
    headers = parse_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS"))
    headers.update(parse_headers(os.environ.get(f"{prefix}_HEADERS")))
    headers.update(parse_headers(os.environ.get(getattr(settings, "otlp_headers_env", "") or "")))
    return endpoint, protocol, headers


def exporter(settings: Any, signal: str) -> Any:
    endpoint, protocol, headers = destination(settings, signal)
    if protocol == "grpc":
        if signal == "traces":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            cls = OTLPSpanExporter
        else:
            from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

            cls = OTLPLogExporter
    elif signal == "traces":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        cls = OTLPSpanExporter
    else:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

        cls = OTLPLogExporter
    return cls(endpoint=endpoint, headers=headers or None, timeout=5)


class Tracing:
    def __init__(self) -> None:
        self.tracer: Any = None
        self.provider: Any = None
        self.processor: Any = None
        self.enabled = False
        self.key: Any = None
        self.logs: Any = None
        self.ratio = 1.0

    def configure(self, settings: Any) -> bool:
        endpoint, protocol, headers = destination(settings)
        ratio = getattr(settings, "sample_ratio", 1.0)
        ratio = 1.0 if ratio is None else float(ratio)
        if not 0 <= ratio <= 1:
            raise ValueError("sample_ratio must be between 0 and 1")
        key = (
            endpoint,
            protocol,
            tuple(sorted(headers.items())),
            destination(settings, "logs"),
            ratio,
            getattr(settings, "service_name", "pheasant"),
            os.environ.get("OTEL_SERVICE_NAME"),
            os.environ.get("PHEASANT_ROLE"),
            os.environ.get("OTEL_RESOURCE_ATTRIBUTES"),
        )
        if self.enabled and key == self.key:
            return True
        self.shutdown()
        if not endpoint:
            return False
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        role = os.environ.get("PHEASANT_ROLE")
        resource = Resource.create(
            {
                "service.name": os.environ.get("OTEL_SERVICE_NAME")
                or str(getattr(settings, "service_name", "pheasant")),
                "service.instance.id": socket.gethostname(),
                **({"pheasant.role": role} if role else {}),
            }
        )
        self.provider = TracerProvider(
            resource=resource, sampler=ParentBased(TraceIdRatioBased(ratio))
        )
        self.processor = BatchSpanProcessor(
            exporter(settings, "traces"), max_queue_size=2048, export_timeout_millis=5000
        )
        self.provider.add_span_processor(self.processor)
        self.tracer = self.provider.get_tracer("pheasant")
        self.ratio, self.key, self.enabled = ratio, key, True
        from pheasant.telemetry.log_export import LogExport

        self.logs = LogExport(settings, resource)
        logger.info("OTLP telemetry enabled protocol=%s role=%s", protocol, role or "process")
        return True

    def parent_context(self, context: Any) -> Any:
        if not context.parent_span_id or not context.trace_id:
            return None
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        flags = "01" if getattr(context, "trace_flags", 1) & 1 else "00"
        return TraceContextTextMapPropagator().extract(
            {"traceparent": f"00-{context.trace_id}-{context.parent_span_id}-{flags}"}
        )

    def current_ids(self) -> tuple[str, str] | None:
        try:
            from opentelemetry import trace

            context = trace.get_current_span().get_span_context()
            if context.is_valid:
                return format(context.trace_id, "032x"), format(context.span_id, "016x")
        except Exception:
            pass
        return None

    def export_span(self, span: dict[str, Any], *, ui: bool = False) -> None:
        """Export recorded IDs unchanged: stdout, queue, ledger and Azure must join."""
        if not self.enabled:
            return
        from datetime import datetime

        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
        from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

        trace_id, span_id = int(span["trace_id"], 16), int(span["span_id"], 16)
        flags = int(span.get("trace_flags", 1))
        if not flags & 1:
            return
        if (
            not span.get("parent_span_id")
            and not TraceIdRatioBased(self.ratio)
            .should_sample(None, trace_id, span["name"])
            .decision.is_sampled()
        ):
            return
        parent = span.get("parent_span_id")
        parent_context = (
            SpanContext(trace_id, int(parent, 16), True, TraceFlags(flags)) if parent else None
        )
        resource = self.provider.resource
        if ui:
            resource = resource.merge(
                Resource({"service.name": "pheasant-ui", "pheasant.role": "ui"})
            )

        def ns(value: str) -> int:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1e9)

        status = Status(
            StatusCode.ERROR if span.get("status", {}).get("code") == "ERROR" else StatusCode.OK
        )
        recorded = ReadableSpan(
            name=span["name"],
            context=SpanContext(trace_id, span_id, False, TraceFlags(flags)),
            parent=parent_context,
            kind=SpanKind.INTERNAL,
            resource=resource,
            attributes=span.get("attributes", {}),
            start_time=ns(span["start_time"]),
            end_time=ns(span["end_time"]),
            status=status,
        )
        self.processor.on_end(recorded)

    def flush(self, timeout_millis: int = 2000) -> None:
        if self.provider is not None:
            self.provider.force_flush(timeout_millis=timeout_millis)
        if self.logs is not None:
            self.logs.flush(timeout_millis)

    def shutdown(self) -> None:
        self.enabled, self.key, self.tracer = False, None, None
        if self.logs is not None:
            self.logs.shutdown()
            self.logs = None
        if self.provider is not None:
            self.provider.shutdown()
        self.provider, self.processor = None, None
