"""Small metadata-only spans at process and dependency boundaries."""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from functools import wraps
from typing import Any


@contextmanager
def span(
    name: str,
    *,
    kind: str = "INTERNAL",
    attributes: dict[str, Any] | None = None,
    traceparent: str | None = None,
    require_parent: bool = False,
):
    from pheasant.telemetry.interactions import (
        TRACING,
        InteractionContext,
        current_trace,
        current_trace_flags,
    )

    if not TRACING.enabled or TRACING.tracer is None:
        yield None
        return
    from opentelemetry.trace import SpanKind, Status, StatusCode

    ambient = current_trace()
    if traceparent is None and ambient:
        traceparent = f"00-{ambient[0]}-{ambient[1]}-{current_trace_flags():02x}"
    if require_parent and not traceparent:
        yield None
        return
    context = TRACING.parent_context(InteractionContext.create(traceparent=traceparent))
    with TRACING.tracer.start_as_current_span(
        name,
        context=context,
        kind=SpanKind[kind],
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as active:
        try:
            yield active
        except BaseException:
            active.set_status(Status(StatusCode.ERROR))
            raise


def instrument(
    name: str,
    *,
    kind: str = "CLIENT",
    attributes: dict[str, Any] | None = None,
    inbound: bool = False,
    require_parent: bool = False,
):
    """Also wrap streaming RPCs for the lifetime of iteration, on their own thread."""

    def decorate(function):
        def context(args, kwargs):
            parent = None
            if inbound:
                rpc = kwargs.get("context") or args[-1]
                parent = dict(rpc.invocation_metadata()).get("traceparent")
            elif name == "pheasant.logger.batch":
                parent = args[-1].payload.get("traceparent")
            return span(
                name,
                kind=kind,
                attributes=attributes,
                traceparent=parent,
                require_parent=require_parent,
            )

        if inspect.isgeneratorfunction(function):

            @wraps(function)
            def streaming(*args, **kwargs):
                with context(args, kwargs):
                    yield from function(*args, **kwargs)

            return streaming

        @wraps(function)
        def wrapped(*args, **kwargs):
            with context(args, kwargs):
                return function(*args, **kwargs)

        return wrapped

    return decorate
