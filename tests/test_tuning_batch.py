"""One tuning batch, end to end, through the real engine.

`test_tuning_refusion.py` pins the cheap path against the real search path.
This pins the whole pass — snapshot, cohorts, stage capture, diagnosis,
proposals, trials, gates, decision, bundle — and the properties that make it
safe to leave switched on in a fleet:

* it changes nothing unless a bundle is applied, and applying is a separate act;
* an applied bundle is fleet-scoped and every replica resolves it from `/state`;
* it survives the container stopping, and resumes rather than restarting;
* it declines to tune when the diagnosis says the failures are somewhere no
  retrieval parameter reaches.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import pheasant.evaluation as evaluation
from pheasant.config.schema import PheasantConfig
from pheasant.search.ranking import DEFAULT_RANKING, RankingResolver
from pheasant.sync.log_queue import write_events
from pheasant.telemetry.interactions import InteractionEvent
from pheasant.tuning import store as tuning_store
from pheasant.tuning.runner import run_tuning
from pheasant.tuning.strategy import Budget

QUERIES = (
    "where is invoice retry configured",
    "filewatch daemon restart schedule",
    "invoice retry handler location",
    "which module owns the retry policy",
    "how does the runbook describe restarts",
    "invoice retry policy owner",
    "nightly restart window",
    "retry behaviour for invoices",
)


def _write_config(tmp_path: Path, **tuning_settings: Any) -> tuple[PheasantConfig, Path]:
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
    # Decoys, and the reason the fixture is not three files. A corpus where
    # every query returns its known positive at rank one has a stage histogram
    # of pure `served`, and the correct behaviour of the plane is then to
    # propose nothing — which is worth asserting (it is) but proves nothing
    # about the search. These give the ranking something to get wrong: they
    # match the query vocabulary strongly and are never the accepted answer.
    for index in range(6):
        (docs / f"noise-{index}.md").write_text(
            f"# Retry notes {index}\n\nretry retry invoice retry policy retry handler "
            f"restart restart nightly daemon retry configuration module {index}.\n",
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
        "evaluation": {
            "enabled": True,
            "proof": {
                "minimum_eligible_queries": 1,
                "minimum_evidenced_queries": 1,
                "minimum_independent_interactions": 1,
                "maximum_single_query_proof_share": 1.0,
            },
            "cohorts": {"anchor_minimum_queries": 2},
        },
        "tuning": {
            "enabled": True,
            # Two results, not ten. With a fixture corpus of nine documents a
            # top-10 returns everything, so every query is `served` whatever
            # the ranking does and the stage histogram cannot distinguish a
            # good parameter set from a bad one. A narrow cut is what makes
            # *rank* matter, which is what is being tuned.
            "max_results": 2,
            "minimum_paired_queries": 2,
            "requery_trials": 3,
            "refusion_trials": 8,
            "max_searches": 400,
            **tuning_settings,
        },
        "sources": [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(docs),
                "include": ["**/*.md"],
                "sync": {"on_startup": False},
            },
        ],
    }
    path = tmp_path / "pheasant.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return PheasantConfig.model_validate(raw), path


def _engine(tmp_path: Path, **tuning_settings: Any) -> Any:
    from pheasant.api.app import create_app

    config, path = _write_config(tmp_path, **tuning_settings)
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
    runbook = _artifact(engine, "runbook")
    for index, query in enumerate(QUERIES):
        target = runbook if "restart" in query or "runbook" in query else invoice
        evaluation.record_evidence(
            engine.state,
            engine.config,
            query=query,
            target_id=target,
            event_type="explicit_accept",
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


def test_a_batch_diagnoses_stages_and_records_everything_behind_it(seeded: Any) -> None:
    outcome = run_tuning(seeded, budget=Budget(refusion_trials=8, requery_trials=2))

    assert outcome.status == "completed", outcome.skipped_reason
    assert outcome.experiment_id

    # The diagnosis is a histogram over *stages*, not a score.
    diagnosis = outcome.diagnosis
    assert diagnosis is not None
    assert diagnosis.histogram["evaluated"] > 0
    assert diagnosis.summary
    # Every stage it names is one of the declared pipeline stages, so a reader
    # can look up what it means.
    from pheasant.tuning.stages import STAGES

    assert set(diagnosis.histogram["counts"]) <= set(STAGES)

    # The whole chain is stored and joinable.
    row = tuning_store.experiment_status(seeded.state, outcome.experiment_id)
    assert row is not None
    assert row["status"] == "completed"
    assert row["diagnosis"]["histogram"] == diagnosis.histogram

    decision = outcome.decision
    assert decision is not None
    assert decision.outcome in {"promote", "reject", "no_change", "insufficient_evidence"}
    assert decision.reason, "a decision must say why"


def test_every_trial_names_the_stage_and_the_reason_it_was_tried(seeded: Any) -> None:
    """Traceability is the requirement; this is the mechanical part of it."""

    run_tuning(seeded, budget=Budget(refusion_trials=8, requery_trials=2))
    experiment = tuning_store.latest_experiment(seeded.state, "kb")
    trials = tuning_store.load_trials(seeded.state, experiment["experiment_id"])
    assert trials, "the batch stored no trials"

    for trial in trials.values():
        if trial["cost_class"] == "baseline":
            continue
        assert trial["motivating_stage"], "a trial with no motivating stage"
        assert trial["proposal"]["rationale"], "a trial with no rationale"
        # The delta is what makes a trial legible: which parameter moved, from
        # what, to what.
        assert trial["point"]["delta"], "a non-baseline trial with no parameter delta"
        assert trial["evaluated_queries"] > 0, "a metric with no denominator"


def test_a_batch_does_not_change_ranking_unless_a_bundle_is_applied(seeded: Any) -> None:
    """Producing a bundle is safe; applying one is the act that changes things."""

    before = tuning_store.active_overlay(seeded.state, "kb")
    assert before is None

    run_tuning(seeded, budget=Budget(refusion_trials=8, requery_trials=2))

    # auto.apply is off by default, so whatever the batch concluded, the
    # region is still serving its configured parameters.
    assert tuning_store.active_overlay(seeded.state, "kb") is None
    resolver = RankingResolver(base=DEFAULT_RANKING, state=seeded.state, kb_id="kb")
    assert resolver.current().values() == DEFAULT_RANKING.values()


# --------------------------------------------------------------------------
# Applying, and the fleet
# --------------------------------------------------------------------------


def test_an_applied_bundle_is_what_every_replica_resolves(seeded: Any) -> None:
    """Fleet scope: the overlay is one row, and a second resolver picks it up.

    This stands in for two API replicas. They share a `/state` and nothing
    else, and the property that matters is that the second one ranks under the
    bundle without being told about it and without a restart.
    """

    from pheasant.tuning.contracts import TuningBundle

    parameters = {"rrf_k": 25.0, "vector_arm_weight": 0.5}
    bundle = TuningBundle(
        bundle_id=TuningBundle.identity(parameters),
        kb_id="kb",
        experiment_id="exp-test",
        decision_id="dec-test",
        snapshot_id="snap-test",
        parameters=parameters,
    )
    tuning_store.save_bundle(seeded.state, bundle)

    # A replica that has already resolved once, before the bundle exists.
    replica = RankingResolver(base=DEFAULT_RANKING, state=seeded.state, kb_id="kb", ttl_seconds=0.0)
    assert replica.current().rrf_k == 60.0

    tuning_store.apply_bundle(seeded.state, "kb", bundle.bundle_id, applied_by="test")

    # ...converges without a restart and without being notified.
    resolved = replica.current()
    assert resolved.rrf_k == 25.0
    assert resolved.vector_arm_weight == 0.5
    assert resolved.provenance == "bundle"
    assert resolved.bundle_id == bundle.bundle_id
    # ...and the region's own searcher ranks under it too.
    assert seeded.search_context("invoice retry")["results"] is not None


def test_applying_supersedes_the_incumbent_so_there_is_never_a_second_overlay(
    seeded: Any,
) -> None:
    from pheasant.tuning.contracts import TuningBundle

    ids = []
    for k in (25.0, 90.0):
        parameters = {"rrf_k": k}
        bundle = TuningBundle(
            bundle_id=TuningBundle.identity(parameters),
            kb_id="kb",
            experiment_id="exp-test",
            decision_id="dec-test",
            snapshot_id="snap-test",
            parameters=parameters,
        )
        tuning_store.save_bundle(seeded.state, bundle)
        tuning_store.apply_bundle(seeded.state, "kb", bundle.bundle_id, applied_by="test")
        ids.append(bundle.bundle_id)

    active = [b for b in tuning_store.list_bundles(seeded.state, "kb") if b["active"]]
    assert len(active) == 1
    assert active[0]["bundle_id"] == ids[-1]

    # Rollback returns the region to its configured parameters, and what the
    # bundle replaced is a stored fact rather than a recollection.
    reverted = tuning_store.revert_bundle(seeded.state, "kb", applied_by="test")
    assert reverted is not None
    assert reverted["bundle_id"] == ids[-1]
    assert tuning_store.active_overlay(seeded.state, "kb") is None


def test_applying_an_unknown_bundle_is_a_refusal_not_a_silent_no_op(seeded: Any) -> None:
    """A no-op here would leave ranking unchanged and report success."""

    with pytest.raises(KeyError):
        tuning_store.apply_bundle(seeded.state, "kb", "bundle-nope", applied_by="test")


def test_pruning_an_experiment_keeps_its_promoted_bundle_and_rollback(seeded: Any) -> None:
    """Job history can go while its published search configuration survives."""

    from pheasant.tuning.contracts import TuningBundle

    outcome = run_tuning(seeded, budget=Budget(refusion_trials=8, requery_trials=2))
    assert outcome.status == "completed"
    parameters = {"rrf_k": 25.0, "vector_arm_weight": 0.5}
    bundle = TuningBundle(
        bundle_id=TuningBundle.identity(parameters),
        kb_id="kb",
        experiment_id=outcome.experiment_id,
        decision_id=outcome.decision.decision_id,
        snapshot_id="snap-prune-test",
        parameters=parameters,
    )
    tuning_store.save_bundle(seeded.state, bundle)
    tuning_store.apply_bundle(seeded.state, "kb", bundle.bundle_id, applied_by="test")
    resolver = RankingResolver(base=DEFAULT_RANKING, state=seeded.state, kb_id="kb", ttl_seconds=0)
    before = resolver.current().values()
    overlay = tuning_store.active_overlay(seeded.state, "kb")
    stored = tuning_store.load_bundle_row(seeded.state, "kb", bundle.bundle_id)

    removed = tuning_store.prune_experiment(seeded.state, "kb", outcome.experiment_id)

    assert removed["experiments"] == 1 and removed["trials"] > 0
    assert tuning_store.experiment_status(seeded.state, outcome.experiment_id) is None
    assert tuning_store.load_trials(seeded.state, outcome.experiment_id) == {}
    assert tuning_store.load_bundle_row(seeded.state, "kb", bundle.bundle_id) == stored
    assert tuning_store.active_overlay(seeded.state, "kb") == overlay
    assert resolver.current().values() == before

    tuning_store.revert_bundle(seeded.state, "kb", applied_by="test")
    assert resolver.current().values() == DEFAULT_RANKING.values()
    tuning_store.apply_bundle(seeded.state, "kb", bundle.bundle_id, applied_by="test")
    assert resolver.current().values() == before


# --------------------------------------------------------------------------
# Resumption and durability
# --------------------------------------------------------------------------


def test_a_re_run_over_unchanged_state_is_the_same_experiment(seeded: Any) -> None:
    """Content-addressed, with no clock in the id.

    Two batches over an unchanged region are one experiment and one row, not
    two data points that happen to look identical.
    """

    first = run_tuning(seeded, budget=Budget(refusion_trials=8, requery_trials=2))
    second = run_tuning(seeded, budget=Budget(refusion_trials=8, requery_trials=2))
    assert first.experiment_id == second.experiment_id
    assert len(tuning_store.list_experiments(seeded.state, "kb")) == 1
    # ...and the second reused the first's trials instead of re-running them.
    assert second.trials_reused > 0


def test_a_stopped_batch_is_reclaimed_and_its_trials_survive(seeded: Any) -> None:
    """A process that dies leaves a resumable row, never a permanent spinner."""

    run_tuning(seeded, budget=Budget(refusion_trials=8, requery_trials=2))
    experiment = tuning_store.latest_experiment(seeded.state, "kb")
    trials_before = tuning_store.load_trials(seeded.state, experiment["experiment_id"])

    # Simulate the container stopping mid-batch: the row says `running` and
    # nothing will ever rewrite it, because the process is gone.
    seeded.state.execute(
        "UPDATE tuning_experiments SET status='running', heartbeat_at='2000-01-01T00:00:00Z' "
        "WHERE experiment_id = ?",
        (experiment["experiment_id"],),
    )
    reclaimed = tuning_store.reclaim_stale_experiments(seeded.state, "kb", "2026-01-01T00:00:00Z")
    assert experiment["experiment_id"] in reclaimed
    row = tuning_store.experiment_status(seeded.state, experiment["experiment_id"])
    assert row["status"] == "interrupted"
    assert row["error"]

    # The trials are still there, which is what makes the next attempt resume
    # rather than start over.
    assert tuning_store.load_trials(seeded.state, experiment["experiment_id"]) == trials_before


def test_the_tuning_tables_survive_a_migration(seeded: Any) -> None:
    """`/state` is user data, and the applied overlay is operationally live."""

    from pheasant.persistence.migrate import NOT_MIGRATED, TABLE_ORDER

    for table in (
        "tuning_experiments",
        "tuning_trials",
        "tuning_decisions",
        "tuning_bundles",
    ):
        assert table in TABLE_ORDER, f"{table} would be silently dropped by a migration"
        assert table not in NOT_MIGRATED


# --------------------------------------------------------------------------
# Refusing to tune
# --------------------------------------------------------------------------


def test_it_declines_when_the_failures_are_not_in_a_tunable_stage() -> None:
    """The most valuable thing this plane can say is "do not tune"."""

    from pheasant.tuning.space import ParameterSpace, baseline_values
    from pheasant.tuning.strategy import propose

    space = ParameterSpace()
    baseline = baseline_values(DEFAULT_RANKING, space)
    histogram = {
        "ranked": [{"stage": "absent_from_corpus", "count": 9}],
        "counts": {"absent_from_corpus": 9, "fusion": 1},
        "misses": 10,
        "actionable_share": 0.1,
    }
    assert propose(space, baseline, histogram) == []


def test_nothing_the_plane_writes_is_retrievable_as_knowledge(seeded: Any) -> None:
    """A region must not answer a question with its own experiment.

    The same invariant the evaluation plane holds, checked the same way: run
    the batch, then ask the region about the things it just wrote.
    """

    run_tuning(seeded, budget=Budget(refusion_trials=8, requery_trials=2))
    experiment = tuning_store.latest_experiment(seeded.state, "kb")

    for query in ("tuning experiment", experiment["experiment_id"], "rrf_k parameter bundle"):
        results = seeded.search_context(query).get("results") or []
        paths = " ".join(str(item.get("relative_path") or "") for item in results)
        assert "tuning" not in paths
        assert experiment["experiment_id"] not in paths

    # And no artifact or chunk was created for any of it.
    rows = seeded.state.rows(
        "SELECT COUNT(*) AS n FROM artifacts WHERE relative_path LIKE '%tuning%'", ()
    )
    assert int(rows[0]["n"]) == 0
