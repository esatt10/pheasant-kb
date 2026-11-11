"""Connect an account, check it, list what is connected, take one away.

The operations behind ``/connections*`` and MCP ``list_connections`` /
``connect_service`` / ``test_connection`` / ``remove_connection`` /
``start_service_sign_in``. A connection is what a source reads with (a git
token, a Drive sign-in, an API token) or what a model service is called with
(an OpenAI or Foundry key), held sealed in the region's state store
(``connections/vault.py``) so a person can start a region from nothing and
connect everything from the UI, an agent from MCP, a script over HTTP.

Two rules shape every answer. **A secret never comes back**: a listing shows
the provider, the account the service named, and the last four characters.
**A rejected secret is not stored**: when the service answers 401/403 the
connection is refused (``CONNECTION_REJECTED``) rather than saved to fail on the
first sync; a service this region cannot reach is stored with that said, since
a key for a firewalled endpoint is still a key.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pheasant.connections import google, oauth, providers, resolve, vault
from pheasant.services import ServiceContext
from pheasant.services.errors import (
    ConnectionForbidden,
    ConnectionInUse,
    ConnectionNotFound,
    ConnectionRejected,
    ConnectionsUnavailable,
    InvalidRequest,
    SignInExpired,
)

logger = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
CALLBACK_PATH = "/connections/oauth/callback"
DEFAULT_RETURN = "/sources"


@dataclass(frozen=True)
class ConnectRequest:
    provider: str
    fields: dict[str, str] = field(default_factory=dict)
    name: str | None = None
    method: str | None = None
    #: Ask the service whether it accepts the secret before storing it.
    verify: bool = True
    actor: str | None = None
    #: ``http`` or ``mcp``: shown beside the connection, so a person can tell
    #: what an agent connected from what they did.
    transport: str = "http"


@dataclass(frozen=True)
class SignInRequest:
    """A browser sign-in to a service (Google Drive)."""

    provider: str = "google_drive"
    name: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    #: A path on this region to send the browser back to.
    return_to: str | None = None
    #: The address the browser reaches this region at; the adapter knows it.
    public_url: str = ""
    actor: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime | None = None) -> str:
    return (moment or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _settings(context: ServiceContext) -> Any:
    return context.config.security.connections


def _ready(context: ServiceContext, *, create: bool = True) -> vault.VaultKey:
    """This process's key, installing the context's store if nothing has."""

    if not getattr(_settings(context), "enabled", True):
        raise ConnectionsUnavailable("connections are turned off (security.connections.enabled)")
    if resolve.installed_state() is not context.state:
        resolve.install(context.config, context.state)
    try:
        return resolve.key(create=create)
    except vault.VaultUnavailable as exc:
        raise ConnectionsUnavailable(str(exc)) from exc


def _require_admin() -> None:
    from pheasant.security.identity import current_identity

    identity = current_identity()
    roles = tuple(getattr(identity, "roles", ()) or ()) if identity is not None else ()
    if roles and "Admin" not in roles:
        raise ConnectionForbidden()


def vault_status(context: ServiceContext) -> dict[str, Any]:
    try:
        key = _ready(context, create=False)
    except ConnectionsUnavailable as exc:
        settings = _settings(context)
        # A process that may write /state generates its key on the first
        # connection; say that rather than that nothing can be connected.
        writable = context.indexing is None or bool(getattr(context.indexing, "local", True))
        if writable and getattr(settings, "enabled", True) and settings.generate_key_file:
            return {"available": True, "key_source": "generated on first use", "reason": None}
        return {"available": False, "key_source": None, "reason": str(exc)}
    return {"available": True, "key_source": key.source, "key_id": key.key_id, "reason": None}


def _used_by(context: ServiceContext, name: str) -> list[str]:
    return sorted(
        source.name
        for source in context.config.sources
        if resolve.source_connection(source) == name
    )


def _public(context: ServiceContext, row: vault.Connection) -> dict[str, Any]:
    import os

    payload = row.public()
    bound = providers.bound_env(row.provider, row.details)
    entry = providers.BY_ID.get(row.provider)
    payload["label"] = entry.label if entry else row.provider
    payload["kind"] = entry.kind if entry else None
    payload["stands_in_for"] = bound
    # An operator's variable wins; a reader deciding why a key is not used
    # needs to see that here, not discover it.
    payload["overridden_by_environment"] = bool(bound and os.environ.get(bound, "").strip())
    payload["used_by"] = _used_by(context, row.name)
    return payload


def list_connections(context: ServiceContext) -> dict[str, Any]:
    status = vault_status(context)
    rows: list[vault.Connection] = []
    try:
        rows = [row for row in vault.all_rows(context.state) if row.status == "connected"]
    except Exception as exc:  # pragma: no cover - a store without the table is pre-migration
        logger.debug("connections table unreadable: %s", exc)
    return {
        "connections": [_public(context, row) for row in rows],
        "providers": [_catalog_entry(entry) for entry in providers.PROVIDERS],
        "vault": status,
    }


def _client(spec: oauth.OAuthSpec) -> tuple[str | None, str | None]:
    """The region's OAuth client for ``spec``'s service, from its variables."""

    return resolve.secret(spec.client_id_env), resolve.secret(spec.client_secret_env)


def _catalog_entry(entry: providers.Provider) -> dict[str, Any]:
    """A provider as the catalog shows it, saying whether its sign-in is ready
    here: with a client configured, the form is one button."""

    payload = entry.public()
    for method, shown in zip(entry.methods, payload["methods"], strict=True):
        if method.oauth is not None:
            client_id, client_secret = _client(method.oauth)
            shown["oauth"]["client_configured"] = bool(
                client_id and (client_secret or not method.oauth.secret_required)
            )
            shown["oauth"]["callback_path"] = CALLBACK_PATH
    return payload


def _name(request_name: str | None, provider: providers.Provider, fields: dict[str, str]) -> str:
    name = (request_name or "").strip()
    if not name:
        name = fields.get("variable", "").strip() if provider.id == "environment" else provider.id
    if not NAME_PATTERN.match(name):
        raise InvalidRequest(
            f"connection name {name!r} must be letters, digits, '.', '_' or '-' (at most 64)"
        )
    return name


def _hint(value: str) -> str:
    value = value.strip()
    return f"…{value[-4:]}" if len(value) > 8 else "…"


def _fields(method: providers.Method, supplied: dict[str, str]) -> dict[str, str]:
    known = {item.name for item in method.fields}
    unknown = sorted(set(supplied) - known)
    if unknown:
        raise InvalidRequest(
            f"unknown field(s) {', '.join(unknown)}; {method.label} takes "
            + ", ".join(sorted(known))
        )
    cleaned = {name: str(value).strip() for name, value in supplied.items() if str(value).strip()}
    missing = [item.label for item in method.fields if item.required and item.name not in cleaned]
    if missing:
        raise InvalidRequest(f"{method.label} needs {', '.join(missing)}")
    return cleaned


def connect(context: ServiceContext, request: ConnectRequest) -> dict[str, Any]:
    """Check a secret with its service and store it as a connection."""

    _require_admin()
    try:
        provider = providers.provider(request.provider)
        # Without a method, the first one that takes a token or key: a sign-in
        # is not something a single call can complete.
        method = provider.method(
            request.method or next((m.id for m in provider.methods if not m.browser), None)
        )
    except KeyError as exc:
        raise InvalidRequest(str(exc.args[0])) from exc
    if method.browser:
        raise InvalidRequest(
            f"{provider.label} sign-in happens in a browser: call start_service_sign_in "
            "(POST /connections/oauth/start) and open the address it returns"
        )
    supplied = _fields(method, dict(request.fields or {}))
    key = _ready(context)
    name = _name(request.name, provider, supplied)
    details: dict[str, Any] = {
        "method_label": method.label,
        "added_via": request.transport,
        **{
            item.name: supplied[item.name]
            for item in method.fields
            if not item.secret and item.name in supplied
        },
    }
    if method.id == "service_account":
        try:
            account = google.parse_service_account(supplied["service_account_json"])
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        details["account"] = account["client_email"]
    main = providers.secret_value(provider.id, supplied)
    if main:
        details["hint"] = _hint(main)
    check = None
    if request.verify and provider.check is not None:
        result = provider.check(supplied)
        if result.rejected:
            raise ConnectionRejected(f"{result.detail}; nothing was stored")
        check = result.public(_stamp())
        if result.account:
            details["account"] = result.account
    row = vault.put(
        context.state,
        key=key,
        name=name,
        provider=provider.id,
        method=method.id,
        secret=supplied,
        details=details,
        now=_stamp(),
        created_by=request.actor,
        check=check,
    )
    resolve.invalidate()
    logger.info("connection %s (%s) saved by %s", name, provider.id, request.actor or "-")
    return {"status": "connected", "connection": _public(context, row)}


def _row(context: ServiceContext, name: str) -> vault.Connection:
    row = vault.get(context.state, name)
    if row is None or row.status != "connected":
        raise ConnectionNotFound(name)
    return row


def check(context: ServiceContext, name: str) -> dict[str, Any]:
    """Ask the service again whether it accepts this connection."""

    key = _ready(context, create=False)
    row = _row(context, name)
    entry = providers.BY_ID.get(row.provider)
    if entry is None or entry.check is None:
        result = providers.CheckResult(
            False, f"{entry.label if entry else row.provider} has no check; it is used as stored"
        )
    else:
        try:
            secret = vault.unseal(key, row)
        except vault.VaultUnavailable as exc:
            raise ConnectionsUnavailable(str(exc)) from exc
        result = entry.check(secret)
    stamp = result.public(_stamp())
    details = None
    if result.account and result.account != row.details.get("account"):
        details = {**row.details, "account": result.account}
    vault.record_check(context.state, name, stamp, details)
    resolve.invalidate()
    return {"connection": _public(context, _row(context, name)), "check": stamp}


def remove(context: ServiceContext, name: str, *, force: bool = False) -> dict[str, Any]:
    _require_admin()
    _row(context, name)
    used = _used_by(context, name)
    if used and not force:
        raise ConnectionInUse(name, used)
    vault.delete(context.state, name)
    resolve.invalidate()
    google.forget_cached_tokens()
    logger.info("connection %s removed", name)
    return {"status": "removed", "name": name, "sources_without_it": used}


def _safe_return(value: str | None) -> str:
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return DEFAULT_RETURN
    return value


def start_sign_in(context: ServiceContext, request: SignInRequest) -> dict[str, Any]:
    """Open a browser sign-in; returns the address to send the person to."""

    _require_admin()
    try:
        provider = providers.provider(request.provider)
    except KeyError as exc:
        raise InvalidRequest(str(exc.args[0])) from exc
    method = provider.sign_in()
    if method is None or method.oauth is None:
        raise InvalidRequest(f"{provider.label} has no sign-in; connect it with a token or key")
    spec = method.oauth
    settings = _settings(context)
    configured_id, configured_secret = _client(spec)
    given_id = (request.client_id or "").strip()
    client_id = given_id or configured_id
    # A client given with the request comes with its own secret, or none.
    client_secret = (request.client_secret or "").strip() or (
        None if given_id else configured_secret
    )
    public_url = (settings.public_url or request.public_url or "").rstrip("/")
    if not client_id or (spec.secret_required and not client_secret):
        callback = f"{public_url}{CALLBACK_PATH}" if public_url else CALLBACK_PATH
        needs = "client ID and secret" if spec.secret_required else "client ID"
        names = spec.client_id_env
        if spec.secret_required:
            names += f" and {spec.client_secret_env}"
        raise InvalidRequest(
            f"{method.label} needs an OAuth client registered with {spec.service} "
            f"({spec.register_url}) whose callback is {callback}: pass its {needs}, or set "
            f"{names} on the api and the indexer. Or connect with a token instead"
        )
    if not public_url:
        raise InvalidRequest(
            "this region does not know its own address; set security.connections.public_url"
        )
    key = _ready(context)
    name = _name(request.name, provider, {})
    state = secrets.token_urlsafe(32)
    digest = hashlib.sha256(state.encode("ascii")).hexdigest()
    verifier, challenge = oauth.pkce_pair()
    tenant = settings.microsoft_tenant if spec is oauth.MICROSOFT_DEVOPS else None
    redirect_uri = public_url + CALLBACK_PATH
    expires = _now() + timedelta(seconds=int(settings.oauth_timeout_seconds))
    vault.delete_expired_pending(
        context.state, _stamp(_now() - timedelta(seconds=int(settings.oauth_timeout_seconds)))
    )
    vault.put(
        context.state,
        key=key,
        name=vault.PENDING_PREFIX + digest[:24],
        provider=provider.id,
        method="oauth",
        status="pending",
        secret={
            "client_id": client_id,
            "client_secret": client_secret or "",
            "tenant": tenant or "",
            "verifier": verifier,
        },
        details={
            "target_name": name,
            "redirect_uri": redirect_uri,
            "return_to": _safe_return(request.return_to),
            "expires_at": _stamp(expires),
        },
        oauth_state=digest,
        now=_stamp(),
        created_by=request.actor,
    )
    url = oauth.authorize_url(
        spec,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        challenge=challenge,
        tenant=tenant,
    )
    return {
        "status": "pending",
        "name": name,
        "authorize_url": url,
        "redirect_uri": redirect_uri,
        "expires_at": _stamp(expires),
    }


def finish_sign_in(
    context: ServiceContext, *, state: str, code: str | None, error: str | None
) -> dict[str, Any]:
    """Complete a sign-in the service redirected back; ``return_to`` is where to go."""

    key = _ready(context, create=False)
    digest = hashlib.sha256((state or "").encode("ascii", "replace")).hexdigest()
    pending = vault.by_oauth_state(context.state, digest) if state else None
    if pending is None or pending.status != "pending":
        raise SignInExpired(
            "this sign-in is not one the region is waiting for (it finished, expired or was "
            "started elsewhere); start it again from the Sources page"
        )
    vault.delete(context.state, pending.name)
    details = pending.details
    return_to = _safe_return(details.get("return_to"))
    provider = providers.provider(pending.provider)
    method = provider.sign_in()
    assert method is not None and method.oauth is not None
    name = str(details.get("target_name") or provider.id)
    if str(details.get("expires_at") or "") < _stamp():
        raise SignInExpired("the sign-in took too long; start it again from the Sources page")
    if error or not code:
        return {"ok": False, "name": name, "return_to": return_to, "detail": error or "no code"}
    try:
        started = vault.unseal(key, pending)
        secret = oauth.exchange_code(
            method.oauth,
            code=code,
            client=started,
            redirect_uri=str(details["redirect_uri"]),
            verifier=started["verifier"],
        )
    except (oauth.OAuthError, vault.VaultUnavailable) as exc:
        return {"ok": False, "name": name, "return_to": return_to, "detail": str(exc)}
    try:
        # A token that is already near expiry is refreshed into the secret about
        # to be sealed, so a rotated refresh token is the one kept.
        oauth.access_token_for(method.oauth, secret, persist=secret.update)
    except oauth.OAuthError as exc:
        return {"ok": False, "name": name, "return_to": return_to, "detail": str(exc)}
    result = provider.check(secret) if provider.check else None
    if result is not None and result.rejected:
        return {"ok": False, "name": name, "return_to": return_to, "detail": result.detail}
    shown: dict[str, Any] = {
        "method_label": f"Signed in with {method.oauth.service}",
        "hint": "…",
        "added_via": "sign-in",
    }
    if result and result.account:
        shown["account"] = result.account
    vault.put(
        context.state,
        key=key,
        name=name,
        provider=provider.id,
        method="oauth",
        secret=secret,
        details=shown,
        now=_stamp(),
        created_by=pending.created_by,
        check=result.public(_stamp()) if result else None,
    )
    resolve.invalidate()
    logger.info("connection %s (%s) signed in", name, provider.id)
    return {"ok": True, "name": name, "return_to": return_to, "detail": "connected"}
