"""The assistant's HTTP routes: grounded chat, its streaming twin, visuals and media.

One router per plane, which is what `api/app.py`'s size ceiling asks for
(the readiness and ingestion routes moved out the same way). Every route is an
adapter over `services.assistant` or `services.media` — the operations the MCP
tools call — so what is left here is HTTP's own: the session key a browser
pasted, server-sent events, and response headers.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel

from pheasant.api.artifact_routes import channel as artifact_channel
from pheasant.assistant.workflows import step_payload
from pheasant.connections import environment as connection_environment
from pheasant.services import answer_resume
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
    # Saved artifacts as evidence: "include" (default), "exclude", "only".
    artifacts: str | bool | None = None
    # Earlier turns of this conversation, oldest first: [{question, answer}].
    # The region keeps no chat state; continuity is what the caller sends.
    history: list[dict[str, Any]] = []
    # "short" / "medium" / "long", or "auto" (default) to read it off the
    # question. See assistant.routing.
    depth: str | None = None
    # "none" / "diagram" / "image", or "auto" to read it off the question.
    visual: str | None = None
    # One file, or everything under one folder, of `source_name` — a path as
    # `GET /documents/tree` lists it. Every retrieval the answer runs stays
    # inside it (the web UI's "search only here" in the taxonomy view).
    path_prefix: str | None = None
    # Documents to leave out (names, paths or ids), and what the caller has
    # open — which `@current` and `@dir` in the question are read against.
    exclude_documents: list[str] | None = None
    current_document: str | None = None
    current_directory: str | None = None
    # The answer shape, pinned: "knowledge", "procedural" or "compose" (a new
    # artifact assembled from what the sources document). `@compose` leading
    # the question says the same. Unset or "auto" reads it off the question.
    intent: str | None = None
    # Several sources at once — the sources rail's multi-select. One name
    # means what `source_name` means.
    source_names: list[str] | None = None
    # The stream only: a caller-chosen id under which the answer is kept for
    # an hour, so a reader whose connection dropped can collect it from
    # `GET /assistant/chat/answers/{answer_id}` (`services/answer_resume.py`).
    answer_id: str | None = None
    # Keep a composed answer as an artifact: true / false, or unset to follow
    # the region's auto_save. `conversation_id` files it under its thread.
    save: bool | None = None
    conversation_id: str | None = None

    def service_request(self, channel: str = "api") -> assistant_service.AnswerRequest:
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
            artifacts=self.artifacts,
            history=list(self.history or []),
            depth=self.depth,
            visual=self.visual,
            path_prefix=self.path_prefix,
            exclude_documents=self.exclude_documents,
            current_document=self.current_document,
            current_directory=self.current_directory,
            intent=self.intent,
            source_names=self.source_names,
            save=self.save,
            conversation_id=self.conversation_id,
            channel=channel,
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


class AssessRequest(BaseModel):
    """Judge `passages` (and optionally `answer`) for `question` — your own evidence."""

    question: str
    passages: list[Any]
    intent: str | None = None
    answer: str | None = None
    relevance: bool = True


class VoiceSessionRequest(BaseModel):
    """Start one dictation. ``session_id`` is a pasted key's opaque handle."""

    session_id: str | None = None


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
        answer_request = req.service_request(artifact_channel(request))
        assistant_service.admit(services, answer_request)
        permit = assistant_service.acquire(services)
        budget = assistant_service.create_request_budget(services, answer_request)
        credential = session_keys().get(req.session_id)
        environ = connection_environment()

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
        answer_request = req.service_request(artifact_channel(request))
        answer_resume.check_answer_id(req.answer_id)
        assistant_service.admit(services, answer_request)
        permit = assistant_service.acquire(services)
        budget = assistant_service.create_request_budget(services, answer_request)

        loop = asyncio.get_running_loop()
        events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        recorder: answer_resume.AnswerRecorder | None = None

        def emit(event: dict[str, Any] | None) -> None:
            # Kept for a reader who reconnects, on this worker thread rather
            # than the event loop; drafts are not (see `answer_resume`).
            if recorder is not None:
                _record(recorder, event)
            if event is not None and event.get("type") == "visual":
                # The whole answer rode along for the recorder; the stream's
                # reader already has it and gets the visual alone, as before.
                event = {key: value for key, value in event.items() if key != "answer"}
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
        environ = connection_environment()
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
            nonlocal recorder
            try:
                recorder = answer_resume.start(services, req.answer_id, _principal(request))
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
                    on_step=lambda step: emit({"type": "step", **step_payload(step)}),
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
                    emit(
                        {
                            "type": "visual",
                            "visual": visual,
                            "steps": answer["steps"],
                            "answer": {**answer, "visual": visual},
                        }
                    )
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

    @app.get("/assistant/chat/answers/{answer_id}")
    def assistant_chat_answer(answer_id: str, request: Request) -> dict:
        """A streamed answer collected after its connection dropped.

        Transport adapter over `services.answer_resume.read`, and HTTP only:
        it is the stream's other half, and MCP has no stream to lose.
        """

        return answer_resume.read(services, answer_id, _principal(request))

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
            env=connection_environment(),
        )

    @app.post("/assistant/assess")
    def assistant_assess(req: AssessRequest) -> dict:
        """Transport adapter. The operation is `services.assistant.assess_evidence`."""

        return assistant_service.assess_evidence(
            services,
            assistant_service.AssessRequest(
                question=req.question,
                passages=req.passages,
                intent=req.intent,
                answer=req.answer,
                relevance=req.relevance,
            ),
            env=connection_environment(),
        )

    @app.get("/assistant/voice")
    def assistant_voice(session_id: str | None = None) -> dict:
        """Whether the composer may offer dictation. The operation is
        `services.voice.voice_status`.

        HTTP only, on purpose: dictation is for a person's microphone, and an
        agent sends its question as text through ``ask_knowledge_base``.
        """

        from pheasant.services import voice as voice_service

        return voice_service.voice_status(
            services, credential=session_keys().get(session_id), env=connection_environment()
        )

    @app.post("/assistant/voice/session")
    def assistant_voice_session(req: VoiceSessionRequest) -> Response:
        """A short-lived secret for one live transcription session.

        The operation is `services.voice.start_voice_session`; HTTP only, like
        ``GET /assistant/voice``. The browser streams its microphone straight
        to the transcriber with this secret — the region's key never leaves
        the process and no audio passes through it. ``no-store``: a secret
        that a cache kept would outlive the dictation it was minted for.
        """

        import json as json_module

        from pheasant.services import voice as voice_service

        minted = voice_service.start_voice_session(
            services, credential=session_keys().get(req.session_id), env=connection_environment()
        )
        return Response(
            content=json_module.dumps(minted),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    from pheasant.services.voice_relay import RelayGate

    relay_gate = RelayGate()

    # Both spellings of the path, as the UI's API base can be "" or "/api": the
    # middleware that accepts `/api/*` for HTTP (`app.py`, `_ui_api_prefix`)
    # does not run for WebSockets, so the alias is registered here instead.
    @app.websocket("/api/assistant/voice/relay")
    @app.websocket("/assistant/voice/relay")
    async def assistant_voice_relay(websocket: WebSocket) -> None:
        """One dictation relayed to the transcriber (``assistant.voice.transport: relay``).

        The operations are `services.voice.open_relay` and
        `services.voice_relay.run`; this adapter owns only Starlette's socket.
        A browser cannot send a bearer token on a WebSocket, and the HTTP
        middleware that checks one does not run for WebSockets, so the
        credential here is the one-use ticket ``POST /assistant/voice/session``
        handed to an authenticated caller, offered as a subprotocol. Every
        failure before the relay starts is the same refusal (1008), so a caller
        cannot learn whether a ticket was unknown, spent or expired.
        """

        from starlette.concurrency import run_in_threadpool
        from starlette.websockets import WebSocketDisconnect

        from pheasant.services import voice as voice_service
        from pheasant.services import voice_relay

        ticket = voice_service.relay_ticket(websocket.headers.get("sec-websocket-protocol", ""))
        target = await run_in_threadpool(
            voice_service.open_relay, services, ticket, env=connection_environment()
        )
        if target is None:
            voice_relay.count("refused")
            await websocket.close(code=voice_relay.CLOSE_POLICY)
            return
        settings = services.config.assistant.voice
        if not relay_gate.acquire(int(settings.max_relay_sessions)):
            voice_relay.count("busy")
            await websocket.close(code=voice_relay.CLOSE_TRY_AGAIN)
            return

        async def receive() -> str | bytes | None:
            try:
                message = await websocket.receive()
            except (WebSocketDisconnect, RuntimeError):
                return None
            if message.get("type") == "websocket.disconnect":
                return None
            if message.get("text") is not None:
                return str(message["text"])
            if message.get("bytes") is not None:
                return bytes(message["bytes"])
            return ""

        async def send(frame: str | bytes) -> None:
            if isinstance(frame, bytes):
                await websocket.send_bytes(frame)
            else:
                await websocket.send_text(frame)

        async def close(code: int, reason: str) -> None:
            await websocket.close(code=code, reason=reason or None)

        try:
            await voice_relay.run(
                voice_relay.BrowserSide(
                    accept=lambda: websocket.accept(subprotocol="realtime"),
                    receive=receive,
                    send=send,
                    close=close,
                ),
                target,
            )
        finally:
            relay_gate.release()

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


def _principal(request: Request) -> str | None:
    """Who asked, as the auth middleware established it; ``None`` on an open region."""

    return getattr(getattr(request.state, "identity", None), "principal", None)


def _record(recorder: answer_resume.AnswerRecorder, event: dict[str, Any] | None) -> None:
    """One stream event into the kept answer."""

    if event is None:
        recorder.finish()
    elif event["type"] == "step":
        recorder.step({key: value for key, value in event.items() if key != "type"})
    elif event["type"] == "answer":
        recorder.answered(event["answer"])
    elif event["type"] == "visual":
        recorder.visual(event["answer"])
    elif event["type"] == "error":
        recorder.failed(str(event.get("error")))
