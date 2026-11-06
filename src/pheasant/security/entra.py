"""Microsoft Entra ID: verifying its tokens, and asking a managed identity for one.

Two jobs, both stdlib HTTP plus ``cryptography`` for the RS256 check, so the
region needs no Azure SDK and a standalone install never imports any of it.

**Verifying.** An Entra access or id token is a JWT signed by a key the tenant
publishes at its JWKS URL. :class:`EntraVerifier` checks the signature against
that key set (cached, refetched once when a token names an unknown key, which
is how Entra's key rollover arrives), then ``exp``/``nbf`` with a small skew,
the issuer (v1 ``sts.windows.net`` and v2 ``login.microsoftonline.com`` for the
tenant both count, because which one a token carries is the *resource* app's
``accessTokenAcceptedVersion``, not the caller's choice), the audience, and the
tenant. A token failing any check is ``None`` -- never a partial identity.

**Asking.** :func:`managed_identity_token` reads a token for a resource from
the platform's identity endpoint: ``IDENTITY_ENDPOINT``/``IDENTITY_HEADER`` on
Container Apps and App Service, else the VM metadata service. Tokens are
cached per (resource, client id) until five minutes before they expire, so a
caller can ask on every request.

The only network calls live in :func:`_http_json` -- tests monkeypatch it, the
connector pattern ``security/idp.py`` already follows.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Seconds of clock skew tolerated on ``exp``/``nbf``.
CLOCK_SKEW_SECONDS = 120
#: How long a fetched key set is trusted before it is fetched again.
JWKS_TTL_SECONDS = 6 * 3600
#: The shortest gap between two refetches forced by an unknown ``kid`` -- an
#: attacker inventing key ids must not be able to make every request a fetch.
JWKS_MIN_REFRESH_SECONDS = 60
#: A cached managed-identity token is reused until this long before expiry.
TOKEN_REFRESH_MARGIN_SECONDS = 300

LOGIN_HOST = "https://login.microsoftonline.com"


class EntraUnavailable(RuntimeError):
    """Entra or the identity endpoint could not be reached or refused us."""


def _http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """The single network hop; tests monkeypatch this."""

    body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, headers=dict(headers or {}))
    if body is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise EntraUnavailable(
            f"{exc.code} from {urllib.parse.urlsplit(url).netloc}: {detail}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise EntraUnavailable(f"{urllib.parse.urlsplit(url).netloc}: {exc}") from exc


def _b64url(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def looks_like_jwt(token: str) -> bool:
    """Three dot-separated segments: cheap, and enough to route a bearer value."""

    return token.count(".") == 2 and not token.startswith("pht_")


def issuers(tenant_id: str) -> tuple[str, str]:
    """The v2 and v1 issuers of ``tenant_id``'s tokens."""

    return (f"{LOGIN_HOST}/{tenant_id}/v2.0", f"https://sts.windows.net/{tenant_id}/")


def authorize_endpoint(tenant_id: str) -> str:
    return f"{LOGIN_HOST}/{tenant_id}/oauth2/v2.0/authorize"


def token_endpoint(tenant_id: str) -> str:
    return f"{LOGIN_HOST}/{tenant_id}/oauth2/v2.0/token"


def token_request(tenant_id: str, form: dict[str, str]) -> dict[str, Any]:
    """POST a token request to the tenant's v2 endpoint and return its reply."""

    return _http_json(token_endpoint(tenant_id), data=form)


@dataclass
class _KeySet:
    keys: dict[str, Any] = field(default_factory=dict)
    fetched_at: float = 0.0
    forced_at: float = 0.0


class EntraVerifier:
    """Checks Entra JWTs for one tenant against an accepted set of audiences."""

    def __init__(self, tenant_id: str, audiences: list[str] | tuple[str, ...]) -> None:
        self.tenant_id = str(tenant_id or "").strip()
        self.audiences = frozenset(a for a in (str(x).strip() for x in audiences) if a)
        self._issuers = frozenset(issuers(self.tenant_id)) if self.tenant_id else frozenset()
        self._jwks = _KeySet()
        self._lock = threading.Lock()

    @property
    def jwks_url(self) -> str:
        return f"{LOGIN_HOST}/{self.tenant_id}/discovery/v2.0/keys"

    def _load_keys(self, *, force: bool) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            fresh = self._jwks.keys and now - self._jwks.fetched_at < JWKS_TTL_SECONDS
            if fresh and not force:
                return self._jwks.keys
            if force and now - self._jwks.forced_at < JWKS_MIN_REFRESH_SECONDS:
                return self._jwks.keys
            if force:
                self._jwks.forced_at = now
        document = _http_json(self.jwks_url)
        keys = {str(k.get("kid")): k for k in document.get("keys") or [] if k.get("kty") == "RSA"}
        with self._lock:
            self._jwks = _KeySet(keys=keys, fetched_at=now, forced_at=self._jwks.forced_at)
        return keys

    def verify(self, token: str, *, nonce: str | None = None) -> dict[str, Any] | None:
        """The token's claims when every check passes, otherwise ``None``."""

        if not self.tenant_id or not self.audiences or not looks_like_jwt(token):
            return None
        try:
            header_b64, payload_b64, signature_b64 = token.split(".")
            header = json.loads(_b64url(header_b64))
            claims = json.loads(_b64url(payload_b64))
            signature = _b64url(signature_b64)
        except (ValueError, json.JSONDecodeError):
            return None
        if header.get("alg") != "RS256" or not isinstance(claims, dict):
            return None
        kid = str(header.get("kid") or "")
        try:
            keys = self._load_keys(force=False)
            if kid not in keys:
                keys = self._load_keys(force=True)
        except EntraUnavailable:
            logger.warning("Entra signing keys unavailable; refusing the token", exc_info=True)
            return None
        jwk = keys.get(kid)
        if jwk is None or not _rs256_valid(jwk, f"{header_b64}.{payload_b64}".encode(), signature):
            return None
        now = time.time()
        try:
            if float(claims["exp"]) < now - CLOCK_SKEW_SECONDS:
                return None
            if "nbf" in claims and float(claims["nbf"]) > now + CLOCK_SKEW_SECONDS:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        if claims.get("iss") not in self._issuers or claims.get("tid") != self.tenant_id:
            return None
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if not self.audiences.intersection(str(a) for a in audiences):
            return None
        if nonce is not None and claims.get("nonce") != nonce:
            return None
        return claims


def _rs256_valid(jwk: dict[str, Any], signed: bytes, signature: bytes) -> bool:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:  # pragma: no cover - the image ships it
        raise EntraUnavailable(
            "Entra token verification needs the `cryptography` package, which "
            "the image ships through the [a2a] extra (pip install cryptography)"
        ) from exc
    try:
        public_key = rsa.RSAPublicNumbers(
            int.from_bytes(_b64url(jwk["e"]), "big"),
            int.from_bytes(_b64url(jwk["n"]), "big"),
        ).public_key()
        public_key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        return True
    except (InvalidSignature, KeyError, ValueError):
        return False


def principal_of(claims: dict[str, Any]) -> str:
    """The name a person recognises: their sign-in name, else their object id.

    ``preferred_username`` (v2), ``upn`` (v1) and ``email`` are display
    strings; ``oid`` is the stable id and what an application token carries
    alone, since a managed identity has no username.
    """

    for key in ("preferred_username", "upn", "email", "unique_name"):
        value = str(claims.get(key) or "").strip()
        if value:
            return value.lower()
    return str(claims.get("oid") or claims.get("sub") or "")


def roles_of(claims: dict[str, Any]) -> list[str]:
    roles = claims.get("roles") or []
    return sorted(str(r) for r in roles) if isinstance(roles, list) else []


# --------------------------------------------------------------------------
# Managed identity
# --------------------------------------------------------------------------

_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_TOKEN_LOCK = threading.Lock()


def managed_identity_available() -> bool:
    return bool(os.environ.get("IDENTITY_ENDPOINT") and os.environ.get("IDENTITY_HEADER"))


def managed_identity_token(resource: str, client_id: str | None = None) -> str:
    """An access token for ``resource`` from this process's managed identity.

    ``client_id`` names a user-assigned identity; empty falls back to
    ``AZURE_CLIENT_ID`` (the Azure SDKs' convention), then to the system
    identity. Raises :class:`EntraUnavailable` when there is no identity
    endpoint or it refuses.
    """

    resource = str(resource or "").strip()
    if not resource:
        raise EntraUnavailable("no resource to request a managed-identity token for")
    client_id = str(client_id or os.environ.get("AZURE_CLIENT_ID") or "").strip()
    key = (resource, client_id)
    now = time.time()
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(key)
        if cached and cached[1] - TOKEN_REFRESH_MARGIN_SECONDS > now:
            return cached[0]
    endpoint = os.environ.get("IDENTITY_ENDPOINT", "")
    secret = os.environ.get("IDENTITY_HEADER", "")
    if endpoint and secret:
        query = {"resource": resource, "api-version": "2019-08-01"}
        if client_id:
            query["client_id"] = client_id
        url = f"{endpoint}?{urllib.parse.urlencode(query)}"
        payload = _http_json(url, headers={"X-IDENTITY-HEADER": secret})
    else:
        query = {"resource": resource, "api-version": "2018-02-01"}
        if client_id:
            query["client_id"] = client_id
        url = (
            f"http://169.254.169.254/metadata/identity/oauth2/token?{urllib.parse.urlencode(query)}"
        )
        payload = _http_json(url, headers={"Metadata": "true"}, timeout=3.0)
    token = str(payload.get("access_token") or "")
    if not token:
        raise EntraUnavailable("the identity endpoint returned no access_token")
    try:
        expires_on = float(payload.get("expires_on") or 0)
    except (TypeError, ValueError):
        expires_on = 0.0
    if expires_on <= now:
        expires_on = now + float(payload.get("expires_in") or 3600)
    with _TOKEN_LOCK:
        _TOKEN_CACHE[key] = (token, expires_on)
    return token


def clear_token_cache() -> None:
    """For tests: forget every cached managed-identity token."""

    with _TOKEN_LOCK:
        _TOKEN_CACHE.clear()
