"""A stand-in for OpenAI's Realtime transcription wire, on a real socket.

Two halves, both as the browser and the region meet them:

* ``POST /v1/realtime/client_secrets`` — what ``services.voice`` asks for a
  dictation's short-lived secret. It records each request (headers and body)
  and answers in the GA shape, ``{"value": "ek_…", "expires_at": …}``, or with
  a scripted refusal.
* ``WS /v1/realtime?intent=transcription`` — what the composer opens with that
  secret, offered as the ``openai-insecure-api-key.<secret>`` subprotocol
  beside ``realtime``. An unknown secret is refused. With ``header_keys`` it
  also admits a credential the way Azure AI Foundry does -- only as a header,
  ``api-key: <key>`` or ``Authorization: Bearer <key>`` -- which is how the
  region's relay opens it; a ``session.update`` is recorded and answered with
  ``session.updated``. It counts the PCM each
  ``input_audio_buffer.append`` carries and, as audio keeps arriving, streams
  ``conversation.item.input_audio_transcription.delta`` events one word at a
  time; ``input_audio_buffer.commit`` answers ``committed``, the words not yet
  sent, and ``completed`` with the whole transcript.

Served by uvicorn in a thread, so a test drives real HTTP and a real
WebSocket handshake rather than a mock of either.
"""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from dataclasses import dataclass, field
from typing import Any

#: What every dictation "hears", one delta per word.
TRANSCRIPT = ["What", " is", " the", " sync", " lease?"]


@dataclass
class Session:
    secret: str
    protocols: list[str]
    query: str
    audio_bytes: int = 0
    appends: int = 0
    first_audio_at: float | None = None
    last_audio_at: float | None = None
    committed: bool = False
    closed: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    updates: list[dict[str, Any]] = field(default_factory=list)
    #: Event types received that were neither audio nor a session update.
    other_events: list[str] = field(default_factory=list)


@dataclass
class FakeRealtime:
    #: Appends (≈100 ms each) between streamed words.
    appends_per_word: int = 3
    #: ``(status, message)`` to refuse every mint with, or ``None``.
    refuse: tuple[int, str] | None = None
    minted: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)
    url: str = ""
    #: Credentials admitted as a header (``api-key`` or ``Bearer``), Foundry-style.
    header_keys: set[str] = field(default_factory=set)
    #: Every socket attempt's headers, admitted or not.
    attempts: list[dict[str, str]] = field(default_factory=list)

    def app(self) -> Any:
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route, WebSocketRoute
        from starlette.websockets import WebSocket, WebSocketDisconnect

        async def client_secrets(request: Request) -> JSONResponse:
            body = await request.json()
            self.minted.append({"headers": dict(request.headers), "body": body})
            if self.refuse:
                status, message = self.refuse
                return JSONResponse({"error": {"message": message}}, status_code=status)
            secret = f"ek_test_{len(self.minted)}"
            return JSONResponse(
                {"value": secret, "expires_at": int(time.time()) + 60, "session": body["session"]}
            )

        async def realtime(socket: WebSocket) -> None:
            protocols = [
                p.strip() for p in socket.headers.get("sec-websocket-protocol", "").split(",") if p
            ]
            secrets = [
                p.split(".", 1)[1] for p in protocols if p.startswith("openai-insecure-api-key.")
            ]
            issued = {f"ek_test_{n}" for n in range(1, len(self.minted) + 1)}
            headers = dict(socket.headers)
            self.attempts.append(headers)
            scheme, _, bearer = headers.get("authorization", "").partition(" ")
            header_key = headers.get("api-key") or (bearer if scheme.lower() == "bearer" else "")
            if header_key and header_key in self.header_keys:
                credential = f"header:{header_key}"
            elif "realtime" in protocols and secrets and secrets[0] in issued:
                credential = secrets[0]
            else:
                await socket.close(code=4001)
                return
            session = Session(
                secret=credential, protocols=protocols, query=socket.url.query, headers=headers
            )
            self.sessions.append(session)
            await socket.accept(subprotocol="realtime" if "realtime" in protocols else None)
            await socket.send_json(
                {"type": "session.created", "session": {"type": "transcription"}}
            )
            item = f"item_{len(self.sessions)}"
            sent = 0
            try:
                while True:
                    event = await socket.receive_json()
                    kind = event.get("type")
                    if kind == "session.update":
                        session.updates.append(event)
                        await socket.send_json(
                            {"type": "session.updated", "session": event.get("session", {})}
                        )
                        continue
                    if kind not in {"input_audio_buffer.append", "input_audio_buffer.commit"}:
                        session.other_events.append(str(kind))
                    if kind == "input_audio_buffer.append":
                        pcm = base64.b64decode(event["audio"])
                        now = time.monotonic()
                        session.first_audio_at = session.first_audio_at or now
                        session.last_audio_at = now
                        session.audio_bytes += len(pcm)
                        session.appends += 1
                        if (
                            session.appends % self.appends_per_word == 0
                            and sent < len(TRANSCRIPT) - 1
                        ):
                            await socket.send_json(
                                {
                                    "type": "conversation.item.input_audio_transcription.delta",
                                    "item_id": item,
                                    "content_index": 0,
                                    "delta": TRANSCRIPT[sent],
                                }
                            )
                            sent += 1
                    elif kind == "input_audio_buffer.commit":
                        session.committed = True
                        await socket.send_json(
                            {
                                "type": "input_audio_buffer.committed",
                                "item_id": item,
                                "previous_item_id": None,
                            }
                        )
                        for word in TRANSCRIPT[sent:]:
                            await asyncio.sleep(0.05)
                            await socket.send_json(
                                {
                                    "type": "conversation.item.input_audio_transcription.delta",
                                    "item_id": item,
                                    "content_index": 0,
                                    "delta": word,
                                }
                            )
                        sent = len(TRANSCRIPT)
                        await socket.send_json(
                            {
                                "type": "conversation.item.input_audio_transcription.completed",
                                "item_id": item,
                                "content_index": 0,
                                "transcript": "".join(TRANSCRIPT),
                            }
                        )
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                session.closed = True

        return Starlette(
            routes=[
                Route("/v1/realtime/client_secrets", client_secrets, methods=["POST"]),
                WebSocketRoute("/v1/realtime", realtime),
            ]
        )


def serve(app: Any) -> tuple[Any, threading.Thread, str]:
    """Run ``app`` under uvicorn on a free loopback port; ``(server, thread, url)``."""

    import socket

    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", ws="websockets-sansio"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the test server did not start")
        time.sleep(0.02)
    return server, thread, f"http://127.0.0.1:{port}"


def stop(server: Any, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)
