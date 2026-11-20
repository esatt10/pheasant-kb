"""The operator's controls over one knowledge base: a kill switch and spend limits.

One implementation behind ``GET``/``PUT /region/controls`` and MCP
``get_region_controls`` / ``set_region_controls`` (rule 13). The mechanisms
are ``telemetry/spend.py``; this is who may see and change them.

* **Reading** is an Admin's (``security.manage``). The amounts are the
  operator's, like cost in ``services/usage.py``: an Admin a customer granted
  sees whether access is suspended and how much of the month's limit is used,
  never the figures.
* **Changing** is an *operator admin's*: the shared token, the local operator,
  or an Admin the deployment pinned (``security.entra.role_assignments``).
  Not an Admin granted inside the region, who could otherwise lift the limit
  that bounds what the operator pays, or lock out the people who run it.

The kill switch is enforced at the front door (``api/auth.py``); the limit at
every model call, at the start of an answer, and by a sync that would embed.

Deep research has two more, in the same row and behind the same rule: what one
deep answer may spend (``deep_answer_spend_limit``, default 5, read by the
research loop through ``telemetry/answer_spend.py``) and how many deep answers
one process runs at once (``max_concurrent_deep_answers``, default 2, read by
``services.assistant.acquire``). And answers of every kind have one: how many
one process runs at once (``max_concurrent_answers``, pheasant-kb's default 16,
read by ``services.answer_admission``). It is not an amount, so any Admin sees
it; only an operator admin changes it, because it is what bounds the model
calls one replica holds open.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, date, datetime
from typing import Any

from pheasant.security.authorization import require_permission
from pheasant.security.identity import current_identity
from pheasant.services import ServiceContext
from pheasant.services.answer_admission import (
    MAX_ANSWER_CONCURRENCY,
    configured_answer_limit,
    effective_answer_limit,
)
from pheasant.services.errors import (
    AccessSuspended,
    InvalidRequest,
    OperatorRequired,
    SpendLimitRefused,
)
from pheasant.telemetry import answer_spend, spend

logger = logging.getLogger(__name__)

#: The longest reason a suspension carries, shown to everyone it refuses.
MAX_REASON = 280
FIELDS = (
    "suspended",
    "reason",
    "monthly_spend_limit",
    "deep_answer_spend_limit",
    "max_concurrent_deep_answers",
    "max_concurrent_answers",
)


def _is_operator(context: ServiceContext) -> bool:
    from pheasant.services.usage import is_operator

    return is_operator(context.config)


def _next_month(today: date) -> str:
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return date(year, month, 1).isoformat()


def describe(context: ServiceContext) -> dict[str, Any]:
    """The kill switch and this month's spend against its limit."""

    require_permission("security.manage")
    config = context.config
    operator = _is_operator(context)
    saved = spend.read_controls(config, context.state)
    limit = spend.effective_limit(config, saved)
    metered = bool(config.observability.usage)
    month = spend.month_spend(config, context.state) if metered else None
    spent = month["spent"] if month else 0.0
    reached = bool(metered and limit is not None and spent >= limit)
    currency = getattr(getattr(config, "models", None), "currency", "USD") or "USD"
    today = datetime.now(UTC).date()
    note = None
    if not metered:
        note = (
            "Usage metering is off (observability.usage), so spend is not counted "
            "and the limit is not enforced."
        )
    elif limit is None:
        note = "No monthly limit: models.monthly_spend_limit is null and none was set here."
    return {
        "knowledge_base": config.knowledge_base_id,
        "can_change": operator,
        "access": {
            "suspended": bool(saved.get("suspended")),
            "reason": saved.get("reason"),
            "since": saved.get("suspended_at"),
            "by": saved.get("suspended_by") if operator else None,
        },
        "spend": {
            "month": today.isoformat()[:7],
            "resets_on": _next_month(today),
            "currency": currency if operator else None,
            "spent": round(spent, 2) if operator else None,
            "limit": limit if operator else None,
            "default_limit": spend.configured_limit(config) if operator else None,
            "custom_limit": "monthly_spend_limit" in saved,
            "remaining": (
                round(max(0.0, limit - spent), 2) if operator and limit is not None else None
            ),
            # A share, not an amount: an Admin a customer granted may see how
            # close the knowledge base is to stopping, not what it costs.
            "percent_used": (
                round(100.0 * spent / limit, 1)
                if metered and limit
                else (100.0 if reached else None)
            ),
            "reached": reached,
            "enforced": bool(metered and limit is not None),
            "unpriced_models": (month["unpriced_models"] if month and operator else []),
            "note": note,
        },
        "deep_research": _deep_research(config, saved, operator, metered, currency),
        "answers": _answers(config, saved),
        "updated_at": saved.get("updated_at"),
        "updated_by": saved.get("updated_by") if operator else None,
    }


def _deep_research(
    config: Any, saved: dict[str, Any], operator: bool, metered: bool, currency: str
) -> dict[str, Any]:
    """The per-answer limit (an amount: the operator's) and the per-process cap."""

    limit = answer_spend.effective_limit(config, saved)
    note = None
    if not metered:
        note = (
            "Usage metering is off (observability.usage), so a deep answer's spend is "
            "not counted and its limit is not enforced."
        )
    elif limit is None:
        note = "No per-answer limit: deep research runs until it has its answer."
    return {
        "answer_spend_limit": limit if operator else None,
        "default_answer_spend_limit": answer_spend.configured_limit(config) if operator else None,
        "custom_answer_spend_limit": "deep_answer_spend_limit" in saved,
        "currency": currency if operator else None,
        "wrap_up_percent": round(100 * answer_spend.WRAP_UP_SHARE),
        "enforced": bool(metered and limit is not None),
        "max_concurrent": answer_spend.effective_concurrency(config, saved),
        "default_max_concurrent": answer_spend.configured_concurrency(config),
        "custom_max_concurrent": saved.get("max_concurrent_deep_answers") is not None,
        "note": note,
    }


def _answers(config: Any, saved: dict[str, Any]) -> dict[str, Any]:
    """How many answers one api process runs at once, and how long the next one waits."""

    limit = effective_answer_limit(config, saved)
    latency = getattr(getattr(config, "assistant", None), "latency", None)
    wait = float(getattr(latency, "admission_wait_seconds", 0) or 0)
    api = getattr(getattr(config, "server", None), "api", None)
    requests = int(getattr(api, "max_concurrent_requests", 0) or 0)
    note = None
    if limit <= 0:
        note = "No answer limit: every question starts at once, bounded only by the request limit."
    elif requests and limit > requests:
        note = (
            f"The request limit ({requests} per server) is lower, so past {requests} "
            "questions it refuses first."
        )
    return {
        "max_concurrent": limit,
        "default_max_concurrent": configured_answer_limit(config),
        "custom_max_concurrent": saved.get("max_concurrent_answers") is not None,
        "max_allowed": MAX_ANSWER_CONCURRENCY,
        "wait_seconds": wait,
        "request_limit": requests or None,
        "note": note,
    }


def _whole(name: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequest(f"{name} is a whole number, or null for the default")
    if not low <= value <= high:
        raise InvalidRequest(f"{name} must be between {low} and {high}")
    return value


def _amount(name: str, value: Any, ceiling: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InvalidRequest(f"{name} is a number, or null for the default")
    try:
        amount = float(value)
    except ValueError as exc:
        raise InvalidRequest(f"{name} is a number, or null for the default") from exc
    if not math.isfinite(amount) or amount < 0 or amount > ceiling:
        raise InvalidRequest(f"{name} must be between 0 and {ceiling:,.0f}")
    return round(amount, 2)


def update(context: ServiceContext, changes: dict[str, Any]) -> dict[str, Any]:
    """Turn the kill switch on or off, or set the spend limits (operator admin).

    ``suspended`` (bool), ``reason`` (text shown to everyone refused, at most
    280 characters), ``monthly_spend_limit`` and ``deep_answer_spend_limit``
    (figures in the catalog's currency, 0 or more), and
    ``max_concurrent_deep_answers`` (a whole number, 0 for no separate cap),
    and ``max_concurrent_answers`` (answers of any kind one process runs at
    once, 1 to 256). ``null`` returns any of the last four to the deployment's
    default.
    """

    require_permission("security.manage")
    if not _is_operator(context):
        raise OperatorRequired("change the kill switch or the spend limits")
    if not isinstance(changes, dict) or not changes:
        raise InvalidRequest(
            "send suspended, reason, monthly_spend_limit, deep_answer_spend_limit, "
            "max_concurrent_deep_answers and/or max_concurrent_answers"
        )
    unknown = sorted(set(changes) - set(FIELDS))
    if unknown:
        raise InvalidRequest(f"unknown control(s): {', '.join(unknown)}")
    saved = dict(spend.read_controls(context.config, context.state))
    identity = current_identity()
    actor = identity.principal if identity is not None else "operator"
    if "monthly_spend_limit" in changes:
        value = changes["monthly_spend_limit"]
        if value is None:
            saved.pop("monthly_spend_limit", None)
        else:
            saved["monthly_spend_limit"] = _amount(
                "monthly_spend_limit", value, spend.MAX_MONTHLY_LIMIT
            )
    if "deep_answer_spend_limit" in changes:
        value = changes["deep_answer_spend_limit"]
        if value is None:
            saved.pop("deep_answer_spend_limit", None)
        else:
            saved["deep_answer_spend_limit"] = _amount(
                "deep_answer_spend_limit", value, answer_spend.MAX_DEEP_ANSWER_LIMIT
            )
    if "max_concurrent_deep_answers" in changes:
        value = changes["max_concurrent_deep_answers"]
        if value is None:
            saved.pop("max_concurrent_deep_answers", None)
        else:
            saved["max_concurrent_deep_answers"] = _whole(
                "max_concurrent_deep_answers", value, 0, answer_spend.MAX_DEEP_CONCURRENCY
            )
    if "max_concurrent_answers" in changes:
        value = changes["max_concurrent_answers"]
        if value is None:
            saved.pop("max_concurrent_answers", None)
        else:
            # Not 0: turning the bound off on a running replica is one setting
            # away from every question holding model calls open at once.
            saved["max_concurrent_answers"] = _whole(
                "max_concurrent_answers", value, 1, MAX_ANSWER_CONCURRENCY
            )
    if "reason" in changes:
        reason = changes["reason"]
        if reason is not None and not isinstance(reason, str):
            raise InvalidRequest("reason is text")
        reason = (reason or "").strip()
        if len(reason) > MAX_REASON:
            raise InvalidRequest(f"reason must be at most {MAX_REASON} characters")
        saved["reason"] = reason or None
    if "suspended" in changes:
        value = changes["suspended"]
        if not isinstance(value, bool):
            raise InvalidRequest("suspended is true or false")
        if value and not saved.get("suspended"):
            saved["suspended_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            saved["suspended_by"] = actor
        if not value:
            for key in ("suspended_at", "suspended_by", "reason"):
                saved.pop(key, None)
        saved["suspended"] = value
    spend.write_controls(context.config, context.state, saved, actor)
    logger.warning(
        "region controls changed: by=%s suspended=%s limit=%s deep_limit=%s deep_cap=%s "
        "answer_cap=%s",
        actor,
        bool(saved.get("suspended")),
        saved.get("monthly_spend_limit", "default"),
        saved.get("deep_answer_spend_limit", "default"),
        saved.get("max_concurrent_deep_answers", "default"),
        saved.get("max_concurrent_answers", "default"),
    )
    return describe(context)


def suspension(config: Any) -> AccessSuspended | None:
    """The refusal a caller gets now if the kill switch is on and they are not
    an operator admin; ``None`` to let the request through. Read by the front
    door on every request, so it uses the guard's cached row."""

    saved = spend.GUARD.suspended()
    if saved is None:
        return None
    from pheasant.services.usage import is_operator

    if is_operator(config):
        return None
    return AccessSuspended(saved.get("reason"))


def require_budget() -> None:
    """Refuse an operation that is about to spend, before it starts, once the
    month's spend has reached its limit. The wires refuse each call too; this
    is what makes an answer say so at once rather than fail part-way."""

    refusal = spend.GUARD.over()
    if refusal is not None:
        raise SpendLimitRefused(str(refusal))
