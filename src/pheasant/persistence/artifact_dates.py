"""The two document dates on ``artifacts``: reads, writes and their migration.

``source_modified_at`` is when the source of truth says a document last
changed; ``ingested_at`` is when this region took in the bytes it holds now
(`docs/how-to/search-by-date.md`). Both are canonical UTC strings
(`ingestion.timestamps`), so every comparison is a string comparison.

Functions over a `StateStore` rather than methods on it, for the reason
`receipts.py` gives: `state_store.py` is held to a size ceiling, and a
self-contained concern does not need to live inside the class to use its
connection.
"""

from __future__ import annotations

from typing import Any

COLUMNS = ("source_modified_at", "ingested_at")


def row_defaults(artifact: dict[str, Any]) -> dict[str, Any]:
    """Values for an artifact row that names neither date.

    The engine always names both; anything else writing a row (a test, an
    older caller) gets "unknown" for the source date and its own indexing time
    as the ingestion time, which is what it would have been.
    """

    return {"source_modified_at": None, "ingested_at": artifact.get("last_indexed_at")}


def migrate(store: Any) -> None:
    """Add the two columns to an ``artifacts`` table that predates them.

    Called from both of `StateStore`'s migration paths, because Postgres
    returns early from `migrate` and an additive column named in only one
    place exists on exactly one backend. Existing rows keep a NULL source
    date, which reads as unknown and is then true: the first sync after the
    upgrade fills it from the listing without re-indexing
    (`sync.timestamps.reconcile`). ``ingested_at`` is backfilled from
    ``last_indexed_at``, the best evidence a row already carries.
    """

    columns = store.backend.table_columns("artifacts")
    missing = [column for column in COLUMNS if columns and column not in columns]
    for column in missing:
        store.conn.execute(f"ALTER TABLE artifacts ADD COLUMN {column} TEXT")
    if missing:
        # Idempotent by its predicate: only rows still NULL are touched.
        store.conn.execute(
            "UPDATE artifacts SET ingested_at = last_indexed_at "
            "WHERE ingested_at IS NULL AND last_indexed_at IS NOT NULL"
        )


def prior_dates(store: Any, source_id: str) -> dict[str, tuple[Any, Any, Any]]:
    """``{artifact id: (sha256, ingested_at, source_modified_at)}`` for one source.

    Read at the start of a sync so dates survive a re-index of identical
    bytes, a full sync's clear included (`sync.engine._artifact_dates`).
    """

    rows = store.rows(
        "SELECT id, sha256, ingested_at, source_modified_at FROM artifacts WHERE source_id=?",
        (source_id,),
    )
    return {
        str(row["id"]): (row["sha256"], row["ingested_at"], row["source_modified_at"])
        for row in rows
    }


def timestamps(store: Any, artifact_ids: list[str]) -> dict[str, dict[str, Any]]:
    """``{artifact id: {"source_modified_at", "ingested_at"}}`` for the ids that exist.

    The date window's lookup for the hits SQL cannot narrow. Through ``rows``
    for the reason `StateStore.artifact_acls` gives (a read must not pin a
    pooled connection), and through `Dialect.in_clause` so Postgres prepares
    one statement for every batch size.
    """

    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(artifact_ids), 500):
        clause, params = store.dialect.in_clause("id", artifact_ids[start : start + 500])
        for row in store.rows(
            f"SELECT id, source_modified_at, ingested_at FROM artifacts WHERE {clause}",
            tuple(params),
        ):
            out[str(row["id"])] = {column: row[column] for column in COLUMNS}
    return out


def set_source_dates(store: Any, updates: list[tuple[str, str]]) -> None:
    """Write ``(source_modified_at, artifact id)`` pairs in batched transactions.

    The first sync after an upgrade re-dates every document in a source, and
    one statement and commit per row is a round trip each on Postgres.
    """

    for start in range(0, len(updates), 1000):
        with store.conn:
            store.conn.executemany(
                "UPDATE artifacts SET source_modified_at=? WHERE id=?",
                updates[start : start + 1000],
            )
