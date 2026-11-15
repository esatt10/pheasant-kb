"""Several knowledge bases on one operator's resources: claiming, asking, overseeing.

One implementation behind ``/fleet*`` and MCP ``list_fleet`` /
``create_knowledge_base`` / ``request_capacity`` / ``resolve_capacity_request``
(rule 13). The directory itself is ``persistence/fleet_store.py``.

**Who does what, and what each costs.**

* The *operator* provisions slots: stacks with nothing in them. That is a
  deployment and a cost, and nothing in this module can cause it.
* An *Admin* of any knowledge base in the fleet claims a free slot for a new
  one. That is one row changing: the slot gets a name, and the claimer (plus
  anyone they name, from their own organization) becomes its Admin. The new
  knowledge base starts with no Contributors and no Readers of its own; those
  are granted inside it (``services/access.py``), so two knowledge bases of
  one group share admins and nothing else.
* When no slot is free, or one is filling up, an Admin files a **capacity
  request**. It is a record of an ask. The operator answers it (approve,
  decline, fulfilled) after deciding whether to provision; the answer is a
  record too.

A slot is never released from here. Giving one back means its database and
files must be wiped first, which is the operator's to do, deliberately.

What an Admin sees: the knowledge bases they administer, and how many slots
are free. What the operator sees as well: every slot, and each one's usage
over the last thirty days (``usage_daily``, which every stack mirrors into the
directory), with cost at the operator's stated prices.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pheasant.persistence import fleet_store
from pheasant.security.authorization import require_permission
from pheasant.security.identity import current_identity
from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest, ServiceError
from pheasant.telemetry import usage as metering
from pheasant.version import __version__

logger = logging.getLogger("pheasant.fleet")

SLOTS, STORAGE = "slots", "storage"
USAGE_DAYS = 30
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class FleetNotEnabled(ServiceError):
    status = 404
    code = "FLEET_NOT_ENABLED"

    def __init__(self) -> None:
        super().__init__("This region is not part of a fleet of knowledge bases (fleet.enabled)")


class FleetUnavailable(ServiceError):
    status = 503
    code = "FLEET_UNAVAILABLE"
    retryable = True

    def __init__(self) -> None:
        super().__init__("The fleet directory cannot be reached right now; retry shortly")


class FleetNoCapacity(ServiceError):
    """Every provisioned slot is in use. Not retryable: an operator must add one."""

    status = 409
    code = "FLEET_NO_CAPACITY"

    def __init__(self) -> None:
        super().__init__(
            "No knowledge-base slot is free. File a capacity request (request_capacity, "
            "POST /fleet/requests) and the operator will decide whether to provision more."
        )


class FleetOperatorRequired(ServiceError):
    status = 403
    code = "FLEET_OPERATOR_REQUIRED"

    def __init__(self) -> None:
        super().__init__("Only the region's operator answers a capacity request")


def _store(context: ServiceContext) -> Any:
    fleet = getattr(context.config, "fleet", None)
    if fleet is None or not fleet.enabled or not str(fleet.slot).strip():
        raise FleetNotEnabled()
    try:
        store = fleet_store.open_store(context.config)
    except Exception as exc:  # noqa: BLE001 - whatever it was, the caller should retry
        logger.warning("fleet directory unavailable: %s", exc)
        raise FleetUnavailable() from exc
    if store is None:
        raise FleetUnavailable()
    return store


def register(config: Any, state: Any = None) -> bool:
    """Put this stack in the directory (at boot). Never raises; ``False`` if it could not."""

    fleet = getattr(config, "fleet", None)
    if fleet is None or not fleet.enabled:
        return False
    try:
        store = fleet_store.open_store(config)
        if store is None:
            return False
        limit = getattr(config.storage, "max_state_size_gb", None)
        fleet_store.register(
            store,
            slot=str(fleet.slot),
            kb_id=config.knowledge_base_id,
            url=str(fleet.url or config.security.entra.public_url or ""),
            claimed=bool(fleet.claimed),
            name=config.pheasant.description or config.pheasant.name,
            version=__version__,
            limit_gb=float(limit) if limit else None,
        )
        if state is not None:
            _beat(store, config, state)
        return True
    except Exception:  # noqa: BLE001 - a stack serves its own knowledge base regardless
        logger.warning("could not register in the fleet directory", exc_info=True)
        return False


def _beat(store: Any, config: Any, state: Any) -> None:
    def one(sql: str) -> int:
        try:
            rows = state.rows(sql)
            return int(next(iter(dict(rows[0]).values())) or 0) if rows else 0
        except Exception:  # noqa: BLE001 - a fresh stack has no tables yet
            return 0

    fleet_store.heartbeat(
        store,
        str(config.fleet.slot),
        documents=one("SELECT COUNT(*) FROM artifacts"),
        indexed_bytes=one("SELECT COALESCE(SUM(size_bytes), 0) FROM artifacts"),
    )


def _caller(config: Any) -> dict[str, str]:
    identity = current_identity()
    person = identity is not None and identity.is_person
    return {
        "principal": identity.principal if identity is not None else "operator",
        "subject": identity.subject if person else "",
        "tenant_id": (identity.tenant_id if person else "") or config.security.entra.tenant_id,
    }


def _administers(slot: dict[str, Any], caller: dict[str, str]) -> bool:
    return any(
        admin.get("tenant_id") == caller["tenant_id"]
        and (
            (admin.get("subject") and admin["subject"] == caller["subject"])
            or admin.get("email") == caller["principal"]
        )
        for admin in slot["admins"]
    )


def _slot_usage(store: Any, config: Any, operator: bool) -> dict[str, dict[str, Any]]:
    """Thirty days of each knowledge base's totals, from the mirrored ledger."""

    from pheasant.services.usage import _cost, _latency, _prices

    today = datetime.now(UTC).date()
    first = (today - timedelta(days=USAGE_DAYS - 1)).isoformat()
    prices = _prices(config) if operator else {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in store.rows(
        "SELECT * FROM usage_daily WHERE day>=? AND day<=?", (first, today.isoformat())
    ):
        grouped.setdefault(str(row["kb_id"]), []).append(dict(row))
    summary: dict[str, dict[str, Any]] = {}
    for kb_id, rows in grouped.items():
        answers = [r for r in rows if r["kind"] == metering.ANSWER]
        models = [r for r in rows if r["kind"] in metering.MODEL_KINDS]
        active = {
            (r["tenant_id"], r["principal"])
            for r in rows
            if r["principal"] and r["kind"] in {metering.ANSWER, metering.SEARCH}
        }
        summary[kb_id] = {
            "days": USAGE_DAYS,
            "questions": sum(int(r["requests"]) for r in answers),
            "searches": sum(int(r["requests"]) for r in rows if r["kind"] == metering.SEARCH),
            "active_people": len(active),
            "tokens": sum(
                int(r["input_tokens"]) + int(r["output_tokens"]) + int(r["cached_input_tokens"])
                for r in models
            ),
            "cost": round(sum(_cost(r, prices) or 0.0 for r in models), 4) if operator else None,
            "answer_p95_ms": _latency(answers)["p95_ms"],
            "last_active": max((r["day"] for r in rows), default=None),
        }
    return summary


def describe(context: ServiceContext) -> dict[str, Any]:
    """The fleet as the caller may see it: knowledge bases, capacity, requests."""

    require_permission("security.manage")
    from pheasant.services.usage import BYTES_PER_GB, is_operator

    config = context.config
    store = _store(context)
    _beat(store, config, context.state)
    metering.METER.flush()
    operator = is_operator(config)
    caller = _caller(config)
    mine = str(config.fleet.slot)
    everything = fleet_store.slots(store)
    usage = _slot_usage(store, config, operator) if operator else {}
    listed = []
    for slot in everything:
        claimed = slot["status"] == fleet_store.CLAIMED
        if not operator and not (claimed and (slot["slot"] == mine or _administers(slot, caller))):
            continue
        used = slot.get("indexed_bytes")
        ceiling_gb = slot.get("limit_gb")
        listed.append(
            {
                "slot": slot["slot"],
                "status": slot["status"],
                "current": slot["slot"] == mine,
                "name": slot.get("name"),
                "description": slot.get("description"),
                "url": slot.get("url"),
                "claimed_by": slot.get("claimed_by"),
                "claimed_at": slot.get("claimed_at"),
                "admins": slot["admins"],
                "version": slot.get("version"),
                "heartbeat_at": slot.get("heartbeat_at"),
                "size": {
                    "documents": slot.get("documents"),
                    "indexed_bytes": used,
                    "limit_gb": ceiling_gb,
                    "limit_used": round(used / (ceiling_gb * BYTES_PER_GB), 4)
                    if used and ceiling_gb
                    else None,
                },
                "usage": usage.get(str(slot["kb_id"])) if operator else None,
            }
        )
    free = sum(1 for slot in everything if slot["status"] == fleet_store.FREE)
    requests = fleet_store.requests(store)
    return {
        "slot": mine,
        "operator": operator,
        "currency": config.models.currency if operator else None,
        # Slots exist because the operator provisioned them. Nothing here adds one.
        "capacity": {"slots": len(everything), "claimed": len(everything) - free, "free": free},
        "knowledge_bases": listed,
        "requests": [
            request
            for request in requests
            if operator
            or (
                request["requested_by"] == caller["principal"]
                and (request.get("tenant_id") or "") == caller["tenant_id"]
            )
        ],
        "can_create": free > 0,
        "can_resolve": operator,
    }


def create_knowledge_base(
    context: ServiceContext,
    *,
    name: str,
    description: str = "",
    admins: list[str] | None = None,
) -> dict[str, Any]:
    """Claim a free slot for a new knowledge base; the caller becomes its Admin."""

    require_permission("security.manage")
    config = context.config
    store = _store(context)
    name = " ".join(str(name or "").split())
    if not 2 <= len(name) <= 80:
        raise InvalidRequest("a knowledge base needs a name of 2 to 80 characters")
    caller = _caller(config)
    people = [
        {
            "tenant_id": caller["tenant_id"],
            "email": caller["principal"],
            "subject": caller["subject"],
        }
    ]
    if not current_identity() or not current_identity().is_person:
        people = []  # a shared credential names no one; admins must be given
    for email in admins or []:
        email = str(email or "").strip().lower()
        if not _EMAIL.match(email):
            raise InvalidRequest(f"{email!r} is not a sign-in name (name@organization)")
        if all(person["email"] != email for person in people):
            people.append({"tenant_id": caller["tenant_id"], "email": email, "subject": ""})
    if not people:
        raise InvalidRequest("name at least one admin (admins) for the new knowledge base")
    if any(
        slot["status"] == fleet_store.CLAIMED and (slot.get("name") or "").lower() == name.lower()
        for slot in fleet_store.slots(store)
    ):
        raise InvalidRequest(f"a knowledge base named {name!r} already exists in this fleet")
    claimed = fleet_store.claim(
        store,
        name=name,
        description=" ".join(str(description or "").split())[:500],
        admins=people,
        claimed_by=caller["principal"],
    )
    if claimed is None:
        raise FleetNoCapacity()
    logger.info(
        "knowledge base created: slot=%s name=%s by=%s admins=%d",
        claimed["slot"],
        name,
        caller["principal"],
        len(people),
    )
    url = str(claimed.get("url") or "").rstrip("/")
    home = config.security.entra.tenant_id
    return {
        "status": "created",
        "knowledge_base": {
            "slot": claimed["slot"],
            "name": claimed["name"],
            "url": url or None,
            "admins": claimed["admins"],
        },
        # A stack with nobody using it is asleep: its first page takes a minute.
        "sign_in_url": (
            f"{url}/auth/login"
            + ("" if caller["tenant_id"] == home else f"?org={caller['tenant_id']}")
            if url
            else None
        ),
        "slots_free": sum(
            1 for slot in fleet_store.slots(store) if slot["status"] == fleet_store.FREE
        ),
    }


def request_capacity(
    context: ServiceContext, *, quantity: int = 1, reason: str = "", kind: str = SLOTS
) -> dict[str, Any]:
    """Ask the operator for more: knowledge-base slots, or room in this one."""

    require_permission("security.manage")
    config = context.config
    store = _store(context)
    kind = str(kind or SLOTS).strip().lower()
    if kind not in {SLOTS, STORAGE}:
        raise InvalidRequest("kind is 'slots' (more knowledge bases) or 'storage' (gigabytes here)")
    try:
        quantity = int(quantity)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(f"quantity must be a whole number; got {quantity!r}") from exc
    if not 1 <= quantity <= 500:
        raise InvalidRequest("quantity must be between 1 and 500")
    caller = _caller(config)
    request = fleet_store.add_request(
        store,
        kind=kind,
        slot=str(config.fleet.slot),
        requested_by=caller["principal"],
        tenant_id=caller["tenant_id"],
        quantity=quantity,
        reason=" ".join(str(reason or "").split())[:1000],
    )
    logger.info(
        "capacity requested: id=%s kind=%s quantity=%d by=%s slot=%s",
        request["id"],
        kind,
        quantity,
        caller["principal"],
        config.fleet.slot,
    )
    return {"status": "filed", "request": request}


def resolve_request(
    context: ServiceContext, request_id: str, *, status: str, note: str = ""
) -> dict[str, Any]:
    """Record the operator's answer to a capacity request. Provisions nothing."""

    require_permission("security.manage")
    from pheasant.services.usage import is_operator

    config = context.config
    store = _store(context)
    if not is_operator(config):
        raise FleetOperatorRequired()
    status = str(status or "").strip().lower()
    if status not in fleet_store.RESOLUTIONS:
        raise InvalidRequest(f"status is one of {', '.join(fleet_store.RESOLUTIONS)}")
    if fleet_store.get_request(store, request_id) is None:
        raise InvalidRequest(f"no capacity request {request_id}")
    resolved = fleet_store.resolve_request(
        store,
        request_id,
        status=status,
        resolved_by=_caller(config)["principal"],
        note=" ".join(str(note or "").split())[:1000],
    )
    logger.info("capacity request %s: %s", request_id, status)
    return {"status": status, "request": resolved}
