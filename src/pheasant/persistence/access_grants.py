"""Access granted while the region runs, as rows.

Free functions over a state store, the shape ``auth_tokens.py`` uses. A row
is one of two kinds:

``user``  one person in one Entra tenant gets a role. Granted by sign-in name
          (``email``); the first sign-in binds the immutable object id
          (``subject``), after which a renamed account keeps its grant.
``org``   a customer's Entra tenant is *connected*: its members may sign in.
          ``role`` is what every member gets (``NULL``: nobody by default,
          members are granted one at a time). ``domains_json`` are the names a
          person may type at sign-in instead of the tenant id.

The id is a digest of what makes a grant the same grant, so granting twice is
an update and two replicas granting at once write one row.

Reads on the request path go through :func:`snapshot`: every row of one
knowledge base, held per process for :data:`SNAPSHOT_SECONDS`. A grant or a
revocation made in this process is seen at once (:func:`_bump`); another
replica sees it within the window, the same contract a revoked key has.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

USER = "user"
ORG = "org"
#: How long a process trusts its copy of the grants before reading them again.
SNAPSHOT_SECONDS = 15.0

ACCESS_GRANTS_SCHEMA = """\
-- Who may use this knowledge base, granted while it runs
-- (`persistence/access_grants.py`, `services/access.py`). `kind` is `user`
-- (one person, by sign-in name until their first sign-in binds `subject`) or
-- `org` (a customer's Entra tenant, connected by an Admin; `role` is then the
-- role every member gets, or NULL for "connected, members are granted singly").
CREATE TABLE IF NOT EXISTS access_grants (
  id TEXT PRIMARY KEY,
  kb_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subject TEXT,
  email TEXT,
  role TEXT,
  display_name TEXT,
  domains_json TEXT,
  granted_by TEXT NOT NULL,
  granted_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_access_grants_kb ON access_grants(kb_id, kind, tenant_id);
"""

_COLUMNS = (
    "id,kb_id,kind,tenant_id,subject,email,role,display_name,domains_json,"
    "granted_by,granted_at,updated_at"
)
_GENERATION = 0
_LOCK = threading.Lock()
_SNAPSHOTS: dict[tuple[int, str], tuple[int, float, Snapshot]] = {}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _bump() -> None:
    global _GENERATION
    with _LOCK:
        _GENERATION += 1


def grant_id(kb_id: str, kind: str, tenant_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{kb_id}\x00{kind}\x00{tenant_id}\x00{key}".encode()).hexdigest()
    return f"ag_{digest[:20]}"


def _decode(row: Any) -> dict[str, Any]:
    record = dict(row)
    record["domains"] = json.loads(record.pop("domains_json", None) or "[]")
    return record


def rows(state: Any, kb_id: str) -> list[dict[str, Any]]:
    found = state.rows(
        f"SELECT {_COLUMNS} FROM access_grants WHERE kb_id=? ORDER BY kind, tenant_id, email",
        (kb_id,),
    )
    return [_decode(row) for row in found]


def get(state: Any, kb_id: str, identifier: str) -> dict[str, Any] | None:
    found = state.rows(
        f"SELECT {_COLUMNS} FROM access_grants WHERE kb_id=? AND id=?", (kb_id, identifier)
    )
    return _decode(found[0]) if found else None


def upsert(
    state: Any,
    *,
    kb_id: str,
    kind: str,
    tenant_id: str,
    key: str,
    role: str | None,
    granted_by: str,
    subject: str | None = None,
    email: str | None = None,
    display_name: str | None = None,
    domains: list[str] | None = None,
) -> dict[str, Any]:
    """Write one grant; an existing one keeps who first granted it and when."""

    identifier = grant_id(kb_id, kind, tenant_id, key)
    now = _now()
    state.execute(
        f"INSERT INTO access_grants({_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET role=excluded.role, "
        "subject=COALESCE(excluded.subject, access_grants.subject), "
        "email=COALESCE(excluded.email, access_grants.email), "
        "display_name=COALESCE(excluded.display_name, access_grants.display_name), "
        "domains_json=excluded.domains_json, updated_at=excluded.updated_at",
        (
            identifier,
            kb_id,
            kind,
            tenant_id,
            subject,
            email,
            role,
            display_name,
            json.dumps(sorted(set(domains or []))),
            granted_by,
            now,
            now,
        ),
    )
    _bump()
    return get(state, kb_id, identifier) or {}


def bind_subject(state: Any, kb_id: str, identifier: str, subject: str, name: str) -> None:
    """Record the object id behind a grant made by sign-in name."""

    state.execute(
        "UPDATE access_grants SET subject=?, display_name=COALESCE(display_name, ?) "
        "WHERE kb_id=? AND id=? AND subject IS NULL",
        (subject, name or None, kb_id, identifier),
    )
    _bump()


def delete(state: Any, kb_id: str, identifier: str) -> bool:
    removed = state.execute_returning(
        "DELETE FROM access_grants WHERE kb_id=? AND id=? RETURNING id", (kb_id, identifier)
    )
    _bump()
    return bool(removed)


def delete_tenant_users(state: Any, kb_id: str, tenant_id: str) -> int:
    removed = state.execute_returning(
        "DELETE FROM access_grants WHERE kb_id=? AND kind=? AND tenant_id=? RETURNING id",
        (kb_id, USER, tenant_id),
    )
    _bump()
    return len(removed or [])


@dataclass(frozen=True)
class Snapshot:
    """Every grant of one knowledge base, indexed for the request path."""

    by_subject: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    by_email: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    orgs: dict[str, dict[str, Any]] = field(default_factory=dict)
    domains: dict[str, str] = field(default_factory=dict)

    def user(self, tenant_id: str, subject: str, email: str) -> dict[str, Any] | None:
        """The grant naming this person: by object id first, else by sign-in name.

        A grant already bound to an object id never matches a *different*
        person by name: a recycled address must not inherit a predecessor's
        access.
        """

        if subject:
            found = self.by_subject.get((tenant_id, subject))
            if found is not None:
                return found
        found = self.by_email.get((tenant_id, email.lower())) if email else None
        if found is not None and found.get("subject") and found["subject"] != subject:
            return None
        return found


def snapshot(state: Any, kb_id: str) -> Snapshot:
    key = (id(state), kb_id)
    now = time.monotonic()
    with _LOCK:
        cached = _SNAPSHOTS.get(key)
        generation = _GENERATION
    if cached and cached[0] == generation and cached[1] > now:
        return cached[2]
    by_subject: dict[tuple[str, str], dict[str, Any]] = {}
    by_email: dict[tuple[str, str], dict[str, Any]] = {}
    orgs: dict[str, dict[str, Any]] = {}
    domains: dict[str, str] = {}
    for row in rows(state, kb_id):
        tenant = str(row["tenant_id"])
        if row["kind"] == ORG:
            orgs[tenant] = row
            for domain in row["domains"]:
                domains[str(domain).lower()] = tenant
            continue
        if row.get("subject"):
            by_subject[(tenant, str(row["subject"]))] = row
        if row.get("email"):
            by_email[(tenant, str(row["email"]).lower())] = row
    built = Snapshot(by_subject, by_email, orgs, domains)
    with _LOCK:
        if len(_SNAPSHOTS) > 64:
            _SNAPSHOTS.clear()
        _SNAPSHOTS[key] = (generation, now + SNAPSHOT_SECONDS, built)
    return built
