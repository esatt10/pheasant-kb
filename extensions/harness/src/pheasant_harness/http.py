"""HTTP adapter: parse, call one service function, marshal."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Literal

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from pheasant_harness import service
from pheasant_harness.common import QueueFull
from pheasant_harness.models import (
    ApprovalRequest,
    ConnectionPolicyRequest,
    DiscoverRequest,
    GrepRequest,
    ReadRequest,
    RegisterRequest,
    ResumeRequest,
    StartRequest,
)


class ResolveBody(BaseModel):
    version_selector: str | None = None


class GrantBody(BaseModel):
    principal: str
    granted: bool = True


class EffectBody(BaseModel):
    disposition: Literal["applied", "not_applied"]


def register_harness_routes(app: FastAPI, *, services: Any) -> None:
    @app.get("/harness/contract")
    def contract() -> dict:
        return service.contract(services)

    @app.get("/harness/registrations")
    def registrations() -> dict:
        return service.registrations(services)

    @app.post("/harness/registrations", status_code=201)
    def register(request: RegisterRequest) -> dict:
        return service.register(services, request)

    @app.post("/harness/registrations/{registration_id}/resolve")
    def resolve(registration_id: str, body: ResolveBody | None = None) -> dict:
        return service.resolve(services, registration_id, (body or ResolveBody()).version_selector)

    @app.post("/harness/registrations/{registration_id}/grants")
    def grant(registration_id: str, body: GrantBody) -> dict:
        return service.set_grant(services, registration_id, body.principal, body.granted)

    @app.get("/harness/registrations/{registration_id}/entries")
    def entries(
        registration_id: str,
        revision: str | None = None,
        prefix: str = "",
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict:
        return service.enumerate_entries(services, registration_id, revision, prefix, cursor, limit)

    @app.post("/harness/grep")
    def grep(request: GrepRequest) -> dict:
        return service.grep(services, request)

    @app.post("/harness/read")
    def read(request: ReadRequest) -> dict:
        return service.read(services, request)

    @app.post("/harness/discover")
    def discover(request: DiscoverRequest) -> dict:
        return service.discover(services, request)

    @app.get("/harness/runs")
    def runs(limit: int = 50) -> dict:
        return service.list_runs(services, limit)

    @app.post("/harness/runs", status_code=201)
    def start(request: StartRequest):
        try:
            return service.start_run(services, request)
        except QueueFull as exc:
            # A shed request gets a bounded retry hint, in the header as well as the body,
            # spelled the way the host spells every other refusal.
            return JSONResponse(
                {
                    "detail": str(exc),
                    "code": exc.code,
                    "retryable": True,
                    "retry_after_seconds": exc.retry_after,
                },
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
            )

    @app.get("/harness/runs/{run_id}")
    def status(run_id: str) -> dict:
        return service.run_status(services, run_id)

    @app.get("/harness/runs/{run_id}/detail")
    def detail(run_id: str) -> dict:
        return service.run_detail(services, run_id)

    @app.get("/harness/runs/{run_id}/events")
    def events(run_id: str, after_seq: int = 0, limit: int = 500) -> dict:
        return service.run_events(services, run_id, after_seq, limit)

    @app.get("/harness/runs/{run_id}/events/stream")
    async def stream(run_id: str, request: Request, after_seq: int = 0):
        """Server-sent events from ``after_seq``.

        A reconnecting client passes the last seq it saw (or ``Last-Event-ID``).
        """
        service.run_status(services, run_id)

        async def feed():
            cursor = int(request.headers.get("last-event-id") or after_seq)
            while not await request.is_disconnected():
                page = await asyncio.to_thread(service.run_events, services, run_id, cursor, 200)
                for event in page["events"]:
                    cursor = event["seq"]
                    head = f"id: {event['seq']}\nevent: {event['type']}\n"
                    yield head + f"data: {json.dumps(event)}\n\n"
                if (
                    page["state"] in ("succeeded", "partial", "failed", "cancelled")
                    and not page["events"]
                ):
                    yield "event: end\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.5)

        return StreamingResponse(
            feed(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/harness/runs/{run_id}/step")
    def step(run_id: str) -> dict:
        return service.advance_run(services, run_id)

    @app.post("/harness/runs/{run_id}/resume")
    def resume(run_id: str, request: ResumeRequest | None = None) -> dict:
        return service.resume_run(services, run_id, request or ResumeRequest())

    @app.post("/harness/runs/{run_id}/cancel")
    def cancel(run_id: str) -> dict:
        return service.cancel_run(services, run_id)

    @app.get("/harness/runs/{run_id}/integrity")
    def integrity(run_id: str) -> dict:
        return service.integrity(services, run_id)

    @app.get("/harness/runs/{run_id}/artifacts/{digest}")
    def artifact(run_id: str, digest: str) -> dict:
        return service.artifact(services, run_id, digest)

    @app.get("/harness/runs/{run_id}/artifacts/{digest}/raw")
    def artifact_raw(run_id: str, digest: str) -> Response:
        item = service.artifact(services, run_id, digest)
        data = (
            base64.b64decode(item["content_base64"])
            if item["content_base64"]
            else item["content"].encode()
        )
        media = (
            item["media_type"]
            if item["media_type"].startswith("image/")
            else "text/plain; charset=utf-8"
        )
        return Response(
            data,
            media_type=media,
            headers={
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=3600, immutable",
            },
        )

    @app.post("/harness/runs/{run_id}/effects/{operation_id}/resolve")
    def resolve_effect(run_id: str, operation_id: str, body: EffectBody) -> dict:
        return service.resolve_effect(services, run_id, operation_id, body.disposition)

    @app.post("/harness/runs/{run_id}/approvals/{operation_id}")
    def decide_approval(run_id: str, operation_id: str, body: ApprovalRequest) -> dict:
        return service.decide_approval(services, run_id, operation_id, body)

    @app.get("/harness/connections")
    def connections() -> dict:
        return service.connections(services)

    @app.get("/harness/connections/usable")
    def usable_connections() -> dict:
        return service.usable_connections(services)

    @app.get("/harness/connections/{connection}/browse")
    def browse_connection(
        connection: str, location: str = "", path: str = "", ref: str | None = None
    ) -> dict:
        return service.browse_connection(services, connection, location, path, ref)

    @app.post("/harness/attachments", status_code=201)
    async def attach_files(
        files: list[UploadFile] = File(...),
        label: str | None = Form(None),
        sync: bool = Form(True),
        keep_in_uploads: bool = Form(True),
    ) -> dict:
        received = [(f.filename or "upload", await f.read()) for f in files]
        return await asyncio.to_thread(
            service.attach_files, services, received, label, sync, keep_in_uploads
        )

    @app.get("/harness/connections/calls")
    def connection_calls(
        run_id: str | None = None, connection: str | None = None, limit: int = 200
    ) -> dict:
        return service.connection_calls(services, run_id, connection, limit)

    @app.put("/harness/connections/{connection}/policy")
    def set_connection_policy(connection: str, body: ConnectionPolicyRequest) -> dict:
        return service.set_connection_policy(services, connection, body)

    @app.post("/harness/connections/{connection}/policy/disable")
    def disable_connection_policy(connection: str) -> dict:
        return service.disable_connection_policy(services, connection)

    @app.get("/harness/connections/{connection}/policy/history")
    def connection_policy_history(connection: str) -> dict:
        return service.connection_policy_history(services, connection)

    @app.get("/harness/sandboxes")
    def sandboxes(run_id: str | None = None) -> dict:
        return service.sandboxes(services, run_id)

    @app.post("/harness/sandboxes/reap")
    def reap() -> dict:
        return service.reap(services)
