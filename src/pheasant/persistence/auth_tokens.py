"""Credentials the region issued, as rows.

Free functions over a state store, the shape `persistence/receipts.py` uses:
one bounded context with a handful of operations and no state of its own.

A credential is ``pht_<id>_<secret>``. The id is stored as it is and is what a
presented credential is looked up by; the secret is stored only as a SHA-256
digest, so a copy of this table opens nothing. Kinds:

``session``        a browser sign-in (``identity.ENTRA_SSO``)
``personal``       a key a signed-in user minted (``identity.PERSONAL_KEY``)
``oauth_access``   an MCP client's access token (``identity.MCP_OAUTH``)
``oauth_refresh``  its refresh token
``oauth_code``     a one-use authorization code handed to an MCP client
``login``          the state of a round trip to Entra, ten minutes at most
``consent``        a signed-in user deciding whether an MCP client may connect

Rows live in the state store rather than in a process so every api replica
honours what any of them issued -- and a revocation everywhere.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

PREFIX = "pht_"

_COLUMNS = (
    "id,kind,secret_hash,principal,display_name,subject,roles_json,name,client_id,"
    "data_json,created_at,expires_at,revoked_at,last_used_at"
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(instant: datetime | None) -> str | None:
    return instant.isoformat() if instant else None


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def split(credential: str) -> tuple[str, str] | None:
    """``(id, secret)`` of a credential this module minted, else ``None``."""

    if not credential.startswith(PREFIX):
        return None
    token_id, sep, secret = credential[len(PREFIX) :].partition("_")
    if not sep or not token_id or not secret:
        return None
    return token_id, secret


def issue(
    state: Any,
    *,
    kind: str,
    lifetime: timedelta,
    principal: str | None = None,
    display_name: str | None = None,
    subject: str | None = None,
    roles: list[str] | tuple[str, ...] = (),
    name: str | None = None,
    client_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Store a new credential and return ``(credential, row)``.

    The credential is returned once and never again: only its digest is kept.
    """

    token_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    now = _now()
    row = {
        "id": token_id,
        "kind": kind,
        "secret_hash": _digest(secret),
        "principal": principal,
        "display_name": display_name,
        "subject": subject,
        "roles_json": json.dumps(sorted(roles)),
        "name": name,
        "client_id": client_id,
        "data_json": json.dumps(data or {}, sort_keys=True),
        "created_at": _iso(now),
        "expires_at": _iso(now + lifetime),
        "revoked_at": None,
        "last_used_at": None,
    }
    state.execute(
        f"INSERT INTO auth_tokens({_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(row[c] for c in _COLUMNS.split(",")),
    )
    return f"{PREFIX}{token_id}_{secret}", row


def _decode(row: Any) -> dict[str, Any]:
    record = dict(row)
    record["roles"] = json.loads(record.get("roles_json") or "[]")
    record["data"] = json.loads(record.get("data_json") or "{}")
    return record


def lookup(state: Any, credential: str, *, kinds: tuple[str, ...]) -> dict[str, Any] | None:
    """The live row a credential names, or ``None``.

    Live means: one of ``kinds``, the secret's digest matches, not revoked and
    not expired. Any failure is the same ``None`` -- a caller must not learn
    which check it failed.
    """

    parts = split(credential)
    if parts is None:
        return None
    token_id, secret = parts
    rows = state.rows(f"SELECT {_COLUMNS} FROM auth_tokens WHERE id=?", (token_id,))
    if not rows:
        return None
    record = _decode(rows[0])
    if record["kind"] not in kinds or record.get("revoked_at"):
        return None
    if not hmac.compare_digest(record["secret_hash"], _digest(secret)):
        return None
    expires = record.get("expires_at")
    if expires and datetime.fromisoformat(expires) <= _now():
        return None
    return record


def consume(state: Any, credential: str, *, kind: str) -> dict[str, Any] | None:
    """:func:`lookup` for a one-use credential, revoking it in the same step.

    The revoke is a conditional ``UPDATE`` whose ``WHERE`` the winner's own
    write falsifies, so two replicas redeeming one code cannot both succeed.
    """

    record = lookup(state, credential, kinds=(kind,))
    if record is None:
        return None
    won = state.execute_returning(
        "UPDATE auth_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL RETURNING id",
        (_iso(_now()), record["id"]),
    )
    return record if won else None


def touch(state: Any, token_id: str) -> None:
    state.execute("UPDATE auth_tokens SET last_used_at=? WHERE id=?", (_iso(_now()), token_id))


def revoke(state: Any, token_id: str, *, principal: str | None = None) -> bool:
    """Revoke one credential; with ``principal``, only if it is theirs."""

    sql = "UPDATE auth_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL"
    params: tuple[Any, ...] = (_iso(_now()), token_id)
    if principal is not None:
        sql += " AND principal=?"
        params += (principal,)
    return bool(state.execute_returning(sql + " RETURNING id", params))


def revoke_family(state: Any, family: str) -> None:
    """Revoke every OAuth token minted from one authorization (``data.family``)."""

    rows = state.rows(
        "SELECT id, data_json FROM auth_tokens WHERE kind IN ('oauth_access','oauth_refresh') "
        "AND revoked_at IS NULL",
    )
    for row in rows:
        if json.loads(row["data_json"] or "{}").get("family") == family:
            revoke(state, row["id"])


def list_for(state: Any, principal: str, *, kind: str) -> list[dict[str, Any]]:
    """A principal's credentials of one kind, newest first, without secrets."""

    rows = state.rows(
        f"SELECT {_COLUMNS} FROM auth_tokens WHERE principal=? AND kind=? ORDER BY created_at DESC",
        (principal, kind),
    )
    listed = []
    for row in rows:
        record = _decode(row)
        record.pop("secret_hash", None)
        listed.append(record)
    return listed


def purge_expired(state: Any, *, older_than: timedelta = timedelta(days=30)) -> None:
    """Drop rows that expired or were revoked more than ``older_than`` ago."""

    cutoff = _iso(_now() - older_than)
    state.execute(
        "DELETE FROM auth_tokens WHERE (expires_at IS NOT NULL AND expires_at < ?) "
        "OR (revoked_at IS NOT NULL AND revoked_at < ?)",
        (cutoff, cutoff),
    )


def save_client(state: Any, client_id: str, data: dict[str, Any]) -> None:
    state.execute(
        "INSERT INTO oauth_clients(client_id, data_json, created_at) VALUES(?,?,?) "
        "ON CONFLICT(client_id) DO UPDATE SET data_json=excluded.data_json",
        (client_id, json.dumps(data, sort_keys=True), _iso(_now())),
    )


def load_client(state: Any, client_id: str) -> dict[str, Any] | None:
    rows = state.rows("SELECT data_json FROM oauth_clients WHERE client_id=?", (client_id,))
    return json.loads(rows[0]["data_json"]) if rows else None
