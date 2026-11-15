"""What a region spent and did: the operator's and the admins' view of usage.

One implementation behind ``GET /usage`` and MCP ``get_usage`` (rule 13). It
reads the daily totals ``telemetry/usage.py`` keeps and answers the questions
a person running or selling a hosted region asks:

* **support**: is it being used, by how many people, how long does an answer
  take, how often does one fail, how big has the knowledge base grown against
  its ceiling;
* **cost**: tokens by model, and what they cost at the prices the operator
  *stated* in the catalog (``models.*.input_price`` ...). A model with no
  stated price is reported with its tokens and no cost, and named, so a total
  is never quietly short;
* **go-to-market**: who uses it (by person and by organization), on which
  surface, and how that moves by day.

Two audiences, one difference. An application Admin sees everything about
activity and tokens. **Cost is the operator's**: it is shown to the local
operator, the shared token and the admins the deployment pinned
(``security.entra.role_assignments``), and withheld from an Admin a customer
granted, who has no business knowing the operator's rates.

Latency percentiles are read off a fixed histogram, so they are the upper
bound of the bucket the percentile falls in, not an exact figure.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from pheasant.persistence import access_grants, usage_ledger
from pheasant.security.authorization import require_permission
from pheasant.security.identity import current_identity
from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest, ServiceError
from pheasant.telemetry import usage as metering

MAX_DAYS = 366
#: People listed by name; the rest are summed as one row, so a very large
#: region's reply stays a page.
MAX_PEOPLE = 200


class UsageNotEnabled(ServiceError):
    status = 404
    code = "USAGE_NOT_ENABLED"

    def __init__(self) -> None:
        super().__init__("This region keeps no usage totals (observability.usage is off)")


def is_operator(config: Any) -> bool:
    """Whether the caller runs the region, rather than having been granted into it."""

    identity = current_identity()
    if identity is None or not identity.is_person:
        return identity is None or "Admin" in identity.roles
    settings = config.security.entra
    if not settings.delegated_access:
        # Every role is the deployment's doing: an Admin is the operator's.
        return "Admin" in identity.roles
    pinned = settings.role_assignments or {}
    return identity.tenant_id == settings.tenant_id and pinned.get(identity.subject) == "Admin"


def _prices(config: Any) -> dict[str, tuple[float | None, float | None, float | None]]:
    """``{wire model or id: (input, output, cached input)}`` per million tokens."""

    models = getattr(config, "models", None)
    prices: dict[str, tuple[float | None, float | None, float | None]] = {}
    if models is None:
        return prices
    for entry in [*models.chat, *models.decisions, *models.embeddings]:
        price = (
            entry.input_price,
            getattr(entry, "output_price", None),
            getattr(entry, "cached_input_price", None),
        )
        if price[0] is None and price[1] is None:
            continue
        for name in {entry.model, entry.id}:
            prices.setdefault(str(name), price)
    return prices


def _cost(row: dict[str, Any], prices: dict[str, Any]) -> float | None:
    price = prices.get(row["model"])
    if price is None:
        return None
    fresh, written, cached = price
    cached = cached if cached is not None else fresh
    return (
        row["input_tokens"] * (fresh or 0.0)
        + row["output_tokens"] * (written or 0.0)
        + row["cached_input_tokens"] * (cached or 0.0)
    ) / 1_000_000


def _percentile(buckets: list[int], fraction: float) -> int | None:
    total = sum(buckets)
    if not total:
        return None
    wanted, seen = fraction * total, 0
    for index, count in enumerate(buckets):
        seen += count
        if seen >= wanted:
            # The last bucket has no upper bound: say "more than" its lower one.
            return (
                metering.BUCKETS_MS[index]
                if index < len(metering.BUCKETS_MS)
                else metering.BUCKETS_MS[-1] * 2
            )
    return None


def _latency(rows: list[dict[str, Any]]) -> dict[str, Any]:
    buckets = [sum(int(row[f"b{i}"]) for row in rows) for i in range(10)]
    count = sum(int(row["requests"]) for row in rows)
    errors = sum(int(row["errors"]) for row in rows)
    return {
        "count": count,
        "errors": errors,
        "mean_ms": round(sum(float(row["duration_ms"]) for row in rows) / count) if count else None,
        "p50_ms": _percentile(buckets, 0.50),
        "p95_ms": _percentile(buckets, 0.95),
        "over_ms": metering.BUCKETS_MS[-1] if buckets[-1] else None,
    }


def _size(context: ServiceContext) -> dict[str, Any]:
    """How big the knowledge base is: what its ceiling is measured against."""

    def one(sql: str) -> int:
        try:
            rows = context.state.rows(sql)
            return int(next(iter(dict(rows[0]).values())) or 0) if rows else 0
        except Exception:  # noqa: BLE001 - a size is a nicety; usage must still answer
            return 0

    indexed = one("SELECT COALESCE(SUM(size_bytes), 0) FROM artifacts")
    limit = getattr(context.config.storage, "max_state_size_gb", None)
    return {
        "sources": one("SELECT COUNT(*) FROM sources WHERE enabled=1"),
        "documents": one("SELECT COUNT(*) FROM artifacts"),
        "chunks": one("SELECT COUNT(*) FROM chunks"),
        "indexed_bytes": indexed,
        "limit_gb": float(limit) if limit else None,
        "limit_used": round(indexed / (float(limit) * 1024**3), 4) if limit else None,
    }


def describe(context: ServiceContext, *, days: int = 30) -> dict[str, Any]:
    """Usage over the last ``days`` days, today included."""

    require_permission("security.manage")
    config = context.config
    if not config.observability.usage:
        raise UsageNotEnabled()
    try:
        days = int(days)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(f"days must be a whole number; got {days!r}") from exc
    if not 1 <= days <= MAX_DAYS:
        raise InvalidRequest(f"days must be between 1 and {MAX_DAYS}")
    metering.METER.flush()  # so a reader sees what this process just counted
    today = datetime.now(UTC).date()
    first = today - timedelta(days=days - 1)
    kb_id = config.knowledge_base_id
    rows = usage_ledger.window(context.state, kb_id, first.isoformat(), today.isoformat())
    show_cost = is_operator(config)
    prices = _prices(config) if show_cost else {}
    home = config.security.entra.tenant_id
    names = {home: "operator", "": "(indexing and shared credentials)"}
    for tenant, org in access_grants.snapshot(context.state, kb_id).orgs.items():
        names[tenant] = org.get("display_name") or (org.get("domains") or [tenant])[0]

    def blank() -> dict[str, Any]:
        return {
            "questions": 0,
            "searches": 0,
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "embedding_tokens": 0,
            "cost": 0.0,
        }

    def add(into: dict[str, Any], row: dict[str, Any]) -> None:
        kind = row["kind"]
        if kind == metering.ANSWER:
            into["questions"] += int(row["requests"])
        elif kind == metering.SEARCH:
            into["searches"] += int(row["requests"])
        elif kind == metering.EMBEDDING:
            into["model_calls"] += int(row["requests"])
            into["embedding_tokens"] += int(row["input_tokens"])
        else:
            into["model_calls"] += int(row["requests"])
            into["input_tokens"] += int(row["input_tokens"])
            into["output_tokens"] += int(row["output_tokens"])
            into["cached_input_tokens"] += int(row["cached_input_tokens"])
        if kind in metering.MODEL_KINDS:
            into["cost"] += _cost(row, prices) or 0.0

    totals = blank()
    by_day: dict[str, dict[str, Any]] = {
        (first + timedelta(days=offset)).isoformat(): blank() for offset in range(days)
    }
    by_model: dict[tuple[str, str], dict[str, Any]] = {}
    by_person: dict[tuple[str, str], dict[str, Any]] = defaultdict(blank)
    by_org: dict[str, dict[str, Any]] = defaultdict(blank)
    by_channel: dict[str, dict[str, Any]] = defaultdict(blank)
    last_seen: dict[tuple[str, str], str] = {}
    people_by_org: dict[str, set[str]] = defaultdict(set)
    unpriced: set[str] = set()
    for row in rows:
        add(totals, row)
        add(by_day.setdefault(row["day"], blank()), row)
        add(by_org[row["tenant_id"]], row)
        add(by_channel[row["channel"] or "api"], row)
        if row["principal"]:
            person = (row["tenant_id"], row["principal"])
            add(by_person[person], row)
            if row["kind"] in {metering.ANSWER, metering.SEARCH}:
                last_seen[person] = max(last_seen.get(person, ""), row["day"])
                people_by_org[row["tenant_id"]].add(row["principal"])
        if row["kind"] in metering.MODEL_KINDS:
            entry = by_model.setdefault(
                (row["kind"], row["model"]),
                {"kind": row["kind"], "model": row["model"] or "(unnamed)", **blank()},
            )
            add(entry, row)
            cost = _cost(row, prices)
            entry["priced"] = (
                (entry.get("priced", True) and cost is not None) if show_cost else None
            )
            if cost is None and (row["input_tokens"] or row["output_tokens"]):
                unpriced.add(row["model"] or "(unnamed)")

    def shown(entry: dict[str, Any]) -> dict[str, Any]:
        entry = dict(entry)
        entry["cost"] = round(entry["cost"], 4) if show_cost else None
        return entry

    people = sorted(
        (
            {
                "principal": principal,
                "tenant_id": tenant,
                "organization": names.get(tenant, tenant),
                "last_active": last_seen.get((tenant, principal)),
                **shown(entry),
            }
            for (tenant, principal), entry in by_person.items()
        ),
        key=lambda p: (-(p["questions"] + p["searches"]), p["principal"]),
    )
    return {
        "knowledge_base": kb_id,
        "window": {"days": days, "from": first.isoformat(), "to": today.isoformat()},
        "currency": config.models.currency if show_cost else None,
        "cost_visible": show_cost,
        "totals": {**shown(totals), "active_people": len(last_seen)},
        "latency": {
            "answer": _latency([r for r in rows if r["kind"] == metering.ANSWER]),
            "search": _latency([r for r in rows if r["kind"] == metering.SEARCH]),
            "buckets_ms": list(metering.BUCKETS_MS),
        },
        "by_day": [{"day": day, **shown(entry)} for day, entry in sorted(by_day.items())],
        "by_model": sorted(
            (shown(entry) for entry in by_model.values()),
            key=lambda m: -(m["input_tokens"] + m["output_tokens"] + m["embedding_tokens"]),
        ),
        "by_organization": sorted(
            (
                {
                    "tenant_id": tenant,
                    "organization": names.get(tenant, tenant),
                    "active_people": len(people_by_org.get(tenant, ())),
                    **shown(entry),
                }
                for tenant, entry in by_org.items()
            ),
            key=lambda o: -(o["questions"] + o["searches"] + o["model_calls"]),
        ),
        "by_channel": [
            {"channel": name, **shown(entry)} for name, entry in sorted(by_channel.items())
        ],
        "people": people[:MAX_PEOPLE],
        "people_omitted": max(0, len(people) - MAX_PEOPLE),
        # Named so a cost total is never read as complete when it is not.
        "unpriced_models": sorted(unpriced) if show_cost else [],
        "size": _size(context),
    }
