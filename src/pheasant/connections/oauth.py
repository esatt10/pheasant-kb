"""Sign in with a service: GitHub, Microsoft (Azure DevOps), GitLab, Google.

One authorization-code flow (with PKCE) for every provider that has one; a
provider differs only in its :class:`OAuthSpec` -- where to send the browser,
where to redeem the code, which scopes to ask for, and which variables hold
the region's OAuth client. The region registers that client once with the
service (an OAuth app on GitHub, an app registration in Microsoft Entra, a
Google Cloud OAuth client), and a person then only presses "Sign in".

What is sealed is the token response: the access token, and the refresh
token where the service issues one. :func:`access_token_for` hands out a live
access token, refreshing it shortly before it expires; a service that rotates
refresh tokens (GitLab, Microsoft) gets the new one written back through
``persist``, so the api and the indexer do not each spend the same one.

Every network call goes through :func:`_post`, which tests replace.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

#: An access token is reused until this many seconds before it expires.
REFRESH_MARGIN_SECONDS = 120


class OAuthError(RuntimeError):
    """A service would not issue a token. ``rejected``: the grant itself is bad."""

    def __init__(self, message: str, *, rejected: bool = False) -> None:
        super().__init__(message)
        self.rejected = rejected


@dataclass(frozen=True)
class OAuthSpec:
    service: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    #: The variables holding the region's OAuth client for this service.
    client_id_env: str
    client_secret_env: str
    #: Where an operator registers that client.
    register_url: str
    #: A public client (PKCE, no secret) is enough: Microsoft's.
    secret_required: bool = True
    extra: dict[str, str] = field(default_factory=dict)
    #: Git signs a remote with ``Authorization: bearer`` rather than basic.
    git_bearer: bool = False

    def endpoint(self, url: str, tenant: str | None) -> str:
        return url.replace("{tenant}", tenant or "common")

    def public(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "client_id_env": self.client_id_env,
            "client_secret_env": self.client_secret_env,
            "register_url": self.register_url,
            "secret_required": self.secret_required,
            "scopes": list(self.scopes),
        }


GITHUB = OAuthSpec(
    service="GitHub",
    authorize_url="https://github.com/login/oauth/authorize",
    token_url="https://github.com/login/oauth/access_token",
    # OAuth apps have no read-only scope for private repositories.
    scopes=("repo", "read:user"),
    client_id_env="GITHUB_OAUTH_CLIENT_ID",
    client_secret_env="GITHUB_OAUTH_CLIENT_SECRET",
    register_url="https://github.com/settings/applications/new",
    extra={"allow_signup": "false"},
)
MICROSOFT_DEVOPS = OAuthSpec(
    service="Microsoft",
    authorize_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
    token_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
    # Azure DevOps' resource id; offline_access is what issues a refresh token.
    scopes=("499b84ac-1321-427f-aa17-267ca6a55798/user_impersonation", "offline_access"),
    client_id_env="MICROSOFT_OAUTH_CLIENT_ID",
    client_secret_env="MICROSOFT_OAUTH_CLIENT_SECRET",
    register_url="https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
    secret_required=False,
    extra={"prompt": "select_account"},
    git_bearer=True,
)
GITLAB = OAuthSpec(
    service="GitLab",
    authorize_url="https://gitlab.com/oauth/authorize",
    token_url="https://gitlab.com/oauth/token",
    scopes=("read_repository", "read_user"),
    client_id_env="GITLAB_OAUTH_CLIENT_ID",
    client_secret_env="GITLAB_OAUTH_CLIENT_SECRET",
    register_url="https://gitlab.com/-/user_settings/applications",
)
GOOGLE_DRIVE = OAuthSpec(
    service="Google",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    scopes=("https://www.googleapis.com/auth/drive.readonly",),
    client_id_env="GOOGLE_OAUTH_CLIENT_ID",
    client_secret_env="GOOGLE_OAUTH_CLIENT_SECRET",
    register_url="https://console.cloud.google.com/apis/credentials",
    # A refresh token is issued only with offline access, and a second
    # sign-in of the same account gets one again only with consent.
    extra={"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"},
)


def _post(url: str, form: dict[str, str]) -> dict[str, Any]:
    """One form POST to a token endpoint; module-level so tests replace it."""

    request = Request(
        url,
        data=urlencode(form).encode("ascii"),
        # GitHub answers form-encoded unless asked for JSON.
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            body = {}
        reason = body.get("error_description") or body.get("error") or exc.reason
        raise OAuthError(
            f"the service refused the request ({exc.code}: {reason})",
            rejected=exc.code in (400, 401),
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise OAuthError(f"could not reach the service ({getattr(exc, 'reason', exc)})") from exc
    # GitHub reports a bad code as 200 with an error field.
    if isinstance(body, dict) and body.get("error"):
        reason = body.get("error_description") or body["error"]
        raise OAuthError(f"the service refused the request ({reason})", rejected=True)
    return body


def pkce_pair() -> tuple[str, str]:
    """``(verifier, challenge)`` for an S256 PKCE exchange."""

    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(
    spec: OAuthSpec,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    challenge: str,
    tenant: str | None = None,
) -> str:
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(spec.scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **spec.extra,
    }
    return f"{spec.endpoint(spec.authorize_url, tenant)}?{urlencode(query)}"


def _client_form(secret: dict[str, Any]) -> dict[str, str]:
    form = {"client_id": str(secret.get("client_id") or "")}
    if secret.get("client_secret"):
        form["client_secret"] = str(secret["client_secret"])
    return form


def exchange_code(
    spec: OAuthSpec,
    *,
    code: str,
    client: dict[str, Any],
    redirect_uri: str,
    verifier: str,
) -> dict[str, Any]:
    """The secret to seal for an authorization code: the client plus its tokens."""

    body = _post(
        spec.endpoint(spec.token_url, client.get("tenant")),
        {
            **_client_form(client),
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
    )
    if not body.get("access_token") and not body.get("refresh_token"):
        raise OAuthError(f"{spec.service} answered without a token")
    return _with_tokens(client, body)


def _with_tokens(client: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    secret = {
        key: client[key] for key in ("client_id", "client_secret", "tenant") if client.get(key)
    }
    if body.get("access_token"):
        secret["access_token"] = str(body["access_token"])
    refresh = body.get("refresh_token") or client.get("refresh_token")
    if refresh:
        secret["refresh_token"] = str(refresh)
    if body.get("expires_in"):
        secret["expires_at"] = int(time.time() + float(body["expires_in"]))
    return secret


#: refresh-token digest -> (access token, expiry, the renewed secret)
_cache: dict[str, tuple[str, float, dict[str, Any]]] = {}
_lock = threading.Lock()


def access_token_for(
    spec: OAuthSpec,
    secret: dict[str, Any],
    *,
    persist: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """A live access token for a signed-in connection's sealed secret.

    A token with no expiry (a GitHub OAuth app's) is used as it is. One near
    its expiry is refreshed; when the service sends a new refresh token, the
    new secret goes to ``persist`` so it is the one every process reads next.
    """

    access = str(secret.get("access_token") or "")
    expires = float(secret.get("expires_at") or 0)
    refresh = str(secret.get("refresh_token") or "")
    if access and (not expires or expires - REFRESH_MARGIN_SECONDS > time.time()):
        return access
    if not refresh:
        if access:
            return access
        raise OAuthError(f"this {spec.service} sign-in holds no token; sign in again")
    key = hashlib.sha256(refresh.encode("utf-8")).hexdigest()
    with _lock:
        cached = _cache.get(key)
    if cached and cached[1] - REFRESH_MARGIN_SECONDS > time.time():
        # Refreshed earlier by a caller with nowhere to keep a rotated refresh
        # token: hand it to this one, or the service's next answer is a refusal.
        if persist is not None and cached[2].get("refresh_token") != refresh:
            _keep(persist, cached[2])
        return cached[0]
    form = {**_client_form(secret), "refresh_token": refresh, "grant_type": "refresh_token"}
    if spec is MICROSOFT_DEVOPS:
        form["scope"] = " ".join(spec.scopes)
    body = _post(spec.endpoint(spec.token_url, secret.get("tenant")), form)
    renewed = _with_tokens(secret, body)
    token = str(renewed.get("access_token") or "")
    if not token:
        raise OAuthError(f"{spec.service} answered without an access token")
    with _lock:
        _cache[key] = (token, float(renewed.get("expires_at") or time.time() + 3600), renewed)
    if persist is not None:
        _keep(persist, renewed)
    return token


def _keep(persist: Callable[[dict[str, Any]], None], renewed: dict[str, Any]) -> None:
    try:
        persist(renewed)
    except Exception:  # a refresh that worked must not fail because a write did
        pass


def forget_cached_tokens() -> None:
    with _lock:
        _cache.clear()
