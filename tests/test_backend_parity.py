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
import os
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.search.hybrid import HybridSearch
from pheasant.search.text_store import SearchStore, corpus_vocabulary
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
        data["storage"]["dsn_env"] = "PHEASANT_DATABASE_URL"
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


@postgres
def test_cold_row_writer_preserves_resident_enrichment_on_postgres(
    tmp_path, monkeypatch, database_variable
):
    from tests.test_row_writer import _content

    workspace = _corpus(tmp_path)
    sqlite = SyncEngine(_config(tmp_path, workspace, "sqlite"))
    cold = _config(tmp_path, workspace, "postgres")
    cold.storage.dsn_env = database_variable()  # a region of its own
    pg = SyncEngine(cold, defer_persisted_graph_load=True)
    monkeypatch.setattr(pg.graph_store, "load", lambda *_: pytest.fail("whole graph loaded"))
    try:
        for engine in (sqlite, pg):
            engine.sync_source("docs", "incremental")
        assert _content(pg) == _content(sqlite)
        (workspace / "runbook.md").write_text("# Updated Runbook\n\nSee [billing](billing.md).\n")
        for engine in (sqlite, pg):
            engine.sync_source("docs", "incremental")
        assert _content(pg) == _content(sqlite)
        before = pg.graph_store.published_generation("parity")["generation_id"]
        pg.reload_graph()
        assert pg.sync_source("docs", "incremental").indexed_artifacts == 0
        assert pg.graph_store.published_generation("parity")["generation_id"] == before
    finally:
        sqlite.close()
        pg.close()


@postgres
def test_streamed_postgres_snapshot_pins_one_generation(tmp_path, monkeypatch):
    from tests.test_snapshot_stream import assert_snapshot_consistency

    workspace = _corpus(tmp_path)
    engine = SyncEngine(_config(tmp_path, workspace, "postgres"), defer_persisted_graph_load=True)
    try:
        assert_snapshot_consistency(engine, monkeypatch)
    finally:
        engine.close()


@pytest.fixture()
def both(tmp_path: Path) -> dict[str, dict[str, Any]]:
    workspace = _corpus(tmp_path)
    return {
        "sqlite": _index(tmp_path, workspace, "sqlite"),
        "postgres": _index(tmp_path, workspace, "postgres"),
        # Last, because selecting it drops the BM25 index the default built.
        "tsvector": _index(tmp_path, workspace, "postgres", "tsvector"),
    }


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

    from pheasant.persistence import pg_driver
    from pheasant.search.text_store import _postgres_total_chunks

    workspace = _corpus(tmp_path)
    engine = SyncEngine(_config(tmp_path, workspace, "postgres"))
    try:
        engine.sync_source("docs", "full")
        real = len(engine.state.rows("SELECT chunk_id FROM chunks_fts"))
        assert real > 0

        # No statistics yet: the sentinel is negative, so the exact count wins.
        with pg_driver.connect(os.environ["PHEASANT_DATABASE_URL"], autocommit=True) as conn:
            conn.query("UPDATE pg_class SET reltuples = -1 WHERE oid = 'chunks_fts'::regclass")
        assert _postgres_total_chunks(engine.state) == real

        with pg_driver.connect(os.environ["PHEASANT_DATABASE_URL"], autocommit=True) as conn:
            conn.query("ANALYZE chunks_fts")
        estimate = _postgres_total_chunks(engine.state)
        assert estimate == real, f"planner estimate {estimate} disagrees with {real}"

        # And ranking still works through the real search path.
        search = HybridSearch(SearchStore(engine.state))
        hits = search.search_context(engine.config.knowledge_base_id, "invoices", "text", 5)
        assert [h.get("relative_path") for h in hits["results"]][:1] == ["billing.md"]
    finally:
        engine.close()


@postgres
def test_a_second_sync_is_zero_work_on_postgres_too(tmp_path: Path) -> None:
    """Idempotency is a product guarantee (rule 4), not a SQLite behaviour."""

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
def test_export_reports_an_unsynced_postgres_database_actionably(tmp_path: Path) -> None:
    """The failure an operator hits when the export runs before the sync.

    Without this the error is ``UndefinedTable`` (42P01): ``relation
    "sources" does not exist``, raised four frames inside the driver — which
    is a true statement about a database and a useless one about pheasant.
    """

    from pheasant.analytics import StateUnavailable, open_state

    # A config pointing at a database this test never syncs. Reusing the same
    # DSN is safe: the tables are dropped and recreated per run by _index, and
    # this only reads.
    config = _config(tmp_path, tmp_path / "docs", "postgres")
    with postgres_dropped_tables(config):
        with pytest.raises(StateUnavailable, match="Run `pheasant sync` first"):
            open_state(config)


@contextlib.contextmanager
def postgres_dropped_tables(config: Any):
    """Drop pheasant's tables for the duration of the block, then leave them
    dropped — the next test that needs them calls ``migrate()`` itself."""

    from pheasant.persistence.state_store import StateStore

    state = StateStore.from_config(config)
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
        "storage": {"backend": "postgres", "dsn_env": "PHEASANT_DATABASE_URL"},
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

    from pheasant.artifacts.maintenance import run_memory_maintenance
    from pheasant.mcp_server.tools import PheasantTools

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

    from pheasant.artifacts.maintenance import run_memory_maintenance
    from pheasant.mcp_server.tools import PheasantTools

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

    config = _config(tmp_path, _corpus(tmp_path), "postgres")
    paths = StatePaths.from_config(config)
    paths.ensure()
    state = StateStore.from_config(config)
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
            "VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
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
            "SELECT owner FROM source_leases WHERE source_id=?", (EVALUATION_LEASE,)
        )
    finally:
        engine.close()
