"""Daily usage totals, as rows (`telemetry/usage.py` counts; this stores).

One row per knowledge base, day, kind, model, tenant, principal and channel.
Every write is ``INSERT ... ON CONFLICT DO UPDATE SET x = x + excluded.x``:
additive, so two replicas flushing the same key at once both land, and a
flush is one statement per row with no read before it.

**Append-only, enforced by the database** (:func:`protect`). The monthly
spend limit is read from these rows (``telemetry/spend.py``), so a row that
could be deleted, or a count that could be lowered, is a limit that could be
lifted without anyone raising it. Triggers refuse ``DELETE``, ``TRUNCATE`` and
any ``UPDATE`` that lowers a count or moves a row to another key, from every
caller: the application, a maintenance script or a hand-typed statement. The
additive upsert only raises counts, so it is untouched. What remains possible
is the database owner's: dropping the triggers, or restoring a backup
(``deploy/compose/restore.sh``), which replaces the table whole.

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


class PartialWrite(Exception):
    """A write that failed part-way: ``remaining`` is every row not yet written,
    the failed one included, so a retry adds each row exactly once."""

    def __init__(self, remaining: dict[tuple[str, ...], Any], cause: BaseException) -> None:
        super().__init__(f"{len(remaining)} usage row(s) not written: {cause}")
        self.remaining = remaining
        self.cause = cause


def add(state: Any, kb_id: str, rows: dict[tuple[str, ...], Any]) -> None:
    """Add ``{(day, kind, model, tenant, principal, channel): Totals}`` to the ledger.

    Counts are never negative here: a provider reporting one would otherwise
    be refused by the append-only trigger and take the whole batch with it.
    Raises :class:`PartialWrite` naming the rows it did not write.
    """

    keys = list(rows)
    for index, key in enumerate(keys):
        try:
            _add_one(state, kb_id, key, rows[key])
        except Exception as exc:
            raise PartialWrite({k: rows[k] for k in keys[index:]}, exc) from exc


def _add_one(state: Any, kb_id: str, key: tuple[str, ...], totals: Any) -> None:
    day, kind, model, tenant, principal, channel = key
    sums = (
        totals.requests,
        totals.errors,
        totals.input_tokens,
        totals.output_tokens,
        totals.cached_input_tokens,
        totals.reasoning_tokens,
        totals.duration_ms,
        *totals.buckets,
    )
    state.execute(
        _UPSERT,
        (kb_id, day, kind, model, tenant, principal, channel, *(max(0, v) for v in sums)),
    )


#: What the triggers say when they refuse; no ``?`` or ``:`` (the Postgres
#: compiler rewrites those outside literals, and a function body is not one).
APPEND_ONLY = "usage_daily is append-only, a usage total is never deleted or lowered"
#: A row changed to another key, or a count lowered: what an UPDATE may not do.
_LOWERED = " OR ".join(
    [*(f"NEW.{key} IS DISTINCT FROM OLD.{key}" for key in _KEYS)]
    + [f"NEW.{name} < OLD.{name}" for name in _SUMS]
)
_SQLITE_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS usage_daily_no_delete BEFORE DELETE ON usage_daily "
    f"BEGIN SELECT RAISE(ABORT, '{APPEND_ONLY}'); END",
    "CREATE TRIGGER IF NOT EXISTS usage_daily_no_lowering BEFORE UPDATE ON usage_daily "
    f"WHEN {_LOWERED.replace('IS DISTINCT FROM', 'IS NOT')} "
    f"BEGIN SELECT RAISE(ABORT, '{APPEND_ONLY}'); END",
)
_PG_FUNCTION = f"""CREATE OR REPLACE FUNCTION pheasant_usage_daily_append_only() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
  IF TG_OP = 'UPDATE' AND NOT ({_LOWERED}) THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION '{APPEND_ONLY}';
END
$fn$"""
_PG_TRIGGERS = {
    "usage_daily_append_only": (
        "CREATE OR REPLACE TRIGGER usage_daily_append_only BEFORE UPDATE OR DELETE "
        "ON usage_daily FOR EACH ROW EXECUTE FUNCTION pheasant_usage_daily_append_only()"
    ),
    "usage_daily_no_truncate": (
        "CREATE OR REPLACE TRIGGER usage_daily_no_truncate BEFORE TRUNCATE "
        "ON usage_daily FOR EACH STATEMENT EXECUTE FUNCTION pheasant_usage_daily_append_only()"
    ),
}


def protect(store: Any) -> None:
    """Make ``usage_daily`` append-only in this database. Idempotent; every boot.

    Not behind a once-only marker, because a trigger somebody dropped should
    come back on the next start rather than stay gone with a marker saying it
    was installed. On Postgres it checks first, so a boot that finds both
    triggers takes no table lock.
    """

    if store.dialect.is_postgres:
        present = {
            str(row["tgname"])
            for row in store.rows(
                "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relname = 'usage_daily' AND NOT t.tgisinternal",
                (),
            )
        }
        if set(_PG_TRIGGERS) <= present:
            return
        store.conn.execute(_PG_FUNCTION)
        for statement in _PG_TRIGGERS.values():
            store.conn.execute(statement)
    else:
        for statement in _SQLITE_TRIGGERS:
            store.conn.execute(statement)
    store.conn.commit()


def window(state: Any, kb_id: str, first_day: str, last_day: str) -> list[dict[str, Any]]:
    """Every row of a knowledge base between two days, inclusive."""

    found = state.rows(
        f"SELECT {','.join(_COLUMNS)} FROM usage_daily "
        "WHERE kb_id=? AND day>=? AND day<=? ORDER BY day",
        (kb_id, first_day, last_day),
    )
    return [dict(row) for row in found]
