"""The ``connections`` table: one sealed row per connected account.

A row's secret fields (a token, a refresh token, a service account's private
key) are one JSON object sealed with AES-256-GCM; everything a list may show
(provider, account, a four-character hint, when it was checked) is plain
``details_json``. The associated data is the row's name and provider, so a
sealed blob copied onto another row does not open.

The key is never stored beside the rows. It is the value of the variable
``security.connections.key_env`` names (any string: it is hashed to 32
bytes), or ``<state_path>/credential.key``, which a process whose ``/state``
is writable generates once when the variable is unset. A process holding
neither opens nothing and every operation says why
(:class:`VaultUnavailable`); a row sealed under another key says that too,
rather than reading as absent.

Free functions over a state store, like ``persistence/archive.py``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KEY_FILE = "credential.key"
SEAL_PREFIX = "v1."
STATUSES = ("connected", "pending")
#: Rows a browser sign-in holds open until the service redirects back.
PENDING_PREFIX = "__pending__:"


class VaultUnavailable(RuntimeError):
    """This process cannot seal or open connections, and the reason why."""


class SealedUnderAnotherKey(VaultUnavailable):
    """A row exists and this process's key does not open it."""


@dataclass(frozen=True)
class VaultKey:
    key: bytes
    #: Where it came from, for a status line: ``environment`` or ``file``.
    source: str

    @property
    def key_id(self) -> str:
        """A short digest naming the key, stored on each row it seals."""

        return hashlib.sha256(b"pheasant-connection-key:" + self.key).hexdigest()[:12]


@dataclass
class Connection:
    name: str
    provider: str
    method: str
    status: str
    details: dict[str, Any] = field(default_factory=dict)
    key_id: str | None = None
    sealed: str | None = None
    created_at: str = ""
    updated_at: str = ""
    created_by: str | None = None
    check: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        """Everything a caller may see: never a secret, never the sealed blob."""

        return {
            "name": self.name,
            "provider": self.provider,
            "method": self.method,
            "status": self.status,
            "details": dict(self.details),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_by": self.created_by,
            "check": dict(self.check) if self.check else None,
        }


def resolve_key(settings: Any, state_path: Path | str | None, *, create: bool) -> VaultKey:
    """This process's key, or :class:`VaultUnavailable` naming the remedy."""

    env_name = str(getattr(settings, "key_env", "") or "PHEASANT_CREDENTIAL_KEY")
    value = os.environ.get(env_name, "").strip()
    if value:
        return VaultKey(hashlib.sha256(value.encode("utf-8")).digest(), "environment")
    if state_path is not None:
        path = Path(state_path) / KEY_FILE
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return VaultKey(hashlib.sha256(text.encode("utf-8")).digest(), "file")
        if create and bool(getattr(settings, "generate_key_file", True)):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                token = secrets.token_hex(32)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(token + "\n")
                return VaultKey(hashlib.sha256(token.encode("utf-8")).digest(), "file")
            except FileExistsError:
                return resolve_key(settings, state_path, create=False)
            except OSError:
                pass
    raise VaultUnavailable(
        f"no key to seal connections with: set {env_name} on this process (any long "
        "random string; the api and the indexer need the same value), or run where "
        "/state is writable so one is generated"
    )


def _aad(name: str, provider: str) -> bytes:
    return f"pheasant-connection:{name}:{provider}".encode()


def seal(key: VaultKey, name: str, provider: str, secret: dict[str, Any]) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    body = AESGCM(key.key).encrypt(
        nonce, json.dumps(secret, sort_keys=True).encode("utf-8"), _aad(name, provider)
    )
    return SEAL_PREFIX + base64.urlsafe_b64encode(nonce + body).decode("ascii")


def unseal(key: VaultKey, connection: Connection) -> dict[str, Any]:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    blob = connection.sealed or ""
    if not blob.startswith(SEAL_PREFIX):
        raise VaultUnavailable(f"connection {connection.name!r} holds no sealed secret")
    raw = base64.urlsafe_b64decode(blob[len(SEAL_PREFIX) :].encode("ascii"))
    try:
        plain = AESGCM(key.key).decrypt(
            raw[:12], raw[12:], _aad(connection.name, connection.provider)
        )
    except InvalidTag as exc:
        raise SealedUnderAnotherKey(
            f"connection {connection.name!r} was sealed under another key "
            f"({connection.key_id or 'unknown'}; this process holds {key.key_id}): give "
            "this process the same PHEASANT_CREDENTIAL_KEY, or connect the account again"
        ) from exc
    return json.loads(plain.decode("utf-8"))


def _row(row: Any) -> Connection:
    return Connection(
        name=row["name"],
        provider=row["provider"],
        method=row["method"],
        status=row["status"],
        details=json.loads(row["details_json"] or "{}"),
        key_id=row["key_id"],
        sealed=row["sealed"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        created_by=row["created_by"],
        check=json.loads(row["check_json"]) if row["check_json"] else None,
    )


def put(
    state: Any,
    *,
    key: VaultKey,
    name: str,
    provider: str,
    method: str,
    secret: dict[str, Any],
    details: dict[str, Any],
    now: str,
    created_by: str | None = None,
    status: str = "connected",
    oauth_state: str | None = None,
    check: dict[str, Any] | None = None,
) -> Connection:
    """Create or replace one connection; a replacement keeps its first time."""

    if status not in STATUSES:
        raise ValueError(f"unknown connection status: {status}")
    sealed = seal(key, name, provider, secret)
    state.execute(
        """INSERT INTO connections(
            name, provider, method, status, sealed, key_id, details_json, oauth_state,
            created_at, updated_at, created_by, check_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            provider=excluded.provider, method=excluded.method, status=excluded.status,
            sealed=excluded.sealed, key_id=excluded.key_id,
            details_json=excluded.details_json, oauth_state=excluded.oauth_state,
            updated_at=excluded.updated_at, check_json=excluded.check_json""",
        (
            name,
            provider,
            method,
            status,
            sealed,
            key.key_id,
            json.dumps(details, sort_keys=True),
            oauth_state,
            now,
            now,
            created_by,
            json.dumps(check, sort_keys=True) if check else None,
        ),
    )
    found = get(state, name)
    assert found is not None
    return found


def record_check(state: Any, name: str, check: dict[str, Any], details: dict | None) -> None:
    if details is None:
        state.execute(
            "UPDATE connections SET check_json=? WHERE name=?",
            (json.dumps(check, sort_keys=True), name),
        )
        return
    state.execute(
        "UPDATE connections SET check_json=?, details_json=? WHERE name=?",
        (json.dumps(check, sort_keys=True), json.dumps(details, sort_keys=True), name),
    )


def get(state: Any, name: str) -> Connection | None:
    rows = state.rows("SELECT * FROM connections WHERE name=?", (name,))
    return _row(rows[0]) if rows else None


def by_oauth_state(state: Any, digest: str) -> Connection | None:
    rows = state.rows("SELECT * FROM connections WHERE oauth_state=?", (digest,))
    return _row(rows[0]) if rows else None


def all_rows(state: Any) -> list[Connection]:
    return [_row(row) for row in state.rows("SELECT * FROM connections ORDER BY name")]


def delete(state: Any, name: str) -> bool:
    if get(state, name) is None:
        return False
    state.execute("DELETE FROM connections WHERE name=?", (name,))
    return True


def delete_expired_pending(state: Any, before: str) -> None:
    state.execute(
        "DELETE FROM connections WHERE status='pending' AND updated_at < ?",
        (before,),
    )
