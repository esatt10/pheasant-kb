"""Standalone mode, resumption, tracking, and cold storage.

Rule 7 says every seam owes a test asserting the no-infrastructure path is
unchanged, and this plane adds several: an optional tracking backend, a cold
storage directory, a lease, a background executor. A region with none of those
must behave exactly as it did — which for the tuning plane means "as if it did
not exist", because it is off by default.

The resumption tests are here rather than in `test_tuning_batch.py` because
they are about the *container*, not the batch: a killed process, a `/exports`
that cannot be written, a tracking backend that is not installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.search.ranking import DEFAULT_RANKING, RankingResolver
from pheasant.tuning import store as tuning_store
from pheasant.tuning.contracts import Decision, Diagnosis, Experiment, ParameterPoint
from pheasant.tuning.executor import BackpressureGate, StoodDown, TuningExecutor
from pheasant.tuning.tracking import CompositeSink, MlflowSink, NullSink, sink_for
from tests.test_tuning_batch import _engine, _seed

# --------------------------------------------------------------------------
# Standalone mode
# --------------------------------------------------------------------------


def test_tuning_is_off_by_default() -> None:
    """A region that never asks for this carries none of it."""

    config = PheasantConfig()
    assert config.tuning.enabled is False
    assert config.tuning.auto.enabled is False
    assert config.tuning.auto.apply is False
    assert config.tuning.tracking.backend == "off"


def test_a_region_with_no_bundle_ranks_on_its_configured_values(tmp_path: Path) -> None:
    """The no-infrastructure path: no experiment, no bundle, no overlay.

    The resolver still runs on every search, so "it does nothing when there is
    nothing" is a property the search path depends on rather than a nicety.
    """

    engine = _engine(tmp_path)
    try:
        resolver = RankingResolver(base=DEFAULT_RANKING, state=engine.state, kb_id="kb")
        assert resolver.current().values() == DEFAULT_RANKING.values()
        assert resolver.current().provenance == "default"
        assert tuning_store.active_overlay(engine.state, "kb") is None
        # ...and search works, unchanged.
        assert engine.search_context("invoice retry")["results"] is not None
    finally:
        engine.close()


def test_the_search_payload_is_unchanged_when_nothing_asks_for_stages(
    tmp_path: Path,
) -> None:
    """Rule: a key appears only when it says something."""

    engine = _engine(tmp_path)
    try:
        payload = engine.search_context("invoice retry")
        assert "stages" not in payload
        assert set(payload) <= {
            "query",
            "knowledge_base",
            "mode",
            "results",
            "counts",
            "memory_policy",
            "memory_steering",
        }
    finally:
        engine.close()


def test_the_default_sink_stack_needs_no_backend(tmp_path: Path) -> None:
    """`/state` is the source of truth; everything else is a mirror."""

    engine = _engine(tmp_path)
    try:
        sink = sink_for(engine.config, engine.state)
        assert isinstance(sink, CompositeSink)
        assert [s.name for s in sink.sinks] == ["state"]
    finally:
        engine.close()


def test_an_unavailable_tracking_backend_never_fails_a_batch(tmp_path: Path) -> None:
    """A dashboard being down must not cost a tuning result.

    MLflow is almost certainly not installed in the offline suite, which is the
    case this asserts: the sink constructs, warns, and every call is a no-op.
    """

    sink = MlflowSink(tracking_uri="", exports_path=tmp_path)
    experiment = Experiment(
        experiment_id="e",
        kb_id="kb",
        snapshot_id="s",
        cohort_id="c",
        holdout_cohort_id="",
        control_cohort_id="",
        space_digest="d",
        budget={},
        baseline_point=ParameterPoint.of({"rrf_k": 60.0}),
    )
    # None of these may raise, whether or not mlflow is importable.
    sink.start_experiment(experiment, {})
    sink.log_diagnosis(
        experiment,
        Diagnosis(
            diagnosis_id="d",
            kb_id="kb",
            snapshot_id="s",
            cohort_id="c",
            cohort_name="rolling",
            baseline_point_id="p",
            histogram={"counts": {"fusion": 1}},
        ),
    )
    sink.log_decision(
        experiment, Decision(decision_id="d", experiment_id="e", outcome="no_change", reason="")
    )
    sink.finish(experiment, "completed")


def test_one_sink_failing_never_reaches_the_next() -> None:
    """Ordering is the contract: the durable write happens first, and a mirror
    that raises must not prevent a second mirror — or the batch."""

    seen: list[str] = []

    class Boom(NullSink):
        name = "boom"

        def finish(self, experiment: Any, status: str) -> None:
            raise RuntimeError("mirror is down")

    class Quiet(NullSink):
        name = "quiet"

        def finish(self, experiment: Any, status: str) -> None:
            seen.append(status)

    CompositeSink([Boom(), Quiet()]).finish(None, "completed")
    assert seen == ["completed"]


# --------------------------------------------------------------------------
# Cold storage
# --------------------------------------------------------------------------


def test_a_cold_payload_round_trips(tmp_path: Path) -> None:
    ref = tuning_store.write_cold(tmp_path, "kb", "exp-1", "trial", [{"a": 1}, {"b": 2}])
    assert ref
    assert Path(ref).suffix == ".zst"
    assert tuning_store.read_cold(ref) == [{"a": 1}, {"b": 2}]


def test_cold_storage_failing_never_fails_a_batch(tmp_path: Path) -> None:
    """`/exports` can be a read-only mount — which is what
    `docker-compose.scale.yml` recommends for the API replicas.

    Losing the audit detail is bad; losing the experiment because a volume
    filled is worse.
    """

    unwritable = tmp_path / "nope"
    unwritable.write_text("not a directory", encoding="utf-8")
    assert tuning_store.write_cold(unwritable, "kb", "exp", "trial", [{"a": 1}]) == ""
    assert tuning_store.read_cold("") == []
    assert tuning_store.read_cold(str(tmp_path / "missing.jsonl.zst")) == []


def test_no_cold_path_configured_is_not_an_error() -> None:
    assert tuning_store.write_cold(None, "kb", "exp", "trial", [{"a": 1}]) == ""


# --------------------------------------------------------------------------
# The executor
# --------------------------------------------------------------------------


def test_the_executor_holds_one_slot(tmp_path: Path) -> None:
    """Not a pool. Parallelism would multiply exactly the database contention
    this exists to avoid, and nobody is waiting for a tuning batch."""

    import threading

    engine = _engine(tmp_path)
    try:
        executor = TuningExecutor(engine.state)
        release = threading.Event()
        started = threading.Event()

        def slow() -> None:
            started.set()
            release.wait(5)

        assert executor.submit("first", slow) is True
        started.wait(5)
        # Refused rather than queued: a second batch is almost always redundant
        # by the time it would run, because the state it would measure has
        # moved on.
        assert executor.submit("second", lambda: None) is False
        release.set()
    finally:
        engine.close()


def test_the_executor_stands_down_when_the_index_queue_has_work(tmp_path: Path) -> None:
    """Indexing is somebody waiting; this is a measurement."""

    engine = _engine(tmp_path)
    try:
        gate = BackpressureGate(
            engine.state, max_queue_depth=1, interval=0.0, queue_depth=lambda: 1
        )
        ok, reason = gate.check()
        assert not ok
        assert "index queue" in reason

        executor = TuningExecutor(engine.state, gate=gate)
        with pytest.raises(StoodDown):
            executor.checkpoint()
    finally:
        engine.close()


def test_the_executor_does_not_stand_down_on_its_own_lease(tmp_path: Path) -> None:
    """`__tuning__` and `__evaluation__` are pseudo-sources.

    A batch that yielded because *it* held the tuning lease would never run at
    all, and the bug would look like "tuning silently does nothing".
    """

    engine = _engine(tmp_path)
    try:
        from pheasant.tuning.executor import TUNING_LEASE

        engine.state.execute(
            "INSERT INTO source_leases (source_id, owner, acquired_at, heartbeat_at) "
            "VALUES (?,?,?,?)",
            (TUNING_LEASE, "me", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        gate = BackpressureGate(engine.state, max_queue_depth=10, interval=0.0)
        ok, _reason = gate.check()
        assert ok
    finally:
        engine.close()


def test_a_stop_request_raises_rather_than_returning_a_flag(tmp_path: Path) -> None:
    """A caller cannot forget to check an exception."""

    engine = _engine(tmp_path)
    try:
        executor = TuningExecutor(engine.state)
        executor.checkpoint()
        executor.stop()
        with pytest.raises(StoodDown):
            executor.checkpoint()
    finally:
        engine.close()


# --------------------------------------------------------------------------
# Resumption
# --------------------------------------------------------------------------


def test_a_resumed_batch_reaches_the_same_decision_for_fewer_searches(
    tmp_path: Path,
) -> None:
    """The property that makes checkpointing worth having.

    A resumed batch must compute the numbers an uninterrupted one would — not
    approximately, and not a weaker version of them because it skipped what it
    already had. It should simply cost less.
    """

    from pheasant.tuning.runner import run_tuning
    from pheasant.tuning.strategy import Budget

    engine = _engine(tmp_path)
    _seed(engine)
    try:
        budget = Budget(refusion_trials=6, requery_trials=2)
        first = run_tuning(engine, budget=budget)
        second = run_tuning(engine, budget=budget)

        assert first.status == second.status == "completed"
        assert first.experiment_id == second.experiment_id
        assert first.decision is not None and second.decision is not None
        assert first.decision.outcome == second.decision.outcome
        assert first.decision.winning_point_id == second.decision.winning_point_id
        assert [c.delta for c in first.decision.comparisons] == [
            c.delta for c in second.decision.comparisons
        ]
        # ...and it did less work to get there.
        assert second.searches < first.searches
        assert second.trials_reused > 0
    finally:
        engine.close()


def test_reclamation_frees_a_batch_whose_process_stopped(tmp_path: Path) -> None:
    """A killed container leaves a row saying `running` that nothing will ever
    rewrite. The staleness test lives *in* the UPDATE, so a legitimate
    successor that started between the read and the write survives."""

    from pheasant.tuning.runner import run_tuning
    from pheasant.tuning.strategy import Budget

    engine = _engine(tmp_path)
    _seed(engine)
    try:
        run_tuning(engine, budget=Budget(refusion_trials=4, requery_trials=1))
        experiment_id = tuning_store.latest_experiment(engine.state, "kb")["experiment_id"]
        engine.state.execute(
            "UPDATE tuning_experiments SET status='running', heartbeat_at=? "
            "WHERE experiment_id = ?",
            ("2000-01-01T00:00:00Z", experiment_id),
        )
        # A fresh heartbeat is not stale, and must survive the sweep.
        assert (
            tuning_store.reclaim_stale_experiments(engine.state, "kb", "1999-01-01T00:00:00Z") == []
        )
        assert tuning_store.reclaim_stale_experiments(
            engine.state, "kb", "2026-01-01T00:00:00Z"
        ) == [experiment_id]
        row = tuning_store.experiment_status(engine.state, experiment_id)
        assert row["status"] == "interrupted"
        assert row["error"]
    finally:
        engine.close()


def test_a_conditional_claim_reads_a_real_rowcount(tmp_path: Path) -> None:
    """Guards a defect that would be invisible on the default backend.

    `SqliteBackend` inherited a `statement()` returning `rowcount=0`
    unconditionally. Every claim here decides on that number, so inheriting it
    would make the claim always report "I did not get it" on SQLite and work
    correctly on Postgres — the mirror image of the failure mode this codebase
    has already produced twice, and the worse direction, because the offline
    suite runs on SQLite.
    """

    engine = _engine(tmp_path)
    try:
        engine.state.execute(
            "INSERT INTO tuning_experiments "
            "(experiment_id, kb_id, snapshot_id, cohort_id, space_digest, "
            " baseline_point_id, budget_json, started_at, status) "
            "VALUES ('e1','kb','s','c','d','p','{}','2026-01-01T00:00:00Z','completed')"
        )
        _rows, changed = engine.state.backend.statement(
            "UPDATE tuning_experiments SET status='running' "
            "WHERE experiment_id='e1' AND status <> 'running'"
        )
        assert changed == 1
        _rows, unchanged = engine.state.backend.statement(
            "UPDATE tuning_experiments SET status='running' "
            "WHERE experiment_id='e1' AND status <> 'running'"
        )
        assert unchanged == 0
    finally:
        engine.close()
