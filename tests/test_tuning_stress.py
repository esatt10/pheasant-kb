"""Stress: what breaks when several things happen at once.

The tuning plane's correctness claims are all about *contention* — one batch
across N replicas, an overlay changing while searches read it, a cancel from a
process that is not the one running the batch. Those are exactly the claims a
single-threaded test cannot make, so these drive them concurrently and look for
the failure rather than asserting the happy path harder.

Each test corresponds to a claim made in prose somewhere else in the plane. If
the claim is wrong, this is where it shows.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from pheasant.search.ranking import DEFAULT_RANKING, RankingResolver
from pheasant.tuning import store as tuning_store
from pheasant.tuning.contracts import TuningBundle
from pheasant.tuning.runner import run_tuning
from pheasant.tuning.strategy import Budget
from tests.test_tuning_batch import _engine, _seed


def _bundle(kb: str, params: dict[str, float]) -> TuningBundle:
    return TuningBundle(
        bundle_id=TuningBundle.identity(params),
        kb_id=kb,
        experiment_id="exp-stress",
        decision_id="dec-stress",
        snapshot_id="snap-stress",
        parameters=params,
    )


def test_racing_batches_produce_one_experiment(tmp_path: Path) -> None:
    """N replicas, one batch. What the `__tuning__` lease exists to guarantee.

    Started from threads on a barrier rather than sequentially, because the
    interesting failure is the interleaving: two processes that both read
    "nothing is running" before either writes. The claim is a conditional
    UPDATE for exactly that reason, and this is what catches it regressing to
    a read-then-write.
    """

    engine = _engine(tmp_path)
    _seed(engine)
    try:
        outcomes: list[Any] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(3)

        def go() -> None:
            try:
                barrier.wait(30)
                outcomes.append(
                    run_tuning(engine, budget=Budget(refusion_trials=4, requery_trials=1))
                )
            except BaseException as exc:  # noqa: BLE001 - recorded, then asserted
                errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(180)

        assert not errors, errors
        assert len(outcomes) == 3
        # One did the work; the others were refused. Which mechanism refused
        # them (the lease or the row claim) is timing and not the point — the
        # point is that exactly one ran and there is exactly one row.
        completed = [o for o in outcomes if o.status == "completed"]
        assert len(completed) == 1, [(o.status, o.skipped_reason) for o in outcomes]
        assert len(tuning_store.list_experiments(engine.state, "kb")) == 1
    finally:
        engine.close()


def test_the_overlay_never_resolves_to_a_torn_configuration(tmp_path: Path) -> None:
    """Applying while readers read must never yield a half-applied point.

    A resolver hands its parameters to the SQL builder and to the fusion loop,
    so a torn read is not cosmetic: it would rank one query with the old column
    weights and the new fusion constant — a configuration the region can never
    serve and nobody could reproduce.
    """

    engine = _engine(tmp_path)
    try:
        low = _bundle("kb", {"rrf_k": 10.0, "title_weight": 4.0})
        high = _bundle("kb", {"rrf_k": 200.0, "title_weight": 24.0})
        for bundle in (low, high):
            tuning_store.save_bundle(engine.state, bundle)

        # TTL zero: every read goes to the database. That is the worst case for
        # tearing, and the only way to exercise it at all.
        resolvers = [
            RankingResolver(base=DEFAULT_RANKING, state=engine.state, kb_id="kb", ttl_seconds=0.0)
            for _ in range(4)
        ]
        seen: list[tuple[float, float]] = []
        stop = threading.Event()
        errors: list[BaseException] = []

        def read(resolver: RankingResolver) -> None:
            try:
                while not stop.is_set():
                    point = resolver.current()
                    seen.append((point.rrf_k, point.title_weight))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        readers = [threading.Thread(target=read, args=(r,)) for r in resolvers]
        for thread in readers:
            thread.start()
        for _ in range(15):
            tuning_store.apply_bundle(engine.state, "kb", low.bundle_id, applied_by="stress")
            tuning_store.apply_bundle(engine.state, "kb", high.bundle_id, applied_by="stress")
        stop.set()
        for thread in readers:
            thread.join(30)

        assert not errors, errors
        assert seen, "the readers never resolved anything"
        allowed = {
            (10.0, 4.0),
            (200.0, 24.0),
            (DEFAULT_RANKING.rrf_k, DEFAULT_RANKING.title_weight),
        }
        torn = sorted({point for point in seen if point not in allowed})
        assert not torn, f"resolved a configuration that was never applied: {torn}"
    finally:
        engine.close()


def test_concurrent_searches_do_not_lose_stage_telemetry(tmp_path: Path) -> None:
    """The counters sit on the request path, so they run under real parallelism.

    A counter that lost increments under contention would silently under-report
    every rate built on it, and the under-reporting would be worst exactly when
    the region is busiest.
    """

    from pheasant.telemetry import metrics

    engine = _engine(tmp_path)
    try:
        before = (
            metrics.REGISTRY.value("pheasant_retrieval_arm_total", arm="text", outcome="ok") or 0.0
        )
        errors: list[BaseException] = []

        def search() -> None:
            try:
                for _ in range(15):
                    engine.search_context("invoice retry")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=search) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)

        assert not errors, errors
        after = (
            metrics.REGISTRY.value("pheasant_retrieval_arm_total", arm="text", outcome="ok") or 0.0
        )
        assert after - before == 90.0, f"expected 90 increments, saw {after - before}"
        assert metrics.REGISTRY.render(), "the exposition still renders"
    finally:
        engine.close()


def test_a_cancel_from_another_state_handle_reaches_the_batch(tmp_path: Path) -> None:
    """Why cancel is a column rather than a flag on a thread.

    The cancelling side holds its *own* state handle — it is the other replica
    as far as the batch is concerned. A cancel that only set an in-memory flag
    would do nothing here, and nothing in a fleet.
    """

    from pheasant.persistence.state_store import StateStore

    engine = _engine(tmp_path)
    _seed(engine)
    try:
        started = threading.Event()
        other = StateStore.from_config(engine.config)
        other.migrate()

        def cancel_soon() -> None:
            if not started.wait(60):
                return
            for _ in range(500):
                row = tuning_store.active_experiment(other, "kb")
                if row:
                    tuning_store.request_cancel(
                        other, "kb", row["experiment_id"], requested_by="another-replica"
                    )
                    return

        watcher = threading.Thread(target=cancel_soon)
        watcher.start()

        def progress(phase: str, _detail: str) -> None:
            if phase == "trial":
                started.set()

        result = run_tuning(
            engine,
            budget=Budget(refusion_trials=200, requery_trials=40, max_searches=100_000),
            on_progress=progress,
        )
        started.set()
        watcher.join(60)
        other.close()

        # Either it finished before the cancel landed (a small fixture is fast)
        # or it stopped and recorded who stopped it. Both are correct; a cancel
        # that was silently ignored is not.
        assert result.status in {"cancelled", "completed"}, result.skipped_reason
        if result.status == "cancelled":
            assert "another-replica" in result.skipped_reason
            row = tuning_store.experiment_status(engine.state, result.experiment_id)
            assert row["status"] == "cancelled"
            # Resumable, not destructive: the work already done survives.
            assert tuning_store.load_trials(engine.state, result.experiment_id)
    finally:
        engine.close()


def test_cancelling_nothing_is_refused_rather_than_silently_succeeding(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        assert not tuning_store.request_cancel(
            engine.state, "kb", "exp-does-not-exist", requested_by="test"
        )
    finally:
        engine.close()


def test_stage_health_refuses_a_denominator_too_thin_to_read(tmp_path: Path) -> None:
    """A rate over nine searches is noise wearing a percentage sign."""

    from pheasant.tuning.health import MINIMUM_SAMPLES, stage_health

    engine = _engine(tmp_path)
    try:
        health = stage_health(engine.state, "kb")
        assert health["status"] == "insufficient_evidence"
        assert health["minimum_samples"] == MINIMUM_SAMPLES
        assert health["reason"]
    finally:
        engine.close()


def test_health_says_when_its_samples_span_two_configurations(tmp_path: Path) -> None:
    """Averaging across an apply hides exactly the change somebody is looking for."""

    from pheasant.tuning.health import stage_health

    engine = _engine(tmp_path)
    try:
        for i in range(40):
            trace = f"{i:032x}"
            digest = {
                "retrieval_stages": {
                    "returned": 2,
                    "arms": {"text": 3},
                    "fused_depth": 3,
                    "truncated": True,
                    "bundle_id": "bundle-a" if i % 2 else "",
                    "provenance": "bundle" if i % 2 else "config",
                }
            }
            engine.state.execute(
                "INSERT INTO interaction_events "
                "(id, kb_id, trace_id, span_id, modality, operation, started_at, status, "
                " attributes_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    trace,
                    "kb",
                    trace,
                    trace[:16],
                    "ui",
                    "/search",
                    f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z",
                    "ok",
                    json.dumps(digest),
                ),
            )
        health = stage_health(engine.state, "kb")
        assert health["status"] == "measured"
        assert health["samples"] == 40
        assert health["mixed_configurations"] is True
        assert len(health["bundles"]) == 2
        assert health["truncation"]["rate"] == 1.0
    finally:
        engine.close()


#: Trace-id shapes a sampler can actually be handed. Only the first is what
#: pheasant mints; the rest arrive from upstream `traceparent` headers, and an
#: SDK deriving ids from a counter or a clock is entirely ordinary.
TRACE_SHAPES = {
    "random": [__import__("secrets").token_hex(16) for _ in range(2000)],
    "sequential": [f"{i:032x}" for i in range(2000)],
    "high_bits_only": [f"{i * 2**96:032x}" for i in range(2000)],
}


@pytest.mark.parametrize("shape", sorted(TRACE_SHAPES))
@pytest.mark.parametrize("rate", [0.0, 0.05, 0.25, 1.0])
def test_sampling_is_uniform_whatever_the_trace_ids_look_like(shape: str, rate: float) -> None:
    """The sampler must not depend on where a trace id keeps its entropy.

    The first implementation sliced the low four hex characters, on the
    reasoning that a W3C trace id is random so any slice of it is uniform.
    True of the ids pheasant mints, false of ids that arrive from upstream: a
    counter-derived id leaves the low bits nearly constant, and sampling
    collapsed to all-or-nothing. It was sampling 100% at a requested 25%, and
    it looked like it was working.

    That is why the shapes below are parameterised rather than represented by
    one realistic case.
    """

    from pheasant.search.observability import should_sample

    traces = TRACE_SHAPES[shape]
    first = [should_sample(rate, trace) for trace in traces]
    # Deterministic: every hop of one call must agree, or the sampled set
    # cannot be joined back together.
    assert first == [should_sample(rate, trace) for trace in traces]

    share = sum(first) / len(first)
    if rate in (0.0, 1.0):
        assert share == rate
    else:
        # Generous, but nowhere near generous enough to admit 0.0 or 1.0 —
        # which is the failure this exists to catch.
        assert abs(share - rate) < 0.1, f"{shape} at {rate}: observed {share}"


def test_sampling_without_a_trace_never_samples() -> None:
    """A search outside an observed handler has no row to annotate."""

    from pheasant.search.observability import should_sample

    assert not should_sample(0.5, "")
    # Rate 1.0 short-circuits before the trace is looked at, deliberately:
    # "sample everything" is an operator instruction, not a question about
    # this particular call.
    assert should_sample(1.0, "")
