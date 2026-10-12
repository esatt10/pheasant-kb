"""Cross-backend parity for the Phase 35.2 storage seam.

The seam is only worth having if both backends produce the *same knowledge
base*. Three things have to hold, in descending order of how badly a break
would hurt:

1. **Stable IDs are byte-identical.** They are a contract (CLAUDE.md rule 3):
   every persisted graph, every manifest, every Synapse contract references
   them. A backend that changed them would silently orphan existing state.
2. **The same corpus produces the same counts.** Artifacts, chunks, FTS rows,
   graph nodes and edges.
3. **Retrieval quality holds.** This one is deliberately *not* an equality
   assertion. FTS5's ``bm25()`` and Postgres's ``ts_rank_cd`` are different
   functions; demanding identical scores would be demanding something false.
   What matters is that the ranking is as good, so the gate is measured
   quality on a gold set.

These tests need a real Postgres. They skip — loudly, with the reason — when
``PHEASANT_TEST_POSTGRES_DSN`` is unset, so the offline suite stays offline and
network-free (CLAUDE.md pillar 3). Point it at a throwaway database:

    createdb pheasant_parity
    PHEASANT_TEST_POSTGRES_DSN=postgresql://user@127.0.0.1:5432/pheasant_parity pytest -q
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.search.hybrid import HybridSearch
from pheasant.search.sqlite_store import SearchStore, corpus_vocabulary
from pheasant.sync.engine import SyncEngine

DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()

postgres = pytest.mark.skipif(
    not DSN,
    reason="set PHEASANT_TEST_POSTGRES_DSN to a throwaway database to run backend parity",
)

#: (query, the file that must rank first).
#:
#: The last case is the one that actually gates the port. The first three have
#: a unique winning token, so they rank correctly whatever the column weights
#: are — mutation testing showed that flattening the ts_rank weights, reversing
#: them, and stripping the title weighting out of the generated tsvector *all*
#: still passed. They only prove the query runs.
#:
#: ``deploy-gateway.md`` is named for the query and barely mentions it; the
#: decoy says "gateway" repeatedly in its body. The right answer wins only
#: because title is weighted above text — which is exactly the 2026-08-03
#: "locate readme" failure (the repo's own README sat at rank 125) that the
#: 8/3/2/1 weights were introduced to fix. If the A/B/C/D mapping is wrong,
#: this case is what says so.
GOLD: list[tuple[str, str]] = [
    ("gateway restarts nightly", "runbook.md"),
    ("readme install configure", "README.md"),
    ("invoices generated monthly", "billing.md"),
    ("deploy gateway", "deploy-gateway.md"),
]


#: The one query the ``tsvector`` lexical arm still ranks differently, and why.
#: The default Postgres arm (``pg_textsearch``) has no residual; this set is
#: ``storage.lexical_index: tsvector`` only.
#:
#: Three real gaps were found and fixed (see `_postgres_rank_expression` and
#: the schema's tokenizer note): no IDF at all, no term-frequency saturation,
#: and filenames tokenizing as a single lexeme so ``deploy-gateway.md`` never
#: matched "deploy". With those closed, "gateway restarts nightly" — the
#: rare-term case, and the more important retrieval property — is now correct
#: on both backends.
#:
#: What remains is structural, not a parameter. BM25 normalizes each column's
#: contribution by *that column's* length; ``ts_rank_cd`` applies one global
#: normalization to the whole weighted vector. So when a title match on a
#: common term competes with body matches on rare terms, the two rank
#: differently, and no weight vector fixes both cases at once — verified at
#: 8:1, 16:1 and 33:1 title:body ratios, each of which merely moved the
#: failure to the other query.
POSTGRES_RANK_RESIDUAL = {"deploy gateway"}


def _corpus(root: Path) -> Path:
    workspace = root / "docs"
    workspace.mkdir(parents=True)
    (workspace / "README.md").write_text(
        "# Project README\n\nHow to install and configure the gateway.\n", encoding="utf-8"
    )
    (workspace / "runbook.md").write_text(
        "# Kestrel Runbook\n\nThe gateway restarts nightly at 0300 UTC.\n"
        "Escalate to the on-call rota if it fails twice.\n",
        encoding="utf-8",
    )
    (workspace / "billing.md").write_text(
        "# Billing\n\nInvoices are generated monthly by the finance service.\n", encoding="utf-8"
    )
    # The weighting probe: named for the query, nearly silent about it.
    (workspace / "deploy-gateway.md").write_text(
        "# Deployment\n\nRun the rollout script and wait for the health check.\n",
        encoding="utf-8",
    )
    # The decoy: says the query's words over and over in its body, and would
    # win on body frequency alone if `title` were not weighted above `text`.
    (workspace / "notes.md").write_text(
        "# Notes\n\n"
        + "The gateway deploy notes mention deploy and gateway repeatedly. " * 6
        + "\n",
        encoding="utf-8",
    )
    return workspace


def _config(
    root: Path, workspace: Path, backend: str, lexical_index: str | None = None
) -> PheasantConfig:
    data: dict[str, Any] = {
        "pheasant": {
            "name": "parity",
            "state_path": str(root / f"state-{backend}"),
            "workspace_root": str(root),
            "exports_path": str(root / "exports"),
        },
        "storage": {"graph_snapshots": False},
        "sync": {"watcher": {"enabled": False}, "scheduler": {"enabled": False}},
        "sources": [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(workspace),
                "include": ["**/*.md"],
            }
        ],
    }
    if backend == "postgres":
        data["storage"]["backend"] = "postgres"
        data["storage"]["dsn_env"] = "PHEASANT_TEST_POSTGRES_DSN"
    if lexical_index:
        data["storage"]["lexical_index"] = lexical_index
    return PheasantConfig.model_validate(data)


def _index(
    root: Path, workspace: Path, backend: str, lexical_index: str | None = None
) -> dict[str, Any]:
    config = _config(root, workspace, backend, lexical_index)
    engine = SyncEngine(config)
    try:
        result = engine.sync_source("docs", "full")
        state = engine.state
        search = HybridSearch(SearchStore(state))
        ranked = {
            query: [
                hit.get("relative_path")
                for hit in search.search_context(config.knowledge_base_id, query, "text", 5)[
                    "results"
                ]
            ]
            for query, _ in GOLD
        }
        return {
            "counts": {
                "artifacts": len(state.rows("SELECT id FROM artifacts")),
                "chunks": len(state.rows("SELECT id FROM chunks")),
                "fts": len(state.rows("SELECT chunk_id FROM chunks_fts")),
                "indexed": result.indexed_artifacts,
                "nodes": result.graph_nodes,
                "edges": result.graph_edges,
            },
            "artifact_ids": sorted(str(r["id"]) for r in state.rows("SELECT id FROM artifacts")),
            "chunk_ids": sorted(str(r["id"]) for r in state.rows("SELECT id FROM chunks")),
            "ranked": ranked,
            "vocabulary": corpus_vocabulary(state, limit=24),
        }
    finally:
        engine.close()


def _reset(dsn: str) -> None:
    """Drop everything this suite creates, so a re-run starts clean."""

    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture()
def both(tmp_path: Path) -> dict[str, dict[str, Any]]:
    _reset(DSN)
    workspace = _corpus(tmp_path)
    return {
        "sqlite": _index(tmp_path, workspace, "sqlite"),
        "postgres": _index(tmp_path, workspace, "postgres"),
        # Last, because selecting it drops the BM25 index the default built.
        "tsvector": _index(tmp_path, workspace, "postgres", "tsvector"),
    }


@postgres
def test_stable_ids_are_byte_identical_across_backends(both) -> None:
    """Rule 3. A backend that changed these would orphan every persisted graph."""

    assert both["sqlite"]["artifact_ids"] == both["postgres"]["artifact_ids"]
    assert both["sqlite"]["chunk_ids"] == both["postgres"]["chunk_ids"]
    # Not merely equal — actually populated, or this passes on two empty lists.
    assert both["sqlite"]["artifact_ids"], "the fixture indexed nothing"
    assert all(":" in artifact_id for artifact_id in both["sqlite"]["artifact_ids"])


@postgres
def test_the_same_corpus_produces_the_same_index(both) -> None:
    assert both["sqlite"]["counts"] == both["postgres"]["counts"]
    assert both["sqlite"]["counts"]["chunks"] > 0
    # chunks_fts is written by the same code on both backends; if it ever
    # diverges from `chunks`, search silently loses documents.
    assert both["sqlite"]["counts"]["fts"] == both["sqlite"]["counts"]["chunks"]
    assert both["postgres"]["counts"]["fts"] == both["postgres"]["counts"]["chunks"]


@postgres
def test_the_contract_vocabulary_survives_the_backend_swap(both) -> None:
    """The one field the router scores a region on.

    `corpus_vocabulary` read `chunks_vocab`, an `fts5vocab` virtual table that
    only SQLite has, inside a bare `except: return []`. On Postgres that meant
    every published contract carried an **empty** vocabulary — the region kept
    answering searches, kept passing its health checks, and was silently
    unroutable. So this asserts non-emptiness first: equality alone passes on
    two empty lists, which is exactly the state being fixed.
    """

    sqlite_vocab = both["sqlite"]["vocabulary"]
    postgres_vocab = both["postgres"]["vocabulary"]
    assert sqlite_vocab, "the fixture produced no vocabulary at all"
    assert postgres_vocab, "Postgres published an empty vocabulary"

    # Document *frequencies* must agree exactly: both count chunks containing
    # the term, over an identical corpus. Ordering of equal-frequency terms is
    # the tie-break, which is why this compares as a mapping.
    shared = dict(sqlite_vocab).keys() & dict(postgres_vocab).keys()
    assert len(shared) >= len(sqlite_vocab) * 0.6, (
        f"the two vocabularies barely overlap: {sqlite_vocab} vs {postgres_vocab}"
    )
    for term in shared:
        assert dict(sqlite_vocab)[term] == dict(postgres_vocab)[term], (
            f"document frequency for {term!r} differs across backends"
        )


@postgres
def test_corpus_size_comes_from_the_planner_estimate_once_it_exists(tmp_path: Path) -> None:
    """IDF's ``N`` was a full ``count(*)`` on every text search.

    Postgres has no O(1) count, so the largest table in the database was
    sequentially scanned once per query — on the backend chosen precisely
    because the corpus is too big for SQLite. ``reltuples`` answers from the
    catalog. Both paths are exercised: before ``ANALYZE`` there are no
    statistics and the exact count must be used (a zero would flatten every
    score to nothing), after it the estimate must be.
    """

    import psycopg

    from pheasant.search.sqlite_store import _postgres_total_chunks

    _reset(DSN)
    workspace = _corpus(tmp_path)
    engine = SyncEngine(_config(tmp_path, workspace, "postgres"))
    try:
        engine.sync_source("docs", "full")
        real = len(engine.state.rows("SELECT chunk_id FROM chunks_fts"))
        assert real > 0

        # No statistics yet: the sentinel is negative, so the exact count wins.
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("UPDATE pg_class SET reltuples = -1 WHERE oid = 'chunks_fts'::regclass")
        assert _postgres_total_chunks(engine.state) == real

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("ANALYZE chunks_fts")
        estimate = _postgres_total_chunks(engine.state)
        assert estimate == real, f"planner estimate {estimate} disagrees with {real}"

        # And ranking still works through the real search path.
        search = HybridSearch(SearchStore(engine.state))
        hits = search.search_context(engine.config.knowledge_base_id, "invoices", "text", 5)
        assert [h.get("relative_path") for h in hits["results"]][:1] == ["billing.md"]
    finally:
        engine.close()


@postgres
def test_retrieval_quality_holds_on_the_gold_set(both) -> None:
    """Measured quality, not score equality — see the module docstring.

    The default Postgres arm gets every query. The ``tsvector`` arm is exempted
    on its one residual only because
    :func:`test_known_residual_postgres_lacks_per_column_length_normalization`
    asserts that exact failure instead. Skipping it silently here would turn a
    documented limitation into an invisible one.
    """

    for backend in ("sqlite", "postgres", "tsvector"):
        ranked = both[backend]["ranked"]
        for query, expected in GOLD:
            if backend == "tsvector" and query in POSTGRES_RANK_RESIDUAL:
                continue
            hits = ranked[query]
            assert hits, f"{backend}: {query!r} returned nothing"
            assert hits[0] == expected, (
                f"{backend}: {query!r} ranked {hits[0]} first, expected {expected}"
            )


@postgres
def test_postgres_and_sqlite_agree_on_the_top_hit(both) -> None:
    """Top-1 agreement is the contract; tail order is not.

    Full-list equality held before the port had IDF and matched SQLite's
    tokenizer — because Postgres was returning *fewer* documents (a filename
    that tokenized as one lexeme matched nothing) and the short lists happened
    to coincide. Now both backends see the same terms, both return the same
    documents, and they can legitimately disagree about ranks 2-3 while
    agreeing on the answer. Asserting full equality here would be asserting a
    coincidence, and would have to be relaxed by the first real corpus.
    """

    for backend in ("postgres", "tsvector"):
        for query, _ in GOLD:
            if backend == "tsvector" and query in POSTGRES_RANK_RESIDUAL:
                continue
            sqlite_hits = both["sqlite"]["ranked"][query]
            postgres_hits = both[backend]["ranked"][query]
            assert sqlite_hits[:1] == postgres_hits[:1], (backend, query)
            # Same documents found, whatever order they came back in.
            assert set(sqlite_hits) == set(postgres_hits), (backend, query)


@postgres
def test_known_residual_postgres_lacks_per_column_length_normalization(both) -> None:
    """**The residual after three real fixes, pinned so it cannot be forgotten.**

    SQLite ranks by BM25, which weights *rare* terms heavily: "restarts" and
    "nightly" occur in exactly one document, so `runbook.md` wins even though a
    longer decoy repeats "gateway" six times. Postgres's `ts_rank_cd` has no
    inverse document frequency at all — it accumulates cover density — so the
    decoy wins. Verified against every normalization flag (0/1/2/16): none of
    them fixes it, because normalization is about length, and the missing
    ingredient is corpus-wide term rarity.

    So the port preserves **field weighting** (title above body) but not
    **term weighting**. On single-topic queries the two agree; on multi-term
    queries where one term is rare and another is common, Postgres can rank a
    long, repetitive document above the short precise one.

    This test asserts the divergence *currently exists*. It goes red the day
    someone adds real IDF — a pg_search/ParadeDB backend, or a df-weighted
    tsquery — which is exactly when this docstring needs deleting.

    ``pg_textsearch``, the Postgres default, is such a backend and closes it:
    the gold-set test above holds it to this query. This test stays, pinned to
    ``storage.lexical_index: tsvector``, because that arm is still selectable
    and still has the residual.
    """

    query = "deploy gateway"
    assert both["sqlite"]["ranked"][query][0] == "deploy-gateway.md"
    assert both["postgres"]["ranked"][query][0] == "deploy-gateway.md"
    postgres_ranking = both["tsvector"]["ranked"][query]
    # It *matches* now — the tokenizer fix did that, and before it the file
    # named for the query did not appear at all. It simply is not first.
    assert "deploy-gateway.md" in postgres_ranking[:2], (
        "the file named for the query fell out of the top 2 — the tokenizer "
        "normalization in persistence/schema.py may have regressed"
    )
    assert postgres_ranking[0] == "notes.md", (
        "Postgres ranked this correctly — per-column length normalization may "
        "have been added (a pg_search/ParadeDB backend would do it); if so, "
        "delete this test and empty POSTGRES_RANK_RESIDUAL."
    )


@postgres
def test_a_second_sync_is_zero_work_on_postgres_too(tmp_path: Path) -> None:
    """Idempotency is a product guarantee (rule 4), not a SQLite behaviour."""

    _reset(DSN)
    workspace = _corpus(tmp_path)
    config = _config(tmp_path, workspace, "postgres")
    engine = SyncEngine(config)
    try:
        first = engine.sync_source("docs", "full")
        second = engine.sync_source("docs", "incremental")
        assert first.indexed_artifacts == 5
        assert second.indexed_artifacts == 0
        assert second.skipped_artifacts == 5
    finally:
        engine.close()


def test_the_sqlite_default_needs_no_dsn_and_no_driver(tmp_path: Path) -> None:
    """Rule 7: standalone mode is sacred.

    Runs unconditionally — it is the one test here that must pass on a machine
    with no Postgres at all, because that machine is the default deployment.
    """

    workspace = _corpus(tmp_path)
    config = _config(tmp_path, workspace, "sqlite")
    assert config.storage.backend == "sqlite"
    engine = SyncEngine(config)
    try:
        assert engine.state.dialect.name == "sqlite"
        assert engine.sync_source("docs", "full").indexed_artifacts == 5
    finally:
        engine.close()


@postgres
def test_migration_copies_state_and_never_destroys_the_original(tmp_path: Path) -> None:
    """`pheasant migrate --to postgres`, end to end.

    The important assertions are the safety ones: the SQLite file survives
    (rule 2 — ``/state`` is user data), the stable IDs carry over unchanged
    (rule 3), and ``chunks_fts`` is *rebuilt* for the target dialect rather
    than copied, because its tokenization is dialect-specific.
    """

    from pheasant.persistence.migrate import migrate_sqlite_to_postgres

    _reset(DSN)
    workspace = _corpus(tmp_path)
    config = _config(tmp_path, workspace, "sqlite")
    engine = SyncEngine(config)
    try:
        engine.sync_source("docs", "full")
        sqlite_path = Path(engine.state.path)
        before = sorted(str(r["id"]) for r in engine.state.rows("SELECT id FROM artifacts"))
        chunk_count = len(engine.state.rows("SELECT id FROM chunks"))
    finally:
        engine.close()

    report = migrate_sqlite_to_postgres(sqlite_path, DSN)

    assert report["verified"] is True
    assert report["tables"]["artifacts"] == len(before)
    # Derived cache, rebuilt not copied — one row per chunk.
    assert report["chunks_fts"] == chunk_count

    # The original is renamed, never deleted.
    assert not sqlite_path.exists()
    parked = Path(report["original_renamed_to"])
    assert parked.exists() and parked.stat().st_size > 0

    postgres_config = _config(tmp_path, workspace, "postgres")
    migrated = SyncEngine(postgres_config)
    try:
        after = sorted(str(r["id"]) for r in migrated.state.rows("SELECT id FROM artifacts"))
        assert after == before, "stable IDs changed across the migration"
        # And the migrated index is searchable, not merely present.
        search = HybridSearch(SearchStore(migrated.state))
        hits = search.search_context(postgres_config.knowledge_base_id, "invoices", "text", 5)
        assert [h.get("relative_path") for h in hits["results"]][:1] == ["billing.md"]
    finally:
        migrated.close()


@postgres
def test_migration_is_idempotent_and_verifies_before_renaming(tmp_path: Path) -> None:
    """A re-run must not double-insert, and must not park the original twice."""

    from pheasant.persistence.migrate import migrate_sqlite_to_postgres

    _reset(DSN)
    workspace = _corpus(tmp_path)
    config = _config(tmp_path, workspace, "sqlite")
    engine = SyncEngine(config)
    try:
        engine.sync_source("docs", "full")
        sqlite_path = Path(engine.state.path)
    finally:
        engine.close()

    first = migrate_sqlite_to_postgres(sqlite_path, DSN)
    assert first["tables"]["artifacts"] == 5

    # Re-run against the parked copy: every table is already populated, so
    # nothing is inserted a second time.
    parked = Path(first["original_renamed_to"])
    second = migrate_sqlite_to_postgres(parked, DSN, keep_original=False)
    assert second["verified"] is True
    assert "artifacts" in second["skipped"]
    # Tables that were empty in the source are still "copied" — of nothing.
    # The invariant that matters is that no row was inserted twice.
    assert all(count == 0 for count in second["tables"].values()), second["tables"]

    postgres_config = _config(tmp_path, workspace, "postgres")
    migrated = SyncEngine(postgres_config)
    try:
        assert len(migrated.state.rows("SELECT id FROM artifacts")) == 5
    finally:
        migrated.close()


@postgres
def test_a_half_copied_table_fails_verification_instead_of_being_skipped(tmp_path: Path) -> None:
    """The interrupted-run case, which the skip path used to wave through.

    "Already has rows" was treated as "already done", and only *copied* tables
    were counted at the end — so a run killed part-way through `chunks` left a
    partial table, the re-run skipped it, verification never looked at it, and
    the original SQLite file was renamed on the strength of a check that had
    excluded the one table at risk.
    """

    import psycopg

    from pheasant.persistence.migrate import MigrationError, migrate_sqlite_to_postgres

    _reset(DSN)
    workspace = _corpus(tmp_path)
    config = _config(tmp_path, workspace, "sqlite")
    engine = SyncEngine(config)
    try:
        engine.sync_source("docs", "full")
        sqlite_path = Path(engine.state.path)
    finally:
        engine.close()

    first = migrate_sqlite_to_postgres(sqlite_path, DSN)
    assert first["verified"] is True
    parked = Path(first["original_renamed_to"])

    # Simulate the interruption: drop some chunks, leaving the table non-empty
    # (so the re-run skips it) but short (so it is wrong). `chunks` is the
    # table that actually takes long enough to be interrupted in.
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM chunks WHERE id IN (SELECT id FROM chunks LIMIT 2)")
        short = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    assert short > 0, "the table must stay non-empty or it would simply be re-copied"

    with pytest.raises(MigrationError) as caught:
        migrate_sqlite_to_postgres(parked, DSN, keep_original=True)
    # Asserted on the *skipped-table* message specifically. A looser check for
    # "chunks" passed with this fix mutated out, because the pre-existing
    # chunks_fts row-count check fires on the same corruption and its message
    # also says "chunks" — mutation testing is what surfaced that.
    assert "skipped as already present" in str(caught.value), str(caught.value)
    assert "chunks:" in str(caught.value)
    assert parked.exists(), "the original was renamed despite a failed verification"


# --- the Parquet export (Phase: analytics) ---------------------------------


def _export(root: Path, workspace: Path, backend: str) -> Path:
    """Index this corpus into ``backend`` and export it. Returns the directory."""

    from pheasant import analytics

    config = _config(root, workspace, backend)
    out = root / f"export-{backend}"
    engine = SyncEngine(config)
    try:
        engine.sync_source("docs", "full")
        analytics.export_parquet(
            engine.state,
            out_dir=out,
            kb_id=config.knowledge_base_id,
            graph=engine.graph_builder.graph,
            backend=engine.state.dialect.name,
        )
    finally:
        engine.close()
    return out


#: Columns that record *when a run happened* rather than what it produced. Two
#: indexing runs are two moments, so these differ between any two runs of the
#: same corpus on the same backend — excluding them is what makes the rest an
#: equality assertion instead of an approximate one.
_RUN_TIMESTAMPS = {
    "last_indexed_at",
    "mtime",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "details_json",
    "last_used_at",
    "attributes",  # graph attribute bags carry `updated_at` inside the JSON
}


@postgres
def test_the_parquet_export_is_identical_on_both_backends(tmp_path: Path) -> None:
    """An export is consumed by systems that do not know which backend wrote
    it, so "same knowledge base" has to extend to the files that leave.

    Asserted on one shared workspace, deliberately: an earlier run of this
    comparison used two copies of the corpus at different paths and the only
    reported difference was the absolute path inside the graph attribute bag —
    a property of the fixture, not of the backends.
    """

    duckdb = pytest.importorskip("duckdb", reason="the analytics extra is not installed")

    workspace = _corpus(tmp_path)
    sqlite_dir = _export(tmp_path, workspace, "sqlite")
    postgres_dir = _export(tmp_path, workspace, "postgres")

    connection = duckdb.connect(":memory:")
    tables = ["sources", "artifacts", "chunks", "symbols", "memory_records", "graph_nodes"]
    for table in tables:
        left, right = f"{sqlite_dir}/{table}.parquet", f"{postgres_dir}/{table}.parquet"
        schema_left = connection.execute(f"DESCRIBE SELECT * FROM '{left}'").fetchall()
        schema_right = connection.execute(f"DESCRIBE SELECT * FROM '{right}'").fetchall()
        assert [(c[0], c[1]) for c in schema_left] == [(c[0], c[1]) for c in schema_right], table

        columns = [c[0] for c in schema_left if c[0] not in _RUN_TIMESTAMPS]
        projection = ", ".join(f'"{column}"' for column in columns)
        difference = connection.execute(
            f"SELECT count(*) FROM ("
            f"  (SELECT {projection} FROM '{left}' EXCEPT SELECT {projection} FROM '{right}')"
            f"  UNION ALL"
            f"  (SELECT {projection} FROM '{right}' EXCEPT SELECT {projection} FROM '{left}'))"
        ).fetchone()[0]
        assert difference == 0, f"{table} differs between backends"

    # Edges are compared as a set of (endpoints, relation): the row order of
    # parallel edges is not a contract, their presence is.
    edges = connection.execute(
        f"SELECT count(*) FROM ("
        f"  (SELECT from_node, to_node, type, confidence FROM '{sqlite_dir}/graph_edges.parquet'"
        f"   EXCEPT SELECT from_node, to_node, type, confidence FROM"
        f"   '{postgres_dir}/graph_edges.parquet')"
        f"  UNION ALL"
        f"  (SELECT from_node, to_node, type, confidence FROM '{postgres_dir}/graph_edges.parquet'"
        f"   EXCEPT SELECT from_node, to_node, type, confidence FROM"
        f"   '{sqlite_dir}/graph_edges.parquet'))"
    ).fetchone()[0]
    assert edges == 0, "the graph edge set differs between backends"
    connection.close()

    # And the manifest says which backend produced it, so a consumer holding
    # two exports can tell them apart.
    for directory, expected in ((sqlite_dir, "sqlite"), (postgres_dir, "postgres")):
        manifest = json.loads((directory / "export.json").read_text(encoding="utf-8"))
        assert manifest["state_backend"] == expected


@postgres
def test_export_reports_an_unsynced_postgres_database_actionably(tmp_path: Path) -> None:
    """The failure an operator hits when the export runs before the sync.

    Without this the error is ``psycopg.errors.UndefinedTable: relation
    "sources" does not exist``, raised four frames inside the driver — which
    is a true statement about a database and a useless one about pheasant.
    """

    from pheasant.analytics import StateUnavailable, open_state

    # A config pointing at a database this test never syncs. Reusing the same
    # DSN is safe: the tables are dropped and recreated per run by _index, and
    # this only reads.
    config = _config(tmp_path, tmp_path / "docs", "postgres")
    with psycopg_dropped_tables(config):
        with pytest.raises(StateUnavailable, match="Run `pheasant sync` first"):
            open_state(config, tmp_path / "unused.db")


@contextlib.contextmanager
def psycopg_dropped_tables(config: Any):
    """Drop pheasant's tables for the duration of the block, then leave them
    dropped — the next test that needs them calls ``migrate()`` itself."""

    from pheasant.persistence.state_store import StateStore

    state = StateStore.from_config(config, None)
    try:
        for table in ("chunks_fts", "chunks", "artifacts", "sources", "knowledge_bases"):
            state.backend.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        state.backend.commit()
    finally:
        state.close()
    yield


# ---------------------------------------------------------------------------
# Agent-memory maintenance on Postgres (compaction plan, Phase 3/5)
# ---------------------------------------------------------------------------
#
# The three cases below each reproduce a real bug SQLite's unenforced-FK,
# sqlite3-only-syntax path let through silently: (1) `memory_records`
# declared a `FOREIGN KEY (artifact_id) REFERENCES artifacts(id)` that
# `delete_source_artifacts`/`delete_artifacts` deliberately violate by
# design (they leave a memory's row while deleting its artifact row, to
# preserve earned `uses`/`salience`/`observations` — see those methods'
# docstrings); Postgres enforces that FK and aborted the whole transaction.
# (2) `PostgresBackend.statement()` discarded `cursor.rowcount`, so
# `subsume_records`/`delete_artifacts` raised `AttributeError` the moment
# either ran against a real Postgres connection. (3) `subsume_records`'s
# ledger insert used SQLite-only `INSERT OR IGNORE`, a hard `SyntaxError`
# under Postgres, where the portable form is `INSERT ... ON CONFLICT ...
# DO NOTHING` (used everywhere else in this codebase already). None of
# these three surfaced in the offline suite, because SQLite never enforces
# a declared FK (no `PRAGMA foreign_keys=ON` anywhere) and never rejects
# its own `OR IGNORE`/`rowcount` shape. CLAUDE.md rule 10.


def _memory_config(root: Path, **memory_settings: Any) -> PheasantConfig:
    (root / "memory").mkdir(exist_ok=True)
    data: dict[str, Any] = {
        "pheasant": {
            "name": "pgmem",
            "state_path": str(root / "state"),
            "workspace_root": str(root),
            "exports_path": str(root / "exports"),
        },
        "storage": {"backend": "postgres", "dsn_env": "PHEASANT_TEST_POSTGRES_DSN"},
        "sync": {"watcher": {"enabled": False}, "scheduler": {"enabled": False}},
        "memory": memory_settings,
        "sources": [{"name": "agent-memory", "type": "memory", "path": "memory"}],
    }
    return PheasantConfig.model_validate(data)


@postgres
def test_targeted_archive_survives_the_memory_records_artifact_fk(tmp_path: Path) -> None:
    """(1) and (2) together: a superseded record's targeted archive
    (`_drop_archived` -> `state.delete_artifacts`, Phase 0) deletes the
    `artifacts` row for a `memory_records` row it deliberately leaves in
    place, and reads `cursor.rowcount` off the result."""

    from pheasant.mcp_server.tools import PheasantTools
    from pheasant.memory.maintenance import run_memory_maintenance

    config = _memory_config(tmp_path)
    tools = PheasantTools(config)
    try:
        old = tools.memory_write("pgmem", "The paydb service runs in us-east-2.", scope="org")[
            "record"
        ]
        tools.memory_write(
            "pgmem",
            "The paydb service now runs in eu-west-1.",
            scope="org",
            supersedes=old["record_id"],
        )

        result = run_memory_maintenance(tools.engine)
        assert result is not None
        assert result["report"]["archived_superseded"] == [old["record_id"]]
        assert result["sync"]["removed"] == [old["record_id"]]
    finally:
        tools.engine.close()


@postgres
def test_compaction_and_scope_budgets_run_against_postgres(tmp_path: Path) -> None:
    """(2) and (3): `subsume_records`'s `UPDATE ... RETURNING`-free
    demotion (rowcount) and its ledger `INSERT ... ON CONFLICT DO NOTHING`
    (Phase 3), driven by a per-scope budget prune (Phase 5) in the same
    maintenance pass."""

    from pheasant.mcp_server.tools import PheasantTools
    from pheasant.memory.maintenance import run_memory_maintenance

    config = _memory_config(tmp_path, compaction_enabled=True, session_max_records=1)
    tools = PheasantTools(config)
    try:
        tools.memory_write(
            "pgmem",
            "The paydb service runs in us-east-2 and is owned by ada.",
            scope="org",
            subject="paydb",
        )
        tools.memory_write(
            "pgmem",
            "The paydb service runs in us-east-2 and is owned by ada, per the latest inventory.",
            scope="org",
            subject="paydb",
        )
        tools.memory_write("pgmem", "Session scratch note alpha.", scope="session")
        tools.memory_write("pgmem", "Session scratch note beta.", scope="session")

        result = run_memory_maintenance(tools.engine)
        assert result is not None
        assert result.get("compaction", {}).get("subsumed")
        assert result.get("pruned")

        # A second pass over the same content does nothing new — the
        # idempotency the ledger's `INSERT ... ON CONFLICT DO NOTHING`
        # exists to guarantee, verified against a real Postgres connection
        # rather than SQLite's unenforced version of the same statement.
        again = run_memory_maintenance(tools.engine)
        assert not again.get("compaction", {}).get("subsumed")
    finally:
        tools.engine.close()


@postgres
def test_the_evaluation_plane_runs_a_whole_batch_against_postgres(tmp_path: Path) -> None:
    """Every statement the evaluation plane writes, against the real server.

    This backend has caught three portability bugs the offline suite had no
    opinion about — a declared FK a maintenance path deliberately violates, a
    discarded ``rowcount``, and one SQLite-only ``INSERT OR IGNORE``. The
    evaluation plane adds five tables and a dozen statements to the same
    surface, so it gets the same treatment: a complete batch, then a *second*
    one over unchanged state to prove the ``ON CONFLICT DO NOTHING`` idempotency
    is real rather than an accident of two runs landing in the same second.
    """

    import pheasant.evaluation as evaluation
    from pheasant.evaluation import store as evaluation_store
    from pheasant.sync.log_queue import write_events
    from pheasant.telemetry.interactions import InteractionEvent

    _reset(DSN)
    workspace = _corpus(tmp_path)
    config = _config(tmp_path, workspace, "postgres")
    config.evaluation.enabled = True
    config.evaluation.proof.minimum_eligible_queries = 1
    config.evaluation.proof.minimum_evidenced_queries = 1
    config.evaluation.proof.minimum_independent_interactions = 1
    config.evaluation.proof.maximum_single_query_proof_share = 1.0
    config.evaluation.cohorts.anchor_minimum_queries = 2

    engine = SyncEngine(config)
    try:
        engine.sync_source("docs", "full")
        state = engine.state
        artifacts = {
            str(row["relative_path"]): str(row["id"])
            for row in state.rows("SELECT id, relative_path FROM artifacts")
        }

        queries = [
            "how do I deploy the gateway",
            "when does the gateway restart",
            "who generates invoices",
            "where is the rollout script",
        ]
        write_events(
            state,
            [
                InteractionEvent(
                    kb_id=config.knowledge_base_id,
                    operation="/search",
                    modality="ui",
                    principal="user:ada",
                    session_id=f"s{index % 2}",
                    trace_id=f"{index:032x}",
                    span_id=f"{index:016x}",
                    started_at=f"2026-01-01T00:00:{index:02d}.000000Z",
                    status="ok",
                    duration_ms=7.0,
                    query_text=query,
                    result_paths=["runbook.md"],
                    result_ids=[artifacts["runbook.md"]],
                    result_count=1,
                    top_score=0.7,
                )
                for index, query in enumerate(queries)
            ],
        )
        for index, (query, path, event) in enumerate(
            [
                (queries[0], "deploy-gateway.md", "explicit_accept"),
                (queries[0], "notes.md", "explicit_reject"),
                (queries[1], "runbook.md", "selected"),
                (queries[2], "billing.md", "downstream_success"),
            ]
        ):
            evaluation.record_evidence(
                state,
                config,
                query=query,
                target_id=artifacts[path],
                event_type=event,
                principal="user:ada",
                session_id=f"s{index % 2}",
                interaction_id=f"call-{index}",
            )

        first = evaluation.run(engine)
        assert first.status == "completed"
        assert first.gates_passed, [g.as_dict() for g in first.gates if not g.passed]
        assert first.report["health_vector"]["evidence_coverage"]["value"]

        # Idempotency across a repeat run: one snapshot row, one run row, no
        # duplicated proof, and the metric rows re-derive their own
        # content-addressed ids rather than piling up a second identical set.
        metric_rows = state.rows("SELECT COUNT(*) AS c FROM evaluation_metrics")[0]["c"]
        second = evaluation.run(engine)
        assert second.snapshot_id == first.snapshot_id
        assert second.run_id == first.run_id
        assert state.rows("SELECT COUNT(*) AS c FROM evaluation_snapshots")[0]["c"] == 1
        assert state.rows("SELECT COUNT(*) AS c FROM evaluation_proofs")[0]["c"] == 4
        assert state.rows("SELECT COUNT(*) AS c FROM evaluation_metrics")[0]["c"] == metric_rows

        # A retry that names its interaction is a no-op, not a second judgment.
        evaluation.record_evidence(
            state,
            config,
            query=queries[0],
            target_id=artifacts["deploy-gateway.md"],
            event_type="explicit_accept",
            principal="user:ada",
            session_id="s0",
            interaction_id="call-0",
        )
        assert state.rows("SELECT COUNT(*) AS c FROM evaluation_proofs")[0]["c"] == 4

        # Every read surface, against the real dialect: the trend join, the
        # report fetch, the run listing and the cohort listing.
        assert evaluation.latest_report(state, config.knowledge_base_id) is not None
        assert len(evaluation_store.list_runs(state, config.knowledge_base_id)) == 1
        assert len(evaluation_store.list_cohorts(state, config.knowledge_base_id)) >= 4
        assert evaluation.trend(
            state,
            config.knowledge_base_id,
            "known_positive_reciprocal_rank",
            cohort_name="anchor",
            variant_id="B5",
        )
    finally:
        engine.close()


@postgres
def test_the_evaluation_progress_columns_reach_a_pre_existing_postgres_table(
    tmp_path: Path,
) -> None:
    """The additive-column case, on the backend that returns early from
    ``migrate()``.

    Two traps in one, and both have bitten this file before. A column stated in
    only the SQLite path exists on exactly one backend. And a marker written
    before the column existed makes Postgres skip the whole DDL block unless
    the ``required`` map names the *newest* column rather than just the table.

    The `/state` this rewinds to is the shape ``evaluation_runs`` shipped in,
    which is what any deployment that ran the plane before progress was durable
    actually has.
    """

    from pheasant.persistence.paths import StatePaths
    from pheasant.persistence.state_store import SCHEMA_VERSION, StateStore

    _reset(DSN)
    config = _config(tmp_path, _corpus(tmp_path), "postgres")
    paths = StatePaths.from_config(config)
    paths.ensure()
    state = StateStore.from_config(config, paths.sqlite)
    try:
        state.migrate()
        # Rewind to the pre-progress shape, with a row in it, and stamp the
        # schema marker as current so nothing but the `required` map can notice.
        state.conn.execute("DROP TABLE IF EXISTS evaluation_runs")
        state.conn.execute(
            "CREATE TABLE evaluation_runs ("
            "run_id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, "
            "started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, "
            "mode TEXT NOT NULL DEFAULT 'current_state', config_digest TEXT NOT NULL, "
            "gates_passed INTEGER NOT NULL DEFAULT 1, report_json TEXT)"
        )
        state.conn.execute(
            "INSERT INTO evaluation_runs(run_id, kb_id, snapshot_id, started_at, status, "
            "config_digest) VALUES('run-legacy','parity','kb-x','2026-01-01T00:00:00Z',"
            "'completed','c')"
        )
        state.conn.execute(
            "INSERT INTO pheasant_schema_meta(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("core", SCHEMA_VERSION, "2026-01-01T00:00:00Z"),
        )
        state.conn.commit()
        before = state.backend.table_columns("evaluation_runs")
        assert "heartbeat_at" not in before

        state.migrate()

        after = state.backend.table_columns("evaluation_runs")
        assert {"phase", "heartbeat_at", "attempts", "total_units"} <= set(after)
        # The row that was already there survives the widening.
        row = dict(state.rows("SELECT * FROM evaluation_runs WHERE run_id='run-legacy'")[0])
        assert row["status"] == "completed"

        # And a progress write — the thing that would otherwise fail with a
        # missing column on every heartbeat — now works.
        from pheasant.evaluation import store as evaluation_store

        evaluation_store.heartbeat_run(
            state,
            run_id="run-legacy",
            now="2026-02-02T00:00:00Z",
            phase="replay",
            completed_units=3,
        )
        status = evaluation_store.run_status(state, "run-legacy")
        assert status is not None
        assert status["phase"] == "replay"
        assert status["completed_units"] == 3
    finally:
        state.close()


@postgres
def test_a_killed_batch_frees_its_lease_on_postgres(tmp_path: Path) -> None:
    """The recovery CI found, on the dialect that has broken three others.

    ``DELETE ... RETURNING`` is the fourth statement shape this plane asks of
    both backends, and the previous three portability bugs here were each a
    statement that ran on SQLite and did something else — or nothing — on the
    real server. So the whole sequence runs against Postgres: a batch that was
    killed without releasing its lease, the reclaim that frees it, and the
    resume that then actually happens instead of reporting ``skipped``.
    """

    from datetime import UTC, datetime, timedelta

    from pheasant.evaluation import store as evaluation_store
    from pheasant.evaluation.runner import EVALUATION_LEASE, reclaim_interrupted_runs

    def ago(seconds: float) -> str:
        return (
            (datetime.now(UTC) - timedelta(seconds=seconds))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    _reset(DSN)
    config = _config(tmp_path, _corpus(tmp_path), "postgres")
    config.evaluation.enabled = True
    engine = SyncEngine(config)
    try:
        engine.sync_source("docs", "full")
        state = engine.state
        kb_id = config.knowledge_base_id

        # 25s dead: past the CI region's `run_stale_seconds` (20s), still
        # inside the lease's own 45s window. That disagreement is the bug.
        stopped = ago(25.0)
        evaluation_store.open_run(
            state,
            run_id="run-killed",
            kb_id=kb_id,
            snapshot_id="kb-x",
            started_at=ago(55.0),
            mode="current_state",
            config_digest="c",
            owner="dead-host:1",
            total_units=36,
        )
        evaluation_store.heartbeat_run(
            state,
            run_id="run-killed",
            now=stopped,
            phase="replay",
            detail="anchor/B3",
            completed_units=7,
        )
        state.conn.execute(
            "INSERT INTO source_leases(source_id, owner, acquired_at, heartbeat_at) "
            "VALUES(%s,%s,%s,%s) ON CONFLICT(source_id) DO UPDATE SET "
            "owner=excluded.owner, acquired_at=excluded.acquired_at, "
            "heartbeat_at=excluded.heartbeat_at",
            (EVALUATION_LEASE, "dead-host:1", stopped, stopped),
        )
        state.conn.commit()

        assert reclaim_interrupted_runs(state, kb_id, stale_after_seconds=20.0) == ["run-killed"]
        assert evaluation_store.run_status(state, "run-killed")["status"] == "interrupted"
        # The statement under test: on SQLite this row is gone. It has to be
        # gone here too, or the resume the reclaim just advertised is a lie.
        assert not state.rows(
            "SELECT owner FROM source_leases WHERE source_id=%s", (EVALUATION_LEASE,)
        )
    finally:
        engine.close()
