"""The evaluation plane: what it measures, and what it refuses to claim.

These tests pin the refusals as hard as the measurements, because the refusals
are what make the measurements worth anything. Exposure must not become
success; unjudged must not become negative; recall of learned experience must
not be reported as generalization; a metric with no inputs must not report
zero; and a gate failure must not be averaged away by a good score beside it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

import pheasant.evaluation as evaluation
from pheasant.config.schema import PheasantConfig
from pheasant.evaluation import cohorts as cohort_builder
from pheasant.evaluation import gates as gate_checks
from pheasant.evaluation import metrics as metric_functions
from pheasant.evaluation import proof as proof_projection
from pheasant.evaluation import report as report_projection
from pheasant.evaluation import snapshots as snapshot_builder
from pheasant.evaluation import store as evaluation_store
from pheasant.evaluation import variants as variant_matrix
from pheasant.evaluation.candidates import CandidateDecision, validate
from pheasant.evaluation.contracts import (
    Cohort,
    EvaluatedQuery,
    MetricResult,
    MetricScope,
    MetricStatus,
    Polarity,
    SnapshotManifest,
    normalize_query,
    query_id,
)
from pheasant.evaluation.metrics import MetricContext
from pheasant.evaluation.replay import QueryReplay, VariantReplay, paired_ids, shadow_records
from pheasant.sync.log_queue import write_events
from pheasant.telemetry.interactions import InteractionEvent

QUERIES = (
    "where is invoice retry configured",
    "filewatch daemon restart schedule",
    "invoice retry handler location",
    "which module owns the retry policy",
)


def _config(tmp_path: Path, **evaluation_settings: Any) -> tuple[PheasantConfig, Path]:
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
        # Off: these tests put rows in the ledger directly. Leaving it on would
        # build a buffer this app never tears down (no TestClient, so no
        # lifespan) and leak the process-wide slot into whatever runs next --
        # the same reason `test_memory_formation.py` disables it.
        "observability": {"interactions": {"enabled": False}},
        "memory": {"steering_enabled": True},
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

    config, path = _config(tmp_path, **evaluation_settings)
    app = create_app(config, config_path=str(path))
    engine = app.state.engine
    engine.sync_source("docs", "full")
    return engine


def _observe(engine: Any, queries: tuple[str, ...] = QUERIES, *, start: int = 0) -> None:
    write_events(
        engine.state,
        [
            InteractionEvent(
                kb_id="kb",
                operation="/search",
                modality="ui",
                principal="user:ada",
                session_id="s1",
                trace_id=f"{start + index:032x}",
                span_id=f"{start + index:016x}",
                started_at=f"2026-01-01T00:00:{start + index:02d}.000000Z",
                status="ok",
                duration_ms=11.0,
                query_text=query,
                result_paths=["invoice.md"],
                result_ids=["file:docs:invoice.md:branch=none"],
                result_count=1,
                top_score=0.8,
            )
            for index, query in enumerate(queries)
        ],
    )


def _artifact(engine: Any, needle: str) -> str:
    rows = engine.state.rows("SELECT id, relative_path FROM artifacts ORDER BY id")
    return next(str(row["id"]) for row in rows if needle in str(row["relative_path"]))


# --------------------------------------------------------------------------
# Off unless asked for
# --------------------------------------------------------------------------


def test_evaluation_is_off_by_default() -> None:
    settings = PheasantConfig().evaluation
    assert settings.enabled is False
    assert settings.on_material_snapshot is False
    assert settings.promotion.enabled is False
    assert settings.composite_weights == {}


def test_a_disabled_run_does_nothing_and_says_why(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine.config.evaluation.enabled = False
    try:
        outcome = evaluation.run(engine)
        assert outcome.status == "skipped"
        assert "enabled" in outcome.skipped_reason
        assert engine.state.rows("SELECT * FROM evaluation_runs") == []
    finally:
        engine.close()


# --------------------------------------------------------------------------
# Identity and reproducibility
# --------------------------------------------------------------------------


def test_query_identity_is_stable_across_spelling_but_not_meaning() -> None:
    """A frozen anchor names the same questions months later, or it is not an anchor."""

    assert query_id("Where Is Invoice Retry?") == query_id("  where is  invoice retry? ")
    assert query_id("where is invoice retry") != query_id("where is billing retry")
    assert normalize_query("  A   B ") == "a b"


def test_an_unchanged_state_produces_one_snapshot_id(tmp_path: Path) -> None:
    """Two replicas computing a manifest for one state must agree without coordinating."""

    engine = _engine(tmp_path)
    try:
        first = snapshot_builder.build_snapshot(
            engine.state, engine.config, graph=engine.graph_builder.graph
        )
        second = snapshot_builder.build_snapshot(
            engine.state, engine.config, graph=engine.graph_builder.graph
        )
        assert first.corpus == second.corpus
        assert first.graph == second.graph
        assert first.retrieval == second.retrieval
        assert first.memory == second.memory
    finally:
        engine.close()


def test_a_manifest_names_every_field_that_differs(tmp_path: Path) -> None:
    """ "The corpus changed" is not actionable; the dotted field name is."""

    engine = _engine(tmp_path)
    try:
        before = snapshot_builder.build_snapshot(
            engine.state, engine.config, graph=engine.graph_builder.graph
        )
        (Path(engine.config.pheasant.workspace_root) / "docs" / "new.md").write_text(
            "# New\n\nSomething else entirely.\n", encoding="utf-8"
        )
        engine.sync_source("docs", "full")
        after = snapshot_builder.build_snapshot(
            engine.state, engine.config, graph=engine.graph_builder.graph
        )
        differences = before.differences(after)
        assert "corpus.content_manifest_digest" in differences
        assert "corpus.artifact_count" in differences
        assert "corpus.content_manifest_digest" in snapshot_builder.material_change(before, after)
    finally:
        engine.close()


def test_a_count_moving_without_a_digest_is_not_material() -> None:
    """Otherwise a re-count would look like a state change and fire a run."""

    def manifest(count: int) -> SnapshotManifest:
        return SnapshotManifest(
            snapshot_id=f"kb-{count}",
            kb_id="kb",
            created_at="2026-01-01T00:00:00Z",
            effective_as_of="2026-01-01T00:00:00Z",
            corpus={"content_manifest_digest": "same", "artifact_count": count},
        )

    assert snapshot_builder.material_change(manifest(10), manifest(11)) == []
    assert snapshot_builder.material_change(None, manifest(10)) == ["initial"]


def test_an_incomplete_manifest_says_so_rather_than_defaulting(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        # No graph handed in: the section cannot be resolved, and that has to be
        # representable or `incomplete_snapshot_blocks_run` cannot be enforced.
        manifest = snapshot_builder.build_snapshot(engine.state, engine.config, graph=None)
        assert "graph" in manifest.incomplete
        assert manifest.complete is False
        assert gate_checks.evaluate_snapshot(manifest, blocking=True).passed is False
        assert gate_checks.evaluate_snapshot(manifest, blocking=False).passed is True
    finally:
        engine.close()


# --------------------------------------------------------------------------
# The evidence model
# --------------------------------------------------------------------------


def test_exposure_is_never_success() -> None:
    """The single most tempting inference in the system, and the one with no evidence."""

    policy = proof_projection.ProofPolicy.from_config(None)
    for event in ("served", "considered", "included_in_context", "not_selected"):
        assert policy.polarity_of(event) == Polarity.UNKNOWN.value
        assert policy.weight_of(event) == 0.0


def test_the_ledger_yields_exposure_and_nothing_stronger(tmp_path: Path) -> None:
    """A metric mined from "appeared at rank 1" improves when ranking gets more
    confident, not when it gets more correct."""

    engine = _engine(tmp_path)
    _observe(engine)
    try:
        policy = proof_projection.ProofPolicy.from_config(engine.config.evaluation.proof)
        derived = proof_projection.project_from_interactions(engine.state, "kb", policy)
        assert derived
        assert {p.event_type for p in derived} == {"served"}
        assert {p.polarity for p in derived} == {Polarity.UNKNOWN.value}
        assert all(p.weight == 0.0 for p in derived)
    finally:
        engine.close()


def test_weight_reports_all_four_multipliers() -> None:
    """A reader shown only 0.25 cannot tell a decayed conclusive result from a
    fresh citation, and the two support different claims."""

    policy = proof_projection.ProofPolicy.from_config(None)
    weight, multipliers = proof_projection.weigh(policy, "explicit_accept")
    assert set(multipliers) == {"type", "strength", "temporal", "source"}
    assert weight == pytest.approx(
        multipliers["type"]
        * multipliers["strength"]
        * multipliers["temporal"]
        * multipliers["source"]
    )


def test_decay_is_opt_in() -> None:
    """Nobody should discover that a year-old conclusive result stopped counting."""

    off = proof_projection.ProofPolicy.from_config(None)
    _, multipliers = proof_projection.weigh(
        off, "explicit_accept", observed_at="2020-01-01T00:00:00Z", now="2026-01-01T00:00:00Z"
    )
    assert multipliers["temporal"] == 1.0

    on = proof_projection.ProofPolicy(
        event_weights={"explicit_accept": 1.0},
        strength_multipliers={"strong": 1.0},
        temporal_decay_enabled=True,
        temporal_half_life_days=365.0,
    )
    _, decayed = proof_projection.weigh(
        on, "explicit_accept", observed_at="2020-01-01T00:00:00Z", now="2026-01-01T00:00:00Z"
    )
    assert decayed["temporal"] < 0.05


def test_positive_and_negative_proof_never_cancel_silently() -> None:
    """A conflicted target reads as unknown in any representation storing only
    the net -- and it is the row a reviewer most needs to see."""

    policy = proof_projection.ProofPolicy.from_config(None)
    proofs = [
        proof_projection.make_proof(
            kb_id="kb",
            query_text="q",
            target_type="artifact",
            target_id="a",
            event_type=event,
            policy=policy,
            interaction_id=f"i{index}",
        )
        for index, event in enumerate(("explicit_accept", "explicit_reject"))
    ]
    evidence = proof_projection.aggregate(proofs, policy)
    target = evidence[query_id("q")].targets["a"]
    assert target.positive == 1.0
    assert target.negative == 1.0
    assert target.net == 0.0
    assert target.conflicted(policy.positive_floor) is True

    conflicted, total = proof_projection.conflict_rate(evidence, policy)
    assert (conflicted, total) == (1, 1)


def test_a_weak_citation_alone_is_not_a_known_positive() -> None:
    """`known_positive_recall` counting one would over-claim in its own name."""

    policy = proof_projection.ProofPolicy.from_config(None)
    proof = proof_projection.make_proof(
        kb_id="kb",
        query_text="q",
        target_type="artifact",
        target_id="a",
        event_type="cited",
        policy=policy,
    )
    evidence = proof_projection.aggregate([proof], policy)
    assert evidence[query_id("q")].positives(policy.positive_floor) == []


def test_an_unknown_event_type_is_refused_at_the_door(tmp_path: Path) -> None:
    """A proof row naming an unweighted event is a row no metric can read;
    finding out at metric time is finding out too late."""

    engine = _engine(tmp_path)
    try:
        with pytest.raises(ValueError, match="Unknown evidence event type"):
            evaluation.record_evidence(
                engine.state,
                engine.config,
                query="q",
                target_id="a",
                event_type="was_helpful",
            )
    finally:
        engine.close()


def test_recording_the_same_event_twice_is_one_row(tmp_path: Path) -> None:
    """At-least-once delivery from an agent must not double-weight a judgment."""

    engine = _engine(tmp_path)
    try:
        first = evaluation.record_evidence(
            engine.state,
            engine.config,
            query="q",
            target_id="a",
            event_type="selected",
            observed_at="2026-01-01T00:00:00Z",
            interaction_id="i1",
        )
        second = evaluation.record_evidence(
            engine.state,
            engine.config,
            query="q",
            target_id="a",
            event_type="selected",
            observed_at="2026-01-01T00:00:00Z",
            interaction_id="i1",
        )
        assert first["proof_id"] == second["proof_id"]
        rows = engine.state.rows("SELECT * FROM evaluation_proofs")
        assert len(rows) == 1
    finally:
        engine.close()


def test_a_retry_naming_its_interaction_does_not_double_weight(tmp_path: Path) -> None:
    """An agent's retried POST must not count the judgment twice. Found by
    re-posting a proof against a real Postgres."""

    engine = _engine(tmp_path)
    try:
        first = evaluation.record_evidence(
            engine.state,
            engine.config,
            query="q",
            target_id="a",
            event_type="explicit_accept",
            interaction_id="call-1",
        )
        second = evaluation.record_evidence(
            engine.state,
            engine.config,
            query="q",
            target_id="a",
            event_type="explicit_accept",
            interaction_id="call-1",
        )
        assert first["proof_id"] == second["proof_id"]
        assert len(engine.state.rows("SELECT * FROM evaluation_proofs")) == 1
    finally:
        engine.close()


def test_two_occasions_without_an_interaction_id_stay_two(tmp_path: Path) -> None:
    """Selecting the same document for the same query on two different days is
    two judgments; collapsing them would under-weight what was said twice."""

    engine = _engine(tmp_path)
    try:
        first = evaluation.record_evidence(
            engine.state,
            engine.config,
            query="q",
            target_id="a",
            event_type="selected",
            observed_at="2026-01-01T00:00:00Z",
        )
        second = evaluation.record_evidence(
            engine.state,
            engine.config,
            query="q",
            target_id="a",
            event_type="selected",
            observed_at="2026-01-05T00:00:00Z",
        )
        assert first["proof_id"] != second["proof_id"]
        assert len(engine.state.rows("SELECT * FROM evaluation_proofs")) == 2
    finally:
        engine.close()


def test_a_partition_token_is_not_an_identity() -> None:
    """Two proofs from one principal belong together; the row must not answer
    "what did Ada ask"."""

    token = proof_projection.partition_token("kb", "user:ada", "s1")
    assert token == proof_projection.partition_token("kb", "user:ada", "s1")
    assert token != proof_projection.partition_token("kb", "user:bo", "s1")
    assert "ada" not in (token or "")
    assert proof_projection.partition_token("kb", None, None) is None


def test_sufficiency_names_the_condition_that_failed() -> None:
    """ "Insufficient evidence" alone tells an operator nothing about whether to
    wait, instrument a surface, or widen a cohort."""

    policy = proof_projection.ProofPolicy(
        event_weights={"selected": 0.5},
        strength_multipliers={"moderate": 1.0},
        minimum_eligible_queries=10,
        minimum_evidenced_queries=5,
        minimum_independent_interactions=5,
    )
    result = proof_projection.assess_sufficiency(
        policy, eligible_query_ids=["q1"], evidence={}, proofs=[]
    )
    assert result.sufficient is False
    assert any("eligible queries 1 < 10" in reason for reason in result.reasons)
    assert any("evidenced queries" in reason for reason in result.reasons)


# --------------------------------------------------------------------------
# Cohorts and leakage
# --------------------------------------------------------------------------


def test_a_frozen_anchor_is_reused_not_rebuilt(tmp_path: Path) -> None:
    """An anchor rebuilt every run is a rolling cohort wearing an anchor's name."""

    engine = _engine(tmp_path)
    _observe(engine)
    try:
        anchor = cohort_builder.build_anchor(engine.state, "kb", minimum_queries=2)
        assert anchor is not None and anchor.frozen
        evaluation_store.save_cohort(engine.state, anchor)

        _observe(engine, ("a brand new question nobody asked before",), start=50)
        again = cohort_builder.build_anchor(engine.state, "kb", minimum_queries=2, existing=anchor)
        assert again is anchor
        assert again.query_ids == anchor.query_ids
    finally:
        engine.close()


def test_an_anchor_is_not_frozen_before_it_is_a_baseline(tmp_path: Path) -> None:
    """Freezing four questions means being stuck with them at every snapshot."""

    engine = _engine(tmp_path)
    _observe(engine, QUERIES[:2])
    try:
        assert cohort_builder.build_anchor(engine.state, "kb", minimum_queries=20) is None
    finally:
        engine.close()


def test_the_holdout_excludes_every_query_that_made_the_intervention(tmp_path: Path) -> None:
    """Being asked again later does not make a query independent."""

    engine = _engine(tmp_path)
    _observe(engine)
    engine.state.upsert_memory_candidate(
        {
            "id": "cand-1",
            "rule_id": "alias-cooccurrence-v1",
            "params_hash": "p",
            "scope": "org",
            "subject": None,
            "kind": "alias",
            "text": "invoice retry -> InvoiceRetryPolicy",
            "written_by": None,
            "evidence_json": json.dumps({"query": QUERIES[0]}),
            "observations": 3,
            "sessions": 2,
            "first_seen": "2026-01-01T00:00:00Z",
            "last_seen": "2026-01-01T00:00:10Z",
        }
    )
    try:
        holdout = cohort_builder.build_temporal_holdout(engine.state, "kb")
        learned = cohort_builder.build_learned(engine.state, "kb")
        assert query_id(QUERIES[0]) in learned.query_ids
        assert query_id(QUERIES[0]) not in holdout.query_ids
        assert not set(learned.query_ids) & set(holdout.query_ids)
    finally:
        engine.close()


def test_control_regression_is_paired_against_the_steering_baseline(tmp_path: Path) -> None:
    """The cohort is defined by "no steering rule fires here", so the treatment
    it controls must be steering alone.

    Pairing it against the corpus baseline compares steering *and* memory
    content, and a memory record legitimately answering a control query then
    reads as an unintended regression — the treatment doing its job, counted as
    harm. B1 (memory content, no steering) against B5 (content plus every
    steering kind) differ in exactly the thing the cohort controls for.
    """

    engine = _engine(tmp_path)
    _observe(engine)
    from pheasant.artifacts.store import MemoryStore, memory_source

    source = memory_source(engine.config, engine.state)
    assert source is not None
    # A record that answers a control query. Under a B0 pairing this is what
    # produced the false regression.
    MemoryStore(source.path).append(
        "The filewatch daemon restarts nightly at 0300 UTC.", scope="org"
    )
    engine.sync_source("agent-memory", "full")
    try:
        outcome = evaluation.run(engine)
        control = outcome.report["controls_and_regressions"]
        assert control is not None, "no control-regression metric was published"
        # Paired against the steering baseline, not the corpus one.
        assert control["calculation"]["operands"]["baseline_variant"] == "B1"
        # And the gate passes: no steering rule exists, so B1 and B5 are the
        # same run on these queries.
        gates = {g["gate_id"]: g for g in outcome.report["gates"]}
        assert gates["control_regression"]["passed"] is True
    finally:
        engine.close()


def test_the_control_cohort_excludes_anything_a_rule_could_fire_on(tmp_path: Path) -> None:
    """ "Should have no effect" has to be deterministic, not intuitive."""

    engine = _engine(tmp_path)
    _observe(engine)
    from pheasant.artifacts.store import MemoryStore, memory_source

    source = memory_source(engine.config, engine.state)
    assert source is not None
    MemoryStore(source.path).append(
        "invoice retry -> InvoiceRetryPolicy", scope="org", kind="alias"
    )
    engine.sync_source("agent-memory", "full")
    try:
        control = cohort_builder.build_control(engine.state, "kb")
        texts = {q.text for q in control.queries}
        assert not any("invoice" in text for text in texts)
        assert any("filewatch" in text for text in texts)
    finally:
        engine.close()


# --------------------------------------------------------------------------
# Metrics: what they say and what they refuse to say
# --------------------------------------------------------------------------


def _ctx(cohort: Cohort, evidence: dict[str, Any]) -> MetricContext:
    return MetricContext(
        snapshot_id="kb-test",
        cohort=cohort,
        policy=proof_projection.ProofPolicy.from_config(None),
        evidence=evidence,
    )


def _replay(variant_id: str, ranked: dict[str, list[str]]) -> VariantReplay:
    variant = next(v for v in variant_matrix.default_matrix() if v.variant_id == variant_id)
    run = VariantReplay(variant=variant, cohort_id="cohort-test")
    for qid, ids in ranked.items():
        run.results[qid] = QueryReplay(
            query_id=qid,
            variant_id=variant_id,
            text=qid,
            ranked_ids=list(ids),
            result_count=len(ids),
            duration_ms=1.0,
        )
    return run


def _evidence(query: str, positives: list[str], negatives: list[str]) -> dict[str, Any]:
    policy = proof_projection.ProofPolicy.from_config(None)
    proofs = [
        proof_projection.make_proof(
            kb_id="kb",
            query_text=query,
            target_type="artifact",
            target_id=target,
            event_type=event,
            policy=policy,
            interaction_id=f"i-{target}",
        )
        for target, event in [
            *((target, "explicit_accept") for target in positives),
            *((target, "explicit_reject") for target in negatives),
        ]
    ]
    return proof_projection.aggregate(proofs, policy)


def test_the_worked_example_from_the_specification() -> None:
    """The whole calculation, end to end, on the numbers the spec states."""

    query = "Where is invoice retry behavior configured?"
    qid = query_id(query)
    cohort = Cohort(
        cohort_id="cohort-test",
        kb_id="kb",
        name="anchor",
        purpose="anchor",
        queries=(EvaluatedQuery(query_id=qid, text=query),),
    )
    evidence = _evidence(query, positives=["A", "B"], negatives=["X"])
    ctx = _ctx(cohort, evidence)

    corpus = _replay("B0", {qid: ["X", "C", "A", "D", "E"]})
    memory = _replay("B5", {qid: ["A", "B", "C", "D", "X"]})

    assert metric_functions.known_positive_recall(ctx, corpus, 5).aggregates[0].value == 0.5
    assert metric_functions.known_positive_recall(ctx, memory, 5).aggregates[0].value == 1.0

    kprr = metric_functions.kprr_scorer(ctx)
    assert kprr(qid, corpus) == pytest.approx(1 / 3)
    assert kprr(qid, memory) == pytest.approx(1.0)

    gain = metric_functions.paired_delta(
        ctx, "memory_attributable_gain", corpus, memory, kprr, label="x", limitation="y"
    )
    assert gain.value == pytest.approx(0.6667, abs=1e-4)

    # Memory improved positive rank but did not remove known-negative exposure.
    assert metric_functions.negative_exposure(ctx, corpus, 5).value == 0.2
    assert metric_functions.negative_exposure(ctx, memory, 5).value == 0.2

    pairwise = metric_functions.pairwise_proof_accuracy(ctx, memory)
    assert pairwise.numerator == 2 and pairwise.denominator == 2


def test_every_published_metric_carries_its_denominator_and_its_limitation() -> None:
    """A score without them is the artifact this plane exists to avoid producing."""

    query = "q"
    qid = query_id(query)
    cohort = Cohort(
        cohort_id="c",
        kb_id="kb",
        name="anchor",
        purpose="anchor",
        queries=(EvaluatedQuery(query_id=qid, text=query),),
    )
    ctx = _ctx(cohort, _evidence(query, ["A"], ["X"]))
    replay = _replay("B5", {qid: ["A", "X"]})

    produced = [
        metric_functions.query_evidence_coverage(ctx),
        metric_functions.proof_conflict_rate(ctx),
        metric_functions.result_evidence_coverage(ctx, replay, 5),
        *metric_functions.known_positive_recall(ctx, replay, 5).all(),
        metric_functions.known_positive_hit(ctx, replay, 5),
        *metric_functions.known_positive_reciprocal_rank(ctx, replay).all(),
        metric_functions.negative_exposure(ctx, replay, 5),
        metric_functions.pairwise_proof_accuracy(ctx, replay),
        metric_functions.evidence_discounted_gain(ctx, replay, 5),
        metric_functions.binary_preference(ctx, replay),
        metric_functions.latency(ctx, replay),
    ]
    for result in produced:
        assert result.validate() == [], f"{result.metric_id}: {result.validate()}"
        assert result.formula
        assert result.substituted
        assert result.does_not_support
        assert result.classification


def test_a_metric_with_no_inputs_reports_null_not_zero() -> None:
    """A red bar describing an instrumentation gap trains people to ignore red bars."""

    qid = query_id("q")
    cohort = Cohort(
        cohort_id="c",
        kb_id="kb",
        name="anchor",
        purpose="anchor",
        queries=(EvaluatedQuery(query_id=qid, text="q"),),
    )
    ctx = _ctx(cohort, {})
    replay = _replay("B5", {qid: ["A", "B"]})

    for result in (
        metric_functions.known_positive_recall(ctx, replay, 5).aggregates[0],
        metric_functions.known_positive_hit(ctx, replay, 5),
        metric_functions.negative_exposure(ctx, replay, 5),
        metric_functions.pairwise_proof_accuracy(ctx, replay),
    ):
        assert result.value is None
        assert result.status == MetricStatus.INSUFFICIENT_EVIDENCE.value


def test_result_evidence_coverage_stops_a_sparse_score_looking_comprehensive() -> None:
    qid = query_id("q")
    cohort = Cohort(
        cohort_id="c",
        kb_id="kb",
        name="anchor",
        purpose="anchor",
        queries=(EvaluatedQuery(query_id=qid, text="q"),),
    )
    ctx = _ctx(cohort, _evidence("q", ["A"], []))
    replay = _replay("B5", {qid: ["A", "B", "C", "D", "E"]})

    recall = metric_functions.known_positive_recall(ctx, replay, 5).aggregates[0]
    coverage = metric_functions.result_evidence_coverage(ctx, replay, 5)
    assert recall.value == 1.0
    assert coverage.value == 0.2  # one of five returned items is judged


def test_a_paired_delta_only_counts_queries_both_runs_answered() -> None:
    """A treatment that improved on the queries it did not crash on is a
    different claim from one that improved on all of them."""

    qid_a, qid_b = query_id("a"), query_id("b")
    cohort = Cohort(
        cohort_id="c",
        kb_id="kb",
        name="anchor",
        purpose="anchor",
        queries=(
            EvaluatedQuery(query_id=qid_a, text="a"),
            EvaluatedQuery(query_id=qid_b, text="b"),
        ),
    )
    evidence = _evidence("a", ["A"], [])
    evidence.update(_evidence("b", ["B"], []))
    ctx = _ctx(cohort, evidence)

    baseline = _replay("B0", {qid_a: ["Z", "A"], qid_b: ["B"]})
    treatment = _replay("B5", {qid_a: ["A"], qid_b: ["B"]})
    treatment.results[qid_b].failed = "boom"
    treatment.failures[qid_b] = "boom"

    both, reasons = paired_ids(baseline, treatment)
    assert both == [qid_a]
    assert reasons == {"failed_in_treatment": 1}

    gain = metric_functions.paired_delta(
        ctx,
        "memory_attributable_gain",
        baseline,
        treatment,
        metric_functions.kprr_scorer(ctx),
        label="x",
        limitation="y",
    )
    assert gain.denominator == 1
    assert gain.excluded_count == 1
    assert gain.exclusion_reasons == {"failed_in_treatment": 1}


def test_a_positive_mean_still_reports_its_worst_regressions() -> None:
    """An intervention that lifts twenty queries and destroys one reads as a
    win by its mean, and as work to do by its worst case."""

    ids = [query_id(f"q{index}") for index in range(4)]
    cohort = Cohort(
        cohort_id="c",
        kb_id="kb",
        name="anchor",
        purpose="anchor",
        queries=tuple(EvaluatedQuery(query_id=qid, text=qid) for qid in ids),
    )
    # `_evidence` keys on the query *text*, so build the proof against the real
    # ids directly: a cohort whose evidence is filed under a different key than
    # its queries silently measures nothing.
    policy = proof_projection.ProofPolicy.from_config(None)
    proofs = [
        proof_projection.make_proof(
            kb_id="kb",
            query_id=qid,
            target_type="artifact",
            target_id="A",
            event_type="explicit_accept",
            policy=policy,
            interaction_id=f"i{qid}",
        )
        for qid in ids
    ]
    ctx = _ctx(cohort, proof_projection.aggregate(proofs, policy))

    baseline = _replay("B0", {qid: ["A"] for qid in ids})
    treatment = _replay("B5", {qid: ["A"] for qid in ids[:3]} | {ids[3]: ["Z", "Y", "Z2", "A"]})
    gain = metric_functions.paired_delta(
        ctx,
        "memory_attributable_gain",
        baseline,
        treatment,
        metric_functions.kprr_scorer(ctx),
        label="x",
        limitation="y",
    )
    worst = gain.operands["worst_regressions"]
    assert worst and worst[0]["query_id"] == ids[3]
    assert worst[0]["delta"] < 0


def test_learned_and_holdout_gains_are_the_same_calculation_named_apart() -> None:
    """Identical in method, explicitly different in scope: that is the point of
    the split, and merging them is the mistake it prevents."""

    from pheasant.evaluation.runner import _relabel

    base = MetricResult(
        metric_id="memory_attributable_gain",
        classification="demonstrated",
        scope=MetricScope(snapshot_id="s"),
        value=0.4,
        formula="f",
        substituted="s",
        denominator=10,
        does_not_support="original",
    )
    learned = _relabel(base, "learned_query_gain")
    holdout = _relabel(base, "future_query_generalization")
    assert learned.value == holdout.value == 0.4
    assert "not evidence of generalization" in learned.does_not_support
    assert "contributed no evidence" in holdout.does_not_support

    holdout_small = MetricResult(
        metric_id="future_query_generalization",
        classification="demonstrated",
        scope=MetricScope(snapshot_id="s"),
        value=0.05,
        formula="f",
        substituted="s",
        denominator=8,
        does_not_support="x",
    )
    gap = metric_functions.generalization_gap(learned, holdout_small)
    assert gap.value == pytest.approx(0.35)


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------


def test_gates_are_reported_apart_from_scores(tmp_path: Path) -> None:
    """An ACL leak is not offset by good recall, and the arithmetic that would
    let it be is the arithmetic gates sit outside of."""

    cohort = Cohort(
        cohort_id="c",
        kb_id="kb",
        name="invariants",
        purpose="synthetic",
        queries=(
            EvaluatedQuery(
                query_id="q-stale",
                text="corrected fact",
                expectation={
                    "kind": "stale_current",
                    "forbidden_record_id": "old",
                    "expected_record_id": "new",
                },
            ),
            EvaluatedQuery(
                query_id="q-acl",
                text="scoped fact",
                expectation={
                    "kind": "acl_isolation",
                    "forbidden_record_id": "private",
                    "principal": "probe",
                },
            ),
            EvaluatedQuery(
                query_id="q-abstain",
                text="zzq nothing",
                expectation={"kind": "abstention", "expected_results": 0},
            ),
        ),
    )
    replay = _replay("B5", {"q-stale": ["n1"], "q-acl": ["n2"], "q-abstain": ["n3"]})
    replay.results["q-stale"].memory_record_ids = ["old"]
    replay.results["q-acl"].memory_record_ids = ["private"]

    gates = {gate.gate_id: gate for gate in gate_checks.evaluate_invariants(cohort, replay)}
    assert gates["stale_current_leak"].passed is False
    assert gates["acl_leak"].passed is False
    assert gates["abstention"].passed is False
    assert gate_checks.all_passed(list(gates.values())) is False
    assert {gate.gate_id for gate in gate_checks.failures(list(gates.values()))} == {
        "stale_current_leak",
        "acl_leak",
        "abstention",
    }


def test_removing_a_known_positive_outright_fails_a_gate_not_a_metric() -> None:
    """Pushing an artifact past rank k is a trade-off; deleting it from the
    result list is not one anybody chose."""

    qid = query_id("q")
    cohort = Cohort(
        cohort_id="c",
        kb_id="kb",
        name="anchor",
        purpose="anchor",
        queries=(EvaluatedQuery(query_id=qid, text="q"),),
    )
    ctx = _ctx(cohort, _evidence("q", ["A"], []))
    baseline = _replay("B0", {qid: ["A", "B"]})
    treatment = _replay("B5", {qid: ["B", "C"]})
    gate = gate_checks.evaluate_known_positive_exclusion(ctx, baseline, treatment)
    assert gate.passed is False
    assert gate.evidence["queries"][0]["artifact_ids"] == ["A"]


def test_an_agent_may_only_inspect_when_a_gate_fails() -> None:
    """The machine-readable half of "hard gates are not averaged away"."""

    from pheasant.evaluation.runner import _allowed_actions

    settings = PheasantConfig().evaluation
    failing = [gate_checks.GateResult(gate_id="acl_leak", passed=False, observed=1, maximum=0)]
    assert _allowed_actions(failing, settings) == [
        "inspect_gate_failures",
        "read_developer_explanation",
    ]
    passing = [gate_checks.GateResult(gate_id="acl_leak", passed=True, observed=0, maximum=0)]
    assert "promote_validated_candidates" not in _allowed_actions(passing, settings)


# --------------------------------------------------------------------------
# The variant matrix
# --------------------------------------------------------------------------


def test_the_corpus_baseline_cannot_be_switched_off() -> None:
    """Every attribution number is a difference against it; without one, a
    treatment score is published absolute and read as accuracy."""

    settings = PheasantConfig().evaluation.variants
    settings.memory_content = False
    settings.alias_only = False
    settings.preference_only = False
    settings.exclusion_only = False
    settings.full_memory = False
    matrix = variant_matrix.selected_matrix(settings)
    assert [variant.variant_id for variant in matrix] == ["B0"]


def test_steering_variants_hold_memory_content_off() -> None:
    """Otherwise a retrieved memory record takes a slot and is counted as the
    rule's doing."""

    matrix = {variant.variant_id: variant for variant in variant_matrix.default_matrix()}
    for variant_id in ("B2", "B3", "B4"):
        assert matrix[variant_id].memory_results == "off"
        assert len(matrix[variant_id].steering_kinds) == 1
    assert matrix["B5"].steering_kinds == variant_matrix.STEERING_KINDS
    assert matrix["B1"].steering_kinds == ()


def test_every_treatment_declares_its_baseline() -> None:
    for variant in variant_matrix.default_matrix(candidate_ids=("c1",)):
        if variant.variant_id == "B0":
            assert variant.baseline_variant_id is None
        else:
            assert variant.baseline_variant_id
    shadow = next(
        v for v in variant_matrix.default_matrix(candidate_ids=("c1",)) if v.variant_id == "B6"
    )
    assert shadow.baseline_variant_id == "B5"


def test_a_shadow_variant_only_exists_when_something_is_proposed() -> None:
    assert all(v.variant_id != "B6" for v in variant_matrix.default_matrix())


# --------------------------------------------------------------------------
# Steering ablation reaches the real retrieval path
# --------------------------------------------------------------------------


def test_steering_kinds_narrows_which_rules_fire(tmp_path: Path) -> None:
    """The ablation has to run against the real rule path, not a copy of it."""

    from pheasant.artifacts.policy import MemoryPolicy
    from pheasant.artifacts.steering import load_steering

    records = [
        {
            "record_id": "r1",
            "scope": "org",
            "subject": None,
            "kind": "alias",
            "text": "invoice retry -> InvoiceRetryPolicy",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": None,
            "tier": "hot",
        },
        {
            "record_id": "r2",
            "scope": "org",
            "subject": None,
            "kind": "exclusion",
            "text": "never: legacy_retry",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": None,
            "tier": "hot",
        },
    ]
    policy = MemoryPolicy()
    now = "2026-06-01T00:00:00Z"

    everything = load_steering(records, policy, now=now, enabled=True)
    assert everything.aliases and everything.exclusions

    alias_only = load_steering(records, policy, now=now, enabled=True, kinds=("alias",))
    assert alias_only.aliases and not alias_only.exclusions

    nothing = load_steering(records, policy, now=now, enabled=True, kinds=())
    assert not nothing


def test_shadow_rules_go_through_the_real_search_call(tmp_path: Path) -> None:
    """A proposed rule must be exercised, not simulated -- and never written."""

    engine = _engine(tmp_path)
    try:
        candidate = {
            "id": "cand-1",
            "kind": "exclusion",
            "text": "never: legacy",
            "scope": "org",
            "first_seen": "2026-01-01T00:00:00Z",
        }
        shadow = shadow_records([candidate], now="2026-06-01T00:00:00Z")
        assert shadow and shadow[0]["kind"] == "exclusion"

        from pheasant.search.hybrid import HybridSearch
        from pheasant.search.text_store import SearchStore

        searcher = HybridSearch(SearchStore(engine.state), steering_enabled=True)
        with_rule = searcher.search_context(
            "kb",
            "invoice retry",
            graph=engine.graph_builder.graph,
            steering_kinds=("exclusion",),
            extra_steering_records=shadow,
        )
        assert with_rule.get("memory_steering", {}).get("exclusions") == ["legacy"]
        # Nothing was written: the store still holds no steering record.
        assert engine.state.rows("SELECT * FROM memory_records WHERE kind='exclusion'") == []
    finally:
        engine.close()


def test_a_proposed_fact_is_not_shadow_replayable() -> None:
    """Its text is in no index, so scoring it against the query would measure
    string similarity and report it as retrieval."""

    facts = shadow_records(
        [{"id": "c1", "kind": "fact", "text": "the retry limit is five"}],
        now="2026-01-01T00:00:00Z",
    )
    assert facts == []


# --------------------------------------------------------------------------
# Candidate promotion
# --------------------------------------------------------------------------


def _promotion_settings(**overrides: Any) -> Any:
    settings = PheasantConfig().evaluation.promotion
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _gain(value: float | None, denominator: int) -> MetricResult:
    return MetricResult(
        metric_id="future_query_generalization",
        classification="demonstrated",
        scope=MetricScope(snapshot_id="s"),
        value=value,
        formula="f",
        substituted="s",
        denominator=denominator,
        does_not_support="x",
    )


def test_a_candidate_is_never_promoted_on_its_own_originating_query() -> None:
    """The self-rewarding loop, refused by default."""

    candidate = {
        "id": "c1",
        "rule_id": "alias-cooccurrence-v1",
        "kind": "alias",
        "evidence_json": json.dumps({"query": "where is invoice retry configured"}),
    }
    decision = validate(
        candidate,
        settings=_promotion_settings(),
        gates=[],
        holdout_gain=_gain(0.4, 5),
        learned_gain=_gain(0.9, 5),
        control_regression=None,
        negative_exposure_gate=None,
        shadow_replayable=True,
        independent_query_ids={query_id("where is invoice retry configured")},
    )
    assert decision.decision == "retain_candidate"
    assert any("derived from" in reason for reason in decision.reasons)


def test_a_candidate_without_a_holdout_result_is_insufficient_not_promoted() -> None:
    """Learned-query performance cannot stand in for forward generalization."""

    candidate = {"id": "c1", "rule_id": "r", "kind": "alias", "evidence_json": "{}"}
    decision = validate(
        candidate,
        settings=_promotion_settings(minimum_independent_queries=1),
        gates=[],
        holdout_gain=None,
        learned_gain=_gain(0.9, 5),
        control_regression=None,
        negative_exposure_gate=None,
        shadow_replayable=True,
        independent_query_ids={query_id("a"), query_id("b")},
    )
    assert decision.decision == "insufficient_evidence"
    assert decision.evidence["learned_gain_only"] == 0.9


def test_a_failing_gate_blocks_promotion_outright() -> None:
    candidate = {"id": "c1", "rule_id": "r", "kind": "alias", "evidence_json": "{}"}
    decision = validate(
        candidate,
        settings=_promotion_settings(minimum_independent_queries=1),
        gates=[
            gate_checks.GateResult(
                gate_id="acl_leak", passed=False, observed=1, maximum=0, detail="leaked"
            )
        ],
        holdout_gain=_gain(0.9, 5),
        learned_gain=_gain(0.9, 5),
        control_regression=None,
        negative_exposure_gate=None,
        shadow_replayable=True,
        independent_query_ids={query_id("a")},
    )
    assert decision.decision == "retain_candidate"
    assert decision.evidence["failed_gates"] == ["acl_leak"]


def test_a_candidate_that_generalizes_is_promoted() -> None:
    candidate = {"id": "c1", "rule_id": "r", "kind": "alias", "evidence_json": "{}"}
    decision = validate(
        candidate,
        settings=_promotion_settings(minimum_independent_queries=1),
        gates=[gate_checks.GateResult(gate_id="acl_leak", passed=True, observed=0, maximum=0)],
        holdout_gain=_gain(0.2, 4),
        learned_gain=_gain(0.5, 6),
        control_regression=None,
        negative_exposure_gate=None,
        shadow_replayable=True,
        independent_query_ids={query_id("a"), query_id("b")},
    )
    assert decision.decision == "promote"
    assert decision.evidence["generalization_gap"] == pytest.approx(0.3)


def test_decisions_are_recorded_even_when_promotion_is_disabled(tmp_path: Path) -> None:
    """Read a month of decisions before letting any of them take effect."""

    from pheasant.evaluation.candidates import apply_decisions

    calls: list[str] = []
    decisions = [
        CandidateDecision(candidate_id="c1", rule_id="r", kind="alias", decision="promote")
    ]
    applied = apply_decisions(
        None, decisions, enabled=False, admit=lambda *a, **k: calls.append("admit")
    )
    assert calls == []
    assert applied[0]["applied"] is False
    assert "disabled" in applied[0]["note"]


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def test_the_health_vector_keeps_a_dimension_it_could_not_measure() -> None:
    """A vector that silently loses a dimension reads as one where that
    dimension was fine."""

    vector = report_projection.health_vector([], primary_variant="B5")
    assert set(vector) == {label for _metric, label in report_projection.HEALTH_VECTOR}
    assert all(entry["value"] is None for entry in vector.values())


def test_the_headline_names_the_cohort_it_is_about() -> None:
    """The same metric is computed per cohort, so a lookup by metric and variant
    alone returns whichever cohort comes first in list order -- the anchor today,
    one reordering away from silently becoming the control."""

    def recall(cohort_id: str, value: float) -> MetricResult:
        return MetricResult(
            metric_id="known_positive_recall_at_5",
            classification="demonstrated",
            scope=MetricScope(snapshot_id="s", cohort_id=cohort_id, variant_id="B5"),
            value=value,
            formula="f",
            substituted="s",
            denominator=10,
            does_not_support="x",
        )

    # Control first in list order, anchor second: the headline must still be
    # the anchor's number.
    results = [recall("cohort-control", 0.1), recall("cohort-anchor", 0.9)]
    vector = report_projection.health_vector(
        results, primary_variant="B5", cohort_id="cohort-anchor"
    )
    assert vector["known_positive_retrieval_at_5"]["value"] == 0.9


def test_the_end_user_paragraph_always_states_the_evidence_limit() -> None:
    results = [
        MetricResult(
            metric_id="query_evidence_coverage",
            classification="demonstrated",
            scope=MetricScope(snapshot_id="s"),
            value=0.44,
            formula="f",
            substituted="46 / 103",
            numerator=46,
            denominator=103,
            does_not_support="x",
        )
    ]
    text = report_projection.end_user_explanation(
        results, [], baseline_variant="B0", treatment_variant="B5"
    )
    assert "46 of 103" in text
    assert "rather than exhaustive corpus accuracy" in text


def test_the_end_user_paragraph_says_when_a_gate_failed() -> None:
    gates = [
        gate_checks.GateResult(
            gate_id="acl_leak", passed=False, observed=1, maximum=0, detail="one record leaked"
        )
    ]
    text = report_projection.end_user_explanation(
        [], gates, baseline_variant="B0", treatment_variant="B5"
    )
    assert "acl_leak" in text
    assert "No promotion is possible" in text


def test_a_composite_excludes_missing_components_rather_than_zero_filling() -> None:
    """Substituting zero or one for a metric nobody could compute is how a
    composite starts describing something other than the configuration."""

    results = [
        MetricResult(
            metric_id="known_positive_recall_at_5",
            classification="demonstrated",
            scope=MetricScope(snapshot_id="s"),
            value=0.8,
            formula="f",
            substituted="s",
            denominator=10,
            does_not_support="x",
        ),
        MetricResult(
            metric_id="negative_exposure_at_5",
            classification="demonstrated",
            scope=MetricScope(snapshot_id="s"),
            value=None,
            formula="f",
            substituted="s",
            denominator=0,
            status=MetricStatus.INSUFFICIENT_EVIDENCE.value,
            does_not_support="x",
        ),
    ]
    composite = report_projection.composite(
        results, {"known_positive_recall_at_5": 0.5, "negative_exposure_at_5": 0.5}
    )
    assert composite["value"] == pytest.approx(0.8)
    assert composite["included"] == {"known_positive_recall_at_5": 1.0}
    assert "negative_exposure_at_5" in composite["excluded"]
    assert composite["not"] == "factual accuracy"


def test_no_composite_is_published_by_default() -> None:
    assert report_projection.composite([], {})["enabled"] is False


def test_the_report_labels_which_kind_of_claim_each_metric_makes() -> None:
    results = [
        MetricResult(
            metric_id="a",
            classification="demonstrated",
            scope=MetricScope(snapshot_id="s"),
            value=1.0,
            formula="f",
            substituted="s",
            denominator=1,
            does_not_support="x",
        ),
        MetricResult(
            metric_id="b",
            classification="structural",
            scope=MetricScope(snapshot_id="s"),
            value=1.0,
            formula="f",
            substituted="s",
            denominator=1,
            does_not_support="x",
        ),
    ]
    breakdown = report_projection.classification_breakdown(results)
    assert breakdown == {"demonstrated": ["a"], "structural": ["b"]}
