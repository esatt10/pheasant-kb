"""The assistant's HTTP routes: grounded chat, its streaming twin, visuals and media.

One router per plane, which is what `api/app.py`'s size ceiling asks for
(the readiness and ingestion routes moved out the same way). Every route is an
adapter over `services.assistant` or `services.media` — the operations the MCP
tools call — so what is left here is HTTP's own: the session key a browser
pasted, server-sent events, and response headers.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import BaseModel

from pheasant.services import assistant as assistant_service

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    question: str
    # Opaque handle for a key the user pasted this session. Never the key.
    session_id: str | None = None
    mode: str = "hybrid"
    max_results: int | None = None
    source_name: str | None = None
    # Scope the answer to (or away from) kinds of source. Same axis as
    # `POST /search`'s, applied to every retrieval the answering loop runs.
    source_types: list[str] | None = None
    exclude_source_types: list[str] | None = None
    principal: str | None = None
    principal_groups: list[str] = []
    # Override assistant.workflow for this one question ("simple",
    # "agentic", or any registered plugin name).
    workflow: str | None = None
    # Per-request workflow knobs, merged over assistant.workflow_options.
    options: dict | None = None
    # How this region's agent memory takes part in the answer: "auto", "off",
    # "only", "prefer", or the full policy object (Step 33.10).
    memory: dict | str | None = None
    # Earlier turns of this conversation, oldest first: [{question, answer}].
    # The region keeps no chat state; continuity is what the caller sends.
    history: list[dict[str, Any]] = []
    # "short" / "medium" / "long", or "auto" (default) to read it off the
    # question. See assistant.routing.
    depth: str | None = None
    # "none" / "diagram" / "image", or "auto" to read it off the question.
    visual: str | None = None

    def service_request(self) -> assistant_service.AnswerRequest:
        return assistant_service.AnswerRequest(
            question=self.question,
            mode=self.mode,
            max_results=self.max_results,
            source_name=self.source_name,
            source_types=self.source_types,
            exclude_source_types=self.exclude_source_types,
            principal=self.principal,
            principal_groups=self.principal_groups,
            workflow=self.workflow,
            options=self.options,
            memory=self.memory,
            history=list(self.history or []),
            depth=self.depth,
            visual=self.visual,
        )


class VisualRequest(BaseModel):
    """Draw `request` from named passages (`node_ids`) or from a search for it."""

    request: str
    node_ids: list[str] = []
    kind: str | None = None
    session_id: str | None = None
    source_name: str | None = None
    principal: str | None = None
    principal_groups: list[str] = []


def register_assistant_routes(
    app: FastAPI,
    *,
    services: Any,
    session_keys: Callable[[], Any],
    record_stream_answer: Callable[..., None],
) -> None:
    """Register the chat, visual and media routes on ``app``.

    ``session_keys`` is a getter because tests swap ``app.state.session_keys``
    after the app is built; ``record_stream_answer`` observes a streamed
    answer as a child of its request, which needs the app's interaction buffer.
    """

    from pheasant.api.app import observed, record_retrieval

    @app.post("/assistant/chat")
    async def assistant_chat(req: ChatRequest, request: Request) -> dict:
        """Transport adapter. The operation is `services.assistant.answer`.

        What stays here is HTTP's: the session key a browser pasted, which MCP
        has no session to hold.
        """

        import asyncio
        from contextvars import copy_context

        handler_started = time.perf_counter()
        answer_request = req.service_request()
        assistant_service.admit(services, answer_request)
        permit = assistant_service.acquire(services)
        budget = assistant_service.create_request_budget(services, answer_request)
        credential = session_keys().get(req.session_id)
        environ = dict(os.environ)

        def run_answer() -> dict[str, Any]:
            worker_started = time.perf_counter()
            result = assistant_service.answer(
                services,
                answer_request,
                credential=credential,
                env=environ,
                permit=permit,
                request_budget=budget,
            )
            result["executor_wait_ms"] = (worker_started - handler_started) * 1000
            return result

        worker_context = copy_context()
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(None, worker_context.run, run_answer)
        except Exception:
            permit.release()
            raise
        # A disconnected client must not release admission while a provider or
        # database call is still running. The shield lets the worker finish;
        # this callback releases on completion, including worker exceptions.
        future.add_done_callback(lambda _future: permit.release())
        answer = await asyncio.shield(future)
        received = getattr(request.state, "server_received_monotonic", None)
        answer["server_queue_wait_ms"] = (
            max(0.0, (handler_started - received) * 1000) if received is not None else None
        )
        answer["server_observed_ms"] = (
            max(0.0, (time.perf_counter() - received) * 1000) if received is not None else None
        )
        # A chat turn is the richest evidence the ledger gets: the question, the
        # passages that answered it, and the answer itself. `citations` is what
        # `extract_results` reads for ids and paths.
        record_retrieval(
            request,
            query=req.question,
            payload=answer,
            criteria={"mode": req.mode, "workflow": req.workflow} if req.mode else None,
            answer=str(answer.get("answer") or "") or None,
        )
        return answer

    @app.post("/assistant/chat/stream")
    async def assistant_chat_stream(req: ChatRequest, request: Request):
        """The same answer as ``/assistant/chat``, with progress as it happens.

        Server-sent events: a ``step`` when each workflow stage finishes,
        optional provisional ``draft`` text during generation, then a single
        verified ``answer`` (or ``error``). The agent loop can take a while, and a
        client that shows "planning… retrieved 35 passages… grading" is
        waiting rather than wondering. The answer runs in a worker thread;
        completed LangGraph steps wake the SSE coroutine on the event loop.
        The async generator holds no anyio thread-pool token while waiting,
        and there is no polling delay before the final answer event.
        """

        import asyncio
        import json as json_module
        import threading
        from contextvars import copy_context

        from starlette.responses import StreamingResponse

        # Refuse with a status code while one can still be sent; once the
        # stream opens, a refusal can only be an event.
        answer_request = req.service_request()
        assistant_service.admit(services, answer_request)
        permit = assistant_service.acquire(services)
        budget = assistant_service.create_request_budget(services, answer_request)

        loop = asyncio.get_running_loop()
        events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        def emit(event: dict[str, Any] | None) -> None:
            # LangGraph nodes and the provider run in a worker thread. Wake
            # the SSE coroutine immediately when a node or answer completes.
            try:
                loop.call_soon_threadsafe(events.put_nowait, event)
            except RuntimeError:
                # The client can disconnect while a provider call is still
                # finishing. Admission remains held by the worker, but there
                # is no live event loop left to receive its progress.
                pass

        credential = session_keys().get(req.session_id)
        environ = dict(os.environ)
        # Captured here, in the request, because `run()` executes on a worker
        # thread after this route has already returned its response object.
        parent_event = observed(request)
        # Monotonic, like every other duration here: a wall-clock delta across a
        # generation that can take a minute is exactly where an NTP step shows up.
        answer_started = time.perf_counter()
        received = getattr(request.state, "server_received_monotonic", None)
        server_queue_wait_ms = (
            max(0.0, (time.perf_counter() - received) * 1000) if received is not None else None
        )

        def run() -> None:
            try:
                worker_started = time.perf_counter()
                answer = assistant_service.answer(
                    services,
                    answer_request,
                    credential=credential,
                    env=environ,
                    # The text is sent the moment it exists and the visual —
                    # one more model call — follows as its own event.
                    defer_visual=True,
                    permit=permit,
                    request_budget=budget,
                    on_step=lambda step: emit(
                        {
                            "type": "step",
                            "name": step.name,
                            "detail": step.detail,
                            "passages": step.passages,
                            "duration_seconds": step.duration_seconds,
                            "input_tokens": step.input_tokens,
                            "output_tokens": step.output_tokens,
                            "cached_input_tokens": step.cached_input_tokens,
                            "reasoning_tokens": step.reasoning_tokens,
                            "provider_calls": step.provider_calls,
                            "provider_retries": step.provider_retries,
                            "fanout_timings": step.fanout_timings,
                        }
                    ),
                    on_draft=lambda delta: emit({"type": "draft", "delta": delta}),
                )
                answer["executor_wait_ms"] = (worker_started - answer_started) * 1000
                answer["server_queue_wait_ms"] = server_queue_wait_ms
                answer["server_observed_ms"] = (
                    (time.perf_counter() - received) * 1000 if received else None
                )
                record_stream_answer(parent_event, req, answer, answer_started)
                emit({"type": "answer", "answer": answer})
                if (answer.get("visual") or {}).get("status") == "pending":
                    visual = assistant_service.render_visual(
                        services, answer, credential=credential, env=environ
                    )
                    emit({"type": "visual", "visual": visual, "steps": answer["steps"]})
            except Exception as exc:  # surfaced to the client, never a 500 mid-stream
                logger.exception("streaming chat failed")
                emit({"type": "error", "error": str(exc)})
            finally:
                permit.release()
                emit(None)

        worker_context = copy_context()
        worker = threading.Thread(
            target=worker_context.run,
            args=(run,),
            name="pheasant-chat-stream",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            permit.release()
            raise

        async def publish():
            while True:
                item = await events.get()
                if item is None:
                    return
                yield f"data: {json_module.dumps(item)}\n\n"

        return StreamingResponse(
            publish(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # nginx sits in front of this in the compose stack and would
                # otherwise buffer the whole stream into one write.
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/assistant/visual")
    def assistant_visual(req: VisualRequest) -> dict:
        """Transport adapter. The operation is `services.assistant.visualize`."""

        return assistant_service.visualize(
            services,
            assistant_service.VisualRequest(
                request=req.request,
                node_ids=list(req.node_ids or []),
                kind=req.kind,
                source_name=req.source_name,
                principal=req.principal,
                principal_groups=req.principal_groups,
            ),
            credential=session_keys().get(req.session_id),
            env=dict(os.environ),
        )

    @app.get("/assistant/apps/knowledge-view")
    def knowledge_view() -> Response:
        """The MCP App view, for the UI to host exactly as an MCP host would.

        One renderer for agents and people: the chat panel loads this into a
        sandboxed iframe and speaks the host half of the MCP Apps protocol.
        Served with a ``sandbox`` CSP so that opening it directly still runs
        it in an opaque origin, never in the API's.
        """

        from pheasant.mcp_server.assistant_tools import app_html

        return Response(
            content=app_html(),
            media_type="text/html",
            headers={
                "Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; "
                "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-cache",
            },
        )

    @app.get("/media")
    def media(node_id: str, principal: str | None = None) -> Response:
        """An indexed image's bytes. The operation is `services.media.get_media`.

        Served inline with ``nosniff`` and a restrictive CSP: the type comes
        from the extension pheasant indexed (raster formats only, never SVG),
        and a browser must not be talked into reading it as anything else.
        Content-addressed, so it is safe to cache for as long as it exists.
        """

        from pheasant.services.media import MediaRequest, get_media

        found = get_media(services, MediaRequest(node_id=node_id, principal=principal))
        return Response(
            content=found["content"],
            media_type=found["mime_type"],
            headers={
                "Content-Disposition": "inline",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Cache-Control": "private, max-age=86400",
                "ETag": f'"{found["sha256"]}"',
            },
        )
