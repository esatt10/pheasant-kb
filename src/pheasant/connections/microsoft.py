"""Microsoft 365 access (OneDrive, SharePoint): a sign-in, an app, or a token.

* ``oauth`` -- a person signs in; the flow is ``connections/oauth.py``'s with
  :data:`~pheasant.connections.oauth.MICROSOFT_365` as its spec, and reads
  what that person can read.
* ``app`` -- an Entra app registration with an application permission
  (``Sites.Read.All``, or ``Sites.Selected`` granted per site) and a client
  secret: the client-credentials grant, no browser, so an agent can connect one.
* ``token`` -- a pasted Graph access token, used until Microsoft refuses it.

Two audiences. Microsoft Graph serves drives, lists, list items and pages;
list item *attachments* are served only by SharePoint's own REST API, which
refuses a Graph token. :func:`sharepoint_token` is the second audience: the
sign-in's refresh token redeemed for ``https://<host>/AllSites.Read``, the app's
client credentials for ``https://<host>/.default``, or a pasted second token.
SharePoint refuses an app-only token obtained with a client *secret* (it wants a
certificate), so with ``app`` the attachments may be named unreadable while
everything Graph serves is read.

Every network call goes through ``oauth._post``, which tests replace.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from typing import Any

from pheasant.connections import oauth

GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


class MicrosoftAuthError(oauth.OAuthError):
    """Microsoft would not issue a token. ``rejected``: the credential is bad."""


_cache: dict[tuple[str, str], tuple[str, float]] = {}
_lock = threading.Lock()


def _client_credentials(secret: dict[str, Any], scope: str) -> str:
    tenant = str(secret.get("tenant_id") or secret.get("tenant") or "").strip()
    client_id = str(secret.get("client_id") or "").strip()
    client_secret = str(secret.get("client_secret") or "")
    if not (tenant and client_id and client_secret):
        raise MicrosoftAuthError("an app connection needs its tenant ID, client ID and secret")
    digest = hashlib.sha256(json.dumps([tenant, client_id, client_secret]).encode()).hexdigest()
    key = (digest, scope)
    with _lock:
        cached = _cache.get(key)
    if cached and cached[1] - oauth.REFRESH_MARGIN_SECONDS > time.time():
        return cached[0]
    try:
        body = oauth._post(
            TOKEN_URL.replace("{tenant}", tenant),
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": scope,
            },
        )
    except oauth.OAuthError as exc:
        raise MicrosoftAuthError(
            str(exc).replace("the service", "Microsoft"), rejected=exc.rejected
        ) from exc
    token = str(body.get("access_token") or "")
    if not token:
        raise MicrosoftAuthError("Microsoft answered without an access token")
    with _lock:
        _cache[key] = (token, time.time() + float(body.get("expires_in") or 3600))
    return token


def graph_token(
    secret: dict[str, Any], *, persist: Callable[[dict[str, Any]], None] | None = None
) -> str:
    """A live Microsoft Graph access token for a Microsoft 365 connection."""

    if secret.get("token"):
        return str(secret["token"])
    if secret.get("refresh_token") or secret.get("access_token"):
        try:
            return oauth.access_token_for(oauth.MICROSOFT_365, secret, persist=persist)
        except oauth.OAuthError as exc:
            raise MicrosoftAuthError(str(exc), rejected=exc.rejected) from exc
    if secret.get("client_secret"):
        return _client_credentials(secret, GRAPH_DEFAULT_SCOPE)
    raise MicrosoftAuthError("this Microsoft 365 connection holds no token, sign-in or app")


def sharepoint_token(secret: dict[str, Any], host: str) -> str:
    """A token SharePoint's REST API accepts for ``host`` (``contoso.sharepoint.com``)."""

    host = host.strip().lower()
    if secret.get("token"):
        if secret.get("sharepoint_token"):
            return str(secret["sharepoint_token"])
        raise MicrosoftAuthError(
            "a pasted Graph token cannot read list attachments; add a SharePoint token, or sign in"
        )
    if secret.get("refresh_token"):
        try:
            return oauth.token_for_scopes(
                oauth.MICROSOFT_365, secret, (f"https://{host}/AllSites.Read", "offline_access")
            )
        except oauth.OAuthError as exc:
            raise MicrosoftAuthError(str(exc), rejected=exc.rejected) from exc
    if secret.get("client_secret"):
        return _client_credentials(secret, f"https://{host}/.default")
    raise MicrosoftAuthError("this Microsoft 365 connection cannot reach SharePoint's REST API")


def forget_cached_tokens() -> None:
    with _lock:
        _cache.clear()
    oauth.forget_cached_tokens()
