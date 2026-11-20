"""A monthly limit on what a knowledge base spends on models, and its kill switch.

Two controls an operator holds over one knowledge base, kept in one
``region_settings`` row (``REGION_CONTROLS``) so every process reads the same
values:

* **The spend limit.** What this calendar month's (UTC) chat, judgement and
  embedding calls cost at the prices the operator *stated* in the catalog
  (``models.*.input_price`` ...), against ``models.monthly_spend_limit``
  (default 500) or the figure an operator set since. At the limit the next
  model call is refused with ``SPEND_LIMIT_REACHED``; answers stop, a sync
  that would embed new content is skipped, and the vector arm drops out of
  search while text search, browsing and reading keep working. The month
  resets on the 1st.
* **The kill switch** (``suspended``). Read by the front-door guard
  (``api/auth.py``): every request is refused with ``ACCESS_SUSPENDED`` except
  an operator admin's, so the person who turned it on can turn it off.

**Priced when spent.** Each call is costed at the moment it is made, with the
price in force then: the catalog's (``models.*.input_price`` ...) or an
operator's runtime override (``PRICES_SECTION``, ``services/model_prices.py``).
The cost is stored beside the tokens (``usage_daily.cost_micros``), so changing
a price applies to later calls only and never reprices the month so far.

**Measured from what the meter already counts.** The daily totals
(``usage_daily``) are written by ``telemetry/usage.py`` at most every 30 s; the
guard adds this process's not-yet-written calls, so a process sees its own
spend at once and other processes' within a flush. A limit can therefore be
passed by what is in flight across the fleet at that moment, never by more.

**Cheap on the call path.** Each model call asks :meth:`SpendGuard.check`,
which reads the database at most every ``SPEND_TTL_SECONDS`` and the controls
row every ``CONTROLS_TTL_SECONDS``; in between it is arithmetic.

A model with no stated price costs nothing here and is named in
:func:`status`, so a total is never read as complete when it is not. With
usage metering off (``observability.usage``) nothing is counted and the limit
cannot be enforced; :func:`status` says so.

Layer 1, beside the meter, because the wires that spend (``assistant/providers``,
``search/embedding_client``) sit there and must not import the service layer.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from pheasant.telemetry import usage as metering

logger = logging.getLogger(__name__)

#: The ``region_settings`` section holding both controls.
REGION_CONTROLS = "region_controls"
#: The ``region_settings`` section holding an operator's runtime prices.
PRICES_SECTION = "model_prices"
#: The price fields an override may set, per million tokens.
PRICE_FIELDS = ("input", "output", "cached_input")
#: Price changes kept in the row's history, newest last.
MAX_PRICE_HISTORY = 200
#: The monthly limit when the config names none (``models.monthly_spend_limit``).
DEFAULT_MONTHLY_LIMIT = 500.0
#: The largest limit an operator may set: a typo guard, not a business rule.
MAX_MONTHLY_LIMIT = 10_000_000.0
#: How stale the database's monthly total may be on the call path.
SPEND_TTL_SECONDS = 15.0
#: How stale the controls row may be: how long a kill switch takes to bite.
CONTROLS_TTL_SECONDS = 5.0
#: The modes that add content, and so spend on embeddings, as in ``size_limit``.
GROWING_MODES = frozenset({"incremental", "full"})


class SpendLimitReached(ValueError):
    """The month's model spend has reached the limit; nothing more is spent.

    A ``ValueError`` with the service layer's refusal shape (``status``,
    ``code``, ``retryable``), so MCP's existing translation forwards its text
    and HTTP registers it by type. Not retryable: repeating the call cannot
    help until the month turns or an operator raises the limit.
    """

    status = 402
    code = "SPEND_LIMIT_REACHED"
    retryable = False

    def __init__(self, spent: float, limit: float, currency: str = "USD") -> None:
        self.spent = spent
        self.limit = limit
        self.currency = currency
        super().__init__(
            f"This knowledge base has reached its monthly model spend limit "
            f"({_money(spent, currency)} of {_money(limit, currency)} this month, UTC). "
            "Answers and new embeddings resume on the 1st, or when an operator admin "
            "raises the limit. Text search, browsing and reading still work."
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "error": str(self),
            "code": self.code,
            "retryable": self.retryable,
            "status": self.status,
        }


def _money(amount: float, currency: str) -> str:
    return f"${amount:,.2f}" if currency.upper() == "USD" else f"{amount:,.2f} {currency}"


# ------------------------------------------------------------------- pricing


def prices(config: Any) -> dict[str, tuple[float | None, float | None, float | None]]:
    """``{wire model or id: (input, output, cached input)}`` per million tokens."""

    models = getattr(config, "models", None)
    table: dict[str, tuple[float | None, float | None, float | None]] = {}
    if models is None:
        return table
    for entry in [*models.chat, *models.decisions, *models.embeddings]:
        price = (
            entry.input_price,
            getattr(entry, "output_price", None),
            getattr(entry, "cached_input_price", None),
        )
        if price[0] is None and price[1] is None:
            continue
        for name in {entry.model, entry.id}:
            table.setdefault(str(name), price)
    return table


def effective_prices(
    config: Any, overrides: dict[str, Any] | None
) -> dict[str, tuple[float | None, float | None, float | None]]:
    """The catalog's prices with an operator's runtime overrides laid over them.

    An override is keyed by catalog id and names any of ``input``, ``output``
    and ``cached_input``; a field it does not name keeps the catalog's.
    """

    table = prices(config)
    models = getattr(config, "models", None)
    if not overrides or models is None:
        return table
    for entry in [*models.chat, *models.decisions, *models.embeddings]:
        override = overrides.get(str(entry.id))
        if not isinstance(override, dict):
            continue
        catalog = (
            entry.input_price,
            getattr(entry, "output_price", None),
            getattr(entry, "cached_input_price", None),
        )
        price = tuple(
            override[name] if name in override else catalog[index]
            for index, name in enumerate(PRICE_FIELDS)
        )
        for name in {entry.model, entry.id}:
            if price[0] is None and price[1] is None:
                table.pop(str(name), None)
            else:
                table[str(name)] = price  # type: ignore[assignment]
    return table


def micros(price: Any, fresh: int, written: int, cached: int) -> int | None:
    """What one call cost, in millionths of the currency; ``None`` when unpriced.

    Prices are per million tokens, so a price is also micros per token.
    """

    if price is None:
        return None
    p_in, p_out, p_cached = price
    if p_cached is None:
        p_cached = p_in
    return int(round(fresh * (p_in or 0.0) + written * (p_out or 0.0) + cached * (p_cached or 0.0)))


def read_price_overrides(config: Any, state: Any) -> dict[str, Any]:
    """The operator's runtime prices row, uncached: ``{"prices": {...}, "history": [...]}``."""

    if state is None:
        return {}
    try:
        rows = state.rows(
            "SELECT data_json, updated_at, updated_by FROM region_settings "
            "WHERE kb_id=? AND section=?",
            (config.knowledge_base_id, PRICES_SECTION),
        )
    except Exception:  # noqa: BLE001 - a store without the row prices as deployed
        return {}
    if not rows:
        return {}
    try:
        saved = json.loads(rows[0]["data_json"] or "{}")
    except ValueError:
        return {}
    if not isinstance(saved, dict):
        return {}
    saved["updated_at"] = rows[0]["updated_at"]
    saved["updated_by"] = rows[0]["updated_by"]
    return saved


def write_price_overrides(config: Any, state: Any, saved: dict[str, Any], actor: str) -> None:
    """Replace the runtime prices row, and forget this process's cached prices."""

    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    body = {k: v for k, v in saved.items() if k not in {"updated_at", "updated_by"}}
    state.execute(
        "INSERT INTO region_settings(kb_id, section, data_json, updated_at, updated_by) "
        "VALUES(?,?,?,?,?) ON CONFLICT(kb_id, section) DO UPDATE SET "
        "data_json=excluded.data_json, updated_at=excluded.updated_at, "
        "updated_by=excluded.updated_by",
        (config.knowledge_base_id, PRICES_SECTION, json.dumps(body), stamp, actor),
    )
    GUARD.forget()


def cost(row: dict[str, Any], table: dict[str, Any]) -> float | None:
    """What one totals row cost; ``None`` when unpriced.

    The cost stored when its calls were made (``cost_micros``) where there is
    one; otherwise the row's tokens at ``table``, which is only ever a row
    never written since costs started being stored.
    """

    stored = row.get("cost_micros")
    if stored is not None:
        return int(stored) / 1_000_000
    price = table.get(row["model"])
    if price is None:
        return None
    fresh, written, cached = price
    cached = cached if cached is not None else fresh
    return (
        row["input_tokens"] * (fresh or 0.0)
        + row["output_tokens"] * (written or 0.0)
        + row["cached_input_tokens"] * (cached or 0.0)
    ) / 1_000_000


# ------------------------------------------------------------------ controls


def month_start(now: datetime | None = None) -> str:
    """The first day of this calendar month (UTC), as the ledger spells a day."""

    now = now or datetime.now(UTC)
    return now.date().replace(day=1).isoformat()


def configured_limit(config: Any) -> float | None:
    """The deployment's monthly limit; ``None`` is no limit."""

    models = getattr(config, "models", None)
    if models is None:
        return DEFAULT_MONTHLY_LIMIT
    value = getattr(models, "monthly_spend_limit", DEFAULT_MONTHLY_LIMIT)
    return None if value is None else float(value)


def read_controls(config: Any, state: Any) -> dict[str, Any]:
    """The saved controls row, uncached; ``{}`` when none was ever saved."""

    if state is None:
        return {}
    try:
        rows = state.rows(
            "SELECT data_json, updated_at, updated_by FROM region_settings "
            "WHERE kb_id=? AND section=?",
            (config.knowledge_base_id, REGION_CONTROLS),
        )
    except Exception:  # noqa: BLE001 - a store without the row answers as provisioned
        return {}
    if not rows:
        return {}
    try:
        saved = json.loads(rows[0]["data_json"] or "{}")
    except ValueError:
        return {}
    if not isinstance(saved, dict):
        return {}
    saved.setdefault("updated_at", rows[0]["updated_at"])
    saved.setdefault("updated_by", rows[0]["updated_by"])
    return saved


def write_controls(config: Any, state: Any, saved: dict[str, Any], actor: str) -> None:
    """Replace the controls row, and forget this process's cached copy."""

    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    body = {k: v for k, v in saved.items() if k not in {"updated_at", "updated_by"}}
    state.execute(
        "INSERT INTO region_settings(kb_id, section, data_json, updated_at, updated_by) "
        "VALUES(?,?,?,?,?) ON CONFLICT(kb_id, section) DO UPDATE SET "
        "data_json=excluded.data_json, updated_at=excluded.updated_at, "
        "updated_by=excluded.updated_by",
        (config.knowledge_base_id, REGION_CONTROLS, json.dumps(body), stamp, actor),
    )
    GUARD.forget()


def effective_limit(config: Any, saved: dict[str, Any]) -> float | None:
    """The limit in force: an operator's figure, else the deployment's."""

    if "monthly_spend_limit" in saved:
        value = saved["monthly_spend_limit"]
        return None if value is None else float(value)
    return configured_limit(config)


# --------------------------------------------------------------------- spend


def month_spend(
    config: Any, state: Any, *, pending: bool = True, table: dict[str, Any] | None = None
) -> dict[str, Any]:
    """This month's model spend: the ledger plus this process's unwritten calls.

    Each row's stored cost, priced when its calls were made; a row with none
    (never written since costs were stored) at ``table``, the prices in force.
    """

    first = month_start()
    if table is None:
        table = effective_prices(config, read_price_overrides(config, state).get("prices"))
    spent, unpriced = 0.0, set()

    def count(row: dict[str, Any]) -> None:
        nonlocal spent
        amount = cost(row, table)
        if amount is None:
            if row["input_tokens"] or row["output_tokens"]:
                unpriced.add(str(row["model"]))
            return
        spent += amount

    kinds = metering.MODEL_KINDS
    try:
        rows = state.rows(
            "SELECT model, (cost_micros IS NULL) AS unpriced, SUM(cost_micros) AS cost_micros, "
            "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
            "SUM(cached_input_tokens) AS cached_input_tokens FROM usage_daily "
            f"WHERE kb_id=? AND day>=? AND kind IN ({','.join('?' * len(kinds))}) "
            "GROUP BY model, (cost_micros IS NULL)",
            (config.knowledge_base_id, first, *kinds),
        )
    except Exception:  # noqa: BLE001 - no ledger yet reads as nothing spent
        rows = []
    for row in rows:
        count(dict(row))
    if pending:
        for row in metering.METER.pending_model_rows(first):
            count(row)
    return {"month": first[:7], "spent": spent, "unpriced_models": sorted(unpriced)}


class SpendGuard:
    """One per process, bound beside the meter. Thread-safe; free when unbound."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._config: Any = None
        self._state: Any = None
        self._controls: tuple[float, dict[str, Any]] | None = None
        self._prices: tuple[float, dict[str, Any]] | None = None
        self._ledger: tuple[float, str, float] | None = None

    @property
    def bound(self) -> bool:
        return self._state is not None

    def bind(self, config: Any, state: Any) -> None:
        """Hold ``config``'s controls. The kill switch works whatever is metered;
        the spend limit only where the meter counts (``observability.usage``)."""

        with self._lock:
            self._config, self._state = config, state
            self._controls = self._prices = self._ledger = None
        if config.observability.usage:
            self.freeze_history()

    def unbind(self, state: Any = None) -> None:
        with self._lock:
            if state is not None and self._state is not state:
                return
            self._config = self._state = None
            self._controls = self._prices = self._ledger = None

    def forget(self) -> None:
        """Drop the cached controls, prices and total: the next check reads them again."""

        with self._lock:
            self._controls = self._prices = self._ledger = None

    def prices(self) -> dict[str, Any]:
        """The prices in force now, at most ``CONTROLS_TTL_SECONDS`` old."""

        config, state = self._config, self._state
        if config is None:
            return {}
        now = time.monotonic()
        cached = self._prices
        if cached is not None and now - cached[0] < CONTROLS_TTL_SECONDS:
            return cached[1]
        table = effective_prices(config, read_price_overrides(config, state).get("prices"))
        with self._lock:
            self._prices = (now, table)
        return table

    def price_of(self, model: str | None) -> Any:
        """``(input, output, cached input)`` per million tokens for ``model`` now, or ``None``."""

        return self.prices().get(str(model or "")) if self._config is not None else None

    def call_micros(self, model: str | None, fresh: int, written: int, cached: int) -> int | None:
        """What a call made now costs, in micros; ``None`` when unpriced or unbound."""

        return micros(self.price_of(model), fresh, written, cached)

    def freeze_history(self) -> None:
        """Price, once, every total recorded before costs were stored.

        Behind a marker, so it runs on the first start of a release that
        stores costs and never again: a total from before then is fixed at the
        price in force at that moment, not repriced by every later change.
        Best-effort: the next start tries again if it fails.
        """

        config, state = self._config, self._state
        marker = "usage_cost_frozen_v1"
        try:
            if state.rows("SELECT value FROM pheasant_schema_meta WHERE key=?", (marker,)):
                return
            from pheasant.persistence import usage_ledger

            frozen = usage_ledger.freeze_costs(state, config.knowledge_base_id, self.prices())
            state.execute(
                "INSERT INTO pheasant_schema_meta(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (marker, str(frozen), datetime.now(UTC).isoformat()),
            )
        except Exception:  # noqa: BLE001 - the next start tries again
            logger.warning("could not price the usage totals recorded before costs were stored")

    def controls(self) -> dict[str, Any]:
        """The saved controls, at most ``CONTROLS_TTL_SECONDS`` old."""

        config, state = self._config, self._state
        if state is None:
            return {}
        now = time.monotonic()
        cached = self._controls
        if cached is not None and now - cached[0] < CONTROLS_TTL_SECONDS:
            return cached[1]
        saved = read_controls(config, state)
        with self._lock:
            self._controls = (now, saved)
        return saved

    def _ledger_spend(self) -> float:
        """The ledger's total for this month, at most ``SPEND_TTL_SECONDS`` old."""

        now, month = time.monotonic(), month_start()
        cached = self._ledger
        if cached is not None and cached[1] == month and now - cached[0] < SPEND_TTL_SECONDS:
            return cached[2]
        spent = month_spend(self._config, self._state, pending=False, table=self.prices())["spent"]
        with self._lock:
            self._ledger = (now, month, spent)
        return spent

    def over(self) -> SpendLimitReached | None:
        """The refusal a model call would get now, or ``None`` to go ahead."""

        config, state = self._config, self._state
        if state is None or not config.observability.usage:
            return None  # nothing is counted, so nothing can be limited
        limit = effective_limit(config, self.controls())
        if limit is None:
            return None
        spent = self._ledger_spend()
        table = self.prices()
        for row in metering.METER.pending_model_rows(month_start()):
            spent += cost(row, table) or 0.0
        if spent < limit:
            return None
        currency = getattr(getattr(config, "models", None), "currency", "USD") or "USD"
        return SpendLimitReached(spent, limit, currency)

    def check(self) -> None:
        """Refuse a model call once the month's spend has reached the limit."""

        refusal = self.over()
        if refusal is not None:
            raise refusal

    def suspended(self) -> dict[str, Any] | None:
        """The kill switch's row when it is on, else ``None``."""

        saved = self.controls()
        return saved if saved.get("suspended") else None


#: The process's guard, bound where the meter is (``SyncEngine.__init__``).
GUARD = SpendGuard()


def blocks(config: Any, mode: str, source_name: str = "") -> dict[str, Any] | None:
    """The details of a sync skipped because the month's spend is at its limit.

    Only a sync that would embed: one that adds content, in a region whose
    embedder is a paid one. Like the size limit, it is a skip rather than a
    failure, so a queued task finishes instead of retrying, and a sync already
    running finishes what it began.
    """

    if mode not in GROWING_MODES or not _embeds(config):
        return None
    refusal = GUARD.over()
    if refusal is None:
        return None
    logger.warning(
        "%s: sync skipped, this month's model spend (%.2f) has reached its limit (%.2f)",
        source_name or "source",
        refusal.spent,
        refusal.limit,
    )
    return {
        "reason": "spend limit reached",
        "spent": round(refusal.spent, 2),
        "limit": refusal.limit,
    }


def _embeds(config: Any) -> bool:
    settings = getattr(getattr(config, "search", None), "embeddings", None)
    if settings is None or not getattr(settings, "enabled", False):
        return False
    return str(getattr(settings, "provider", "")).lower() not in {"stub", "hash", "none", ""}
