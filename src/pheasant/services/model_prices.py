"""What each model costs, set at runtime by the operator, applying from then on.

One implementation behind ``GET``/``PUT /models/prices`` and MCP
``get_model_prices`` / ``set_model_prices`` (rule 13). The deployment's catalog
states a price per model (``models.*.input_price`` ...); a cloud provider's
prices change now and then, and an operator should not need a redeploy to
follow. An override is a ``region_settings`` row (``telemetry.spend.PRICES_SECTION``)
laid over the catalog by every process within seconds.

**A change applies to calls made after it.** Each call is costed when it is
made and the cost stored with its tokens (``usage_daily.cost_micros``), so the
spend limit, the Usage tab and every export keep what was already spent at the
price it was charged at. The row keeps a history of who changed which price,
when, from what to what.

Prices are the operator's, like cost: reading and changing them both need an
operator admin (``OPERATOR_REQUIRED`` for anyone else).
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any

from pheasant.security.authorization import require_permission
from pheasant.security.identity import current_identity
from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest, OperatorRequired
from pheasant.telemetry import spend

logger = logging.getLogger(__name__)

#: The highest price per million tokens an operator may set: a typo guard.
MAX_PRICE = 100_000.0
#: History entries a reply lists, newest first.
SHOWN_HISTORY = 50


def _require_operator(context: ServiceContext, what: str) -> None:
    from pheasant.services.usage import is_operator

    require_permission("security.manage")
    if not is_operator(context.config):
        raise OperatorRequired(what)


def _entries(config: Any) -> list[tuple[str, Any]]:
    models = getattr(config, "models", None)
    if models is None:
        return []
    return [
        *(("chat", entry) for entry in models.chat),
        *(("decision", entry) for entry in models.decisions),
        *(("embedding", entry) for entry in models.embeddings),
    ]


def _catalog_price(entry: Any) -> dict[str, float | None]:
    return {
        "input": entry.input_price,
        "output": getattr(entry, "output_price", None),
        "cached_input": getattr(entry, "cached_input_price", None),
    }


def _as_price(value: tuple[Any, ...] | None) -> dict[str, float | None] | None:
    if value is None:
        return None
    return dict(zip(spend.PRICE_FIELDS, value, strict=True))


def describe(context: ServiceContext) -> dict[str, Any]:
    """Each catalog model's price: the catalog's, any override, and the one in force."""

    _require_operator(context, "see the model prices")
    config = context.config
    saved = spend.read_price_overrides(config, context.state)
    overrides = saved.get("prices") or {}
    effective = spend.effective_prices(config, overrides)
    rows = []
    for kind, entry in _entries(config):
        rows.append(
            {
                "id": entry.id,
                "kind": kind,
                "label": getattr(entry, "label", "") or entry.id,
                "model": entry.model,
                "catalog": _catalog_price(entry),
                "override": overrides.get(str(entry.id)),
                "effective": _as_price(effective.get(str(entry.id))),
            }
        )
    history = list(saved.get("history") or [])
    return {
        "knowledge_base": config.knowledge_base_id,
        "currency": getattr(getattr(config, "models", None), "currency", "USD") or "USD",
        "unit": "per million tokens",
        "models": rows,
        "history": history[::-1][:SHOWN_HISTORY],
        "updated_at": saved.get("updated_at"),
        "updated_by": saved.get("updated_by"),
        "note": (
            "A price applies to calls made after it is set. What was already spent keeps "
            "the price it was charged at, in the spend limit, the Usage tab and every export."
        ),
    }


def _price_field(model: str, name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InvalidRequest(f"{model}.{name} is a number (per million tokens)")
    try:
        amount = float(value)
    except ValueError as exc:
        raise InvalidRequest(f"{model}.{name} is a number (per million tokens)") from exc
    if not math.isfinite(amount) or amount < 0 or amount > MAX_PRICE:
        raise InvalidRequest(f"{model}.{name} must be between 0 and {MAX_PRICE:,.0f}")
    return amount


def update(context: ServiceContext, changes: dict[str, Any]) -> dict[str, Any]:
    """Set or clear prices by catalog id, from now on (operator admin).

    ``changes`` is ``{"prices": {id: {"input": 2.5, "output": 10, "cached_input":
    0.25} | null}}``: the fields given replace the catalog's, the rest keep it,
    and ``null`` returns the model to the catalog's price.
    """

    _require_operator(context, "change the model prices")
    prices = changes.get("prices") if isinstance(changes, dict) else None
    if not isinstance(prices, dict) or not prices:
        raise InvalidRequest('send {"prices": {model id: {input, output, cached_input} or null}}')
    config = context.config
    known = {str(entry.id): entry for _, entry in _entries(config)}
    unknown = sorted(set(prices) - set(known))
    if unknown:
        listed = ", ".join(sorted(known)) or "(none)"
        raise InvalidRequest(f"unknown model id(s): {', '.join(unknown)}. Catalog: {listed}")
    saved = spend.read_price_overrides(config, context.state)
    overrides: dict[str, Any] = dict(saved.get("prices") or {})
    before = spend.effective_prices(config, overrides)
    for model, value in prices.items():
        if value is None:
            overrides.pop(model, None)
            continue
        if not isinstance(value, dict) or not value:
            raise InvalidRequest(f"{model}: send input, output and/or cached_input, or null")
        stray = sorted(set(value) - set(spend.PRICE_FIELDS))
        if stray:
            raise InvalidRequest(f"{model}: unknown price field(s): {', '.join(stray)}")
        merged = dict(overrides.get(model) or {})
        for name, amount in value.items():
            merged[name] = _price_field(model, name, amount)
        overrides[model] = merged
    after = spend.effective_prices(config, overrides)
    identity = current_identity()
    actor = identity.principal if identity is not None else "operator"
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    history = list(saved.get("history") or [])
    for model in prices:
        old, new = _as_price(before.get(model)), _as_price(after.get(model))
        if old != new:
            history.append({"at": stamp, "by": actor, "model": model, "from": old, "to": new})
    spend.write_price_overrides(
        config,
        context.state,
        {"prices": overrides, "history": history[-spend.MAX_PRICE_HISTORY :]},
        actor,
    )
    logger.warning("model prices changed: by=%s models=%s", actor, sorted(prices))
    return describe(context)
