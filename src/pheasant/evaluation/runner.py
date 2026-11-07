"""The batch: resolve, replay, measure, gate, explain, decide, persist.

This is the specification's fourteen-step process in one place, and the order
is not arbitrary -- gates are evaluated before aggregation so a failure cannot
be averaged away, and candidate decisions come after gates so a proposal cannot
be promoted out of a run that failed one.

Four things make it safe to run in a fleet, which is the deployment this
region is built for.

**One run per state directory, enforced by a lease.** Several API replicas
pointed at one ``/state`` would otherwise each replay the whole cohort and each
write a run row. :data:`EVALUATION_LEASE` is claimed through the existing
:class:`~pheasant.sync.locks.SourceLease` -- a single conditional ``UPDATE``
that the database arbitrates, already proven against a real Postgres server --
so a second process finds the lease held and declines rather than duplicating
the work. A replica that dies mid-run stops heartbeating and the lease goes
stale, which is exactly the recovery an indexer already gets.

**It never takes ``sync_lock``.** The scheduler holds that across all its work,
and a thousand-query replay inside it would stall incremental sync for every
source in the region. This is the same mistake the observation plane's
hot-to-cold Parquet roll was deliberately moved outside the lock to avoid, and
it is worth restating because the pull toward "just put it on the beat" is
strong: the beat *triggers* a run, the run does not run *inside* the beat's
lock.

**The work is read-only and bounded.** Replay issues searches and nothing else;
usage tracking is off on the replay searcher so a measured retrieval cannot
inflate the salience of the record it measured. A run is capped by
``maximum_queries_per_run`` and ``maximum_runtime_seconds``, and a truncated run
says which queries it dropped rather than reporting a smaller denominator as if
it were the whole cohort.

**Roles.** ``--role api`` replicas serve requests and must not spend their
budget replaying cohorts, so an automatic trigger fires only where the scheduler
runs (``all`` and ``indexer``); every role can still start one on request,
because an operator asking for a report is not background work.

**Replay is deliberately not fanned out over the worker transport.** The fleet
has one (``sync.concurrency.worker_transport``, HTTP or gRPC), and it carries
*preparation* -- which is an optimization, so a failed hop falls back to local
preparation and the sync produces the same thing either way. Replay has the
opposite contract: it is a measurement, and one that fell back to a different
execution path would be measuring the fallback. Distributing a cohort would also
introduce the two things a reproducible run cannot have -- per-worker variation
in what the index holds at the moment each query runs, and a result whose
composition depends on which worker answered. A batch runs in one process
against one snapshot; scale here is fewer queries or a longer interval, and a
region past that is one ``pheasant shard plan`` should be splitting.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from pheasant import decision
from pheasant.evaluation import candidates as candidate_validation
from pheasant.evaluation import cohorts as cohort_builder
from pheasant.evaluation import gates as gate_checks
from pheasant.evaluation import metrics as metric_functions
from pheasant.evaluation import proof as proof_projection
from pheasant.evaluation import report as report_projection
from pheasant.evaluation import snapshots as snapshot_builder
from pheasant.evaluation import store as evaluation_store
from pheasant.evaluation import variants as variant_matrix
from pheasant.evaluation.contracts import (
    EVALUATION_SCHEMA_VERSION,
    Cohort,
    CohortPurpose,
    GateResult,
    MetricResult,
    digest,
    utc_now,
)
from pheasant.evaluation.metrics import MetricContext, MetricSet
from pheasant.evaluation.replay import ReplayEngine, VariantReplay
from pheasant.persistence.heartbeat import WARN_EVERY

logger = logging.getLogger(__name__)

#: The exclusion scope an evaluation run claims, in the existing
#: ``source_leases`` table. A named scope rather than a new table because the
#: mechanism is identical -- one conditional UPDATE the database arbitrates --
#: and a second lease implementation is a second set of staleness bugs. The
#: double underscores keep it out of any real source's namespace.
EVALUATION_LEASE = "__evaluation__"

#: Which variant the health vector and the end-user paragraph are written
#: about. B5 is "everything the memory system currently does", which is the
#: configuration a reader is actually running.
PRIMARY_VARIANT = "B5"


@dataclass
class RunOutcome:
    """What a batch produced, whether or not it completed."""

    run_id: str
    snapshot_id: str
    status: str
    report: dict[str, Any] = field(default_factory=dict)
    gates: list[GateResult] = field(default_factory=list)
    skipped_reason: str = ""
    #: How many times this batch has been picked up. Above 1 means a previous
    #: attempt was interrupted -- a restarted container, a killed process --
    #: and this one resumed it.
    attempts: int = 1
    #: (cohort, variant) replays this attempt reused from checkpoints rather
    #: than re-running. The measure of what the restart cost, and of what the
    #: checkpointing saved.
    resumed_replays: int = 0

    @property
    def gates_passed(self) -> bool:
        """Whether every gate passed -- and never vacuously.

        ``all([])`` is ``True``, so the obvious spelling reports a run that
        evaluated *no* gates as one that passed them all. That is the wrong
        answer for the two outcomes that carry no gates at all: a batch skipped
        because the lease was held, and one skipped because evaluation is off.
        ``pheasant eval run`` turns this straight into its exit status, so the
        vacuous reading is a green CI gate for a run that never happened -- the
        `insufficient_evidence`-rather-than-`0.0` rule, one level up.

        Delegated to :mod:`pheasant.decision` since 35.9. This plane and the
        tuning plane each carried their own copy of the guard, the second one
        citing the first one's incident in its docstring -- which is the lesson
        propagating while the code does not.
        """

        return decision.all_passed(self.gates)


def _owner() -> str:
    """Which process is running this batch. Read by an operator, not by code."""

    import os
    import socket

    return f"{socket.gethostname()}:{os.getpid()}"


class _RunProgress:
    """Durable progress for one batch, plus a heartbeat that outlives a phase.

    Two jobs, and the second is the subtle one.

    **Publishing.** Every phase transition writes ``phase``, ``phase_detail``
    and the unit counters to the run row, so any process -- the UI polling
    HTTP, an agent over MCP, a CLI in another terminal -- can watch a batch it
    did not start. Fail-soft throughout: a progress write must never be able to
    fail the run it is describing.

    **Beating between transitions.** A single cohort/variant replay can run for
    minutes, and during it nothing else writes. Without a beat, a healthy run
    looks exactly like a dead one to
    :func:`~pheasant.evaluation.store.reclaim_stale_runs`, and would be
    reclaimed out from under itself. A daemon thread stamps the clock on an
    interval well inside the stale window -- the same arrangement
    :class:`~pheasant.sync.locks.SourceLease` uses, and for exactly the same
    reason.
    """

    def __init__(self, state: Any, *, heartbeat_seconds: float | None = None):
        self.state = state
        self.run_id: str | None = None
        self.completed_units = 0
        self.total_units = 0
        # Derived from the *configured* stale window, not fixed: see
        # `evaluation_store.heartbeat_interval_for` for what a beat slower than
        # the window does to a run that is merely slow.
        self.heartbeat_seconds = float(
            heartbeat_seconds
            if heartbeat_seconds is not None
            else evaluation_store.RUN_HEARTBEAT_SECONDS
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def bind(self, run_id: str) -> None:
        self.run_id = run_id
        self._start_heartbeat()

    def plan(self, total_units: int) -> None:
        self.total_units = max(0, int(total_units))

    def advance(self, units: int = 1) -> None:
        self.completed_units += int(units)

    def publish(self, phase: str, detail: str = "", units: int | None = None) -> None:
        if units is not None:
            self.completed_units = int(units)
        if self.run_id is None:
            return
        evaluation_store.heartbeat_run(
            self.state,
            run_id=self.run_id,
            now=utc_now(),
            phase=phase,
            detail=detail or None,
            completed_units=self.completed_units,
            total_units=self.total_units or None,
        )

    def _start_heartbeat(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._beat, name="pheasant-evaluation-heartbeat", daemon=True
        )
        self._thread.start()

    def _beat(self) -> None:
        failures = 0
        while not self._stop.wait(self.heartbeat_seconds):
            if self.run_id is None:
                continue
            # One failed write used to end this thread for the rest of the
            # batch, and the run was then reclaimed as `interrupted` while it
            # was still working. A failure is a missed beat, said and retried.
            try:
                evaluation_store.heartbeat_run(
                    self.state,
                    run_id=self.run_id,
                    now=utc_now(),
                    completed_units=self.completed_units,
                )
            except Exception as exc:  # noqa: BLE001 - retried on the next beat
                failures += 1
                if failures == 1 or failures % WARN_EVERY == 0:
                    logger.warning(
                        "evaluation run %s heartbeat failed (%d in a row): %s",
                        self.run_id,
                        failures,
                        exc,
                    )
                continue
            failures = 0

    def fail(self, error: BaseException) -> None:
        """Mark the run failed, durably, with the reason.

        The alternative is a row that says ``running`` forever after a crash --
        and a watcher in another process cannot tell that apart from work still
        in flight. The checkpoints are deliberately *not* cleared: a failed run
        is resumable, and re-running it is how an operator retries.
        """

        if self.run_id is None:
            return
        try:
            evaluation_store.close_run(
                self.state,
                run_id=self.run_id,
                finished_at=utc_now(),
                status="failed",
                gates_passed=False,
                report={"run_id": self.run_id, "error": f"{type(error).__name__}: {error}"},
                error=f"{type(error).__name__}: {error}"[:2000],
            )
        except Exception:  # noqa: BLE001 - we are already failing
            logger.debug("evaluation: could not record failure", exc_info=True)

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None


class EvaluationLease:
    """The evaluation run's exclusion, or a no-op when the table is absent.

    Degrading to a no-op is deliberate and narrow: a single-process region
    running one CLI command has nothing to exclude, and failing the run because
    a lease table could not be written would make ``pheasant eval run`` depend
    on a fleet feature it does not need. In a fleet the table is there and the
    lease is real.
    """

    def __init__(
        self,
        state: Any,
        *,
        owner: str | None = None,
        stale_after_seconds: float | None = None,
    ):
        self._lease: Any = None
        self._progress: _RunProgress | None = None
        try:
            from pheasant.sync.locks import SourceLease

            # The lease is aged out on the *evaluation* window, by
            # `reclaim_interrupted_runs`, so it has to beat on the evaluation
            # window too. Left on `SOURCE_HEARTBEAT_SECONDS` (10s) it was the
            # slower of the two clocks under any configured window below 30s,
            # which made a live batch's own lease look abandoned -- the second
            # half of the same defect `heartbeat_interval_for` documents.
            kwargs: dict[str, Any] = {"owner": owner}
            if stale_after_seconds is not None:
                beat = evaluation_store.heartbeat_interval_for(stale_after_seconds)
                kwargs["heartbeat_interval_s"] = beat
                kwargs["stale_after_s"] = max(
                    float(stale_after_seconds), evaluation_store.MINIMUM_RUN_STALE_SECONDS
                )
            self._lease = SourceLease(state, EVALUATION_LEASE, **kwargs)
        except Exception:  # noqa: BLE001 - leases are optional infrastructure
            logger.debug("evaluation: lease unavailable; running unguarded", exc_info=True)

    def __enter__(self) -> bool:
        if self._lease is None:
            return True
        try:
            return bool(self._lease.try_acquire())
        except Exception:  # noqa: BLE001
            # Fail open -- a measurement must not be blocked by its own
            # exclusion -- but say so at warning level. Running unguarded means
            # a fleet can produce N runs where it should produce one, and that
            # is a degraded guarantee rather than a normal condition. (The
            # constructor's own failure stays at debug: no lease table is the
            # ordinary single-process case, not an anomaly.)
            logger.warning(
                "evaluation: could not claim the %s lease; running unguarded, so "
                "concurrent replicas may each produce a run",
                EVALUATION_LEASE,
                exc_info=True,
            )
            return True

    def watch(self, progress: _RunProgress) -> None:
        """Hand the lease the run's progress, so leaving marks the attempt over.

        The lease's exit *is* "this attempt has ended", which is the one moment
        the run row must stop saying ``running`` -- whether the batch returned,
        raised, or the process is unwinding. Putting it here rather than in a
        ``try`` around the body keeps one place responsible for the end of an
        attempt, and there is no second place to forget.
        """

        self._progress = progress

    def __exit__(self, exc_type: Any, exc: BaseException | None, _tb: Any) -> None:
        progress = getattr(self, "_progress", None)
        if progress is not None:
            if exc is not None:
                progress.fail(exc)
            progress.close()
        if self._lease is None:
            return
        try:
            self._lease.release()
        except Exception:  # noqa: BLE001
            logger.debug("evaluation: lease release failed", exc_info=True)


def reclaim_interrupted_runs(
    state: Any, kb_id: str, *, stale_after_seconds: float | None = None
) -> list[str]:
    """Close out batches whose process stopped without finishing them.

    Called at startup and on the scheduler beat. This is the whole answer to
    "the container was turned off": a run row that says ``running`` with a dead
    heartbeat becomes ``interrupted``, with a reason, so a watcher in any
    process stops showing a spinner for work nobody is doing -- and so the next
    attempt can *resume* it, since its replay checkpoints are still there.

    Deliberately not destructive. The checkpoints survive, the metric rows
    survive, and re-running the same batch re-derives the same content-addressed
    run id and picks up where it left off.

    The window is floored at :data:`~pheasant.evaluation.store.MINIMUM_RUN_STALE_SECONDS`.
    Below that no beat can keep the three-beat margin, so reclamation would be
    declaring *live* batches dead -- which is not recovery, it is the opposite:
    it frees the ``__evaluation__`` lease under a running batch and admits a
    concurrent duplicate.
    """

    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    window = (
        float(stale_after_seconds)
        if stale_after_seconds is not None
        else evaluation_store.RUN_STALE_SECONDS
    )
    window = max(window, evaluation_store.MINIMUM_RUN_STALE_SECONDS)
    stale_before = (
        (now - timedelta(seconds=window)).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    try:
        reclaimed = evaluation_store.reclaim_stale_runs(
            state, kb_id, now=utc_now(), stale_before=stale_before
        )
    except Exception:  # noqa: BLE001 - recovery must not break a boot
        logger.debug("evaluation: could not reclaim stale runs", exc_info=True)
        return []
    if reclaimed:
        # The run row and the lease are two records of one dead process, and
        # until this they aged out on two different clocks: the run on
        # `evaluation.run_stale_seconds`, the lease on `SOURCE_STALE_SECONDS`.
        # In the gap between them a region reported a batch `interrupted` --
        # which invites a resume, and the UI says so -- and then skipped the
        # resume, because a lease nobody was holding said someone was. A
        # skipped run is not a loud failure either: it has no gates, so it
        # reported none failing. Freeing the lease *here* is what makes the
        # invitation true, and it uses this function's own evidence: a
        # heartbeat-expired run is a process that is gone.
        try:
            from pheasant.sync.locks import release_stale_lease

            release_stale_lease(state, EVALUATION_LEASE, stale_before=stale_before)
        except Exception:  # noqa: BLE001 - leases are optional infrastructure
            logger.debug("evaluation: could not free the evaluation lease", exc_info=True)
    return reclaimed


def _evaluation_digests(settings: Any, policy: proof_projection.ProofPolicy) -> dict[str, Any]:
    """The manifest's ``evaluation`` section.

    Present so a metric that moved because the *rules* changed is
    distinguishable from one that moved because the region did -- which is the
    difference between a finding and an artifact of a config edit.
    """

    return {
        "metric_registry_digest": digest(sorted(m for m, _ in report_projection.HEALTH_VECTOR)),
        "proof_policy_digest": policy.policy_digest,
        "cohort_policy_digest": digest(
            getattr(settings.cohorts, "anchor_minimum_queries", None),
            getattr(settings.cohorts, "rolling_lookback_days", None),
            getattr(settings.cohorts, "holdout_minimum_separation_days", None),
            getattr(settings.cohorts, "maximum_queries_per_cohort", None),
        ),
        "schema_version": EVALUATION_SCHEMA_VERSION,
    }


def resolve_cohorts(state: Any, kb_id: str, settings: Any) -> dict[str, Cohort]:
    """Materialize the cohorts this run will use, reusing the frozen anchor.

    The anchor is looked up by name first and rebuilt only when there is none.
    Rebuilding it every run would make it a rolling cohort with an anchor's
    label, and every trend it produced would mix "the region changed" with
    "the questions changed" -- the exact confound it exists to remove.
    """

    out: dict[str, Cohort] = {}
    cohort_settings = settings.cohorts
    cap = int(getattr(cohort_settings, "maximum_queries_per_cohort", 200))

    if cohort_settings.anchor:
        existing = evaluation_store.find_cohort(state, kb_id, CohortPurpose.ANCHOR.value, "anchor")
        anchor = cohort_builder.build_anchor(
            state,
            kb_id,
            minimum_queries=int(cohort_settings.anchor_minimum_queries),
            maximum_queries=cap,
            existing=existing,
        )
        if anchor is not None:
            out["anchor"] = anchor
    if cohort_settings.rolling:
        out["rolling"] = cohort_builder.build_rolling(
            state,
            kb_id,
            lookback_days=int(cohort_settings.rolling_lookback_days),
            maximum_queries=cap,
        )
    if cohort_settings.learned:
        out["learned"] = cohort_builder.build_learned(state, kb_id, maximum_queries=cap)
    if cohort_settings.temporal_holdout:
        out["temporal_holdout"] = cohort_builder.build_temporal_holdout(
            state,
            kb_id,
            minimum_separation_days=float(cohort_settings.holdout_minimum_separation_days),
            maximum_queries=cap,
        )
    if cohort_settings.control:
        out["control"] = cohort_builder.build_control(state, kb_id, maximum_queries=cap)
    if cohort_settings.synthetic_invariants:
        out["invariants"] = cohort_builder.build_synthetic_invariants(state, kb_id)
    return out


def collect_proof(
    state: Any,
    kb_id: str,
    policy: proof_projection.ProofPolicy,
    *,
    before: str | None,
    query_ids: list[str],
) -> list[Any]:
    """Recorded proof plus what the ledger and the candidate trail imply.

    Three sources, in increasing order of what they license: explicitly
    recorded proof (somebody said so), ledger exposure (the region served it),
    and admitted candidates (somebody approved the record it produced). All
    three are capped at ``before`` when the run is a historical reconstruction,
    which is where the leakage rule lives -- filtering here means every metric
    inherits it rather than each one having to remember.
    """

    recorded = evaluation_store.load_proofs(state, kb_id, query_ids=query_ids, before=before)
    derived = proof_projection.project_from_interactions(state, kb_id, policy)
    admitted = proof_projection.project_from_admitted_candidates(state, kb_id, policy)
    wanted = set(query_ids)
    extra = [
        p
        for p in (*derived, *admitted)
        if p.query_id in wanted and (not before or not p.observed_at or p.observed_at <= before)
    ]
    seen = {p.proof_id for p in recorded}
    return [*recorded, *[p for p in extra if p.proof_id not in seen]]


def _variant_metrics(
    ctx: MetricContext, replay: VariantReplay, settings: Any, state: Any
) -> MetricSet:
    """Every within-variant metric, for one variant on one cohort."""

    out = MetricSet()
    for k in (5, 10):
        out.aggregates.append(metric_functions.result_evidence_coverage(ctx, replay, k))
        out.extend(metric_functions.known_positive_recall(ctx, replay, k))
        out.aggregates.append(metric_functions.known_positive_hit(ctx, replay, k))
        out.aggregates.append(metric_functions.negative_exposure(ctx, replay, k))
    out.extend(metric_functions.known_positive_reciprocal_rank(ctx, replay))
    out.aggregates.append(metric_functions.pairwise_proof_accuracy(ctx, replay))
    out.aggregates.append(metric_functions.latency(ctx, replay))
    if getattr(settings, "retrieval_diagnostics", False):
        for k in (5, 10):
            out.aggregates.append(metric_functions.evidence_discounted_gain(ctx, replay, k))
            out.aggregates.append(metric_functions.arm_contribution(ctx, replay, k))
    if getattr(settings, "binary_preference", False):
        out.aggregates.append(metric_functions.binary_preference(ctx, replay))
    _ = state
    return out


def _attribution_metrics(
    ctx: MetricContext, replays: dict[str, VariantReplay], settings: Any
) -> MetricSet:
    """Paired treatment-minus-baseline metrics across the matrix.

    Every one of these subtracts a variant from the baseline it *declares*, so
    a pair that differs in more than the intervention cannot be produced by
    accident -- the pairing comes off ``Variant.baseline_variant_id`` rather
    than from the order the loop happens to visit variants in.
    """

    out = MetricSet()
    baseline = replays.get(variant_matrix.CORPUS_BASELINE)
    if baseline is None:
        return out
    kprr = metric_functions.kprr_scorer(ctx)

    for variant_id, replay in sorted(replays.items()):
        parent_id = replay.variant.baseline_variant_id
        if not parent_id or parent_id not in replays:
            continue
        parent = replays[parent_id]
        if variant_id in ("B1", "B5", "B6"):
            out.aggregates.append(
                metric_functions.paired_delta(
                    ctx,
                    "memory_attributable_gain",
                    parent,
                    replay,
                    kprr,
                    label="memory-attributable gain in known-positive reciprocal rank",
                    limitation=(
                        "measured on queries with positive proof only; the metric it is a gain "
                        "*in* is named in the formula and it is not a measure of correctness"
                    ),
                )
            )
            out.aggregates.extend(metric_functions.displacement(ctx, parent, replay, 5))
        elif variant_id in ("B2", "B3", "B4"):
            out.aggregates.append(
                metric_functions.paired_delta(
                    ctx,
                    "steering_lift",
                    parent,
                    replay,
                    kprr,
                    label=f"{replay.variant.label} lift in known-positive reciprocal rank",
                    limitation=(
                        "one steering kind in isolation; kinds can interact, and the sum of the "
                        "three lifts is not the full-memory gain"
                    ),
                )
            )
    _ = settings
    return out


def run_evaluation(
    engine: Any,
    *,
    mode: str = "current_state",
    effective_as_of: str | None = None,
    force: bool = False,
    on_progress: Any = None,
) -> RunOutcome:
    """One complete batch. Read-only unless promotion is explicitly enabled.

    ``mode`` is ``current_state`` ("how does the region handle that historical
    question now") or ``historical`` ("what could it have known at
    ``effective_as_of``"). Both are supported, and the report always names
    which ran -- a longitudinal chart that silently mixed them would be
    comparing two different questions.
    """

    config = engine.config
    state = engine.state
    settings = config.evaluation
    kb_id = config.knowledge_base_id
    started_at = utc_now()
    started_clock = time.monotonic()

    # Set once the run row exists; before that there is nothing durable to
    # write progress to, and the in-process callback is all a caller has.
    stale_window = getattr(settings, "run_stale_seconds", None)
    progress = _RunProgress(
        state, heartbeat_seconds=evaluation_store.heartbeat_interval_for(stale_window)
    )

    def report(phase: str, detail: str = "", *, units: int | None = None) -> None:
        """Publish one phase transition to every watcher there is.

        Two destinations, and both matter. The callback is the in-process one
        (the CLI's printed line, the HTTP job registry). The row in
        ``evaluation_runs`` is the durable one -- it is what a *different*
        process reads, and what survives this one being killed. A batch is
        minutes of work; a progress signal that lives only in the process
        running it is a progress signal the UI cannot show after a restart.
        """

        progress.publish(phase, detail, units)
        if on_progress is not None:
            try:
                on_progress(phase, detail)
            except Exception:  # noqa: BLE001 - progress must never fail a run
                logger.debug("evaluation: progress hook failed", exc_info=True)

    if not settings.enabled and not force:
        return RunOutcome(
            run_id="",
            snapshot_id="",
            status="skipped",
            skipped_reason="evaluation.enabled is false; pass force=True to run anyway",
        )

    lease = EvaluationLease(state, stale_after_seconds=stale_window)
    with lease as acquired:
        if not acquired:
            return RunOutcome(
                run_id="",
                snapshot_id="",
                status="skipped",
                skipped_reason="another process holds the evaluation lease",
            )

        # 1-2. Resolve evaluation time and build the snapshot manifest.
        report("snapshot")
        policy = proof_projection.ProofPolicy.from_config(settings.proof)
        as_of = effective_as_of if mode == "historical" else None
        manifest = snapshot_builder.build_snapshot(
            state,
            config,
            graph=getattr(engine.graph_builder, "graph", None),
            effective_as_of=as_of,
            evaluation_digests=_evaluation_digests(settings, policy),
        )
        previous = evaluation_store.previous_snapshot(
            state, kb_id, before=manifest.created_at, exclude=manifest.snapshot_id
        )
        evaluation_store.save_snapshot(state, manifest)

        snapshot_gate = gate_checks.evaluate_snapshot(
            manifest, blocking=bool(settings.gates.incomplete_snapshot_blocks_run)
        )
        config_digest = digest(
            policy.policy_digest,
            settings.max_results,
            settings.mode,
            sorted(settings.composite_weights.items()),
        )
        # Deterministic, and with no clock in it -- the same argument the
        # snapshot id makes. A run *is* its (state, configuration, mode,
        # instant-described) tuple: two runs over an unchanged region with an
        # unchanged configuration produce the same numbers, so they are one run
        # and one trend point rather than two identical ones. `mode` and
        # `effective_as_of` are in the digest because a historical
        # reconstruction reads the same state under a different proof cutoff,
        # which is genuinely a different evaluation.
        #
        # Without this the id was seeded from the wall clock, so two runs a
        # second apart were two rows and two runs *within* a second silently
        # collapsed into one -- the worst of both, and caught by running the
        # batch twice against a real Postgres.
        run_id = "run-" + digest(
            kb_id,
            manifest.snapshot_id,
            config_digest,
            mode,
            manifest.effective_as_of if mode == "historical" else None,
        )
        claim = evaluation_store.open_run(
            state,
            run_id=run_id,
            kb_id=kb_id,
            snapshot_id=manifest.snapshot_id,
            started_at=started_at,
            mode=mode,
            config_digest=config_digest,
            owner=_owner(),
        )
        if not claim["claimed"]:
            # This exact batch -- same state, same configuration, same mode and
            # instant -- already reached an answer and published it. Replaying
            # it can only re-derive the report it already holds, so the honest
            # thing is to say so and hand back the run that has it, rather than
            # spend a full cohort-by-variant matrix overwriting a document with
            # itself. In a fleet on an hourly beat over a region that has not
            # changed, that is the difference between doing the work every hour
            # and doing it when something moved.
            #
            # It is also the only way the run row stays truthful: this batch
            # has no row of its own to write progress to, and writing progress
            # to the *finished* run's row is what made a live batch read as
            # `completed` to every watcher. See `open_run`.
            #
            # The stored report comes back with it rather than an empty dict.
            # A caller asking for this batch wants its numbers, and they exist
            # -- re-deriving them is what is redundant, not having them. So
            # `pheasant eval run` still prints a summary, the HTTP job still
            # reports gates, and "two runs over one state agree" stays a
            # property a caller can actually check.
            settled = evaluation_store.load_report(state, run_id) or {}
            return RunOutcome(
                run_id=run_id,
                snapshot_id=manifest.snapshot_id,
                status="skipped",
                report=settled,
                gates=_stored_gates(settled),
                skipped_reason=(
                    f"this batch already {claim['previous_status']}; its report is unchanged "
                    "because nothing the run id covers has moved"
                ),
                attempts=int(claim["attempts"]),
            )
        progress.bind(run_id)
        lease.watch(progress)
        checkpoints = (
            evaluation_store.load_replay_checkpoints(state, run_id) if claim["resumed"] else {}
        )
        if claim["resumed"]:
            logger.info(
                "evaluation: resuming %s (attempt %s, was %s) with %s replay checkpoint(s)",
                run_id,
                claim["attempts"],
                claim["previous_status"],
                len(checkpoints),
            )
            report(
                "resuming",
                f"attempt {claim['attempts']}; {len(checkpoints)} replay(s) already done",
            )
        if not snapshot_gate.passed:
            outcome = RunOutcome(
                run_id=run_id,
                snapshot_id=manifest.snapshot_id,
                status="invalid",
                gates=[snapshot_gate],
                skipped_reason=snapshot_gate.detail,
            )
            evaluation_store.close_run(
                state,
                run_id=run_id,
                finished_at=utc_now(),
                status="invalid",
                gates_passed=False,
                report={"run_id": run_id, "gates": [snapshot_gate.as_dict()]},
            )
            # `invalid` is settled, so no later attempt will pick this run id up
            # again -- which means any checkpoints an *earlier* attempt left are
            # now unreachable. Dropping them here is the same argument the
            # completed path makes: the scaffolding outlives its reader
            # otherwise, and it is a copy of every result list.
            evaluation_store.clear_replay_checkpoints(state, run_id)
            return outcome

        # 3. Resolve cohorts.
        report("cohorts")
        cohorts = resolve_cohorts(state, kb_id, settings)
        for cohort in cohorts.values():
            evaluation_store.save_cohort(state, cohort)

        # 4. Resolve proof, under the leakage cap when reconstructing history.
        report("proof")
        all_query_ids = sorted({qid for c in cohorts.values() for qid in c.query_ids})
        before = manifest.effective_as_of if mode == "historical" else None
        proofs = collect_proof(state, kb_id, policy, before=before, query_ids=all_query_ids)
        evidence = proof_projection.aggregate(proofs, policy)

        # 5. Generate variants (candidates first: B6 only exists if any).
        report("variants")
        pending = candidate_validation.load_candidates(state)
        shadow_candidate_ids = candidate_validation.shadow_ids(pending)
        matrix = variant_matrix.selected_matrix(
            settings.variants, candidate_ids=shadow_candidate_ids
        )

        # 6. Replay.
        report("replay")
        searcher = _replay_searcher(engine)
        replay_engine = ReplayEngine(
            searcher,
            kb_id,
            graph=getattr(engine.graph_builder, "graph", None),
            security=config.security,
            max_results=int(settings.max_results),
            mode=str(settings.mode),
            shadow=pending,
        )
        budget = int(settings.maximum_queries_per_run)
        deadline = float(settings.maximum_runtime_seconds)
        replays: dict[str, dict[str, VariantReplay]] = {}
        truncated: dict[str, int] = {}
        spent = 0
        resumed_pairs = 0
        # One unit is one (cohort, variant) replay. Counting *pairs* rather than
        # queries is what makes the progress bar honest across cohorts of very
        # different sizes: a bar that jumped 40% for the invariant cohort and
        # crawled through the anchor would be measuring the wrong thing.
        planned = [
            (cohort_name, cohort, variant)
            for cohort_name, cohort in cohorts.items()
            for variant in matrix
        ]
        progress.plan(len(planned))
        by_cohort: dict[str, dict[str, VariantReplay]] = {name: {} for name in cohorts}
        for cohort_name, cohort, variant in planned:
            key = (cohort_name, variant.variant_id)
            checkpoint = checkpoints.get(key)
            if checkpoint is not None:
                # Already replayed, by this run, before it was interrupted.
                # Reusing it is what makes a restart cheap instead of a
                # restart-from-zero -- and it is *sound* because the run id is
                # content-addressed: the same run id means the same snapshot,
                # the same configuration and the same cohorts.
                by_cohort[cohort_name][variant.variant_id] = VariantReplay.from_dict(
                    variant, checkpoint
                )
                resumed_pairs += 1
                progress.advance()
                report("replay", f"{cohort_name}/{variant.variant_id} (checkpointed)")
                continue
            if spent >= budget or (time.monotonic() - started_clock) > deadline:
                # Recorded, and deliberately *not* counted as a completed unit.
                # Advancing here made a truncated run finish at 42/42 -- a
                # progress bar reporting every replay done for a batch that
                # skipped most of them, which is the mirror of the
                # smaller-denominator sin this module refuses elsewhere. A
                # truncated run stops short and the counter says where.
                truncated[f"{cohort_name}:{variant.variant_id}"] = cohort.query_count
                continue
            report("replay", f"{cohort_name}/{variant.variant_id}")
            pair_started = time.monotonic()
            replayed = replay_engine.replay_variant(cohort, variant)
            by_cohort[cohort_name][variant.variant_id] = replayed
            spent += cohort.query_count
            progress.advance()
            # Checkpointed the moment it finishes, not at the end of the phase:
            # a container stopped between two replays must keep the one that
            # completed. Writing a batch of them at the end would lose exactly
            # the work a restart is trying to avoid redoing.
            evaluation_store.save_replay_checkpoint(
                state,
                run_id=run_id,
                kb_id=kb_id,
                cohort_id=cohort.cohort_id,
                cohort_name=cohort_name,
                variant_id=variant.variant_id,
                completed_at=utc_now(),
                query_count=cohort.query_count,
                failure_count=len(replayed.failures),
                duration_ms=(time.monotonic() - pair_started) * 1000.0,
                payload=replayed.as_dict(),
            )
        replays = by_cohort

        # 7-9. Metrics, then paired deltas.
        report("metrics")
        results: list[MetricResult] = []
        per_query: list[MetricResult] = []
        cohort_results: dict[str, MetricSet] = {}
        for cohort_name, cohort in cohorts.items():
            if cohort_name == "invariants" or not replays.get(cohort_name):
                continue
            ctx = MetricContext(
                snapshot_id=manifest.snapshot_id,
                cohort=cohort,
                policy=policy,
                evidence=evidence,
                max_per_query_results=int(settings.maximum_stored_per_query_results),
            )
            collected = MetricSet()
            if cohort_name == "anchor":
                collected.aggregates.append(metric_functions.query_evidence_coverage(ctx))
                collected.aggregates.append(metric_functions.proof_conflict_rate(ctx))
                collected.aggregates.extend(metric_functions.index_completeness(ctx, state))
                collected.aggregates.append(metric_functions.growth(ctx, state))
            for variant_id, replay in replays[cohort_name].items():
                collected.extend(_variant_metrics(ctx, replay, settings, state))
                _ = variant_id
            collected.extend(_attribution_metrics(ctx, replays[cohort_name], settings))
            cohort_results[cohort_name] = collected
            results.extend(collected.aggregates)
            per_query.extend(collected.per_query)

        # Generalization: learned versus holdout, reported apart and compared.
        learned_gain = _gain(cohort_results.get("learned"), PRIMARY_VARIANT)
        holdout_gain = _gain(cohort_results.get("temporal_holdout"), PRIMARY_VARIANT)
        if learned_gain is not None:
            learned_gain = _relabel(learned_gain, "learned_query_gain")
            results.append(learned_gain)
        if holdout_gain is not None:
            holdout_gain = _relabel(holdout_gain, "future_query_generalization")
            results.append(holdout_gain)
        if learned_gain is not None and holdout_gain is not None:
            results.append(metric_functions.generalization_gap(learned_gain, holdout_gain))

        # Control regression, on the control cohort only.
        #
        # Paired against **B1**, not the corpus baseline. The cohort is defined
        # by "no steering rule can fire here", so the treatment it controls has
        # to be steering alone -- and B1 (memory content, no steering) against
        # B5 (memory content plus every steering kind) differ in exactly that.
        # Pairing it against B0 instead compares steering *and* memory content,
        # so a record legitimately answering a control query read as an
        # unintended regression: the treatment doing its job, counted as harm.
        #
        # Falls back to the corpus baseline only when B1 was not run, and that
        # is a weaker comparison rather than an equivalent one.
        control_metric = None
        control_replays = replays.get("control") or {}
        steering_baseline = "B1" if "B1" in control_replays else variant_matrix.CORPUS_BASELINE
        if steering_baseline in control_replays and PRIMARY_VARIANT in control_replays:
            control_ctx = MetricContext(
                snapshot_id=manifest.snapshot_id,
                cohort=cohorts["control"],
                policy=policy,
                evidence=evidence,
                max_per_query_results=int(settings.maximum_stored_per_query_results),
            )
            control_metric = metric_functions.control_regression(
                control_ctx,
                control_replays[steering_baseline],
                control_replays[PRIMARY_VARIANT],
                tolerance=float(settings.gates.control_regression_tolerance),
            )
            results.append(control_metric)

        # 10. Gates, before aggregation.
        report("gates")
        run_gates: list[GateResult] = [snapshot_gate]
        invariant_replays = replays.get("invariants") or {}
        if PRIMARY_VARIANT in invariant_replays:
            run_gates.extend(
                gate_checks.evaluate_invariants(
                    cohorts["invariants"],
                    invariant_replays[PRIMARY_VARIANT],
                    acl_enforced=bool(getattr(config.security, "acl_enforced", False)),
                )
            )
        anchor_replays = replays.get("anchor") or {}
        if variant_matrix.CORPUS_BASELINE in anchor_replays and PRIMARY_VARIANT in anchor_replays:
            anchor_ctx = MetricContext(
                snapshot_id=manifest.snapshot_id,
                cohort=cohorts["anchor"],
                policy=policy,
                evidence=evidence,
            )
            run_gates.append(
                gate_checks.evaluate_known_positive_exclusion(
                    anchor_ctx,
                    anchor_replays[variant_matrix.CORPUS_BASELINE],
                    anchor_replays[PRIMARY_VARIANT],
                )
            )
        negative_gate = gate_checks.evaluate_negative_exposure_increase(
            _named(cohort_results.get("anchor"), "negative_exposure_at_5", "B0"),
            _named(cohort_results.get("anchor"), "negative_exposure_at_5", PRIMARY_VARIANT),
            tolerance=float(settings.gates.negative_exposure_increase_tolerance),
        )
        run_gates.append(negative_gate)
        if control_metric is not None:
            run_gates.append(
                gate_checks.evaluate_control_regression(
                    control_metric, tolerance=float(settings.gates.control_regression_tolerance)
                )
            )

        # 11-13. Explanations, persistence, candidate decisions.
        report("report")
        sufficiency = proof_projection.assess_sufficiency(
            policy,
            eligible_query_ids=list(cohorts["anchor"].query_ids) if "anchor" in cohorts else [],
            evidence=evidence,
            proofs=proofs,
        )
        decisions = _validate_candidates(
            pending,
            settings=settings,
            gates=run_gates,
            holdout_gain=holdout_gain,
            learned_gain=learned_gain,
            control_metric=control_metric,
            negative_gate=negative_gate,
            holdout_cohort=cohorts.get("temporal_holdout"),
        )
        applied = candidate_validation.apply_decisions(
            engine,
            decisions,
            enabled=bool(settings.promotion.enabled) and gate_checks.all_passed(run_gates),
            admit=_admit_hook(),
        )

        published = [result for result in results if not result.validate()]
        rejected = [
            {"metric_id": result.metric_id, "problems": result.validate()}
            for result in results
            if result.validate()
        ]
        for result in rejected:
            # A metric that cannot state its denominator, formula or limitation
            # is dropped rather than published with them missing. Logged so the
            # omission is a bug report and not a silent gap.
            logger.warning("evaluation: refusing to publish %s: %s", result["metric_id"], result)

        payload = _build_report(
            run_id=run_id,
            manifest=manifest,
            previous=previous,
            mode=mode,
            cohorts=cohorts,
            replays=replays,
            results=published,
            per_query=per_query,
            gates=run_gates,
            sufficiency=sufficiency,
            decisions=applied,
            settings=settings,
            policy=policy,
            truncated=truncated,
            rejected_metrics=rejected,
            runtime={
                "seconds": round(time.monotonic() - started_clock, 3),
                "queries_replayed": spent,
                "variants": [variant.variant_id for variant in matrix],
            },
        )
        report("persisting")
        evaluation_store.save_metrics(state, run_id, kb_id, [*published, *per_query])
        finished = utc_now()
        status = "completed" if not truncated else "truncated"
        payload["run_identity"]["attempts"] = claim["attempts"]
        payload["run_identity"]["resumed_replays"] = resumed_pairs
        evaluation_store.close_run(
            state,
            run_id=run_id,
            finished_at=finished,
            status=status,
            gates_passed=gate_checks.all_passed(run_gates),
            report=payload,
        )
        # Only once the report is committed. The checkpoints are the recovery
        # path for a run that did not get this far; dropping them earlier would
        # mean a crash during persistence had to replay everything again.
        evaluation_store.clear_replay_checkpoints(state, run_id)
        # Notified, not published: `close_run` has already written the terminal
        # phase (which is the *status*, and is the truthful one), and a
        # "done" phase written after it would overwrite "completed" with a word
        # that says less. The lease's exit stops the heartbeat.
        if on_progress is not None:
            try:
                on_progress("done", status)
            except Exception:  # noqa: BLE001 - progress must never fail a run
                logger.debug("evaluation: progress hook failed", exc_info=True)
        return RunOutcome(
            run_id=run_id,
            snapshot_id=manifest.snapshot_id,
            status=status,
            report=payload,
            gates=run_gates,
            attempts=int(claim["attempts"]),
            resumed_replays=resumed_pairs,
        )


def _stored_gates(report: dict[str, Any]) -> list[GateResult]:
    """The gates a settled run published, for a batch that declined to redo it.

    ``RunOutcome.gates_passed`` is what ``pheasant eval run`` turns into an exit
    status, and a run skipped because it had *already* answered should report
    the answer it gave rather than an empty list -- which
    :attr:`RunOutcome.gates_passed`, correctly, reads as "no gates, so no pass".
    A caller that scripts on this gets the region's real gate outcome instead of
    a false negative every time nothing has changed.
    """

    out: list[GateResult] = []
    for gate in report.get("gates") or []:
        if not isinstance(gate, dict):
            continue
        try:
            out.append(
                GateResult(
                    gate_id=str(gate.get("gate_id") or ""),
                    passed=bool(gate.get("passed")),
                    observed=float(gate.get("observed") or 0.0),
                    maximum=float(gate.get("maximum") or 0.0),
                    detail=str(gate.get("detail") or ""),
                    evidence=dict(gate.get("evidence") or {}),
                )
            )
        except (TypeError, ValueError):  # a stored report must not fail a skip
            logger.debug("evaluation: unreadable stored gate %r", gate, exc_info=True)
    return out


def _replay_searcher(engine: Any, ranking: Any = None) -> Any:
    """A searcher configured for measurement rather than for serving.

    ``usage_tracking=False`` is the important argument. It is a write on the
    read path that credits a memory record for being *served*, and letting the
    evaluation plane trigger it would let a region raise the salience of the
    very records it is measuring -- the tightest self-rewarding loop available
    here, and one that would look like the memory system improving.

    ``steering_enabled=True`` because the variant, not the region's live
    config, decides which rules fire: measuring what alias steering *would* do
    is a legitimate question in a region that has it turned off.
    """

    from pheasant.search.hybrid import HybridSearch
    from pheasant.search.ranking import resolver_for
    from pheasant.search.sqlite_store import SearchStore

    config = engine.config
    return HybridSearch(
        SearchStore(engine.state, ranking=ranking or resolver_for(config, engine.state)),
        vector=engine.vector_searcher(),
        node_index=getattr(engine, "node_index", None),
        wasm_relationship_search=config.search.wasm_relationship_search,
        steering_enabled=True,
        default_memory_policy=config.memory.default_policy,
        usage_tracking=False,
    )


def _admit_hook() -> Any:
    try:
        from pheasant.memory.formation import admit

        return admit
    except Exception:  # noqa: BLE001 - formation is optional
        return None


def _gain(metrics: MetricSet | None, variant_id: str) -> MetricResult | None:
    if metrics is None:
        return None
    return metrics.by_id("memory_attributable_gain", variant_id)


def _named(metrics: MetricSet | None, metric_id: str, variant_id: str) -> Any:
    if metrics is None:
        return None
    return metrics.by_id(metric_id, variant_id)


def _relabel(result: MetricResult, metric_id: str) -> MetricResult:
    """Rename a computed gain for the cohort it was computed on.

    The learned and holdout numbers are the *same* calculation over different
    query sets, and giving them one name would be the exact conflation the
    cohort split exists to prevent. Renaming here rather than recomputing keeps
    them provably identical in method and explicitly different in scope.
    """

    from dataclasses import replace

    limitation = (
        "recall of learned experience: these queries produced the evidence the intervention "
        "was derived from, so this is not evidence of generalization"
        if metric_id == "learned_query_gain"
        else "measured on later queries that contributed no evidence to the intervention"
    )
    return replace(result, metric_id=metric_id, does_not_support=limitation)


def _validate_candidates(
    pending: list[dict[str, Any]],
    *,
    settings: Any,
    gates: list[GateResult],
    holdout_gain: MetricResult | None,
    learned_gain: MetricResult | None,
    control_metric: MetricResult | None,
    negative_gate: GateResult | None,
    holdout_cohort: Cohort | None,
) -> list[candidate_validation.CandidateDecision]:
    replayable = set(candidate_validation.shadow_ids(pending))
    independent = set(holdout_cohort.query_ids) if holdout_cohort else set()
    return [
        candidate_validation.validate(
            candidate,
            settings=settings.promotion,
            gates=gates,
            holdout_gain=holdout_gain,
            learned_gain=learned_gain,
            control_regression=control_metric,
            negative_exposure_gate=negative_gate,
            shadow_replayable=str(candidate.get("id") or "") in replayable,
            independent_query_ids=independent,
        )
        for candidate in pending
    ]


def _build_report(
    *,
    run_id: str,
    manifest: Any,
    previous: Any,
    mode: str,
    cohorts: dict[str, Cohort],
    replays: dict[str, dict[str, VariantReplay]],
    results: list[MetricResult],
    per_query: list[MetricResult],
    gates: list[GateResult],
    sufficiency: Any,
    decisions: list[dict[str, Any]],
    settings: Any,
    policy: Any,
    truncated: dict[str, int],
    rejected_metrics: list[dict[str, Any]],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    """The thirteen required report sections, in the specification's order."""

    failures = {
        f"{cohort_name}:{variant_id}": dict(replay.failures)
        for cohort_name, per_variant in replays.items()
        for variant_id, replay in per_variant.items()
        if replay.failures
    }
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "run_identity": {
            "run_id": run_id,
            "snapshot_id": manifest.snapshot_id,
            "mode": mode,
            "effective_as_of": manifest.effective_as_of,
            "proof_policy_digest": policy.policy_digest,
            "primary_variant": PRIMARY_VARIANT,
            "baseline_variant": variant_matrix.CORPUS_BASELINE,
        },
        "snapshot_integrity": {
            "complete": manifest.complete,
            "incomplete_sections": list(manifest.incomplete),
            "manifest": manifest.as_dict(),
        },
        "evidence_coverage": sufficiency.as_dict(),
        # Scoped to the anchor cohort: the headline is about the frozen
        # question set, not about whichever cohort the metric list happens to
        # hold first. Falls back to the whole list when there is no anchor yet.
        "health_vector": report_projection.health_vector(
            results,
            primary_variant=PRIMARY_VARIANT,
            cohort_id=getattr(cohorts.get("anchor"), "cohort_id", None),
        ),
        "classification_breakdown": report_projection.classification_breakdown(results),
        "baseline_comparison": [
            result.as_dict()
            for result in results
            if result.metric_id
            in {
                "memory_attributable_gain",
                "steering_lift",
                "displacement_at_5",
                "positive_displacement_at_5",
            }
        ],
        "memory_attribution": [
            result.as_dict() for result in results if result.metric_id == "steering_lift"
        ],
        "generalization": {
            "learned": next(
                (r.as_dict() for r in results if r.metric_id == "learned_query_gain"), None
            ),
            "temporal_holdout": next(
                (r.as_dict() for r in results if r.metric_id == "future_query_generalization"), None
            ),
            "gap": next(
                (r.as_dict() for r in results if r.metric_id == "generalization_gap"), None
            ),
            "note": (
                "learned-cohort performance is recall of the experience that created the "
                "intervention and is never reported as generalization"
            ),
        },
        "controls_and_regressions": next(
            (r.as_dict() for r in results if r.metric_id == "control_regression_rate"), None
        ),
        "gates": [gate.as_dict() for gate in gates],
        "optional_diagnostics": {
            "retrieval_diagnostics_enabled": bool(settings.retrieval_diagnostics),
            "binary_preference_enabled": bool(settings.binary_preference),
            "note": ("diagnostics are corpus-relative and are never a factual-accuracy claim"),
        },
        "candidate_decisions": decisions,
        "composite": report_projection.composite(results, dict(settings.composite_weights)),
        "limitations": {
            "unjudged_share": next(
                (
                    1.0 - (r.value or 0.0)
                    for r in results
                    if r.metric_id == "result_evidence_coverage_at_5"
                ),
                None,
            ),
            "failed_queries": failures,
            "truncated_replays": truncated,
            "metrics_withheld": rejected_metrics,
        },
        "longitudinal": {
            "previous_snapshot_id": getattr(previous, "snapshot_id", None),
            "snapshot_diff": manifest.differences(previous)
            if previous is not None
            else ["initial"],
            "material_change": snapshot_builder.material_change(previous, manifest),
        },
        "explanations": {
            "end_user": report_projection.end_user_explanation(
                results,
                gates,
                baseline_variant=variant_matrix.CORPUS_BASELINE,
                treatment_variant=PRIMARY_VARIANT,
                cohort_id=getattr(cohorts.get("anchor"), "cohort_id", None),
            ),
            "agent": report_projection.agent_explanation(
                results,
                gates,
                snapshot_id=manifest.snapshot_id,
                baseline_variant=variant_matrix.CORPUS_BASELINE,
                treatment_variant=PRIMARY_VARIANT,
                sufficiency=sufficiency.as_dict(),
                allowed_actions=_allowed_actions(gates, settings),
            ),
            "developer": report_projection.developer_explanation(
                results,
                per_query,
                snapshot_diff=(
                    manifest.differences(previous) if previous is not None else ["initial"]
                ),
                cohorts={
                    name: {
                        "cohort_id": cohort.cohort_id,
                        "purpose": cohort.purpose,
                        "query_count": cohort.query_count,
                        "frozen": cohort.frozen,
                    }
                    for name, cohort in cohorts.items()
                },
                replay_failures=failures,
                runtime=runtime,
                versions={
                    "schema_version": EVALUATION_SCHEMA_VERSION,
                    "proof_policy_digest": policy.policy_digest,
                    "metric_registry_digest": manifest.evaluation.get("metric_registry_digest"),
                },
            ),
        },
    }


def _allowed_actions(gates: list[GateResult], settings: Any) -> list[str]:
    """What an agent reading this report may do about it.

    Stated because an agent handed a report with no affordances will infer
    some. The list shrinks to inspection alone whenever a gate fails, which is
    the machine-readable half of "hard gates are not averaged away".
    """

    if not gate_checks.all_passed(gates):
        return ["inspect_gate_failures", "read_developer_explanation"]
    actions = ["read_report", "compare_with_previous_snapshot", "inspect_candidate_decisions"]
    if settings.promotion.enabled:
        actions.append("promote_validated_candidates")
    else:
        actions.append("request_operator_review_of_candidates")
    return actions
