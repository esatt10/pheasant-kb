"""The HTTP half of identity: the guard, the sign-in routes, the MCP OAuth server.

Everything that decides *who* a credential names lives in
:mod:`pheasant.services.identity`; this module parses requests, calls it, and
marshals the answer (rule 13). It is HTTP only, and says so: a browser sign-in
and an OAuth authorization server are things a transport does. MCP clients
reach the same identity because ``/mcp`` is mounted behind this guard.

The guard replaces 35.8's shared-token middleware and keeps its behaviour for a
region that configures only the token: same exemptions (probes, ``/metrics``,
the UI shell, ``/internal/*``), same 401 text. What it adds is that a request
leaves with an :class:`~pheasant.security.identity.Identity` bound for the
handlers below it, and one access-log line naming the principal and how it
proved itself.
"""

from __future__ import annotations

import html
import logging
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from pheasant.security.identity import Identity, bind_identity, current_identity, reset_identity
from pheasant.services.errors import ServiceError
from pheasant.services.identity import IdentityService

logger = logging.getLogger(__name__)
#: One line per authenticated request: who, how, what, and the outcome.
access_log = logging.getLogger("pheasant.access")

#: Answerable without a credential: the sign-in round trip and the OAuth
#: server a client must reach *before* it holds one.
AUTH_PUBLIC_PATHS = frozenset(
    {
        "/auth/config",
        "/auth/login",
        "/auth/callback",
        "/auth/consent",
        "/authorize",
        "/token",
        "/register",
        "/revoke",
    }
)
MCP_SCOPE = "pheasant"


def _base_url(request: Request, service: IdentityService) -> str:
    return service.public_url(str(request.base_url))


def _resource_metadata_url(base: str) -> str:
    return f"{base}/.well-known/oauth-protected-resource/mcp"


def install_guard(
    app: Any,
    service: IdentityService,
    *,
    public_paths: frozenset[str],
    is_public_ui_request: Any,
) -> None:
    """Require a credential on every request that is not exempt."""

    @app.middleware("http")
    async def require_identity(request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        # `/internal/*` is exempt structurally, not by the configured list:
        # each of those routes checks the credential for its own boundary
        # (`security/service_auth.py`), and requiring this one there would
        # hand the region's front-door credential to every worker.
        if (
            path.startswith("/internal/")
            or path.startswith("/.well-known/")
            or path in public_paths
            or path in AUTH_PUBLIC_PATHS
            or is_public_ui_request(request)
        ):
            return await call_next(request)
        started = time.monotonic()
        scheme, _, supplied = (request.headers.get("authorization") or "").partition(" ")
        identity: Identity | None = None
        if scheme.lower() == "bearer" and supplied:
            identity = await run_in_threadpool(service.authenticate, supplied)
        if identity is None:
            access_log.info(
                "%s %s 401 principal=- auth=%s",
                request.method,
                path,
                "rejected" if supplied else "none",
            )
            challenge = "Bearer"
            if service.entra_enabled and service.public_url():
                metadata = _resource_metadata_url(service.public_url())
                challenge = f'Bearer resource_metadata="{metadata}"'
            return JSONResponse(
                status_code=401,
                content={"detail": "this region requires a bearer token"},
                headers={"WWW-Authenticate": challenge},
            )
        request.state.identity = identity
        token = bind_identity(identity)
        try:
            response = await call_next(request)
        finally:
            reset_identity(token)
        access_log.info(
            "%s %s %d principal=%s auth=%s %.0fms",
            request.method,
            path,
            response.status_code,
            identity.principal,
            identity.method,
            (time.monotonic() - started) * 1000,
        )
        return response


def request_identity(request: Request) -> Identity | None:
    return getattr(request.state, "identity", None) or current_identity()


class PersonalKeyRequest(BaseModel):
    name: str
    days: int | None = None


def _refusal(exc: ServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


def _safe_next(value: str | None) -> str:
    """A same-site path to return to after sign-in, never another origin."""

    value = str(value or "/")
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def register_identity_routes(app: Any, service: IdentityService, *, resource_name: str) -> None:
    """The `/auth/*` routes and, with Entra on, the MCP authorization server."""

    app.include_router(build_router(service))
    for route in oauth_routes(service, resource_name=resource_name):
        app.router.routes.append(route)


def build_router(service: IdentityService) -> APIRouter:
    router = APIRouter(tags=["identity"])

    @router.get("/auth/config")
    def auth_config() -> dict:
        """What a client may know before it holds a credential."""

        return service.describe()

    @router.get("/auth/me")
    def auth_me(request: Request) -> dict:
        identity = request_identity(request)
        if identity is None:
            return {"principal": None, "auth_method": None, "person": False, "roles": []}
        return identity.as_dict()

    @router.post("/auth/logout")
    def auth_logout(request: Request) -> dict:
        service.end_session(request_identity(request))
        return {"signed_out": True}

    @router.get("/auth/login")
    def auth_login(request: Request, next: str | None = None) -> RedirectResponse:  # noqa: A002
        try:
            url = service.begin_login(
                flow="ui", base_url=_base_url(request, service), data={"next": _safe_next(next)}
            )
        except ServiceError as exc:
            raise _refusal(exc) from exc
        return RedirectResponse(url, status_code=302)

    @router.get("/auth/callback")
    def auth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
    ) -> Any:
        if error or not code or not state:
            reason = error_description or error or "the sign-in returned no code"
            return _message_page("Sign-in did not complete", reason, status=400)
        try:
            result = service.complete_login(
                code=code, state=state, base_url=_base_url(request, service)
            )
        except ServiceError as exc:
            return _message_page("Sign-in did not complete", str(exc), status=exc.status)
        if result.flow == "mcp":
            consent = service.begin_consent(result.identity, result.data)
            return RedirectResponse(f"/auth/consent?request={consent}", status_code=302)
        session = service.issue_session(result.identity)
        target = _safe_next(result.data.get("next"))
        # The fragment never reaches a server log or a Referer; the UI reads
        # it once and replaces the history entry.
        return RedirectResponse(f"{target}#pheasant_session={session}", status_code=302)

    @router.get("/auth/consent")
    def auth_consent_page(request: str) -> HTMLResponse:  # noqa: A002 - the OAuth spelling
        record = service.consent_request(request)
        if record is None:
            return _message_page(
                "This approval has expired", "Start the connection from your client again.", 400
            )
        data = record["data"]
        client = html.escape(str(data.get("client_name") or data.get("client_id") or "A client"))
        redirect_host = html.escape(urlsplit(str(data.get("redirect_uri") or "")).netloc or "?")
        who = html.escape(str(record.get("principal") or ""))
        body = f"""
<p><strong>{client}</strong> wants to use this knowledge base as <strong>{who}</strong>.</p>
<p class="muted">After you approve, it is sent back to <code>{redirect_host}</code>.
Approve only if you just started connecting this client.</p>
<form method="post" action="/auth/consent">
  <input type="hidden" name="request" value="{html.escape(request)}">
  <button name="decision" value="deny" type="submit">Deny</button>
  <button name="decision" value="allow" type="submit" class="primary">Allow</button>
</form>"""
        return _page("Connect a client", body)

    @router.post("/auth/consent")
    def auth_consent_decide(
        request: str = Form(...),  # noqa: A002
        decision: str = Form(...),
    ) -> Any:
        from mcp.server.auth.provider import construct_redirect_uri

        try:
            data, code = service.decide_consent(request, allow=decision == "allow")
        except ServiceError as exc:
            return _message_page("This approval has expired", str(exc), status=exc.status)
        redirect_uri = str(data.get("redirect_uri") or "")
        if code is None:
            target = construct_redirect_uri(
                redirect_uri, error="access_denied", state=data.get("state")
            )
        else:
            target = construct_redirect_uri(redirect_uri, code=code, state=data.get("state"))
        return RedirectResponse(target, status_code=302)

    @router.get("/auth/keys")
    def list_keys(request: Request) -> dict:
        try:
            return {"keys": service.list_personal_keys(request_identity(request))}
        except ServiceError as exc:
            raise _refusal(exc) from exc

    @router.post("/auth/keys")
    def create_key(request: Request, body: PersonalKeyRequest) -> dict:
        try:
            return service.create_personal_key(
                request_identity(request), name=body.name, days=body.days or 0
            )
        except ServiceError as exc:
            raise _refusal(exc) from exc

    @router.delete("/auth/keys/{key_id}")
    def revoke_key(request: Request, key_id: str) -> dict:
        try:
            return {"revoked": service.revoke_personal_key(request_identity(request), key_id)}
        except ServiceError as exc:
            raise _refusal(exc) from exc

    return router


def oauth_routes(service: IdentityService, *, resource_name: str) -> list[Any]:
    """The MCP authorization server and protected-resource metadata routes.

    Empty without a ``public_url``: every URL in OAuth metadata is absolute,
    and one guessed from a request behind an ingress is a URL a client
    cannot reach.
    """

    base = service.public_url()
    if not service.entra_enabled or not base:
        return []
    from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
    from pydantic import AnyHttpUrl

    provider = RegionOAuthProvider(service, base_url=base)
    routes = create_auth_routes(
        provider,
        issuer_url=AnyHttpUrl(base),
        client_registration_options=ClientRegistrationOptions(
            enabled=True, default_scopes=[MCP_SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    routes += create_protected_resource_routes(
        resource_url=AnyHttpUrl(f"{base}/mcp"),
        authorization_servers=[AnyHttpUrl(base)],
        scopes_supported=[MCP_SCOPE],
        resource_name=resource_name,
    )
    return routes


class RegionOAuthProvider:
    """The MCP SDK's authorization-server protocol over :class:`IdentityService`.

    The SDK handles the wire (PKCE checks, redirect-URI matching, error
    shapes); this answers its questions from ``auth_tokens``. Sign-in itself
    is delegated to Entra, then to the consent page above.
    """

    def __init__(self, service: IdentityService, *, base_url: str) -> None:
        self.service = service
        self.base_url = base_url

    async def get_client(self, client_id: str) -> Any:
        from mcp.shared.auth import OAuthClientInformationFull

        from pheasant.persistence import auth_tokens

        data = await run_in_threadpool(auth_tokens.load_client, self.service.state, client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: Any) -> None:
        from pheasant.persistence import auth_tokens

        data = client_info.model_dump(mode="json", exclude_none=True)
        await run_in_threadpool(
            auth_tokens.save_client, self.service.state, str(client_info.client_id), data
        )
        logger.info(
            "MCP client registered: client=%s name=%s",
            client_info.client_id,
            getattr(client_info, "client_name", None),
        )

    async def authorize(self, client: Any, params: Any) -> str:
        request = {
            "client_id": str(client.client_id),
            "client_name": getattr(client, "client_name", None),
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": bool(params.redirect_uri_provided_explicitly),
            "code_challenge": params.code_challenge,
            "scopes": list(params.scopes or [MCP_SCOPE]),
            "state": params.state,
            "resource": params.resource,
        }
        return await run_in_threadpool(
            lambda: self.service.begin_login(flow="mcp", base_url=self.base_url, data=request)
        )

    async def load_authorization_code(self, client: Any, authorization_code: str) -> Any:
        from mcp.server.auth.provider import AuthorizationCode

        record = await run_in_threadpool(
            self.service.load_code, authorization_code, str(client.client_id)
        )
        if record is None:
            return None
        data = record["data"]
        from datetime import datetime

        return AuthorizationCode(
            code=authorization_code,
            scopes=list(data.get("scopes") or []),
            expires_at=datetime.fromisoformat(record["expires_at"]).timestamp(),
            client_id=str(client.client_id),
            code_challenge=str(data.get("code_challenge") or ""),
            redirect_uri=data.get("redirect_uri"),
            redirect_uri_provided_explicitly=bool(data.get("redirect_uri_provided_explicitly")),
            resource=data.get("resource"),
            subject=record.get("principal"),
        )

    async def exchange_authorization_code(self, client: Any, authorization_code: Any) -> Any:
        from mcp.server.auth.provider import TokenError

        try:
            pair = await run_in_threadpool(
                self.service.exchange_code, authorization_code.code, str(client.client_id)
            )
        except ServiceError as exc:
            raise TokenError("invalid_grant", str(exc)) from exc
        return _oauth_token(pair)

    async def load_refresh_token(self, client: Any, refresh_token: str) -> Any:
        from mcp.server.auth.provider import RefreshToken

        record = await run_in_threadpool(
            self.service.load_refresh, refresh_token, str(client.client_id)
        )
        if record is None:
            return None
        from datetime import datetime

        return RefreshToken(
            token=refresh_token,
            client_id=str(client.client_id),
            scopes=list(record["data"].get("scopes") or []),
            expires_at=int(datetime.fromisoformat(record["expires_at"]).timestamp()),
            resource=record["data"].get("resource"),
            subject=record.get("principal"),
        )

    async def exchange_refresh_token(
        self, client: Any, refresh_token: Any, scopes: list[str]
    ) -> Any:
        from mcp.server.auth.provider import TokenError

        try:
            pair = await run_in_threadpool(
                self.service.exchange_refresh, refresh_token.token, str(client.client_id), scopes
            )
        except ServiceError as exc:
            raise TokenError("invalid_grant", str(exc)) from exc
        return _oauth_token(pair)

    async def load_access_token(self, token: str) -> Any:
        from mcp.server.auth.provider import AccessToken

        record = await run_in_threadpool(self.service.load_access, token)
        if record is None:
            return None
        return AccessToken(
            token=token,
            client_id=str(record.get("client_id") or ""),
            scopes=list(record["data"].get("scopes") or []),
            subject=record.get("principal"),
        )

    async def revoke_token(self, token: Any) -> None:
        await run_in_threadpool(self.service.revoke_oauth, token.token)


def _oauth_token(pair: dict[str, Any]) -> Any:
    from mcp.shared.auth import OAuthToken

    return OAuthToken(
        access_token=pair["access_token"],
        token_type="Bearer",
        expires_in=pair["expires_in"],
        refresh_token=pair["refresh_token"],
        scope=" ".join(pair["scopes"]) or None,
    )


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - pheasant</title>
<style>
:root {{ color-scheme: light dark; --bg:#f7f7f5; --fg:#1d1d1b; --muted:#6b6b66; --card:#fff;
  --line:#deded8; --accent:#2f6f4f; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161614; --fg:#ecece8; --muted:#a3a39c;
  --card:#20201d; --line:#34342f; --accent:#6fbf8f; }} }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg);
  color:var(--fg); font:16px/1.5 system-ui, sans-serif; padding:16px; box-sizing:border-box; }}
main {{ max-width:440px; width:100%; background:var(--card); border:1px solid var(--line);
  border-radius:12px; padding:24px; }}
h1 {{ font-size:1.2rem; margin:0 0 12px; }} .muted {{ color:var(--muted); font-size:.9rem; }}
form {{ display:flex; gap:8px; justify-content:flex-end; margin-top:20px; }}
button {{ font:inherit; padding:8px 16px; border-radius:8px; border:1px solid var(--line);
  background:transparent; color:var(--fg); cursor:pointer; }}
button.primary {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
code {{ font-size:.9em; }}
</style></head><body><main><h1>{title}</h1>{body}</main></body></html>"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        _PAGE.format(title=html.escape(title), body=body),
        status_code=status,
        # A consent page in someone else's frame is a clickjacking target.
        headers={"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"},
    )


def _message_page(title: str, message: str, status: int = 400) -> HTMLResponse:
    return _page(title, f'<p class="muted">{html.escape(message)}</p>', status)
