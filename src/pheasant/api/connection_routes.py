"""Connected accounts over HTTP: connect, check, list, remove, sign in.

One router per plane, as `api/app.py`'s ceiling comment asks. Every handler
parses the request and calls `services.connections`; MCP's
``list_connections`` / ``connect_service`` / ``test_connection`` /
``remove_connection`` / ``start_service_sign_in`` call the same functions.

``GET /connections/oauth/callback`` is HTTP only, and says so here: it is where
a service sends a *browser* back after its sign-in, so it carries no bearer
token (``auth.AUTH_PUBLIC_PATHS``) and is authenticated by the single-use,
expiring ``state`` the region minted when the sign-in began. It answers with a
redirect to the page that started it, never with a secret.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from pheasant.services import connections
from pheasant.services.errors import SignInExpired


class ConnectRequest(BaseModel):
    provider: str
    fields: dict[str, str] = Field(default_factory=dict)
    name: str | None = None
    method: str | None = None
    verify: bool = True


class SignInStartRequest(BaseModel):
    provider: str = "google_drive"
    name: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    return_to: str | None = None


def public_url(request: Request, config: Any) -> str:
    """The address a browser reached this region at, behind a proxy too."""

    configured = getattr(getattr(config.security, "entra", None), "public_url", None)
    if configured:
        return str(configured).rstrip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    return f"{proto}://{host}".rstrip("/") if host else str(request.base_url).rstrip("/")


def _actor(request: Request) -> str | None:
    identity = getattr(request.state, "identity", None)
    return getattr(identity, "principal", None) or "http"


def register_connection_routes(app: FastAPI, *, services: Any, config: Any) -> None:
    @app.get("/connections")
    def list_connections() -> dict:
        """Connected accounts (never a secret), what can be connected, and whether
        this process can hold them (``vault``)."""

        return connections.list_connections(services)

    @app.post("/connections")
    def connect(req: ConnectRequest, request: Request) -> dict:
        """Check a token or key with its service and store it as a connection.

        A secret the service rejects is refused (``CONNECTION_REJECTED``) and
        not stored."""

        return connections.connect(
            services,
            connections.ConnectRequest(
                provider=req.provider,
                fields=dict(req.fields),
                name=req.name,
                method=req.method,
                verify=req.verify,
                actor=_actor(request),
            ),
        )

    @app.post("/connections/oauth/start")
    def start_sign_in(req: SignInStartRequest, request: Request) -> dict:
        """Begin a browser sign-in (Google Drive): open ``authorize_url``."""

        return connections.start_sign_in(
            services,
            connections.SignInRequest(
                provider=req.provider,
                name=req.name,
                client_id=req.client_id,
                client_secret=req.client_secret,
                return_to=req.return_to,
                public_url=public_url(request, config),
                actor=_actor(request),
            ),
        )

    @app.get("/connections/oauth/callback", include_in_schema=False)
    def finish_sign_in(
        state: str = "", code: str | None = None, error: str | None = None
    ) -> RedirectResponse:
        try:
            result = connections.finish_sign_in(services, state=state, code=code, error=error)
        except SignInExpired as exc:
            query = urlencode({"connection_error": str(exc)})
            return RedirectResponse(f"{connections.DEFAULT_RETURN}?{query}", status_code=303)
        if result["ok"]:
            query = urlencode({"connected": result["name"]})
        else:
            query = urlencode({"connection_error": result["detail"], "connection": result["name"]})
        return RedirectResponse(f"{result['return_to']}?{query}", status_code=303)

    @app.post("/connections/{name}/test")
    def test_connection(name: str) -> dict:
        """Ask the service again whether it accepts this connection."""

        return connections.check(services, name)

    @app.delete("/connections/{name}")
    def remove_connection(name: str, force: bool = False) -> dict:
        """Remove a connection; refused while a source reads with it unless ``force``."""

        return connections.remove(services, name, force=force)
