"""Daily usage totals, as rows (`telemetry/usage.py` counts; this stores).

One row per knowledge base, day, kind, model, tenant, principal and channel.
Every write is ``INSERT ... ON CONFLICT DO UPDATE SET x = x + excluded.x``:
additive, so two replicas flushing the same key at once both land, and a
flush is one statement per row with no read before it.

``kind`` is ``chat`` / ``decision`` / ``embedding`` (a model's tokens) or
``answer`` / ``search`` (an operation's count and latency histogram, ``b0`` to
``b9`` cut at ``telemetry.usage.BUCKETS_MS``). ``input_tokens`` is input *not*
served from a provider's cache; ``cached_input_tokens`` is the rest.
"""

from __future__ import annotations

from typing import Any

USAGE_SCHEMA = """\
-- What the region spent and did, by day (`persistence/usage_ledger.py`).
-- Totals only: never a question, an answer or a document.
CREATE TABLE IF NOT EXISTS usage_daily (
  kb_id TEXT NOT NULL,
  day TEXT NOT NULL,
  kind TEXT NOT NULL,
  model TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  principal TEXT NOT NULL,
  channel TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  duration_ms REAL NOT NULL DEFAULT 0,
  b0 INTEGER NOT NULL DEFAULT 0,
  b1 INTEGER NOT NULL DEFAULT 0,
  b2 INTEGER NOT NULL DEFAULT 0,
  b3 INTEGER NOT NULL DEFAULT 0,
  b4 INTEGER NOT NULL DEFAULT 0,
  b5 INTEGER NOT NULL DEFAULT 0,
  b6 INTEGER NOT NULL DEFAULT 0,
  b7 INTEGER NOT NULL DEFAULT 0,
  b8 INTEGER NOT NULL DEFAULT 0,
  b9 INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (kb_id, day, kind, model, tenant_id, principal, channel)
);
"""

_KEYS = ("kb_id", "day", "kind", "model", "tenant_id", "principal", "channel")
_SUMS = (
    "requests",
    "errors",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "duration_ms",
    *(f"b{i}" for i in range(10)),
)
_COLUMNS = (*_KEYS, *_SUMS)
_UPSERT = (
    f"INSERT INTO usage_daily({','.join(_COLUMNS)}) VALUES({','.join('?' * len(_COLUMNS))}) "
    f"ON CONFLICT({','.join(_KEYS)}) DO UPDATE SET "
    + ", ".join(f"{name}=usage_daily.{name}+excluded.{name}" for name in _SUMS)
)


def add(state: Any, kb_id: str, rows: dict[tuple[str, ...], Any]) -> None:
    """Add ``{(day, kind, model, tenant, principal, channel): Totals}`` to the ledger."""

    for (day, kind, model, tenant, principal, channel), totals in rows.items():
        state.execute(
            _UPSERT,
            (
                kb_id,
                day,
                kind,
                model,
                tenant,
                principal,
                channel,
                totals.requests,
                totals.errors,
                totals.input_tokens,
                totals.output_tokens,
                totals.cached_input_tokens,
                totals.reasoning_tokens,
                totals.duration_ms,
                *totals.buckets,
            ),
        )


def window(state: Any, kb_id: str, first_day: str, last_day: str) -> list[dict[str, Any]]:
    """Every row of a knowledge base between two days, inclusive."""

    found = state.rows(
        f"SELECT {','.join(_COLUMNS)} FROM usage_daily "
        "WHERE kb_id=? AND day>=? AND day<=? ORDER BY day",
        (kb_id, first_day, last_day),
    )
    return [dict(row) for row in found]
