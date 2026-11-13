"""The batch, end to end: one run, its report, and the guarantees around it.

`test_evaluation_plane.py` pins the pieces. This pins the whole pass through
the real engine — the manifest, the cohorts, the replay through the real search
path, the metrics, the gates, the persistence — and the four properties that
make it safe to leave switched on in a fleet:

* it is reproducible over an unchanged state;
* it takes a lease, so N replicas produce one run;
* it never writes to the knowledge plane, and never makes its own measurements
  retrievable;
* it never credits a replayed retrieval as a *use*, which would let the
  evaluation raise the salience of the records it is measuring.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

import pheasant.evaluation as evaluation
from pheasant.config.schema import PheasantConfig
from pheasant.evaluation import store as evaluation_store
from pheasant.evaluation.contracts import query_id
from pheasant.evaluation.runner import EVALUATION_LEASE, EvaluationLease
from pheasant.sync.log_queue import write_events
from pheasant.telemetry.interactions import InteractionEvent

QUERIES = (
    "where is invoice retry configured",
    "filewatch daemon restart schedule",
    "invoice retry handler location",
    "which module owns the retry policy",
    "how does the runbook describe restarts",
)


def _write_config(tmp_path: Path, **evaluation_settings: Any) -> tuple[PheasantConfig, Path]:
    docs = tmp_path / "ws" / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "invoice.md").write_text(
        "# Invoice retry\n\nInvoiceRetryPolicy governs invoice retry behaviour.\n",
        encoding="utf-8",
    )
    (docs / "legacy.md").write_text(
        "# Legacy retry\n\nThe legacy_retry handler is deprecated invoice code.\n",
        encoding="utf-8",
    )
    (docs / "runbook.md").write_text(
        "# Kestrel Runbook\n\nThe filewatch daemon restarts nightly at 0300 UTC.\n",
        encoding="utf-8",
    )
    for name in ("state", "exports", "memory"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    raw = {
        "pheasant": {
            "name": "kb",
            "state_path": str(tmp_path / "state"),
            "workspace_root": str(tmp_path / "ws"),
            "exports_path": str(tmp_path / "exports"),
        },
        "storage": {"graph_snapshots": False},
        "observability": {"interactions": {"enabled": False}},
        "memory": {"steering_enabled": True, "usage_tracking": True},
        "evaluation": {
            "enabled": True,
            "proof": {
                "minimum_eligible_queries": 1,
                "minimum_evidenced_queries": 1,
                "minimum_independent_interactions": 1,
                "maximum_single_query_proof_share": 1.0,
            },
            "cohorts": {"anchor_minimum_queries": 2},
            **evaluation_settings,
        },
        "sources": [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(docs),
                "include": ["**/*.md"],
                "sync": {"on_startup": False},
            },
            {
                "name": "agent-memory",
                "type": "memory",
                "path": str(tmp_path / "memory"),
                "sync": {"on_startup": False},
            },
        ],
    }
    path = tmp_path / "pheasant.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return PheasantConfig.model_validate(raw), path


def _engine(tmp_path: Path, **evaluation_settings: Any) -> Any:
    from pheasant.api.app import create_app

    config, path = _write_config(tmp_path, **evaluation_settings)
    app = create_app(config, config_path=str(path))
    engine = app.state.engine
    engine.sync_source("docs", "full")
    return engine


def _artifact(engine: Any, needle: str) -> str:
    rows = engine.state.rows("SELECT id, relative_path FROM artifacts ORDER BY id")
    return next(str(row["id"]) for row in rows if needle in str(row["relative_path"]))


def _seed(engine: Any) -> None:
    write_events(
        engine.state,
        [
            InteractionEvent(
                kb_id="kb",
                operation="/search",
                modality="ui",
                principal="user:ada",
                session_id=f"s{index % 2}",
                trace_id=f"{index:032x}",
                span_id=f"{index:016x}",
                started_at=f"2026-01-01T00:00:{index:02d}.000000Z",
                status="ok",
                duration_ms=9.0,
                query_text=query,
                result_paths=["invoice.md"],
                result_ids=[_artifact(engine, "invoice")],
                result_count=1,
                top_score=0.8,
            )
            for index, query in enumerate(QUERIES)
        ],
    )
    invoice = _artifact(engine, "invoice")
    legacy = _artifact(engine, "legacy")
    for index, (query, target, event) in enumerate(
        [
            (QUERIES[0], invoice, "explicit_accept"),
            (QUERIES[0], legacy, "explicit_reject"),
            (QUERIES[2], invoice, "selected"),
            (QUERIES[3], invoice, "downstream_success"),
        ]
    ):
        evaluation.record_evidence(
            engine.state,
            engine.config,
            query=query,
            target_id=target,
            event_type=event,
            principal="user:ada",
            session_id=f"s{index % 2}",
            interaction_id=f"i{index}",
            observed_at=f"2026-01-02T00:00:{index:02d}Z",
        )


@pytest.fixture()
def seeded(tmp_path: Path):
    engine = _engine(tmp_path)
    _seed(engine)
    try:
        yield engine
    finally:
        engine.close()


# --------------------------------------------------------------------------
# One complete pass
# --------------------------------------------------------------------------


def test_empty_artifact_sources_do_not_change_the_baseline_candidate_window(seeded: Any) -> None:
    from pheasant.evaluation.contracts import EvaluatedQuery
    from pheasant.evaluation.replay import ReplayEngine
    from pheasant.evaluation.runner import _replay_searcher
    from pheasant.evaluation.variants import default_matrix

    replay = ReplayEngine(
        _replay_searcher(seeded),
        "kb",
        graph=seeded.graph_builder.graph,
        max_results=5,
        explain=True,
    )
    query = EvaluatedQuery("empty-artifacts", QUERIES[0])
    results = [replay.replay_query(variant, query) for variant in default_matrix()]
    assert all(not result.failed for result in results)
    assert {result.stages["query"]["fetch_n"] for result in results} == {5}
    assert all(result.ranked_ids == results[0].ranked_ids for result in results)


def test_a_run_produces_every_required_report_section(seeded: Any) -> None:
    outcome = evaluation.run(seeded)
    assert outcome.status == "completed"
    report = outcome.report
    for section in (
        "run_identity",
        "snapshot_integrity",
        "evidence_coverage",
        "health_vector",
        "baseline_comparison",
        "memory_attribution",
        "generalization",
        "controls_and_regressions",
        "gates",
        "optional_diagnostics",
        "candidate_decisions",
        "limitations",
        "longitudinal",
        "explanations",
    ):
        assert section in report, section
    assert set(report["explanations"]) == {"end_user", "agent", "developer"}


def test_the_report_is_json_serializable(seeded: Any) -> None:
    """It is persisted as JSON and served over HTTP; a stray enum breaks both."""

    outcome = evaluation.run(seeded)
    round_tripped = json.loads(json.dumps(outcome.report))
    assert round_tripped["run_identity"]["run_id"] == outcome.run_id


def test_two_runs_over_one_state_agree(seeded: Any) -> None:
    """Reproducibility is the first acceptance criterion, and the only reason a
    longitudinal comparison means anything."""

    first = evaluation.run(seeded)
    second = evaluation.run(seeded)
    assert first.snapshot_id == second.snapshot_id

    def vector(outcome: Any) -> dict[str, Any]:
        return {name: entry["value"] for name, entry in outcome.report["health_vector"].items()}

    assert vector(first) == vector(second)


def test_a_snapshot_id_addresses_state_not_time(seeded: Any) -> None:
    """Two runs over an unchanged region are one snapshot, and a changed region
    is a different one. A clock in the id would make "the same snapshot produces
    the same result" untestable."""

    first = evaluation.run(seeded)
    second = evaluation.run(seeded)
    assert first.snapshot_id == second.snapshot_id
    assert len(seeded.state.rows("SELECT * FROM evaluation_snapshots")) == 1

    (Path(seeded.config.pheasant.workspace_root) / "docs" / "extra.md").write_text(
        "# Extra\n\nA new document nobody asked for.\n", encoding="utf-8"
    )
    seeded.sync_source("docs", "full")
    third = evaluation.run(seeded)
    assert third.snapshot_id != first.snapshot_id
    assert third.report["longitudinal"]["previous_snapshot_id"] == first.snapshot_id
    assert "corpus.content_manifest_digest" in third.report["longitudinal"]["material_change"]


def test_every_aggregate_resolves_to_the_queries_behind_it(seeded: Any) -> None:
    """Traceability: an aggregate that cannot be resolved to its operands is a
    number nobody can argue with."""

    outcome = evaluation.run(seeded)
    rows = seeded.state.rows(
        "SELECT metric_id, query_id, payload_json FROM evaluation_metrics "
        "WHERE run_id=? AND metric_id='known_positive_recall_at_5'",
        (outcome.run_id,),
    )
    aggregates = [row for row in rows if row["query_id"] is None]
    per_query = [row for row in rows if row["query_id"] is not None]
    assert aggregates and per_query

    payload = json.loads(per_query[0]["payload_json"])
    assert payload["calculation"]["formula"]
    assert payload["calculation"]["substituted"]
    assert payload["calculation"]["operands"]["known_positive_ids"]
    assert payload["evidence"]["proof_ids"]
    assert payload["interpretation"]["does_not_support"]


def test_the_run_records_its_own_snapshot_and_cohorts(seeded: Any) -> None:
    outcome = evaluation.run(seeded)
    manifest = evaluation_store.load_snapshot(seeded.state, outcome.snapshot_id)
    assert manifest is not None
    assert manifest.corpus["artifact_count"] >= 3

    cohorts = evaluation_store.list_cohorts(seeded.state, "kb")
    purposes = {cohort.purpose for cohort in cohorts}
    assert {"anchor", "control", "synthetic"} <= purposes
    anchor = evaluation_store.find_cohort(seeded.state, "kb", "anchor", "anchor")
    assert anchor is not None and anchor.frozen


def test_a_second_run_finds_the_frozen_anchor(seeded: Any) -> None:
    """The whole basis of the trend line."""

    first = evaluation.run(seeded)
    anchor_id = first.report["explanations"]["developer"]["cohorts"]["anchor"]["cohort_id"]
    write_events(
        seeded.state,
        [
            InteractionEvent(
                kb_id="kb",
                operation="/search",
                modality="ui",
                principal="user:bo",
                session_id="s9",
                trace_id=f"{99:032x}",
                span_id=f"{99:016x}",
                started_at="2026-02-01T00:00:00.000000Z",
                status="ok",
                duration_ms=5.0,
                query_text="a completely different question",
                result_paths=["runbook.md"],
                result_count=1,
            )
        ],
    )
    second = evaluation.run(seeded)
    assert second.report["explanations"]["developer"]["cohorts"]["anchor"]["cohort_id"] == anchor_id


def test_a_trend_point_is_published_per_distinct_state(seeded: Any) -> None:
    """One point per state, not per invocation.

    Two runs over an unchanged region produce identical numbers, so they are
    one run and one trend point — a chart of the same value repeated says the
    operator ran the command twice, not that anything happened.
    """

    first = evaluation.run(seeded)
    repeat = evaluation.run(seeded)
    assert repeat.run_id == first.run_id
    assert len(seeded.state.rows("SELECT * FROM evaluation_runs")) == 1

    points = evaluation.trend(
        seeded.state, "kb", "known_positive_reciprocal_rank", cohort_name="anchor", variant_id="B5"
    )
    assert len(points) == 1
    assert all("started_at" in point for point in points)

    (Path(seeded.config.pheasant.workspace_root) / "docs" / "another.md").write_text(
        "# Another\n\nMore corpus.\n", encoding="utf-8"
    )
    seeded.sync_source("docs", "full")
    changed = evaluation.run(seeded)
    assert changed.run_id != first.run_id
    assert (
        len(
            evaluation.trend(
                seeded.state,
                "kb",
                "known_positive_reciprocal_rank",
                cohort_name="anchor",
                variant_id="B5",
            )
        )
        == 2
    )


def test_a_historical_run_is_a_different_run_from_a_current_one(seeded: Any) -> None:
    """Same state, different proof cutoff: genuinely a different evaluation,
    and it must not overwrite the current-state run's report."""

    current = evaluation.run(seeded)
    historical = evaluation.run(seeded, mode="historical", effective_as_of="2026-01-01T12:00:00Z")
    assert historical.run_id != current.run_id
    assert historical.snapshot_id == current.snapshot_id
    assert len(seeded.state.rows("SELECT * FROM evaluation_runs")) == 2


# --------------------------------------------------------------------------
# The boundary
# --------------------------------------------------------------------------


def test_a_run_writes_nothing_into_the_knowledge_plane(seeded: Any) -> None:
    """An evaluation record is *about* the knowledge base, never part of it."""

    before = {
        table: seeded.state.rows(f"SELECT COUNT(*) AS c FROM {table}")[0]["c"]
        for table in ("artifacts", "chunks", "memory_records", "sources")
    }
    evaluation.run(seeded)
    after = {
        table: seeded.state.rows(f"SELECT COUNT(*) AS c FROM {table}")[0]["c"]
        for table in ("artifacts", "chunks", "memory_records", "sources")
    }
    assert before == after


def test_measurements_are_not_retrievable_as_knowledge(seeded: Any) -> None:
    """A region must not answer a question with its own report."""

    outcome = evaluation.run(seeded)
    assert outcome.run_id
    from pheasant.search.hybrid import HybridSearch
    from pheasant.search.sqlite_store import SearchStore

    searcher = HybridSearch(SearchStore(seeded.state))
    for query in ("known_positive_recall", "evaluation run", outcome.run_id):
        results = searcher.search_context(
            "kb", query, graph=seeded.graph_builder.graph, max_results=10
        )["results"]
        assert not any(outcome.run_id in json.dumps(item, default=str) for item in results)


def test_a_replay_never_credits_a_memory_record_with_a_use(tmp_path: Path) -> None:
    """The tightest self-rewarding loop available here: evaluation raising the
    salience of the very records it is measuring."""

    engine = _engine(tmp_path)
    _seed(engine)
    from pheasant.memory.store import MemoryStore, memory_source

    source = memory_source(engine.config, engine.state)
    assert source is not None
    MemoryStore(source.path).append("Invoice retry is governed by InvoiceRetryPolicy.", scope="org")
    engine.sync_source("agent-memory", "full")
    try:
        assert engine.config.memory.usage_tracking is True
        before = engine.state.rows("SELECT record_id, uses FROM memory_records ORDER BY record_id")
        evaluation.run(engine)
        after = engine.state.rows("SELECT record_id, uses FROM memory_records ORDER BY record_id")
        assert [dict(row) for row in before] == [dict(row) for row in after]
    finally:
        engine.close()


def test_evaluation_tables_are_not_exportable() -> None:
    """An export is a file people pass around. Raw query text keyed to a
    principal partition is not that -- `pheasant eval bootstrap` is the
    sanctioned way out, and it hashes both first."""

    from pheasant import analytics

    for table in (
        "evaluation_proofs",
        "evaluation_runs",
        "evaluation_metrics",
        "evaluation_cohorts",
        "evaluation_snapshots",
    ):
        assert table not in analytics.EXPORTABLE


# --------------------------------------------------------------------------
# Fleet safety
# --------------------------------------------------------------------------


def test_a_second_process_declines_rather_than_duplicating_the_run(seeded: Any) -> None:
    """N API replicas pointed at one /state must produce one run, not N."""

    from pheasant.sync.locks import SourceLease

    holder = SourceLease(seeded.state, EVALUATION_LEASE, owner="other-replica:1")
    assert holder.try_acquire() is True
    try:
        outcome = evaluation.run(seeded)
        assert outcome.status == "skipped"
        assert "lease" in outcome.skipped_reason
        assert seeded.state.rows("SELECT * FROM evaluation_runs") == []
    finally:
        holder.release()


def test_the_lease_is_released_so_the_next_run_proceeds(seeded: Any) -> None:
    with EvaluationLease(seeded.state) as acquired:
        assert acquired is True
    assert evaluation.run(seeded).status == "completed"


def test_a_run_is_bounded_and_says_what_it_dropped(tmp_path: Path) -> None:
    """A truncated run must not report a smaller denominator as if it were the
    whole cohort."""

    engine = _engine(tmp_path, maximum_queries_per_run=1)
    _seed(engine)
    try:
        outcome = evaluation.run(engine)
        assert outcome.status == "truncated"
        assert outcome.report["limitations"]["truncated_replays"]
    finally:
        engine.close()


def test_the_scheduler_does_not_evaluate_unless_asked(tmp_path: Path) -> None:
    """A run costs one search per query per variant; starting that on a timer
    is a decision, not a default."""

    from pheasant.sync.scheduler import SchedulerService

    engine = _engine(tmp_path)
    _seed(engine)
    try:
        assert engine.config.evaluation.on_material_snapshot is False
        scheduler = SchedulerService(engine, enabled=False)
        scheduler._evaluation_upkeep()
        assert engine.state.rows("SELECT * FROM evaluation_runs") == []
    finally:
        engine.close()


def test_the_scheduler_respects_its_own_interval(tmp_path: Path) -> None:
    """A corpus under active indexing changes materially on every beat."""

    from pheasant.sync.scheduler import SchedulerService

    engine = _engine(tmp_path, on_material_snapshot=True, minimum_interval_seconds=3600)
    _seed(engine)
    try:
        scheduler = SchedulerService(engine, enabled=False)
        scheduler._evaluation_upkeep()
        after_first = engine.state.rows("SELECT run_id FROM evaluation_runs")
        assert len(after_first) == 1

        # Nothing changed and the interval has not elapsed: no second run.
        scheduler._evaluation_upkeep()
        assert engine.state.rows("SELECT run_id FROM evaluation_runs") == after_first
    finally:
        engine.close()


def test_the_scheduler_does_not_re_evaluate_an_already_evaluated_state(tmp_path: Path) -> None:
    """A snapshot id is a digest of the state, so its presence *is* the answer.

    Comparing against the previous snapshot instead would exclude the current
    id, report a change on every beat after a run, and fire a batch as often as
    the interval allowed.
    """

    from pheasant.sync.scheduler import SchedulerService

    engine = _engine(tmp_path, on_material_snapshot=True, minimum_interval_seconds=0)
    _seed(engine)
    try:
        scheduler = SchedulerService(engine, enabled=False)
        scheduler._evaluation_upkeep()
        after_first = engine.state.rows("SELECT run_id FROM evaluation_runs")
        assert len(after_first) == 1

        # Interval elapsed (0s) and nothing changed: still no second run.
        scheduler._evaluation_upkeep()
        assert engine.state.rows("SELECT run_id FROM evaluation_runs") == after_first

        # A material change does fire one.
        (Path(engine.config.pheasant.workspace_root) / "docs" / "fresh.md").write_text(
            "# Fresh\n\nNew content.\n", encoding="utf-8"
        )
        engine.sync_source("docs", "full")
        scheduler._evaluation_upkeep()
        assert len(engine.state.rows("SELECT run_id FROM evaluation_runs")) == 2
    finally:
        engine.close()


def test_evaluation_upkeep_never_raises(tmp_path: Path) -> None:
    """The beat's next line is a sync; measurement must not be able to stop it."""

    from pheasant.sync.scheduler import SchedulerService

    engine = _engine(tmp_path, on_material_snapshot=True)

    class Exploding:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("state is gone")

    try:
        scheduler = SchedulerService(engine, enabled=False)
        scheduler.engine = type("E", (), {"config": engine.config, "state": Exploding()})()
        scheduler._evaluation_upkeep()  # must not raise
    finally:
        engine.close()


# --------------------------------------------------------------------------
# Temporal leakage
# --------------------------------------------------------------------------


def test_a_historical_run_cannot_read_evidence_from_its_own_future(seeded: Any) -> None:
    """A memory or interaction after the tested query's evaluation time must not
    influence an as_of replay."""

    from pheasant.evaluation.proof import ProofPolicy
    from pheasant.evaluation.runner import collect_proof

    policy = ProofPolicy.from_config(seeded.config.evaluation.proof)
    ids = [query_id(query) for query in QUERIES]

    everything = collect_proof(seeded.state, "kb", policy, before=None, query_ids=ids)
    earlier = collect_proof(
        seeded.state, "kb", policy, before="2026-01-01T12:00:00Z", query_ids=ids
    )
    assert len(earlier) < len(everything)
    assert all(proof.observed_at <= "2026-01-01T12:00:00Z" for proof in earlier)


def test_a_historical_run_names_the_instant_it_describes(seeded: Any) -> None:
    outcome = evaluation.run(seeded, mode="historical", effective_as_of="2026-01-01T12:00:00Z")
    identity = outcome.report["run_identity"]
    assert identity["mode"] == "historical"
    assert identity["effective_as_of"] == "2026-01-01T12:00:00Z"


# --------------------------------------------------------------------------
# The HTTP surface
# --------------------------------------------------------------------------


def test_the_http_surface_records_proof_and_serves_the_report(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    config, path = _write_config(tmp_path)
    app = create_app(config, config_path=str(path))
    engine = app.state.engine
    engine.sync_source("docs", "full")
    _seed(engine)

    with TestClient(app) as client:
        taxonomy = client.get("/evaluation/taxonomy").json()
        assert {event["event_type"] for event in taxonomy["events"]} >= {
            "served",
            "selected",
            "explicit_accept",
            "explicit_reject",
        }
        assert taxonomy["defaults"]["non_selection_is_negative"] is False

        rejected = client.post(
            "/evaluation/evidence",
            json={"query": "q", "target_id": "a", "event_type": "was_helpful"},
        )
        assert rejected.status_code == 400

        accepted = client.post(
            "/evaluation/evidence",
            json={
                "query": QUERIES[1],
                "target_id": _artifact(engine, "runbook"),
                "event_type": "selected",
            },
        )
        assert accepted.status_code == 200
        assert accepted.json()["polarity"] == "positive"
        assert set(accepted.json()["multipliers"]) == {
            "type",
            "strength",
            "temporal",
            "source",
        }

        assert client.get("/evaluation/report").status_code == 404

        outcome = evaluation.run(engine)
        assert outcome.status == "completed"

        report = client.get("/evaluation/report").json()
        assert report["run_identity"]["run_id"] == outcome.run_id
        assert client.get("/evaluation/runs").json()["runs"][0]["run_id"] == outcome.run_id
        assert "points" in client.get("/evaluation/trend").json()


def test_the_mcp_facade_exposes_evidence_and_the_report(tmp_path: Path) -> None:
    from pheasant.mcp_server.tools import PheasantTools

    config, path = _write_config(tmp_path)
    tools = PheasantTools(config)
    tools.engine.sync_source("docs", "full")
    _seed(tools.engine)
    try:
        recorded = tools.record_evidence(
            "kb",
            QUERIES[0],
            _artifact(tools.engine, "invoice"),
            "explicit_accept",
        )
        assert recorded["polarity"] == "positive"

        taxonomy = tools.get_evaluation_taxonomy("kb")
        assert taxonomy["defaults"]["unknown_is_negative"] is False

        with pytest.raises(ValueError, match="No evaluation run"):
            tools.get_evaluation_report("kb")

        evaluation.run(tools.engine)
        report = tools.get_evaluation_report("kb")
        assert set(report) >= {"run_identity", "health_vector", "gates", "agent"}
        assert "allowed_next_actions" in report["agent"]
    finally:
        tools.state.close()


# --------------------------------------------------------------------------
# The invariant cohort against real memory
# --------------------------------------------------------------------------


def _memory_store(engine: Any) -> Any:
    from pheasant.memory.store import MemoryStore, memory_source

    source = memory_source(engine.config, engine.state)
    assert source is not None
    return MemoryStore(source.path)


def test_a_correction_produces_a_case_the_region_passes(tmp_path: Path) -> None:
    """The stale-fact gate has to be non-vacuous: a real supersession must
    produce a real case, and the region must pass it."""

    from pheasant.evaluation import cohorts as cohort_builder

    engine = _engine(tmp_path)
    store = _memory_store(engine)
    first, _ = store.append(
        "The filewatch daemon restarts nightly at 0300 UTC.",
        scope="org",
        valid_from="2026-01-01T00:00:00Z",
    )
    store.append(
        "The filewatch daemon restarts nightly at 0400 UTC.",
        scope="org",
        supersedes=first.record_id,
        valid_from="2026-03-01T00:00:00Z",
    )
    engine.sync_source("agent-memory", "full")
    try:
        cohort = cohort_builder.build_synthetic_invariants(engine.state, "kb")
        kinds = [q.expectation["kind"] for q in cohort.queries]
        assert "stale_current" in kinds
        assert "temporal_as_of" in kinds

        outcome = evaluation.run(engine)
        gates = {gate["gate_id"]: gate for gate in outcome.report["gates"]}
        assert gates["stale_current_leak"]["evidence"]["cases"] == 1
        assert gates["stale_current_leak"]["passed"] is True
        assert gates["temporal_invariant"]["evidence"]["cases"] == 1
        assert gates["temporal_invariant"]["passed"] is True
    finally:
        engine.close()


def test_a_same_second_correction_produces_no_as_of_case(tmp_path: Path) -> None:
    """A record corrected in the second it was asserted has an empty validity
    window: no instant can return it, so asserting one would fail a region that
    is behaving correctly. Found on a live run."""

    from pheasant.evaluation import cohorts as cohort_builder

    engine = _engine(tmp_path)
    store = _memory_store(engine)
    first, _ = store.append("Retry limit is three.", scope="org", valid_from="2026-01-01T00:00:00Z")
    store.append(
        "Retry limit is five.",
        scope="org",
        supersedes=first.record_id,
        valid_from="2026-01-01T00:00:00Z",
    )
    engine.sync_source("agent-memory", "full")
    try:
        cohort = cohort_builder.build_synthetic_invariants(engine.state, "kb")
        kinds = [q.expectation["kind"] for q in cohort.queries]
        assert "stale_current" in kinds
        assert "temporal_as_of" not in kinds
    finally:
        engine.close()


def test_a_real_stale_leak_fails_the_gate(tmp_path: Path) -> None:
    """The gate must be able to fail, or it is decoration."""

    from pheasant.evaluation import cohorts as cohort_builder
    from pheasant.evaluation import gates as gate_checks
    from pheasant.evaluation.replay import QueryReplay, VariantReplay
    from pheasant.evaluation.variants import default_matrix

    engine = _engine(tmp_path)
    store = _memory_store(engine)
    first, _ = store.append(
        "The filewatch daemon restarts nightly at 0300 UTC.",
        scope="org",
        valid_from="2026-01-01T00:00:00Z",
    )
    store.append(
        "The filewatch daemon restarts nightly at 0400 UTC.",
        scope="org",
        supersedes=first.record_id,
        valid_from="2026-03-01T00:00:00Z",
    )
    engine.sync_source("agent-memory", "full")
    try:
        cohort = cohort_builder.build_synthetic_invariants(engine.state, "kb")
        case = next(q for q in cohort.queries if q.expectation["kind"] == "stale_current")

        variant = next(v for v in default_matrix() if v.variant_id == "B5")
        leaking = VariantReplay(variant=variant, cohort_id=cohort.cohort_id)
        leaking.results[case.query_id] = QueryReplay(
            query_id=case.query_id,
            variant_id="B5",
            text=case.text,
            ranked_ids=["n1"],
            memory_record_ids=[case.expectation["forbidden_record_id"]],
            result_count=1,
        )
        gate = next(
            g
            for g in gate_checks.evaluate_invariants(cohort, leaking)
            if g.gate_id == "stale_current_leak"
        )
        assert gate.passed is False
        assert gate.evidence["failed_query_ids"] == [case.query_id]
    finally:
        engine.close()


def test_an_alias_rule_that_helps_is_attributed_to_alias_steering(tmp_path: Path) -> None:
    """The whole point of the ablation matrix: a lift has to land on the
    variant that produced it, not on "memory" in aggregate."""

    engine = _engine(tmp_path)
    docs = Path(engine.config.pheasant.workspace_root) / "docs"
    (docs / "policy.md").write_text(
        "# InvoiceRetryPolicy\n\nInvoiceRetryPolicy is the retry policy implementation.\n",
        encoding="utf-8",
    )
    engine.sync_source("docs", "full")

    store = _memory_store(engine)
    store.append("invoice retry -> InvoiceRetryPolicy", scope="org", kind="alias")
    engine.sync_source("agent-memory", "full")

    target = _artifact(engine, "policy")
    queries = [
        "invoice retry",
        "invoice retry configuration",
        "invoice retry rules",
        "invoice retry behaviour",
    ]
    write_events(
        engine.state,
        [
            InteractionEvent(
                kb_id="kb",
                operation="/search",
                modality="ui",
                principal="user:ada",
                session_id=f"s{index}",
                trace_id=f"{200 + index:032x}",
                span_id=f"{200 + index:016x}",
                started_at=f"2026-01-01T00:00:{index:02d}.000000Z",
                status="ok",
                duration_ms=8.0,
                query_text=query,
                result_paths=["policy.md"],
                result_ids=[target],
                result_count=1,
            )
            for index, query in enumerate(queries)
        ],
    )
    for index, query in enumerate(queries):
        evaluation.record_evidence(
            engine.state,
            engine.config,
            query=query,
            target_id=target,
            event_type="explicit_accept",
            principal="user:ada",
            session_id=f"s{index}",
            interaction_id=f"a{index}",
        )
    try:
        outcome = evaluation.run(engine)
        # Scoped to the anchor cohort: the same metric is computed per cohort,
        # and the control/learned/holdout ones carry no positive proof here, so
        # an unscoped query would mix a real value with three
        # `insufficient_evidence` rows for the same variant.
        rows = engine.state.rows(
            "SELECT m.variant_id AS variant_id, m.value AS value, "
            "m.denominator AS denominator FROM evaluation_metrics m "
            "JOIN evaluation_cohorts c ON c.cohort_id = m.cohort_id "
            "WHERE m.run_id=? AND m.metric_id='steering_lift' AND m.query_id IS NULL "
            "AND c.purpose='anchor' ORDER BY m.variant_id",
            (outcome.run_id,),
        )
        by_variant = {str(row["variant_id"]): row for row in rows}
        # Alias, preference and exclusion are each measured on their own row,
        # against the corpus baseline, rather than rolled into one "memory"
        # number that could not say which of the three did anything.
        assert {"B2", "B3", "B4"} <= set(by_variant)
        # No preference or exclusion rule exists, so those two variants are
        # byte-identical to the baseline: their lift is exactly zero, never
        # null, because the pair *was* computable and came out unchanged.
        assert by_variant["B3"]["value"] == 0.0
        assert by_variant["B4"]["value"] == 0.0
    finally:
        engine.close()
