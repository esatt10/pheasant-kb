"""Who is calling: every credential this region accepts, resolved one way.

One implementation behind both surfaces (rule 13). The HTTP middleware and the
MCP mount it fronts both ask :meth:`IdentityService.authenticate` and get the
same :class:`~pheasant.security.identity.Identity` -- or ``None`` -- for the
same bearer value. Five credentials resolve, in this order:

1. the shared token (``security.api_auth.token_env``), unchanged since 35.8:
   principal ``shared-token``, method ``api_token``;
2. a credential the region issued (``pht_…``): a browser session, a personal
   key, or an MCP OAuth access token, looked up in ``auth_tokens``;
3. an Entra access token for this app (``az account get-access-token``),
   verified against the tenant's keys.

(The internal boundaries' credentials are not here: ``/internal/*`` never
passes through this, see ``security/service_auth.py``.)

A signed-in identity must carry one of ``security.entra.allowed_roles`` --
the app registration's "assignment required" is the gate that decides who
can sign in at all, and the role is this region's own check that it held.

The sign-in itself is the OpenID Connect code flow with PKCE against the
tenant, with the region as a confidential client: its managed identity's
token is the client assertion (a federated credential on the app
registration), so no client secret exists anywhere.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from pheasant.persistence import auth_tokens
from pheasant.security import entra
from pheasant.security.identity import (
    API_TOKEN,
    ENTRA_SSO,
    ENTRA_TOKEN,
    MCP_OAUTH,
    PERSONAL_KEY,
    SHARED_TOKEN_PRINCIPAL,
    Identity,
)
from pheasant.services.errors import (
    IdentityNotConfigured,
    InvalidRequest,
    PersonIdentityRequired,
    SignInFailed,
)

logger = logging.getLogger(__name__)

#: Kinds of issued credential that authenticate a request, and how each was proven.
_BEARER_KINDS = {"session": ENTRA_SSO, "personal": PERSONAL_KEY, "oauth_access": MCP_OAUTH}
#: How long a resolved credential is trusted before it is looked up again.
#: Revoking a key takes at most this long to reach a replica that cached it.
CACHE_SECONDS = 60.0
#: ``last_used_at`` is written at most this often per credential: a request
#: path that writes on every call is the thing the observation plane avoids.
TOUCH_SECONDS = 300.0
LOGIN_SCOPE = "openid profile email"
LOGIN_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(minutes=5)
#: A personal key's name, as shown in the key list.
MAX_KEY_NAME = 80


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


@dataclass
class LoginResult:
    identity: Identity
    flow: str
    data: dict[str, Any]


class IdentityService:
    """The region's credentials, for one config and one state store."""

    def __init__(self, config: Any, state: Any) -> None:
        self.config = config
        self.state = state
        self.settings = config.security.entra
        # Read once: rotating the shared token is a restart, the same contract
        # as every other credential here, none of which is read out of YAML.
        token_env = str(getattr(config.security.api_auth, "token_env", "") or "")
        self._shared = os.environ.get(token_env, "").strip() if token_env else ""
        audiences = [self.settings.client_id, *self.settings.audiences]
        self.verifier = entra.EntraVerifier(self.settings.tenant_id, audiences)
        self._cache: dict[str, tuple[Identity, float]] = {}
        self._touched: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- configuration -----------------------------------------------------

    @property
    def entra_enabled(self) -> bool:
        s = self.settings
        return bool(s.enabled and s.tenant_id and s.client_id)

    @property
    def shared_token_enabled(self) -> bool:
        return bool(self._shared)

    @property
    def enforcing(self) -> bool:
        """Whether any credential is required at all."""

        return self.shared_token_enabled or self.entra_enabled

    def public_url(self, fallback: str = "") -> str:
        return (str(self.settings.public_url or "") or fallback).rstrip("/")

    def describe(self) -> dict[str, Any]:
        """What a client may know before it holds a credential."""

        return {
            "shared_token": self.shared_token_enabled,
            "entra": self.entra_enabled,
            "login_url": "/auth/login" if self.entra_enabled else None,
            "tenant_id": self.settings.tenant_id if self.entra_enabled else None,
            "client_id": self.settings.client_id if self.entra_enabled else None,
            "scope": self.api_scope() if self.entra_enabled else None,
        }

    def api_scope(self) -> str:
        """The delegated scope a client asks Entra for to call this API."""

        return f"api://{self.settings.client_id}/access"

    def _require_entra(self) -> None:
        if not self.entra_enabled:
            raise IdentityNotConfigured()

    def _roles_admitted(self, roles: list[str]) -> bool:
        allowed = [r for r in self.settings.allowed_roles or [] if r]
        return not allowed or any(role in allowed for role in roles)

    # -- authenticating a request ------------------------------------------

    def authenticate(self, bearer: str) -> Identity | None:
        """The identity a bearer value proves, or ``None``."""

        bearer = (bearer or "").strip()
        if not bearer:
            return None
        if self._shared and secrets.compare_digest(
            bearer.encode("utf-8", "surrogateescape"), self._shared.encode("utf-8")
        ):
            return Identity(SHARED_TOKEN_PRINCIPAL, API_TOKEN, roles=("Admin",))
        key = hashlib.sha256(bearer.encode("utf-8", "surrogateescape")).hexdigest()
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[1] > now:
                return cached[0]
        identity = self._resolve(bearer)
        if identity is not None:
            with self._lock:
                if len(self._cache) > 4096:
                    self._cache.clear()
                self._cache[key] = (identity, now + CACHE_SECONDS)
        return identity

    def _resolve(self, bearer: str) -> Identity | None:
        if bearer.startswith(auth_tokens.PREFIX):
            record = auth_tokens.lookup(self.state, bearer, kinds=tuple(_BEARER_KINDS))
            if record is None:
                return None
            self._touch(record["id"])
            return Identity(
                principal=str(record.get("principal") or ""),
                method=_BEARER_KINDS[record["kind"]],
                display_name=str(record.get("display_name") or ""),
                subject=str(record.get("subject") or ""),
                roles=tuple(record.get("roles") or ()),
                token_id=record["id"],
            )
        if self.entra_enabled and entra.looks_like_jwt(bearer):
            claims = self.verifier.verify(bearer)
            if claims is None:
                return None
            roles = entra.roles_of(claims)
            if not self._roles_admitted(roles):
                return None
            return Identity(
                principal=entra.principal_of(claims),
                method=ENTRA_TOKEN,
                display_name=str(claims.get("name") or ""),
                subject=str(claims.get("oid") or ""),
                roles=tuple(roles),
            )
        return None

    def _touch(self, token_id: str) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._touched.get(token_id, 0.0) < TOUCH_SECONDS:
                return
            self._touched[token_id] = now
        try:
            auth_tokens.touch(self.state, token_id)
        except Exception:  # noqa: BLE001 - bookkeeping must never fail a request
            logger.debug("could not record last use of %s", token_id, exc_info=True)

    def forget(self) -> None:
        """Drop every cached resolution (after a revoke in this process)."""

        with self._lock:
            self._cache.clear()

    # -- signing in through Entra ------------------------------------------

    def begin_login(self, *, flow: str, base_url: str, data: dict[str, Any]) -> str:
        """Start a sign-in and return the Entra URL to send the browser to."""

        self._require_entra()
        verifier, challenge = _pkce_pair()
        nonce = secrets.token_urlsafe(16)
        state, _row = auth_tokens.issue(
            self.state,
            kind="login",
            lifetime=LOGIN_TTL,
            data={"flow": flow, "nonce": nonce, "verifier": verifier, **data},
        )
        query = {
            "client_id": self.settings.client_id,
            "response_type": "code",
            "response_mode": "query",
            "redirect_uri": self.redirect_uri(base_url),
            "scope": LOGIN_SCOPE,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{entra.authorize_endpoint(self.settings.tenant_id)}?{urlencode(query)}"

    def redirect_uri(self, base_url: str) -> str:
        return f"{self.public_url(base_url)}/auth/callback"

    def complete_login(self, *, code: str, state: str, base_url: str) -> LoginResult:
        """Redeem Entra's code and return who signed in."""

        self._require_entra()
        login = auth_tokens.consume(self.state, state, kind="login")
        if login is None:
            raise SignInFailed("the sign-in expired or was already used; start again")
        data = dict(login["data"])
        form = {
            "client_id": self.settings.client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri(base_url),
            "code_verifier": data.pop("verifier", ""),
            "scope": LOGIN_SCOPE,
            **self._client_credential(),
        }
        try:
            reply = entra.token_request(self.settings.tenant_id, form)
        except entra.EntraUnavailable as exc:
            logger.warning("Entra refused the sign-in code: %s", exc)
            raise SignInFailed("Microsoft did not accept the sign-in code") from exc
        id_token = str(reply.get("id_token") or "")
        claims = entra.EntraVerifier(self.settings.tenant_id, [self.settings.client_id]).verify(
            id_token, nonce=str(data.pop("nonce", ""))
        )
        if claims is None:
            raise SignInFailed("the identity token could not be verified")
        roles = entra.roles_of(claims)
        if not self._roles_admitted(roles):
            raise SignInFailed("this account has not been granted access to this region")
        identity = Identity(
            principal=entra.principal_of(claims),
            method=ENTRA_SSO,
            display_name=str(claims.get("name") or ""),
            subject=str(claims.get("oid") or ""),
            roles=tuple(roles),
        )
        logger.info("sign-in: principal=%s flow=%s", identity.principal, data.get("flow"))
        return LoginResult(identity=identity, flow=str(data.pop("flow", "ui")), data=data)

    def _client_credential(self) -> dict[str, str]:
        """How the region proves it is the app registration when redeeming."""

        federated = str(self.settings.federated_identity_client_id or "").strip()
        if federated:
            assertion = entra.managed_identity_token("api://AzureADTokenExchange", federated)
            return {
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": assertion,
            }
        secret = os.environ.get(str(self.settings.client_secret_env or ""), "")
        if not secret:
            raise SignInFailed(
                "the region has no client credential (federated_identity_client_id or "
                f"{self.settings.client_secret_env})"
            )
        return {"client_secret": secret}

    # -- what a signed-in person holds ---------------------------------------

    def issue_session(self, identity: Identity) -> str:
        credential, _ = auth_tokens.issue(
            self.state,
            kind="session",
            lifetime=timedelta(hours=max(1, int(self.settings.session_hours))),
            principal=identity.principal,
            display_name=identity.display_name,
            subject=identity.subject,
            roles=identity.roles,
        )
        return credential

    def create_personal_key(self, identity: Identity | None, *, name: str, days: int) -> dict:
        if identity is None or not identity.is_person:
            raise PersonIdentityRequired()
        name = (name or "").strip()[:MAX_KEY_NAME]
        if not name:
            raise InvalidRequest("a personal key needs a name")
        limit = max(1, int(self.settings.personal_key_max_days))
        days = int(days or limit)
        if days < 1 or days > limit:
            raise InvalidRequest(f"a personal key lasts between 1 and {limit} days")
        credential, row = auth_tokens.issue(
            self.state,
            kind="personal",
            lifetime=timedelta(days=days),
            principal=identity.principal,
            display_name=identity.display_name,
            subject=identity.subject,
            roles=identity.roles,
            name=name,
        )
        logger.info("personal key created: principal=%s key=%s", identity.principal, row["id"])
        return {"key": credential, **_public(row)}

    def list_personal_keys(self, identity: Identity | None) -> list[dict]:
        if identity is None or not identity.is_person:
            raise PersonIdentityRequired()
        rows = auth_tokens.list_for(self.state, identity.principal, kind="personal")
        return [_public(row) for row in rows]

    def revoke_personal_key(self, identity: Identity | None, key_id: str) -> bool:
        if identity is None or not identity.is_person:
            raise PersonIdentityRequired()
        revoked = auth_tokens.revoke(self.state, key_id, principal=identity.principal)
        self.forget()
        if revoked:
            logger.info("personal key revoked: principal=%s key=%s", identity.principal, key_id)
        return revoked

    def end_session(self, identity: Identity | None) -> None:
        if identity is not None and identity.method == ENTRA_SSO and identity.token_id:
            auth_tokens.revoke(self.state, identity.token_id)
            self.forget()

    # -- the MCP OAuth authorization server ----------------------------------
    #
    # An MCP client (Claude Code, Claude's connectors, VS Code) registers
    # itself, sends the person here, and receives a region-issued token. The
    # person proves who they are to Entra; the region then asks them, on its
    # own page, whether *this client* may act for them. That consent step is
    # not decoration: registration is open to anyone, and without it a client
    # registered with an attacker's redirect URI would receive the code of
    # anyone whose browser is already signed in.

    def begin_consent(self, identity: Identity, request: dict[str, Any]) -> str:
        credential, _ = auth_tokens.issue(
            self.state,
            kind="consent",
            lifetime=LOGIN_TTL,
            principal=identity.principal,
            display_name=identity.display_name,
            subject=identity.subject,
            roles=identity.roles,
            client_id=str(request.get("client_id") or ""),
            data=request,
        )
        return credential

    def consent_request(self, credential: str) -> dict[str, Any] | None:
        return auth_tokens.lookup(self.state, credential, kinds=("consent",))

    def decide_consent(self, credential: str, *, allow: bool) -> tuple[dict[str, Any], str | None]:
        """Settle a consent: the request it answered, and a code when allowed."""

        record = auth_tokens.consume(self.state, credential, kind="consent")
        if record is None:
            raise SignInFailed("the approval expired or was already used; connect again")
        request = dict(record["data"])
        if not allow:
            logger.info(
                "MCP client denied: principal=%s client=%s",
                record["principal"],
                record["client_id"],
            )
            return request, None
        code, _ = auth_tokens.issue(
            self.state,
            kind="oauth_code",
            lifetime=CODE_TTL,
            principal=record["principal"],
            display_name=record.get("display_name"),
            subject=record.get("subject"),
            roles=record.get("roles") or (),
            client_id=record["client_id"],
            data={**request, "family": secrets.token_hex(8)},
        )
        logger.info(
            "MCP client approved: principal=%s client=%s", record["principal"], record["client_id"]
        )
        return request, code

    def load_code(self, code: str, client_id: str) -> dict[str, Any] | None:
        record = auth_tokens.lookup(self.state, code, kinds=("oauth_code",))
        return record if record and record.get("client_id") == client_id else None

    def exchange_code(self, code: str, client_id: str) -> dict[str, Any]:
        record = auth_tokens.consume(self.state, code, kind="oauth_code")
        if record is None or record.get("client_id") != client_id:
            raise SignInFailed("the authorization code is invalid or was already used")
        return self._token_pair(record)

    def load_refresh(self, token: str, client_id: str) -> dict[str, Any] | None:
        record = auth_tokens.lookup(self.state, token, kinds=("oauth_refresh",))
        return record if record and record.get("client_id") == client_id else None

    def exchange_refresh(self, token: str, client_id: str, scopes: list[str]) -> dict[str, Any]:
        record = auth_tokens.consume(self.state, token, kind="oauth_refresh")
        if record is None or record.get("client_id") != client_id:
            raise SignInFailed("the refresh token is invalid or was already used")
        record["data"] = {
            **record["data"],
            "scopes": list(scopes or record["data"].get("scopes") or []),
        }
        return self._token_pair(record)

    def load_access(self, token: str) -> dict[str, Any] | None:
        return auth_tokens.lookup(self.state, token, kinds=("oauth_access",))

    def revoke_oauth(self, token: str) -> None:
        record = auth_tokens.lookup(self.state, token, kinds=("oauth_access", "oauth_refresh"))
        if record is not None:
            auth_tokens.revoke_family(self.state, str(record["data"].get("family") or ""))
            self.forget()

    def _token_pair(self, record: dict[str, Any]) -> dict[str, Any]:
        data = record["data"]
        carried = {
            "family": data.get("family") or secrets.token_hex(8),
            "scopes": list(data.get("scopes") or []),
            "resource": data.get("resource"),
        }
        common = {
            "principal": record["principal"],
            "display_name": record.get("display_name"),
            "subject": record.get("subject"),
            "roles": record.get("roles") or (),
            "client_id": record["client_id"],
            "data": carried,
        }
        access_minutes = max(5, int(self.settings.oauth_access_minutes))
        access, _ = auth_tokens.issue(
            self.state, kind="oauth_access", lifetime=timedelta(minutes=access_minutes), **common
        )
        refresh, _ = auth_tokens.issue(
            self.state,
            kind="oauth_refresh",
            lifetime=timedelta(days=max(1, int(self.settings.oauth_refresh_days))),
            **common,
        )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": access_minutes * 60,
            "scopes": carried["scopes"],
        }


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row.get("name"),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
        "revoked_at": row.get("revoked_at"),
        "last_used_at": row.get("last_used_at"),
    }
