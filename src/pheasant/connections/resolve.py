"""How a process reads connected accounts, wherever it needs a secret.

Every module that needs a key used to read ``os.environ`` itself. They read
through here now, and here is the only place that knows connections exist:

* :func:`secret` -- an environment variable's value, else the connection that
  stands in for it (``providers.bound_env``). An operator's variable wins.
* :func:`environment` -- ``os.environ`` with those connections filled in, for
  the callers that pass a whole mapping (the chat provider's ``auto``).
* :func:`source_secret` -- what a source reads with: the connection it names
  (``connector.connection``), which wins over everything; otherwise ``None``,
  and the caller keeps its own fallback.
* :func:`ambient` -- the one connection of a provider, for a source that names
  none (a pasted repository URL when one GitHub account is connected).

A process opts in with :func:`install`, which ``SyncEngine`` does for the state
store it opens; a process that never installs reads the environment exactly as
before. The table is read at most every ``cache_seconds``, so a connection made
on the api reaches an indexer in another container within that window, and a
lookup on a hot path (an embedding batch) costs a dict read.
"""

from __future__ import annotations

import os
import threading
import time
import weakref
from typing import Any

from pheasant.connections import oauth, providers, vault


class ConnectionUnavailable(RuntimeError):
    """A source names a connection this process cannot use, and why."""


_lock = threading.Lock()
_installed: dict[str, Any] = {}
_snapshot: dict[str, Any] = {"at": 0.0, "rows": None, "state": None}


def install(config: Any, state: Any) -> None:
    """Read connections from ``state`` for the rest of this process's life."""

    settings = getattr(getattr(config, "security", None), "connections", None)
    if settings is not None and not getattr(settings, "enabled", True):
        return
    state_path = getattr(getattr(config, "pheasant", None), "state_path", None)
    with _lock:
        _installed.update(
            state=weakref.ref(state),
            settings=settings,
            state_path=state_path,
            key=None,
        )
        _snapshot.update(at=0.0, rows=None, state=None)


def uninstall() -> None:
    with _lock:
        _installed.clear()
        _snapshot.update(at=0.0, rows=None, state=None)


def invalidate() -> None:
    """Forget the cached table: this process just changed it."""

    with _lock:
        _snapshot.update(at=0.0, rows=None, state=None)


def installed_state() -> Any | None:
    ref = _installed.get("state")
    return ref() if ref is not None else None


def key(*, create: bool = False) -> vault.VaultKey:
    """This process's sealing key (``vault.resolve_key``), cached once found."""

    with _lock:
        cached = _installed.get("key")
        settings = _installed.get("settings")
        state_path = _installed.get("state_path")
    if cached is not None:
        return cached
    found = vault.resolve_key(settings, state_path, create=create)
    with _lock:
        _installed["key"] = found
    return found


def _rows() -> list[vault.Connection]:
    state = installed_state()
    if state is None:
        return []
    settings = _installed.get("settings")
    ttl = float(getattr(settings, "cache_seconds", 15) if settings is not None else 15)
    now = time.monotonic()
    with _lock:
        if (
            _snapshot["rows"] is not None
            and _snapshot["state"] is state
            and now - _snapshot["at"] < ttl
        ):
            return list(_snapshot["rows"])
    try:
        rows = [row for row in vault.all_rows(state) if row.status == "connected"]
    except Exception:  # a closed or pre-migration store must never break a key lookup
        return []
    with _lock:
        _snapshot.update(at=now, rows=rows, state=state)
    return list(rows)


def connection(name: str) -> vault.Connection | None:
    return next((row for row in _rows() if row.name == name), None)


def opened(row: vault.Connection) -> dict[str, Any]:
    """The row's sealed secret, or :class:`ConnectionUnavailable` naming why not."""

    try:
        return vault.unseal(key(), row)
    except vault.VaultUnavailable as exc:
        raise ConnectionUnavailable(str(exc)) from exc


def token(row: vault.Connection, fields: dict[str, Any]) -> str | None:
    """The token ``row`` reads with now: a sign-in's live access token (refreshed,
    and its rotated refresh token written back), a Drive service account's, or
    the pasted token or key itself."""

    from pheasant.connections import google

    try:
        if providers.signed_in(fields):
            entry = providers.BY_ID.get(row.provider)
            method = entry.sign_in() if entry else None
            if method is None or method.oauth is None:
                return str(fields.get("access_token") or "") or None
            return oauth.access_token_for(
                method.oauth, fields, persist=lambda renewed: _persist(row, renewed)
            )
        if row.provider == "google_drive":
            return google.access_token_for(fields)
    except oauth.OAuthError as exc:
        raise ConnectionUnavailable(f"connection {row.name!r}: {exc}") from exc
    return providers.secret_value(row.provider, fields)


def _persist(row: vault.Connection, renewed: dict[str, Any]) -> None:
    """Seal a refreshed sign-in over the row, so the next process reads the new one."""

    state = installed_state()
    if state is None:
        return
    vault.put(
        state,
        key=key(),
        name=row.name,
        provider=row.provider,
        method=row.method,
        secret=renewed,
        details=row.details,
        now=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        created_by=row.created_by,
        check=row.check,
    )
    invalidate()


def is_bearer(row: vault.Connection) -> bool:
    """Whether git signs this connection's remote with a bearer header (Entra)."""

    entry = providers.BY_ID.get(row.provider)
    method = entry.sign_in() if entry else None
    return bool(row.method == "oauth" and method and method.oauth and method.oauth.git_bearer)


def secret(name: str | None) -> str | None:
    """``$name``, else the connected account that stands in for it."""

    if not name:
        return None
    value = os.environ.get(name, "").strip()
    if value:
        return value
    for row in _bound(name):
        try:
            found = token(row, opened(row))
        except ConnectionUnavailable:
            continue
        if found:
            return found
    return None


def _bound(name: str) -> list[vault.Connection]:
    """Connections answering for ``$name``, the one named after the provider first."""

    rows = [row for row in _rows() if providers.bound_env(row.provider, row.details) == name]
    return sorted(rows, key=lambda row: (row.name != row.provider, row.created_at))


def environment() -> dict[str, str]:
    """``os.environ``, with each unset variable a connection stands in for filled."""

    env = dict(os.environ)
    names = {
        bound
        for row in _rows()
        if (bound := providers.bound_env(row.provider, row.details))
        and not env.get(bound, "").strip()
    }
    for name in names:
        value = secret(name)
        if value:
            env[name] = value
    return env


def ambient(provider_id: str) -> vault.Connection | None:
    """The single connection of ``provider_id``, or ``None`` with none or several."""

    rows = [row for row in _rows() if row.provider == provider_id]
    return rows[0] if len(rows) == 1 else None


def named(
    name: str, *, providers_allowed: tuple[str, ...] | None = None
) -> tuple[vault.Connection, dict[str, Any]]:
    """The connection a source names, opened; refuses one it cannot use."""

    row = connection(name)
    if row is None:
        raise ConnectionUnavailable(
            f"no connection named {name!r} on this region: connect it from the Sources "
            "page, MCP connect_service or POST /connections"
        )
    if providers_allowed and row.provider not in providers_allowed:
        allowed = ", ".join(providers_allowed)
        raise ConnectionUnavailable(
            f"connection {name!r} is a {row.provider} connection; this source reads with {allowed}"
        )
    return row, opened(row)


def source_connection(source: Any) -> str | None:
    connector = getattr(source, "connector", None)
    value = getattr(connector, "connection", None) if connector is not None else None
    return str(value).strip() or None if value else None


def request_headers(source: Any) -> dict[str, str]:
    """A source's connector headers, plus its connection's bearer token if it names one."""

    connector = getattr(source, "connector", None)
    headers = dict(getattr(connector, "headers", None) or {})
    name = source_connection(source)
    if not name:
        return headers
    row, secret_fields = named(name)
    value = token(row, secret_fields)
    if not value:
        return headers
    header = str(secret_fields.get("header") or "").strip() or "Authorization"
    headers[header] = f"Bearer {value}" if header.lower() == "authorization" else value
    return headers
