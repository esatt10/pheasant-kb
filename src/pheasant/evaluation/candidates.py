"""Shadow validation: a proposal earns production ranking or it does not get it.

The state machine is the specification's::

    observed evidence -> proposed -> candidate -> shadow validated -> active
                                                 -> retained | rejected

and the load-bearing edge is the one from *candidate* to *active*. Everything
before it already exists in this codebase: ``memory.formation`` mines the
observation plane and writes rows to ``memory_candidates``, which a person
promotes from the Memory tab. What was missing is the evidence a promotion
decision could be made *on*, and the guarantee that gathering it cannot itself
change what production returns.

Both are here.

**A candidate never touches the store to be measured.** Shadow rules are passed
into the search call per query and live for its duration --
``ReplayEngine`` hands them to ``search_context`` as ``extra_steering_records``,
which routes them through the same ``parse_rule``/``admits`` path a stored rule
takes. Nothing is written, so a candidate cannot reach production ranking by
being evaluated.

**Promotion on originating-query performance alone is refused.** A candidate
that improves the query that created it has demonstrated recall of its own
evidence. That is the self-rewarding loop, and
``allow_originating_query_only_promotion`` is off by default so it stays
closed: the decision needs independent queries, and -- when generalization is
required -- a temporal holdout the candidate could not have been fitted to.

**A proposed *fact* is not shadow-replayable, and says so.** Its text is not in
any index, so no arm can return it; scoring the candidate's own text against
the query would measure string similarity and report it as retrieval. Those
candidates come back as ``not_shadow_replayable`` with that reason, which is a
smaller answer than the alternative and a true one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pheasant.evaluation.contracts import GateResult, MetricResult, utc_now
from pheasant.evaluation.replay import shadow_records

logger = logging.getLogger(__name__)

#: Decisions a validation can reach. ``reject`` is never reached automatically:
#: a rejection is permanent by design (the candidate upsert keeps a rejected row
#: rejected forever), and spending that on absent evidence would make the review
#: queue forget proposals nobody has had a chance to demonstrate yet.
DECISIONS = ("promote", "retain_candidate", "insufficient_evidence", "not_shadow_replayable")


@dataclass
class CandidateDecision:
    """One candidate's outcome, with the reasons behind it."""

    candidate_id: str
    rule_id: str
    kind: str
    decision: str
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    decided_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "rule_id": self.rule_id,
            "kind": self.kind,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "evidence": self.evidence,
            "decided_at": self.decided_at,
        }


def load_candidates(state: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    """Pending proposals, most recently reinforced first."""

    try:
        return list(state.list_memory_candidates(status="pending", limit=int(limit)))
    except Exception:  # noqa: BLE001 - formation is optional
        logger.debug("evaluation: memory candidates unavailable", exc_info=True)
        return []


def shadow_ids(candidates: list[dict[str, Any]]) -> tuple[str, ...]:
    """Ids of the candidates a shadow variant can actually exercise."""

    replayable = shadow_records(candidates, now=utc_now())
    return tuple(sorted(record["record_id"] for record in replayable if record["record_id"]))


def _originating_queries(candidate: dict[str, Any]) -> set[str]:
    from pheasant.evaluation.contracts import query_id as make_query_id

    try:
        evidence = json.loads(candidate.get("evidence_json") or "{}")
    except (TypeError, ValueError):
        return set()
    texts: list[str] = []
    if evidence.get("query"):
        texts.append(str(evidence["query"]))
    for item in evidence.get("queries") or []:
        texts.append(str(item))
    return {make_query_id(text) for text in texts if text}


def validate(
    candidate: dict[str, Any],
    *,
    settings: Any,
    gates: list[GateResult],
    holdout_gain: MetricResult | None,
    learned_gain: MetricResult | None,
    control_regression: MetricResult | None,
    negative_exposure_gate: GateResult | None,
    shadow_replayable: bool,
    independent_query_ids: set[str],
) -> CandidateDecision:
    """Apply the promotion conditions to one candidate and say what decided it.

    Every condition contributes a *reason string* whether it passes or fails,
    because "retained" with no explanation is indistinguishable from "the
    validator did not run", and an operator staring at a queue that never moves
    needs to know which condition is the one blocking it.
    """

    decision = CandidateDecision(
        candidate_id=str(candidate.get("id") or ""),
        rule_id=str(candidate.get("rule_id") or ""),
        kind=str(candidate.get("kind") or "fact"),
        decision="retain_candidate",
        decided_at=utc_now(),
    )

    if not shadow_replayable:
        decision.decision = "not_shadow_replayable"
        decision.reasons.append(
            "a proposed fact is not in any index, so no retrieval arm can return it; "
            "scoring its own text against the query would measure string similarity, "
            "not retrieval"
        )
        return decision

    failed_gates = [gate for gate in gates if not gate.passed]
    if settings.require_all_hard_gates and failed_gates:
        decision.reasons.extend(
            f"gate {gate.gate_id} failed: {gate.detail}" for gate in failed_gates
        )
        decision.evidence["failed_gates"] = [gate.gate_id for gate in failed_gates]
        return decision
    decision.reasons.append("all hard gates passed")

    originating = _originating_queries(candidate)
    independent = independent_query_ids - originating
    decision.evidence["independent_queries"] = len(independent)
    decision.evidence["originating_queries"] = len(originating)
    if not settings.allow_originating_query_only_promotion and not independent:
        decision.reasons.append(
            "every query showing a gain is one this candidate was derived from; "
            "promoting on that alone is recall of its own evidence"
        )
        return decision
    if len(independent) < int(settings.minimum_independent_queries):
        decision.decision = "insufficient_evidence"
        decision.reasons.append(
            f"{len(independent)} independent queries < "
            f"{settings.minimum_independent_queries} required"
        )
        return decision
    decision.reasons.append(f"{len(independent)} independent queries")

    if control_regression is not None and control_regression.value is not None:
        decision.evidence["control_regression"] = control_regression.value
        if control_regression.value > float(settings.maximum_control_regression):
            decision.reasons.append(
                f"control regression {control_regression.value:.1%} > "
                f"{float(settings.maximum_control_regression):.1%} tolerated"
            )
            return decision
        decision.reasons.append(
            f"control regression {control_regression.value:.1%} within tolerance"
        )

    if negative_exposure_gate is not None and not negative_exposure_gate.passed:
        decision.reasons.append(
            f"known-negative exposure rose by {negative_exposure_gate.observed:+.4f}"
        )
        return decision

    if holdout_gain is None or holdout_gain.value is None:
        decision.decision = "insufficient_evidence"
        decision.reasons.append(
            "no temporal holdout result: forward generalization is unmeasured, and learned-query "
            "performance alone cannot stand in for it"
        )
        if learned_gain is not None and learned_gain.value is not None:
            decision.evidence["learned_gain_only"] = learned_gain.value
        return decision

    decision.evidence["holdout_gain"] = holdout_gain.value
    decision.evidence["holdout_queries"] = holdout_gain.denominator
    if int(holdout_gain.denominator or 0) < int(settings.minimum_temporal_holdout_queries):
        decision.decision = "insufficient_evidence"
        decision.reasons.append(
            f"{int(holdout_gain.denominator or 0)} holdout queries < "
            f"{settings.minimum_temporal_holdout_queries} required"
        )
        return decision
    if holdout_gain.value < float(settings.minimum_target_metric_gain):
        decision.reasons.append(
            f"forward gain {holdout_gain.value:+.4f} < "
            f"{float(settings.minimum_target_metric_gain):+.4f} required"
        )
        return decision

    if learned_gain is not None and learned_gain.value is not None:
        decision.evidence["learned_gain"] = learned_gain.value
        gap = round(learned_gain.value - holdout_gain.value, 6)
        decision.evidence["generalization_gap"] = gap
        decision.reasons.append(
            f"learned gain {learned_gain.value:+.4f}, forward gain {holdout_gain.value:+.4f}, "
            f"gap {gap:+.4f}"
        )

    decision.decision = "promote"
    decision.reasons.append(
        f"forward gain {holdout_gain.value:+.4f} over "
        f"{int(holdout_gain.denominator or 0)} independent later queries"
    )
    return decision


def apply_decisions(
    engine: Any,
    decisions: list[CandidateDecision],
    *,
    enabled: bool,
    admit: Any = None,
) -> list[dict[str, Any]]:
    """Act on the decisions -- or record them and act on none.

    ``enabled`` is ``evaluation.promotion.enabled``, and it is off by default.
    A run with it off produces exactly the same decisions and writes exactly
    nothing, which is what makes it safe to turn measurement on everywhere and
    promotion on nowhere: an operator can read a month of decisions before
    letting any of them take effect.

    Admission itself goes through the caller's ``admit`` -- in practice
    :func:`pheasant.artifacts.formation.admit`, which appends through
    ``MemoryStore.append`` like every other write. There is no second ingestion
    path here, and adding one would break memory's first invariant.
    """

    applied: list[dict[str, Any]] = []
    for decision in decisions:
        record = decision.as_dict()
        record["applied"] = False
        if decision.decision == "promote" and enabled and admit is not None:
            try:
                result = admit(engine, decision.candidate_id, admitted_by="evaluation")
                record["applied"] = True
                record["record_id"] = (result or {}).get("record_id")
            except Exception as exc:  # noqa: BLE001 - a failed admit is reported, not fatal
                logger.warning(
                    "evaluation: promoting %s failed", decision.candidate_id, exc_info=True
                )
                record["error"] = f"{type(exc).__name__}: {exc}"
        elif decision.decision == "promote" and not enabled:
            record["note"] = "promotion is disabled; the decision was recorded and not applied"
        applied.append(record)
    return applied
