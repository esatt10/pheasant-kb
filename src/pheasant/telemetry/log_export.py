"""Bounded OTLP application logs. Console collection remains the platform's job."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any


def safe_message(message: str) -> str:
    message = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redacted]", message)
    message = re.sub(
        r"(?i)([?&](?:token|key|code|sig|password|api_key|access_token)=)[^&\s]+",
        r"\1[redacted]",
        message,
    )
    message = re.sub(
        r"(?i)((?:https?|postgres(?:ql)?|nats)://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", message
    )
    message = re.sub(
        r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{24,})", "[redacted]", message
    )
    return message[:4096]


class LogExport(logging.Handler):
    def __init__(self, settings: Any, resource: Any) -> None:
        super().__init__(level=logging.INFO)
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        from pheasant.telemetry.tracing import exporter

        self.provider = LoggerProvider(resource=resource)
        self.provider.add_log_record_processor(
            BatchLogRecordProcessor(
                exporter(settings, "logs"), max_queue_size=2048, export_timeout_millis=5000
            )
        )
        self.handler = LoggingHandler(logger_provider=self.provider)
        logging.getLogger().addHandler(self)

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("pheasant"):
            return
        try:
            # Copy only standard fields: arbitrary logging extras can contain credentials.
            clean = logging.LogRecord(
                record.name,
                record.levelno,
                Path(record.pathname).name,
                record.lineno,
                safe_message(record.getMessage()),
                (),
                None,
                record.funcName,
            )
            if record.exc_info and record.exc_info[0]:
                clean.msg += " [" + record.exc_info[0].__name__ + "]"
            self.handler.emit(clean)
        except Exception:
            # Never log an export failure through this same handler.
            pass

    def flush(self) -> None:
        """``logging``'s flush: never wait for an export here.

        ``logging.shutdown`` calls it -- and ``logging.config.dictConfig``
        calls that while it holds ``logging``'s module lock (uvicorn does at
        start). Waiting for the exporter there deadlocks: the export thread's
        HTTP client checks a log level, which needs that same lock. Every
        region with an OTLP endpoint hung before serving its first request.
        Records are delivered by the processor's own thread, and
        :meth:`drain` (``Tracing.flush`` / ``shutdown``) waits for them.
        """

    def drain(self, timeout_millis: int = 2000) -> None:
        if getattr(self, "provider", None) is not None:
            self.provider.force_flush(timeout_millis=timeout_millis)

    def shutdown(self) -> None:
        logging.getLogger().removeHandler(self)
        self.provider.shutdown()
