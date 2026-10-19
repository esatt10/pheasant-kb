"""One tuning batch: diagnose, propose, trial, decide, package.

The four movements are separate phases with separate durable artifacts rather
than steps inside a function, because each one is independently useful and each
one is something an operator may want to stop after. A region that runs only
the diagnosis and reads "71% of your misses are documents that were never
indexed" has got the most valuable thing this plane produces without changing a
single parameter.

**The batch is resumable, not merely idempotent.** The experiment id is
content-addressed over (region, snapshot, space, cohort, budget), so a restarted
container re-derives the same id, loads the trials already stored, and evaluates
only what is missing. Trials are checkpointed as they complete for the same
reason the evaluation plane checkpoints replays: the expensive part is the
retrieval, so remember what was retrieved.

**It is read-only unless something applies a bundle.** Every phase below writes
to the ``tuning_*`` tables and to cold storage, and nothing else. The region's
ranking changes at exactly one point -- ``apply_bundle`` -- and only when
``tuning.auto.apply`` is on or a person asks.

**It never takes ``sync_lock``.** It takes the ``__tuning__`` lease and yields
to indexing between units. See :mod:`.executor`.

The one structural subtlety worth stating here: the baseline replay is run
**once**, with ``explain=True``, and its captures serve two purposes at once --
they are the diagnosis's raw material *and* the substrate every re-fusion trial
is evaluated against. That is why the cheap path is cheap, and it is why
:func:`_verify_refusion` runs before any cheap trial is trusted: if the captures
do not reproduce what the region served, the whole fusion family falls back to
real searches rather than reporting numbers from a re-implementation that has
drifted.
"""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from pheasant.evaluation import snapshots as snapshot_builder
from pheasant.evaluation import store as evaluation_store
from pheasant.evaluation.contracts import Cohort, digest
from pheasant.evaluation.proof import ProofPolicy
from pheasant.evaluation.proof import aggregate as aggregate_proof
from pheasant.evaluation.replay import ReplayEngine
from pheasant.evaluation.runner import collect_proof, resolve_cohorts
from pheasant.evaluation.variants import default_matrix
from pheasant.search.ranking import RankingParameters
from pheasant.tuning import bundle as bundle_builder
from pheasant.tuning import gates as gate_checks
from pheasant.tuning import refusion, scoring
from pheasant.tuning import store as tuning_store
from pheasant.tuning.contracts import (
    Comparison,
    Decision,
    Diagnosis,
    Experiment,
    ParameterPoint,
    Proposal,
    Trial,
)
from pheasant.tuning.executor import (
    BackpressureGate,
    Cancelled,
    StoodDown,
    TuningExecutor,
    TuningLease,
)
from pheasant.tuning.objective import Objective
from pheasant.tuning.objective import resolve as resolve_objective
from pheasant.tuning.space import ParameterSpace, apply_point, baseline_values, validate_space
from pheasant.tuning.stages import attribute, stage_histogram
from pheasant.tuning.store import PRIMARY_METRIC
from pheasant.tuning.strategy import Budget, propose, select_survivors
from pheasant.tuning.tracking import sink_for

logger = logging.getLogger(__name__)

#: The cohort roles this plane needs, mapped onto the evaluation plane's names.
#:
#: Reused rather than rebuilt, and that is the important decision here. A
#: separately-built cohort would drift from the one the evaluation plane
#: reports on, and the region would then have two different answers to "which
#: queries matter" -- with the tuning plane optimizing for one while the
#: published trend measured the other.
#: The search cohort, in order of preference. ``rolling`` first because recent
#: traffic is what a region most wants ranked well, and ``anchor`` as the
#: fallback for a region whose traffic predates the lookback window -- which is
#: the ordinary case for a corpus that was heavily used and then settled.
#:
#: Falling back to the anchor is safe only because promotion requires
#: confirmation on a *holdout*: the anchor is also the trend line, so selecting
#: on it and reporting on it would otherwise be the cohort-level version of
#: promoting a candidate by its own evidence. The report always names which
#: cohort was used.
SEARCH_COHORTS = ("rolling", "anchor")
HOLDOUT_COHORT = "temporal_holdout"
CONTROL_COHORT = "control"


@dataclass
class TuningOutcome:
    """What one batch produced."""

    experiment_id: str = ""
    status: str = "skipped"
    skipped_reason: str = ""
    diagnosis: Diagnosis | None = None
    decision: Decision | None = None
    bundle_id: str = ""
    applied: bool = False
    trials_run: int = 0
    trials_reused: int = 0
    #: Each retrieval mechanism scored alone, plus what the merge adds.
    mechanisms: dict[str, Any] = field(default_factory=dict)
    searches: int = 0
    duration_ms: float = 0.0
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def gates_passed(self) -> bool:
        return bool(self.decision and self.decision.gates_passed)


def _corpus_membership(state: Any) -> Any:
    """An ``indexed(artifact_id)`` predicate backed by one query.

    Loaded up front rather than probed per target: the attribution asks this
    once per miss, and a per-miss round trip against the same database the
    trials are reading would be the single most expensive thing in the phase.
    The set is artifact ids only, which is a few hundred kilobytes on a corpus
    where the chunks are gigabytes.
    """

    try:
        rows = state.rows("SELECT id FROM artifacts", ())
        known = {str(row["id"]) for row in rows}
    except Exception:  # noqa: BLE001
        logger.warning("tuning: could not read artifact ids; absence will not be diagnosed")
        return None
    return known.__contains__


def _verify_refusion(captures: dict[str, dict[str, Any]], ranking: RankingParameters) -> str:
    """Check the cheap path against the real one. Returns "" or a reason.

    Every capture is checked, not a sample. The check is arithmetic over lists
    already in memory, and the thing it is protecting against -- a
    re-implementation that has silently drifted from the merge -- is exactly
    the kind of failure that shows up on one query shape and not another.
    """

    checked = 0
    for query_id, stages in captures.items():
        if not refusion.refusable(stages):
            continue
        ok, reason = refusion.verify_equivalence(stages, ranking)
        if not ok:
            return f"{query_id}: {reason}"
        checked += 1
    if not checked:
        return "no capture was re-fusable"
    return ""


def run_tuning(
    engine: Any,
    *,
    force: bool = False,
    space: ParameterSpace | None = None,
    budget: Budget | None = None,
    executor: TuningExecutor | None = None,
    on_progress: Any = None,
) -> TuningOutcome:
    """One complete tuning batch."""

    config = engine.config
    state = engine.state
    settings = getattr(config, "tuning", None)
    kb_id = config.knowledge_base_id
    started = time.monotonic()

    if settings is None or (not settings.enabled and not force):
        return TuningOutcome(
            status="skipped",
            skipped_reason="tuning.enabled is false; pass force=True to run anyway",
        )

    space = space or ParameterSpace(pinned=frozenset(settings.pinned_parameters or ()))
    problems = validate_space(space)
    if problems:
        # Refused rather than filtered. A space whose declared stages disagree
        # with the ranking module would make the strategy propose parameters
        # for the wrong diagnosis -- a silent failure that still produces
        # numbers, which is the worst kind.
        return TuningOutcome(status="failed", skipped_reason=f"invalid parameter space: {problems}")

    objective = resolve_objective(settings.objective)
    budget = budget or Budget(
        refusion_trials=int(settings.refusion_trials),
        requery_trials=int(settings.requery_trials),
        max_searches=int(settings.max_searches),
    )

    lease = TuningLease(state, stale_after_seconds=float(settings.stale_seconds))
    with lease as acquired:
        if not acquired:
            return TuningOutcome(
                status="skipped", skipped_reason="another process holds the tuning lease"
            )
        return _run(
            engine,
            config=config,
            state=state,
            settings=settings,
            kb_id=kb_id,
            space=space,
            budget=budget,
            objective=objective,
            executor=executor
            or TuningExecutor(
                state,
                gate=BackpressureGate(
                    state,
                    max_queue_depth=int(settings.max_index_queue_depth),
                    respect_sync=bool(settings.yield_to_sync),
                ),
            ),
            on_progress=on_progress,
            started=started,
        )


def _run(
    engine: Any,
    *,
    config: Any,
    state: Any,
    settings: Any,
    kb_id: str,
    space: ParameterSpace,
    budget: Budget,
    objective: Objective,
    executor: TuningExecutor,
    on_progress: Any,
    started: float,
) -> TuningOutcome:
    sink = sink_for(config, state)
    exports = getattr(getattr(config, "pheasant", None), "exports_path", None)
    graph = getattr(engine.graph_builder, "graph", None)

    # --- 0. snapshot and cohorts ----------------------------------------
    manifest = snapshot_builder.build_snapshot(state, config, graph=graph)
    evaluation_store.save_snapshot(state, manifest)
    cohorts = resolve_cohorts(state, kb_id, config.evaluation)
    search_cohort = next(
        (
            cohorts[name]
            for name in SEARCH_COHORTS
            if cohorts.get(name) is not None and cohorts[name].queries
        ),
        None,
    )
    if search_cohort is None:
        return TuningOutcome(
            status="skipped",
            skipped_reason=(
                f"no cohort with queries among {', '.join(SEARCH_COHORTS)}; a tuning pass "
                "needs real queries with recorded proof, and there is nothing to tune "
                "against"
            ),
        )
    holdout = cohorts.get(HOLDOUT_COHORT)
    control = cohorts.get(CONTROL_COHORT)

    from pheasant.search.ranking import RankingParameters as RP

    base_ranking = RP.from_config(config)
    baseline = baseline_values(base_ranking, space)
    baseline_point = ParameterPoint.of(baseline)

    experiment = Experiment(
        experiment_id=Experiment.identity(
            kb_id,
            manifest.snapshot_id,
            space.digest,
            search_cohort.cohort_id,
            budget.as_dict(),
            baseline_point.point_id,
        ),
        kb_id=kb_id,
        snapshot_id=manifest.snapshot_id,
        cohort_id=search_cohort.cohort_id,
        holdout_cohort_id=holdout.cohort_id if holdout else "",
        control_cohort_id=control.cohort_id if control else "",
        space_digest=space.digest,
        budget=budget.as_dict(),
        baseline_point=baseline_point,
    )

    stale_before = _stale_before(float(settings.stale_seconds))
    tuning_store.reclaim_stale_experiments(state, kb_id, stale_before)
    # Unique per *attempt*, not per process. `open_experiment` inserts with
    # `ON CONFLICT DO NOTHING` and then reads the owner back to find out
    # whether it won — so an owner two claimants share makes both of them read
    # their own name and both conclude they won.
    #
    # A host:pid owner looks unique and is not: two threads in one API replica
    # (a scheduled trigger and somebody pressing Run) share it, and the
    # `__tuning__` lease does not separate them either, because `SourceLease`
    # grants a lease its current holder already owns. Found by running three
    # batches from three threads and getting two completed experiments.
    owner = f"{kb_id}:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
    if not tuning_store.open_experiment(state, experiment, owner=owner, stale_before=stale_before):
        return TuningOutcome(
            experiment_id=experiment.experiment_id,
            status="skipped",
            skipped_reason="this experiment is already running in another process",
        )

    # Now that the row exists, a cancel from *any* replica can reach this
    # batch: the executor reads the column between units.
    executor.cancel_check = lambda: tuning_store.cancel_requested(state, experiment.experiment_id)

    def phase(name: str, detail: str = "", **kwargs: Any) -> None:
        tuning_store.publish_phase(
            state, experiment.experiment_id, phase=name, detail=detail, **kwargs
        )
        if on_progress is not None:
            try:
                on_progress(name, detail)
            except Exception:  # noqa: BLE001 - progress must never fail a batch
                logger.debug("tuning: progress hook failed", exc_info=True)

    sink.start_experiment(experiment, space.as_dict())
    outcome = TuningOutcome(experiment_id=experiment.experiment_id, status="running")
    searches = 0

    try:
        # --- 1. diagnose ------------------------------------------------
        phase("diagnose", f"replaying {search_cohort.query_count} queries with stage capture")
        policy = ProofPolicy.from_config(config.evaluation.proof)
        proofs = collect_proof(
            state, kb_id, policy, before=None, query_ids=list(search_cohort.query_ids)
        )
        evidence = aggregate_proof(proofs, policy)
        positives = {
            query_id: ev.positives(policy.positive_floor) for query_id, ev in evidence.items()
        }

        searcher = _tuning_searcher(engine, base_ranking)
        replay = ReplayEngine(
            searcher,
            kb_id,
            graph=graph,
            security=getattr(config, "security", None),
            max_results=int(settings.max_results),
            mode=str(settings.mode),
            explain=True,
        )
        variant = default_matrix()[-1]  # B5: the region as it actually serves.
        captures, baseline_rankings, searched = _replay_cohort(
            replay, search_cohort, variant, executor
        )
        searches += searched

        indexed = _corpus_membership(state)
        attributions = []
        unevidenced = 0
        for query_id, stages in captures.items():
            targets = positives.get(query_id) or []
            if not targets:
                unevidenced += 1
                continue
            for target in targets:
                attributions.append(
                    attribute(
                        stages,
                        target,
                        max_results=int(settings.max_results),
                        query_id=query_id,
                        indexed=indexed,
                    )
                )
        histogram = stage_histogram(attributions)
        diagnosis = Diagnosis(
            diagnosis_id="diag-" + digest(experiment.experiment_id, manifest.snapshot_id),
            kb_id=kb_id,
            snapshot_id=manifest.snapshot_id,
            cohort_id=search_cohort.cohort_id,
            cohort_name=search_cohort.name,
            baseline_point_id=baseline_point.point_id,
            histogram=histogram,
            attributions=tuple(attributions),
            unevidenced_queries=unevidenced,
            summary=_summarize(histogram, unevidenced),
        )
        # Each mechanism on its own, from the captures already in hand.
        mechanisms = _measure_mechanisms(
            captures,
            positives,
            base_ranking,
            objective,
            provider=str(getattr(getattr(config.search, "embeddings", None), "provider", "") or "")
            if getattr(getattr(config.search, "embeddings", None), "enabled", False)
            else "off",
        )
        outcome.mechanisms = mechanisms
        outcome.diagnosis = diagnosis
        sink.log_diagnosis(experiment, diagnosis)
        tuning_store.write_cold(
            exports,
            kb_id,
            experiment.experiment_id,
            "diagnosis",
            [item.as_dict() for item in attributions],
        )
        tuning_store.write_cold(
            exports,
            kb_id,
            experiment.experiment_id,
            "baseline-captures",
            [{"query_id": qid, "stages": stages} for qid, stages in captures.items()],
        )

        # --- 2. propose --------------------------------------------------
        phase("propose", diagnosis.summary)
        proposals = propose(space, baseline, histogram, budget=budget)
        refusion_problem = _verify_refusion(captures, base_ranking)
        if refusion_problem:
            # The cheap path cannot be trusted for this batch. Say so loudly
            # and drop to real searches rather than reporting numbers from a
            # re-implementation that no longer matches the merge.
            logger.error(
                "tuning: re-fusion does not reproduce the served ranking (%s); "
                "every trial will run a real search",
                refusion_problem,
            )
            proposals = [
                Proposal(
                    point=p.point,
                    motivating_stage=p.motivating_stage,
                    rationale=p.rationale,
                    cost_class="requery",
                    strategy=p.strategy,
                    generation=p.generation,
                )
                for p in proposals
            ]

        if not proposals:
            return _finish_without_change(
                state,
                sink,
                experiment,
                outcome,
                diagnosis,
                reason=diagnosis.summary,
                started=started,
                searches=searches,
                exports=exports,
                histogram=histogram,
                objective=objective,
            )

        # --- 3. trial ----------------------------------------------------
        stored = tuning_store.load_trials(state, experiment.experiment_id)
        outcome.trials_reused = len(stored)
        baseline_score = scoring.score_cohort(baseline_rankings, positives)
        baseline_trial = _baseline_trial(
            experiment, search_cohort, baseline_point, baseline_score, histogram
        )
        tuning_store.save_trial(state, baseline_trial, kb_id)

        results: dict[str, Trial] = {}
        trial_scores: dict[str, scoring.CohortScore] = {}
        total = len(proposals)
        phase("trial", f"{total} points ({len(stored)} already done)", total=total, completed=0)
        for index, proposal in enumerate(proposals, start=1):
            executor.checkpoint()
            key = f"{proposal.point.point_id}::{search_cohort.cohort_id}"
            if key in stored:
                # Resumed. A stored trial has to come back as a *comparable*
                # result, not merely be skipped: the decision pairs per query,
                # and a run that skipped its way to an empty comparison set
                # would report `insufficient_evidence` for a batch that had in
                # fact evaluated everything.
                restored = _restore_trial(proposal, stored[key], experiment, search_cohort)
                if restored is not None:
                    results[proposal.point.point_id] = restored[0]
                    trial_scores[proposal.point.point_id] = restored[1]
                    # Counted as progress: a watcher must not see a resumed
                    # batch sit at 0% while it works through everything it
                    # already has, and then jump.
                    phase("trial", f"{index}/{total}: reused", completed=index, total=total)
                    continue
                if proposal.cost_class == "requery":
                    # The per-query rows are gone (cold storage absent or
                    # pruned) and re-running is the expensive path. Skip it:
                    # the aggregate is still on the row for the report, and a
                    # point missing from the comparison is better than one that
                    # spends the budget a second time.
                    logger.info(
                        "tuning: cannot restore %s for comparison; its per-query rows are gone",
                        proposal.point.point_id,
                    )
                    continue
                # A re-fusion trial costs nothing to redo, so redo it rather
                # than dropping it.
            if searches >= budget.max_searches:
                logger.info("tuning: search budget exhausted after %s searches", searches)
                break
            trial, used, trial_score = _evaluate(
                proposal,
                experiment=experiment,
                cohort=search_cohort,
                captures=captures,
                positives=positives,
                base_ranking=base_ranking,
                replay=replay,
                variant=variant,
                executor=executor,
                max_results=int(settings.max_results),
                indexed=indexed,
            )
            searches += used
            outcome.trials_run += 1
            results[proposal.point.point_id] = trial
            trial_scores[proposal.point.point_id] = trial_score
            cold_ref = tuning_store.write_cold(
                exports,
                kb_id,
                experiment.experiment_id,
                f"trial-{trial.trial_id}",
                [{"trial": trial.as_dict(), "score": trial_score.as_dict()}],
            )
            # The ref goes on the row, so a resumed batch can find the
            # per-query rows again without scanning the export directory.
            tuning_store.save_trial(state, trial, kb_id, cold_ref=cold_ref)
            sink.log_trial(experiment, trial, cold_ref)
            phase(
                "trial",
                f"{index}/{total}: {proposal.point.describe_delta()}",
                completed=index,
                total=total,
                searches=searches,
            )

        # --- 4. decide ---------------------------------------------------
        phase(
            "decide",
            "gating the best point against the holdout and control cohorts",
            completed=total,
            total=total,
        )
        decision, winner, comparisons = _decide(
            experiment=experiment,
            baseline_trial=baseline_trial,
            baseline_score=baseline_score,
            trials=results,
            trial_scores=trial_scores,
            positives=positives,
            base_ranking=base_ranking,
            holdout=holdout,
            control=control,
            replay=replay,
            variant=variant,
            executor=executor,
            settings=settings,
            histogram=histogram,
            objective=objective,
        )
        outcome.decision = decision
        sink.log_decision(experiment, decision)

        packaged = None
        if decision.outcome == "promote" and winner is not None:
            packaged = bundle_builder.package(
                experiment,
                decision,
                parameters=winner.proposal.point.values,
                baseline=baseline,
                metrics=winner.metrics,
                comparisons=comparisons,
                diagnosis=diagnosis,
                motivating_stage=winner.proposal.motivating_stage,
            )
            sink.log_bundle(experiment, packaged)
            outcome.bundle_id = packaged.bundle_id
            if settings.auto.apply:
                tuning_store.apply_bundle(
                    state, kb_id, packaged.bundle_id, applied_by="tuning.auto.apply"
                )
                outcome.applied = True
                _invalidate(engine)

        report = _report(
            experiment,
            diagnosis,
            decision,
            packaged,
            baseline_trial,
            results,
            searches,
            objective,
            mechanisms,
        )
        outcome.report = report
        outcome.status = "completed"
        outcome.searches = searches
        outcome.duration_ms = (time.monotonic() - started) * 1000.0
        tuning_store.close_experiment(
            state,
            experiment.experiment_id,
            status=tuning_store.COMPLETED,
            report=report,
            diagnosis=diagnosis,
        )
        sink.finish(experiment, "completed")
        return outcome

    except Cancelled as cancelled:
        # A distinct terminal state from `interrupted`. Both leave the trials
        # on disk and both are resumable, but only one of them should be
        # resumed *automatically*: an interrupted batch yielded to the region
        # and should come back on its own, and a cancelled one was stopped by
        # a person who would not thank us for restarting it.
        tuning_store.close_experiment(
            state,
            experiment.experiment_id,
            status=tuning_store.CANCELLED,
            error=str(cancelled),
        )
        sink.finish(experiment, "cancelled")
        outcome.status = "cancelled"
        outcome.skipped_reason = str(cancelled)
        return outcome
    except StoodDown as stood:
        # Not a failure: the region needed the machine. The experiment row goes
        # back to `interrupted` and its trials are on disk, so the next attempt
        # resumes from where this one yielded.
        tuning_store.close_experiment(
            state,
            experiment.experiment_id,
            status=tuning_store.INTERRUPTED,
            error=f"stood down: {stood}",
        )
        sink.finish(experiment, "interrupted")
        outcome.status = "interrupted"
        outcome.skipped_reason = str(stood)
        return outcome
    except Exception as exc:  # noqa: BLE001
        logger.exception("tuning: batch failed")
        tuning_store.close_experiment(
            state,
            experiment.experiment_id,
            status=tuning_store.FAILED,
            error=f"{type(exc).__name__}: {exc}",
        )
        sink.finish(experiment, "failed")
        outcome.status = "failed"
        outcome.skipped_reason = f"{type(exc).__name__}: {exc}"
        return outcome


# --------------------------------------------------------------------------
# phases
# --------------------------------------------------------------------------


def _replay_cohort(
    replay: ReplayEngine,
    cohort: Cohort,
    variant: Any,
    executor: TuningExecutor,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], int]:
    """One pass over a cohort. Returns (captures, rankings, searches run)."""

    captures: dict[str, dict[str, Any]] = {}
    rankings: dict[str, list[str]] = {}
    searches = 0
    for query in cohort.queries:
        executor.checkpoint()
        result = replay.replay_query(variant, query)
        searches += 1
        if result.failed:
            continue
        rankings[query.query_id] = list(result.ranked_ids)
        if result.stages:
            captures[query.query_id] = result.stages
    return captures, rankings, searches


def _evaluate(
    proposal: Proposal,
    *,
    experiment: Experiment,
    cohort: Cohort,
    captures: dict[str, dict[str, Any]],
    positives: dict[str, list[str]],
    base_ranking: RankingParameters,
    replay: ReplayEngine,
    variant: Any,
    executor: TuningExecutor,
    max_results: int,
    indexed: Any,
) -> tuple[Trial, int, scoring.CohortScore]:
    """One proposal on the search cohort. Returns (trial, searches, per-query score).

    The per-query score comes back with the trial because the decision needs a
    *paired* comparison, and a pair is defined per query. Two aggregates cannot
    be paired after the fact: they have already lost which queries each of them
    was able to score.

    The cheap and expensive paths produce the *same* ``Trial`` shape, including
    the stage histogram, so a decision can compare a re-fused point against a
    re-queried one without knowing which is which. That is only sound because
    :func:`_verify_refusion` has already established that re-fusion reproduces
    the real ranking on this corpus.
    """

    started = time.monotonic()
    point = apply_point(base_ranking, proposal.point.values)
    searches = 0
    rankings: dict[str, list[str]] = {}
    restaged: dict[str, dict[str, Any]] = {}

    if proposal.cost_class == "refusion":
        for query_id, stages in captures.items():
            if not refusion.refusable(stages):
                continue
            fused = refusion.refuse(
                stages["fusion_input"], int(stages.get("max_results") or max_results), point
            )
            rankings[query_id] = fused
            restaged[query_id] = refusion.restage(stages, fused, point)
    else:
        trial_replay = ReplayEngine(
            _repointed(replay.searcher, point),
            replay.kb_id,
            graph=replay.graph,
            security=replay.security,
            max_results=replay.max_results,
            mode=replay.mode,
            explain=True,
        )
        for query in cohort.queries:
            executor.checkpoint()
            result = trial_replay.replay_query(variant, query)
            searches += 1
            if result.failed:
                continue
            rankings[query.query_id] = list(result.ranked_ids)
            if result.stages:
                restaged[query.query_id] = result.stages

    score = scoring.score_cohort(rankings, positives)
    attributions = [
        attribute(stages, target, max_results=max_results, query_id=query_id, indexed=indexed)
        for query_id, stages in restaged.items()
        for target in positives.get(query_id) or []
    ]
    trial = Trial(
        trial_id=Trial.identity(
            experiment.experiment_id, proposal.point.point_id, cohort.cohort_id
        ),
        experiment_id=experiment.experiment_id,
        proposal=proposal,
        cohort_id=cohort.cohort_id,
        cohort_name=cohort.name,
        metrics=score.aggregate(),
        histogram=stage_histogram(attributions),
        evaluated_queries=score.evaluated,
        excluded_queries=score.unevidenced + len(score.failed),
        exclusion_reasons={
            "no_known_positive": score.unevidenced,
            "query_failed": len(score.failed),
        },
        duration_ms=(time.monotonic() - started) * 1000.0,
        searches=searches,
    )
    return trial, searches, score


def _decide(
    *,
    experiment: Experiment,
    baseline_trial: Trial,
    baseline_score: scoring.CohortScore,
    trials: dict[str, Trial],
    trial_scores: dict[str, scoring.CohortScore],
    positives: dict[str, list[str]],
    base_ranking: RankingParameters,
    holdout: Cohort | None,
    control: Cohort | None,
    replay: ReplayEngine,
    variant: Any,
    executor: TuningExecutor,
    settings: Any,
    histogram: dict[str, Any],
    objective: Objective,
) -> tuple[Decision, Trial | None, tuple[Comparison, ...]]:
    """Pick a winner, confirm it on unseen queries, and gate it.

    The winner is chosen on the search cohort and then has to *earn* promotion
    on a holdout the search never touched. That second step is the difference
    between a tuning system and a curve-fitter, and it is why the holdout is
    replayed with real searches even for a point the cheap path selected: the
    confirmation must not share a substrate with the selection.
    """

    # Ranked by the region's objective, not by a constant. `score` returns
    # None when a component metric is missing, and such a trial is excluded
    # rather than scored as zero: a point that could not be measured is not
    # the same as one that measured badly, and ranking them together would
    # put the unmeasurable ones last and call it a result.
    ranked = []
    for trial in trials.values():
        if trial.failed or not trial.metrics:
            continue
        if trial.proposal.point.point_id not in trial_scores:
            continue
        value = objective.score(trial.metrics)
        if value is not None:
            ranked.append((trial.proposal.point.point_id, value))
    best_ids = select_survivors(ranked, 1)
    winner = trials.get(best_ids[0]) if best_ids else None

    decision_id = "dec-" + digest(experiment.experiment_id, winner.trial_id if winner else "none")

    if winner is None:
        return (
            Decision(
                decision_id=decision_id,
                experiment_id=experiment.experiment_id,
                outcome="insufficient_evidence",
                reason="no trial produced a usable score",
                gates=(
                    gate_checks.gate(
                        "sufficient_evidence",
                        False,
                        summary="no trial produced a usable score",
                    ),
                ),
            ),
            None,
            (),
        )

    # Paired per query against the baseline pass, not a subtraction of two
    # aggregates: the point of pairing is that a query only one side could
    # score is excluded with a reason rather than silently changing the
    # denominator on one side of a difference.
    # Compared on the objective's own metric where it has one. A composite is
    # compared on its dominant component, and the report publishes the full
    # substituted arithmetic beside it — pairing per query needs a single
    # metric, and inventing a per-query composite would be a second scoring
    # path that nothing else exercises.
    comparison_metric = objective.primary_metric or max(
        objective.normalized(), key=lambda name: objective.normalized()[name]
    )
    search_comparison = Comparison(
        baseline_trial_id=baseline_trial.trial_id,
        treatment_trial_id=winner.trial_id,
        **scoring.compare(
            comparison_metric,
            baseline_score,
            trial_scores[winner.proposal.point.point_id],
        ),
    )

    holdout_comparison = None
    control_comparison = None
    for cohort, label in ((holdout, "holdout"), (control, "control")):
        if cohort is None or not cohort.queries:
            continue
        base_rank, _ = _replay_for(replay, cohort, variant, executor, base_ranking)
        point_rank, _ = _replay_for(
            replay,
            cohort,
            variant,
            executor,
            apply_point(base_ranking, winner.proposal.point.values),
        )
        base_score = scoring.score_cohort(base_rank, positives)
        point_score = scoring.score_cohort(point_rank, positives)
        raw = scoring.compare(comparison_metric, base_score, point_score)
        comparison = Comparison(
            baseline_trial_id=baseline_trial.trial_id,
            treatment_trial_id=winner.trial_id,
            **raw,
        )
        if label == "holdout":
            holdout_comparison = comparison
        else:
            control_comparison = comparison

    gates = gate_checks.evaluate(
        search_comparison=search_comparison,
        holdout_comparison=holdout_comparison,
        control_comparison=control_comparison,
        baseline_histogram=histogram,
        winning_histogram=winner.histogram,
        parameters=winner.proposal.point.values,
        min_paired=int(settings.minimum_paired_queries),
    )
    failures = gate_checks.blocking_failures(gates)
    improved = search_comparison.delta > 0

    if not improved:
        outcome, reason = (
            "no_change",
            f"the best point did not beat the current configuration "
            f"({search_comparison.substituted})",
        )
    elif failures:
        outcome, reason = (
            "reject",
            f"the best point improved the search cohort ({search_comparison.delta:+.4f}) "
            f"but failed {', '.join(failures)}",
        )
    else:
        outcome, reason = (
            "promote",
            f"{winner.proposal.point.describe_delta()} — {search_comparison.substituted}; "
            f"every gate passed. {winner.proposal.rationale}",
        )

    comparisons = tuple(
        c for c in (search_comparison, holdout_comparison, control_comparison) if c is not None
    )
    return (
        Decision(
            decision_id=decision_id,
            experiment_id=experiment.experiment_id,
            outcome=outcome,
            reason=reason,
            winning_point_id=winner.proposal.point.point_id,
            comparisons=comparisons,
            gates=tuple(gates),
            holdout_confirmed=bool(
                holdout_comparison and holdout_comparison.delta >= gate_checks.HOLDOUT_MIN_DELTA
            ),
            control_regressed=bool(
                control_comparison
                and control_comparison.delta < -gate_checks.CONTROL_MAX_REGRESSION
            ),
        ),
        winner,
        comparisons,
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _measure_mechanisms(
    captures: dict[str, dict[str, Any]],
    positives: dict[str, list[str]],
    base_ranking: RankingParameters,
    objective: Objective,
    provider: str = "",
) -> dict[str, Any]:
    """Each retrieval mechanism, scored on its own and in the merge.

    An ablation, and it costs **no retrieval**: the arms already ran during
    the baseline replay, so isolating one is a re-fusion with the other two
    weighted to zero. That is the same equivalence the cheap trial path rests
    on, so it is sound for exactly the reasons `verify_equivalence` checks.

    Worth having because "hybrid is better" is an assumption most regions
    never test, and it is frequently false. A vector arm that was never built
    contributes nothing and costs latency on every search; a graph arm can be
    actively harmful on a prose corpus. This is the number that says so, and
    it says it per mechanism rather than as one hybrid score that hides which
    part is carrying it.

    Reported, never acted on automatically. Dropping an arm is a decision with
    consequences beyond this cohort — and the gates already refuse a parameter
    set that empties one without saying so.
    """

    from pheasant.search.observability import ARMS

    out: dict[str, Any] = {}
    for arm in (*ARMS, "hybrid"):
        # Isolated by *excluding* the other arms, not by weighting them to
        # zero. A zero weight is a zero score, so the other arms' candidates
        # would stay in the merge ordered by their original ranks — which made
        # "vector alone" return the text arm's ranking verbatim whenever
        # embeddings were off, and score just under it.
        point = base_ranking
        contributing = ARMS if arm == "hybrid" else (arm,)
        rankings = {
            query_id: refusion.refuse(
                stages["fusion_input"],
                int(stages.get("max_results") or 10),
                point,
                arms=contributing,
            )
            for query_id, stages in captures.items()
            if refusion.refusable(stages)
        }
        if not rankings:
            continue
        score = scoring.score_cohort(rankings, positives)
        metrics = score.aggregate()
        out[arm] = {
            "metrics": metrics,
            "objective_score": objective.score(metrics),
            "evaluated_queries": score.evaluated,
        }
    # Which embedder produced the vector arm's numbers. Load-bearing for
    # honesty rather than decoration: the offline `stub` provider is a
    # bag-of-words hasher with a handful of planted synonyms, so it behaves
    # like a second lexical retriever and can score respectably. Reporting
    # that row as "vector" without saying so invites a reader to conclude the
    # region's *semantic* retrieval was measured, when no embedding model ran.
    if "vector" in out:
        out["vector"]["provider"] = provider
        # `off` is emphatically not a semantic result — no embedder ran at
        # all — and neither is `stub`, which is a bag-of-words hasher. Only a
        # real provider licenses reading that row as semantic retrieval.
        out["vector"]["semantic"] = provider not in ("stub", "off", "", None)

    hybrid = (out.get("hybrid") or {}).get("objective_score")
    for arm, entry in out.items():
        if arm == "hybrid" or hybrid is None or entry["objective_score"] is None:
            continue
        # What the merge is worth over this arm alone. Negative means hybrid
        # is *losing* to a single mechanism on this cohort, which is a finding
        # worth surfacing loudly rather than a rounding error.
        entry["hybrid_gain"] = hybrid - entry["objective_score"]
    return out


def _restore_trial(
    proposal: Proposal,
    row: dict[str, Any],
    experiment: Experiment,
    cohort: Cohort,
) -> tuple[Trial, scoring.CohortScore] | None:
    """Rebuild a stored trial and its per-query score, or ``None``.

    ``None`` rather than a partially-restored trial: a comparison built from an
    aggregate with no per-query rows is not a paired comparison, and reporting
    it as one would be the exact confound the pairing exists to remove.
    """

    payload = tuning_store.read_cold(str(row.get("cold_ref") or ""))
    score_payload = next(
        (item.get("score") for item in payload if isinstance(item, dict) and item.get("score")),
        None,
    )
    if not score_payload:
        return None
    score = scoring.CohortScore.from_dict(score_payload)
    if not score.per_query:
        return None
    trial = Trial(
        trial_id=str(row.get("trial_id") or ""),
        experiment_id=experiment.experiment_id,
        proposal=proposal,
        cohort_id=cohort.cohort_id,
        cohort_name=cohort.name,
        metrics=dict(row.get("metrics") or {}),
        histogram=dict(row.get("histogram") or {}),
        evaluated_queries=int(row.get("evaluated_queries") or 0),
        excluded_queries=int(row.get("excluded_queries") or 0),
        duration_ms=float(row.get("duration_ms") or 0.0),
        searches=int(row.get("searches") or 0),
    )
    return trial, score


def _replay_for(
    replay: ReplayEngine,
    cohort: Cohort,
    variant: Any,
    executor: TuningExecutor,
    ranking: RankingParameters,
) -> tuple[dict[str, list[str]], int]:
    engine = ReplayEngine(
        _repointed(replay.searcher, ranking),
        replay.kb_id,
        graph=replay.graph,
        security=replay.security,
        max_results=replay.max_results,
        mode=replay.mode,
    )
    rankings: dict[str, list[str]] = {}
    searches = 0
    for query in cohort.queries:
        executor.checkpoint()
        result = engine.replay_query(variant, query)
        searches += 1
        if not result.failed:
            rankings[query.query_id] = list(result.ranked_ids)
    return rankings, searches


def _repointed(searcher: Any, ranking: RankingParameters) -> Any:
    """The same searcher with one parameter point pinned.

    A *new* searcher over the same store rather than a mutated one: the batch
    and the region's request path share a process, and mutating the live
    searcher's parameters would re-rank production traffic for the duration of
    a trial.
    """

    from pheasant.search.hybrid import HybridSearch
    from pheasant.search.sqlite_store import SearchStore

    return HybridSearch(
        SearchStore(searcher.store.state, ranking=ranking),
        vector=searcher.vector,
        node_index=searcher.node_index,
        wasm_relationship_search=searcher.wasm_relationship_search,
        steering_enabled=searcher.steering_enabled,
        default_memory_policy=searcher.default_memory_policy,
        usage_tracking=False,
    )


def _tuning_searcher(engine: Any, ranking: RankingParameters) -> Any:
    """The baseline searcher, with usage tracking off.

    Off for the same reason the evaluation plane's replay searcher has it off:
    counting a tuning replay as a memory *use* would let the plane raise the
    salience of the records whose effect it is measuring, which is the tightest
    self-rewarding loop this system could build.
    """

    from pheasant.evaluation.runner import _replay_searcher

    return _replay_searcher(engine, ranking)


def _baseline_trial(
    experiment: Experiment,
    cohort: Cohort,
    point: ParameterPoint,
    score: scoring.CohortScore,
    histogram: dict[str, Any],
) -> Trial:
    return Trial(
        trial_id=Trial.identity(experiment.experiment_id, point.point_id, cohort.cohort_id),
        experiment_id=experiment.experiment_id,
        proposal=Proposal(
            point=point,
            motivating_stage="baseline",
            rationale="The configuration the region is currently serving.",
            cost_class="baseline",
            strategy="baseline",
        ),
        cohort_id=cohort.cohort_id,
        cohort_name=cohort.name,
        metrics=score.aggregate(),
        histogram=histogram,
        evaluated_queries=score.evaluated,
        excluded_queries=score.unevidenced,
        exclusion_reasons={"no_known_positive": score.unevidenced},
    )


def _summarize(histogram: dict[str, Any], unevidenced: int) -> str:
    misses = int(histogram.get("misses") or 0)
    if not misses:
        return (
            f"every evidenced query returned its known positive; "
            f"{unevidenced} queries had no proof and were excluded"
        )
    share = histogram.get("actionable_share")
    dominant = histogram.get("dominant_stage") or "unknown"
    lead = (
        f"{misses} misses, most in {dominant} "
        f"({histogram.get('counts', {}).get(dominant, 0)} of them)"
    )
    if share is not None and float(share) < 0.34:
        return (
            f"{lead}. Only {float(share):.0%} of misses are in a stage any retrieval "
            "parameter can move, so tuning is the wrong tool here — the failures are "
            "upstream, in what is indexed and how it is chunked."
        )
    return f"{lead}. {float(share or 0):.0%} of misses are in a tunable stage."


def _finish_without_change(
    state: Any,
    sink: Any,
    experiment: Experiment,
    outcome: TuningOutcome,
    diagnosis: Diagnosis,
    *,
    reason: str,
    started: float,
    searches: int,
    exports: Any,
    histogram: dict[str, Any],
    objective: Objective | None = None,
) -> TuningOutcome:
    """A completed batch that proposed nothing. A result, not a failure."""

    decision = Decision(
        decision_id="dec-" + digest(experiment.experiment_id, "no-proposals"),
        experiment_id=experiment.experiment_id,
        outcome="no_change",
        reason=reason,
        gates=(
            gate_checks.gate(
                "tuning_is_applicable",
                False,
                summary=reason,
                observed=histogram.get("actionable_share"),
                threshold=0.34,
                blocking=False,
            ),
        ),
    )
    sink.log_decision(experiment, decision)
    outcome.decision = decision
    outcome.status = "completed"
    outcome.searches = searches
    outcome.duration_ms = (time.monotonic() - started) * 1000.0
    report = {
        "experiment": experiment.as_dict(),
        "diagnosis": diagnosis.as_dict(),
        "decision": decision.as_dict(),
        "trials": [],
        "searches": searches,
        "objective": objective.as_dict() if objective is not None else None,
    }
    outcome.report = report
    tuning_store.close_experiment(
        state,
        experiment.experiment_id,
        status=tuning_store.COMPLETED,
        report=report,
        diagnosis=diagnosis,
    )
    sink.finish(experiment, "completed")
    return outcome


def _report(
    experiment: Experiment,
    diagnosis: Diagnosis,
    decision: Decision,
    packaged: Any,
    baseline_trial: Trial,
    trials: dict[str, Trial],
    searches: int,
    objective: Objective,
    mechanisms: dict[str, Any],
) -> dict[str, Any]:
    ordered = sorted(
        trials.values(),
        key=lambda t: (-(objective.score(t.metrics) or 0.0), t.trial_id),
    )
    return {
        "experiment": experiment.as_dict(),
        "diagnosis": diagnosis.as_dict(),
        "decision": decision.as_dict(),
        "bundle": packaged.as_dict() if packaged is not None else None,
        "baseline": baseline_trial.as_dict(),
        "trials": [t.as_dict() for t in ordered[:25]],
        "trial_count": len(trials),
        "searches": searches,
        # Which definition of "better" produced this result, and what it
        # accepted getting worse. A report that named a winner without naming
        # its objective would be unreadable a month later.
        "mechanisms": mechanisms,
        "objective": {
            **objective.as_dict(),
            "baseline_score": objective.score(baseline_trial.metrics),
            "baseline_substituted": objective.substituted(baseline_trial.metrics),
        },
        # Kept for readers (and the UI's sweep charts) that plot one metric.
        "primary_metric": objective.primary_metric or PRIMARY_METRIC,
    }


def _stale_before(stale_seconds: float) -> str:
    from datetime import datetime, timedelta

    return (
        (datetime.now(UTC) - timedelta(seconds=max(1.0, stale_seconds)))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _invalidate(engine: Any) -> None:
    """Make this process pick up a just-applied bundle at once.

    Other replicas converge on their own TTL, which is the point of a polled
    overlay. This is only about not making the process that applied it wait.
    """

    for holder in (getattr(engine, "searcher", None), engine):
        store = getattr(getattr(holder, "store", None), "ranking", None)
        invalidate = getattr(store, "invalidate", None)
        if callable(invalidate):
            invalidate()
