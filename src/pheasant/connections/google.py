"""Google Drive access: a sign-in, a service account, or a pasted token.

* ``oauth`` -- a person signs in; the flow is ``connections/oauth.py``'s, with
  :data:`~pheasant.connections.oauth.GOOGLE_DRIVE` as its spec.
* ``service_account`` -- a JSON key. The region signs a JWT (RS256) and
  exchanges it for an access token; ``subject`` impersonates a user under
  domain-wide delegation. Works with no browser, so an agent can connect one.
* ``token`` -- a pasted access token, used as it is until Google refuses it.

Every network call goes through :func:`_post` (here) or ``oauth._post``,
which tests replace.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from collections.abc import Callable
from typing import Any

from pheasant.connections import oauth

TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


class GoogleAuthError(oauth.OAuthError):
    """Google would not issue a token. ``rejected``: the credential itself is bad."""


def _post(url: str, form: dict[str, str]) -> dict[str, Any]:
    """One form POST to Google's token endpoint; module-level so tests replace it."""

    try:
        return oauth._post(url, form)
    except oauth.OAuthError as exc:
        message = str(exc).replace("the service", "Google")
        raise GoogleAuthError(message, rejected=exc.rejected) from exc


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _service_account_assertion(key: dict[str, Any], subject: str | None) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(time.time())
    claims = {
        "iss": key["client_email"],
        "scope": DRIVE_SCOPE,
        "aud": key.get("token_uri") or TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    if subject:
        claims["sub"] = subject
    header = {"alg": "RS256", "typ": "JWT"}
    if key.get("private_key_id"):
        header["kid"] = key["private_key_id"]
    signing_input = (
        f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    ).encode("ascii")
    private_key = serialization.load_pem_private_key(
        str(key["private_key"]).encode("utf-8"), password=None
    )
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[call-arg, union-attr]
    return f"{signing_input.decode('ascii')}.{_b64(signature)}"


def parse_service_account(text: str) -> dict[str, Any]:
    """A service account key, refusing anything that is not one."""

    try:
        key = json.loads(text)
    except ValueError as exc:
        raise ValueError("the service account key is not valid JSON") from exc
    if not isinstance(key, dict) or key.get("type") != "service_account":
        raise ValueError("that JSON is not a service account key (type is not service_account)")
    for name in ("client_email", "private_key"):
        if not key.get(name):
            raise ValueError(f"the service account key has no {name}")
    return key


_cache: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def access_token_for(
    secret: dict[str, Any], *, persist: Callable[[dict[str, Any]], None] | None = None
) -> str:
    """A live access token for a Drive connection's sealed secret."""

    if secret.get("token"):
        return str(secret["token"])
    if secret.get("refresh_token") or secret.get("access_token"):
        try:
            return oauth.access_token_for(oauth.GOOGLE_DRIVE, secret, persist=persist)
        except oauth.OAuthError as exc:
            raise GoogleAuthError(str(exc), rejected=exc.rejected) from exc
    if not secret.get("service_account_json"):
        raise GoogleAuthError("this Drive connection holds no token, sign-in or key")
    key = hashlib.sha256(json.dumps(secret, sort_keys=True).encode("utf-8")).hexdigest()
    with _lock:
        cached = _cache.get(key)
        if cached and cached[1] - oauth.REFRESH_MARGIN_SECONDS > time.time():
            return cached[0]
    account = parse_service_account(str(secret["service_account_json"]))
    body = _post(
        str(account.get("token_uri") or TOKEN_URL),
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": _service_account_assertion(account, secret.get("subject") or None),
        },
    )
    token = str(body.get("access_token") or "")
    if not token:
        raise GoogleAuthError("Google answered without an access token")
    with _lock:
        _cache[key] = (token, time.time() + float(body.get("expires_in") or 3600))
    return token


def forget_cached_tokens() -> None:
    with _lock:
        _cache.clear()
    oauth.forget_cached_tokens()
