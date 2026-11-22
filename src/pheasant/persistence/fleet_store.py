"""The fleet directory: slots, who administers each, and capacity requests.

A second, small database every stack of a fleet can reach
(``config/fleet.py``). Free functions over a store, like ``auth_tokens.py``.
Three tables, plus the usage ledger's own (``usage_daily``), which each stack
mirrors its daily totals into so the operator reads the whole fleet at once:

``fleet_slots``     one row per stack. ``status`` is ``free`` or ``claimed``;
                    a claim sets the knowledge base's name and its admins.
                    Each stack keeps its own row's address, version and size
                    current (a registration at boot, a heartbeat after).
``fleet_requests``  somebody asking the operator for more slots or more room.
                    A record of an ask and its answer; nothing here provisions.

A claim is one conditional ``UPDATE``: its outer ``WHERE status='free'`` is
the predicate the winner's own write falsifies, so two admins claiming the
last slot at once cannot both get it (CLAUDE.md, Postgres READ COMMITTED).

The store is opened once per process and never migrated with the region's
schema: a fleet database holds these tables and nothing of any knowledge base.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import UTC, datetime
from typing import Any

from pheasant.persistence.usage_ledger import USAGE_SCHEMA

FREE, CLAIMED = "free", "claimed"
OPEN, APPROVED, DECLINED, FULFILLED = "open", "approved", "declined", "fulfilled"
RESOLUTIONS = (APPROVED, DECLINED, FULFILLED)
#: How long a process trusts its copy of its own slot's admins.
SNAPSHOT_SECONDS = 15.0

FLEET_SCHEMA = (
    """\
CREATE TABLE IF NOT EXISTS fleet_slots (
  slot TEXT PRIMARY KEY,
  kb_id TEXT NOT NULL,
  url TEXT,
  status TEXT NOT NULL,
  name TEXT,
  description TEXT,
  admins_json TEXT,
  claimed_by TEXT,
  claimed_at TEXT,
  registered_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  version TEXT,
  documents INTEGER,
  indexed_bytes INTEGER,
  limit_gb REAL
);
CREATE TABLE IF NOT EXISTS fleet_requests (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  slot TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  tenant_id TEXT,
  quantity INTEGER NOT NULL,
  reason TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by TEXT,
  note TEXT
);
"""
    + USAGE_SCHEMA
)

_SLOT_COLUMNS = (
    "slot,kb_id,url,status,name,description,admins_json,claimed_by,claimed_at,"
    "registered_at,heartbeat_at,version,documents,indexed_bytes,limit_gb"
)
_REQUEST_COLUMNS = (
    "id,kind,slot,requested_by,tenant_id,quantity,reason,status,created_at,"
    "resolved_at,resolved_by,note"
)
_LOCK = threading.Lock()
_STORES: dict[str, Any] = {}
_ADMINS: dict[tuple[int, str], tuple[float, list[dict[str, Any]]]] = {}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def open_store(config: Any) -> Any | None:
    """The fleet directory for this config, or ``None`` when it has none.

    Opened once per process and target. A directory that cannot be reached is
    ``None`` too, with nothing cached, so the next caller tries again: a stack
    must come up and serve its own knowledge base while the directory is down.
    """

    fleet = getattr(config, "fleet", None)
    if fleet is None or not fleet.enabled or not str(fleet.slot).strip():
        return None
    dsn = os.environ.get(str(fleet.dsn_env or ""), "").strip()
    target = dsn
    if not target:
        return None
    with _LOCK:
        store = _STORES.get(target)
        if store is not None:
            return store
        from pheasant.persistence.backends import open_backend
        from pheasant.persistence.state_store import StateStore

        backend = open_backend(dsn, pool_size=2)
        store = StateStore(backend=backend)
        store.backend.executescript(FLEET_SCHEMA)
        # The directory's mirror of every stack's usage is append-only too.
        from pheasant.persistence import usage_ledger

        usage_ledger.protect(store)
        _STORES[target] = store
        return store


def close_all() -> None:
    """For tests and shutdown: drop every open directory."""

    with _LOCK:
        for store in _STORES.values():
            try:
                store.close()
            except Exception:  # noqa: BLE001, S110 - closing is best effort
                pass
        _STORES.clear()
        _ADMINS.clear()


def _slot(row: Any) -> dict[str, Any]:
    record = dict(row)
    record["admins"] = json.loads(record.pop("admins_json", None) or "[]")
    return record


def register(  # noqa: PLR0913 - one argument per registered fact
    store: Any,
    *,
    slot: str,
    kb_id: str,
    url: str,
    claimed: bool,
    name: str | None,
    version: str | None,
    limit_gb: float | None,
) -> None:
    """Make this stack's row exist and keep its address and version current.

    Never changes a row's ``status``, name or admins once it exists: those
    belong to whoever claimed it.
    """

    now = _now()
    store.execute(
        f"INSERT INTO fleet_slots({_SLOT_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(slot) DO UPDATE SET kb_id=excluded.kb_id, url=excluded.url, "
        "heartbeat_at=excluded.heartbeat_at, version=excluded.version, "
        "limit_gb=excluded.limit_gb",
        (
            slot,
            kb_id,
            url,
            CLAIMED if claimed else FREE,
            name if claimed else None,
            None,
            "[]",
            "operator" if claimed else None,
            now if claimed else None,
            now,
            now,
            version,
            None,
            None,
            limit_gb,
        ),
    )


def heartbeat(store: Any, slot: str, *, documents: int, indexed_bytes: int) -> None:
    store.execute(
        "UPDATE fleet_slots SET heartbeat_at=?, documents=?, indexed_bytes=? WHERE slot=?",
        (_now(), documents, indexed_bytes, slot),
    )


def slots(store: Any) -> list[dict[str, Any]]:
    return [
        _slot(row) for row in store.rows(f"SELECT {_SLOT_COLUMNS} FROM fleet_slots ORDER BY slot")
    ]


def get_slot(store: Any, slot: str) -> dict[str, Any] | None:
    found = store.rows(f"SELECT {_SLOT_COLUMNS} FROM fleet_slots WHERE slot=?", (slot,))
    return _slot(found[0]) if found else None


def claim(
    store: Any, *, name: str, description: str, admins: list[dict[str, Any]], claimed_by: str
) -> dict[str, Any] | None:
    """Give the lowest free slot a name and its admins; ``None`` when none is free."""

    for _attempt in range(8):
        free = store.rows(
            "SELECT slot FROM fleet_slots WHERE status=? ORDER BY slot LIMIT 1", (FREE,)
        )
        if not free:
            return None
        won = store.execute_returning(
            "UPDATE fleet_slots SET status=?, name=?, description=?, admins_json=?, "
            "claimed_by=?, claimed_at=? WHERE slot=? AND status=? RETURNING slot",
            (
                CLAIMED,
                name,
                description,
                json.dumps(admins),
                claimed_by,
                _now(),
                free[0]["slot"],
                FREE,
            ),
        )
        if won:
            with _LOCK:
                _ADMINS.clear()
            return get_slot(store, str(free[0]["slot"]))
    return None


def set_admins(store: Any, slot: str, admins: list[dict[str, Any]]) -> None:
    store.execute("UPDATE fleet_slots SET admins_json=? WHERE slot=?", (json.dumps(admins), slot))
    with _LOCK:
        _ADMINS.clear()


def admins(store: Any, slot: str) -> list[dict[str, Any]]:
    """Who a claim made Admin of ``slot``, held per process for a few seconds."""

    key, now = (id(store), slot), time.monotonic()
    with _LOCK:
        cached = _ADMINS.get(key)
    if cached and cached[0] > now:
        return cached[1]
    row = get_slot(store, slot)
    found = list(row["admins"]) if row else []
    with _LOCK:
        _ADMINS[key] = (now + SNAPSHOT_SECONDS, found)
    return found


def slot_admins(config: Any) -> list[dict[str, Any]]:
    """The admins a claim gave *this* stack; empty with no fleet, or it unreachable."""

    fleet = getattr(config, "fleet", None)
    if fleet is None or not fleet.enabled:
        return []
    try:
        store = open_store(config)
        return admins(store, str(fleet.slot)) if store is not None else []
    except Exception:  # noqa: BLE001 - a directory that is down must not sign anyone out wrongly
        return []


def admin_tenants(config: Any) -> set[str]:
    """The Entra tenants this stack's claimed admins sign in from."""

    return {str(a.get("tenant_id") or "") for a in slot_admins(config)} - {""}


def add_request(  # noqa: PLR0913 - one argument per column
    store: Any,
    *,
    kind: str,
    slot: str,
    requested_by: str,
    tenant_id: str,
    quantity: int,
    reason: str,
) -> dict[str, Any]:
    identifier = f"fr_{secrets.token_hex(6)}"
    store.execute(
        f"INSERT INTO fleet_requests({_REQUEST_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (identifier, kind, slot, requested_by, tenant_id, quantity, reason, OPEN, _now())
        + (None, None, None),
    )
    return get_request(store, identifier) or {}


def get_request(store: Any, identifier: str) -> dict[str, Any] | None:
    found = store.rows(f"SELECT {_REQUEST_COLUMNS} FROM fleet_requests WHERE id=?", (identifier,))
    return dict(found[0]) if found else None


def requests(store: Any) -> list[dict[str, Any]]:
    found = store.rows(
        f"SELECT {_REQUEST_COLUMNS} FROM fleet_requests ORDER BY created_at DESC, id"
    )
    return [dict(row) for row in found]


def resolve_request(
    store: Any, identifier: str, *, status: str, resolved_by: str, note: str
) -> dict[str, Any] | None:
    store.execute(
        "UPDATE fleet_requests SET status=?, resolved_at=?, resolved_by=?, note=? WHERE id=?",
        (status, _now(), resolved_by, note, identifier),
    )
    return get_request(store, identifier)
