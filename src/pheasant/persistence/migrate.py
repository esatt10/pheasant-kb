"""One-shot SQLite → Postgres state migration (Phase 35.2).

``/state`` is user data (CLAUDE.md rule 2), so this obeys the same three rules
every other migration in pheasant does: **one-shot, idempotent, and it never
destroys the original.** The SQLite file is renamed ``*.migrated``, not
deleted, so a migration that turns out to have been a mistake is a rename away
from being undone.

Copying rather than re-indexing is the point. A large corpus takes hours to
index and may need an embedding provider that costs money; the rows are already
correct, and the stable IDs in them are a contract
(:mod:`pheasant.persistence.schema` shares every table definition verbatim so
they carry over byte-identically).

``chunks_fts`` is deliberately **not** copied. It is a derived cache — the same
argument the FTS title rebuild makes — and on Postgres its ``search_vector`` is
a generated column, so re-inserting the rows regenerates it correctly for the
target dialect. Copying it row-for-row would carry SQLite's tokenization into a
database that tokenizes differently.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pheasant.persistence.backends import PostgresBackend, SqliteBackend
from pheasant.persistence.secrets import redact_dsn
from pheasant.persistence.state_store import StateStore

logger = logging.getLogger(__name__)

#: Copied in dependency order: a row whose foreign key is not yet present would
#: be rejected. ``chunks_fts`` is rebuilt from ``chunks``, never copied.
TABLE_ORDER = (
    "knowledge_bases",
    "sources",
    "artifacts",
    "chunks",
    "symbols",
    "artifact_terms",
    "sync_events",
    "source_checkpoints",
    "source_audit_events",
    "manifests",
    "idp_groups",
    "idp_sync_meta",
    "memory_records",
    # Phase 3: after memory_records — its rows reference record_id (member
    # and canonical), the same FK-dependency-order rule every other table
    # here follows, even though SQLite itself never enforces the FK (see
    # CLAUDE.md's delete_source_artifacts note).
    "memory_compactions",
    "sync_fingerprints",
    # The observation plane. Retention-bounded, but within its window it is
    # what memory formation counts, so dropping it on a backend migration
    # would silently reset every formation threshold — the same argument the
    # memory counters make for being carried across a projection rebuild.
    # `log_tasks` is deliberately absent alongside `index_tasks` and
    # `source_leases`: in-flight claim state is meaningless once detached
    # from the process that claimed it.
    "interaction_events",
    # After memory_records: an admitted candidate names the record it became.
    # The FK is not declared (same reason as memory_compactions), but the copy
    # order follows it anyway.
    "memory_candidates",
    # The evaluation plane, in dependency order. Migrating to Postgres is what
    # a region does *before* it runs a fleet, so this is precisely the moment
    # its measurement history is most worth keeping -- and dropping it is
    # silent, because a table in neither `copied` nor `skipped` appears in the
    # report not at all.
    #
    # Two of these are unreproducible, which is why they matter more than the
    # row counts suggest. `evaluation_proofs` is the only data in the region
    # that cannot be re-derived from anything: proof comes from a surface where
    # somebody said so, and nothing can say it again on their behalf. And
    # `evaluation_cohorts` carries the **frozen anchor** -- lose it and the
    # next run silently builds a new one, so every trend point after the
    # migration is measured against different questions than every point
    # before, which is the exact confound the anchor exists to remove.
    "evaluation_snapshots",
    "evaluation_cohorts",
    "evaluation_proofs",
    # After snapshots: a run names the snapshot it measured.
    "evaluation_runs",
    # After runs, snapshots and cohorts: a metric row names all three.
    "evaluation_metrics",
    # The tuning plane. `tuning_bundles` is the one table here whose loss is
    # operationally visible rather than merely historical: the applied row is
    # the region's live retrieval overlay, so a migration that dropped it
    # would silently revert every replica's ranking to the config file.
    "tuning_experiments",
    "tuning_trials",
    "tuning_decisions",
    "tuning_bundles",
    # The knowledge graph (Phase 35.10). Copied rather than rebuilt: the rows
    # *are* the graph now, and their `digest` column is what the published
    # generation id folds, so re-deriving them on the target would be
    # re-deriving the region's identity. `graph_generations` last, after the
    # rows whose digests it summarizes.
    "graph_nodes",
    "graph_edges",
    "graph_generations",
    # The readiness plane. Both are evidence about work that already happened
    # and neither can be re-derived: a receipt records what a caller submitted
    # and what became of it, and nothing on the target can reconstruct a
    # submission nobody kept. Dropping them would leave a migrated region
    # reporting zero submissions and zero silent loss — a confident, wrong
    # answer, which is the failure mode the whole ledger exists to prevent.
    #
    # `snapshot_seals` after `evaluation_snapshots`, because a seal names the
    # manifest it sealed.
    "ingest_receipts",
    "snapshot_seals",
)

#: Core tables deliberately left behind, and why.
#:
#: Every table in ``CORE_SCHEMA`` must be in exactly one of this and
#: :data:`TABLE_ORDER`, which ``tests/test_scaling_regressions.py`` asserts
#: mechanically. That check exists because the alternative already happened: the
#: evaluation plane added six tables and none reached ``TABLE_ORDER``, so
#: ``pheasant migrate --to postgres`` -- the step a region takes on its way to a
#: fleet -- silently dropped every snapshot, cohort, run, metric and proof it
#: had, and reported success. A table in neither list appears in neither
#: ``copied`` nor ``skipped``, so there was nothing in the output to notice.
NOT_MIGRATED: dict[str, str] = {
    # Bookkeeping for the schema itself; `migrate()` writes the target's own.
    "pheasant_schema_meta": "the target writes its own schema bookkeeping",
    # In-flight claim state, meaningless once detached from the process that
    # claimed it.
    "index_tasks": "in-flight claim state, tied to the process that claimed it",
    "log_tasks": "in-flight claim state, tied to the process that claimed it",
    "source_leases": "in-flight claim state, tied to the process that claimed it",
    # A replay checkpoint is a cached *retrieval result*, and the two backends
    # rank differently on purpose -- FTS5's bm25() and Postgres's ts_rank_cd
    # are different functions (`tests/test_backend_parity.py` says so, and
    # declines to assert score equality). Carrying checkpoints across would let
    # one run's numbers be half SQLite ranking and half Postgres ranking, which
    # is the one thing a measurement must not be. An interrupted run replays
    # from scratch on the new backend instead, which is correct.
    "evaluation_replays": "cached retrieval results; the two backends rank differently",
}

#: Rows per INSERT batch. Large enough that a million-row table is not a
#: million round trips, small enough not to build a multi-GB parameter list.
BATCH = 1_000


class MigrationError(RuntimeError):
    """The migration could not complete, and nothing was renamed."""


def _copy_table(source: StateStore, target: StateStore, table: str) -> int:
    """Stream one table across in ``BATCH``-row pages.

    Streamed with ``fetchmany`` rather than ``SELECT *`` into a list. The whole
    point of this command is corpora too big for one process, and the table it
    exists to move is ``chunks`` — the largest by an order of magnitude, whose
    every row carries the chunk's full text. Materializing it meant peak memory
    proportional to the corpus, so the migration would OOM at exactly the size
    that motivates migrating. A cursor holds one page.

    The source is always SQLite here (that is the direction this command
    supports), so its cursor is used directly rather than through ``rows``,
    which is list-returning by contract.
    """

    cursor = source.conn.execute(f"SELECT * FROM {table}")
    columns: list[str] | None = None
    statement = ""
    copied = 0
    while True:
        page = cursor.fetchmany(BATCH)
        if not page:
            break
        if columns is None:
            columns = [description[0] for description in cursor.description]
            placeholders = ",".join("?" for _ in columns)
            statement = f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})"
        target.backend.executemany(
            statement, [tuple(row[column] for column in columns) for row in page]
        )
        copied += len(page)
    if copied:
        target.conn.commit()
    return copied


def _rebuild_fts(target: StateStore) -> int:
    """Regenerate ``chunks_fts`` from the copied tables, for the target dialect.

    The same projection ``_migrate_fts_titles`` uses, so ``title`` is the
    basename and not a second copy of the path — the distinction the
    2026-08-03 ranking work depends on.
    """

    target.conn.execute("DELETE FROM chunks_fts")
    target.conn.execute(
        """INSERT INTO chunks_fts(
               chunk_id, source_id, artifact_id, title, path, heading_path, text)
           SELECT chunks.id, chunks.source_id, chunks.artifact_id,
                  replace(
                      COALESCE(artifacts.relative_path, artifacts.path),
                      rtrim(COALESCE(artifacts.relative_path, artifacts.path),
                            replace(COALESCE(artifacts.relative_path, artifacts.path),
                                    '/', '')),
                      ''),
                  COALESCE(artifacts.relative_path, artifacts.path),
                  COALESCE(chunks.heading_path, ''),
                  chunks.text
           FROM chunks JOIN artifacts ON artifacts.id = chunks.artifact_id"""
    )
    target.conn.commit()
    return len(target.rows("SELECT chunk_id FROM chunks_fts"))


def migrate_sqlite_to_postgres(
    sqlite_path: str | Path,
    dsn: str,
    *,
    pool_size: int = 10,
    keep_original: bool = True,
) -> dict[str, Any]:
    """Copy a SQLite state database into Postgres and verify it landed.

    Returns a report: per-table row counts, the verification outcome, and where
    the original was parked.

    **Idempotent.** A target that already holds rows for a table is left alone
    rather than double-inserted, so an interrupted run can simply be re-run.
    """

    source_path = Path(sqlite_path)
    if not source_path.is_file():
        raise MigrationError(f"no SQLite state database at {source_path}")

    source = StateStore(backend=SqliteBackend(source_path))
    target = StateStore(backend=PostgresBackend(dsn, pool_size=pool_size))
    report: dict[str, Any] = {
        "source": str(source_path),
        "target": redact_dsn(dsn),
        "tables": {},
        "skipped": [],
    }
    skipped_counts: dict[str, int] = {}
    try:
        target.migrate()
        for table in TABLE_ORDER:
            if not source.backend.table_columns(table):
                continue  # older state file that predates this table
            existing = target.rows(f"SELECT count(*) AS n FROM {table}")
            if existing and int(existing[0]["n"]) > 0:
                report["skipped"].append(table)
                # Remembered so verification can check it too. A table skipped
                # as "already there" is exactly a table a previous run may have
                # been interrupted part-way through, and those were the only
                # tables the check did not look at — so the one shape that
                # needs verifying most was the one that got a free pass, and
                # then the original was renamed on the strength of it.
                skipped_counts[table] = int(existing[0]["n"])
                continue
            report["tables"][table] = _copy_table(source, target, table)
        report["chunks_fts"] = _rebuild_fts(target)

        # Verify before renaming anything. A migration that silently dropped
        # half the artifacts and then moved the original aside would be the
        # worst possible outcome.
        mismatches = []
        for table, copied in report["tables"].items():
            landed = int(target.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"])
            if landed != copied:
                mismatches.append(f"{table}: copied {copied}, found {landed}")
        for table, landed in skipped_counts.items():
            expected = int(source.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"])
            if landed != expected:
                mismatches.append(
                    f"{table}: skipped as already present with {landed} rows, "
                    f"but the source has {expected} — a previous run was interrupted "
                    f"part-way through this table; empty it and re-run"
                )
        source_chunks = int(source.rows("SELECT count(*) AS n FROM chunks")[0]["n"])
        if report["chunks_fts"] != source_chunks:
            mismatches.append(
                f"chunks_fts: rebuilt {report['chunks_fts']} rows for {source_chunks} chunks"
            )
        if mismatches:
            report["verified"] = False
            report["mismatches"] = mismatches
            raise MigrationError(
                "migration did not verify; the original was left untouched: "
                + "; ".join(mismatches)
            )
        report["verified"] = True
    finally:
        source.close()
        target.close()

    if keep_original:
        parked = source_path.with_suffix(source_path.suffix + ".migrated")
        # Never overwrite an earlier parked copy — that would destroy the very
        # thing this is preserving.
        counter = 1
        while parked.exists():
            parked = source_path.with_suffix(f"{source_path.suffix}.migrated.{counter}")
            counter += 1
        source_path.rename(parked)
        report["original_renamed_to"] = str(parked)
        logger.info("SQLite state parked at %s", parked)
    return report
