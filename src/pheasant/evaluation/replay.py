"""Deterministic batch replay: run the cohort through the real retrieval path.

Nothing here re-implements retrieval. Every result comes from
:meth:`pheasant.search.hybrid.HybridSearch.search_context` -- the same call the
HTTP and MCP surfaces make -- because a replay against a re-implementation
measures the re-implementation. That is not a hypothetical: this repository has
already lost time to a hand-rolled `yaml.py` that shadowed the real parser and
made the suite validate against something the image never ran.

Three properties the replay owes its callers.

**Paired by query id, not by position.** A variant that fails on one query must
not shift every later comparison by one. Results are keyed, and a query that
completed in one variant and failed in the other is excluded from the pair with
a recorded reason rather than compared against a hole.

**Read-only, and it writes nothing anywhere.** Usage tracking is off on the
replay searcher: counting a replayed retrieval as a *use* would let evaluation
inflate the salience of the records it is measuring, which is the tightest
self-rewarding loop this system could build. Shadow candidates are passed in
per call and never persisted.

**Over-fetch, then exclude.** Leave-one-out attribution removes a record's hits
from a result list the region really produced. Done naively that is a top-k with
a hole in it; done here the searcher is asked for ``k * OVERFETCH`` and the list
is truncated to ``k`` *after* exclusion, so the slots a held-out record occupied
are refilled from real ranked candidates. It is still an approximation of "the
record was never written" -- fusion scores are unchanged, and a hit that would
have entered from beyond the widened window does not -- and the report says so
rather than implying the exclusion was exact.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pheasant.evaluation.contracts import Cohort, EvaluatedQuery, Variant

logger = logging.getLogger(__name__)

#: How far past ``max_results`` a run fetches when it will exclude records
#: afterwards. Three: enough to refill a top-10 after excluding a whole
#: cluster, small enough that a replay does not become a different workload
#: from the one it claims to reproduce.
OVERFETCH = 3


@dataclass
class QueryReplay:
    """One query under one variant."""

    query_id: str
    variant_id: str
    text: str
    ranked_ids: list[str] = field(default_factory=list)
    ranked_paths: list[str] = field(default_factory=list)
    memory_record_ids: list[str] = field(default_factory=list)
    arms: dict[str, list[str]] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    contributing_arms: dict[str, list[str]] = field(default_factory=dict)
    result_count: int = 0
    duration_ms: float = 0.0
    steering_applied: dict[str, Any] = field(default_factory=dict)
    #: The per-stage diagnostic, present only when the engine was built with
    #: ``explain=True`` -- which the evaluation plane never does and the
    #: tuning plane always does. Empty here means "not asked for", never
    #: "nothing happened": a stage block is large (three ranked arms and a
    #: pre-truncation fused list per query) and belongs in cold storage, not
    #: in an evaluation checkpoint that a resumed batch reloads in full.
    stages: dict[str, Any] = field(default_factory=dict)
    failed: str = ""

    def rank_of(self, target_id: str) -> int | None:
        try:
            return self.ranked_ids.index(target_id) + 1
        except ValueError:
            return None

    def top(self, k: int) -> list[str]:
        return self.ranked_ids[:k]

    def as_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "variant_id": self.variant_id,
            "text": self.text,
            "ranked_ids": list(self.ranked_ids),
            "ranked_paths": list(self.ranked_paths),
            "memory_record_ids": list(self.memory_record_ids),
            "arms": {name: list(ids) for name, ids in self.arms.items()},
            "scores": dict(self.scores),
            "contributing_arms": {k: list(v) for k, v in self.contributing_arms.items()},
            "result_count": self.result_count,
            "duration_ms": round(self.duration_ms, 3),
            "steering_applied": self.steering_applied,
            "stages": self.stages,
            "failed": self.failed,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> QueryReplay:
        """Rebuild a replayed query from its checkpoint.

        Lossless for everything the metrics read -- ranks, paths, memory ids,
        per-arm attribution, scores, latency and the failure reason. A resumed
        run must compute the *same* numbers as an uninterrupted one, so a
        checkpoint that dropped a field would make the answer depend on whether
        the container happened to restart.
        """

        return cls(
            query_id=str(payload.get("query_id") or ""),
            variant_id=str(payload.get("variant_id") or ""),
            text=str(payload.get("text") or ""),
            ranked_ids=[str(item) for item in payload.get("ranked_ids") or []],
            ranked_paths=[str(item) for item in payload.get("ranked_paths") or []],
            memory_record_ids=[str(item) for item in payload.get("memory_record_ids") or []],
            arms={k: list(v) for k, v in (payload.get("arms") or {}).items()},
            scores={k: float(v) for k, v in (payload.get("scores") or {}).items()},
            contributing_arms={
                k: list(v) for k, v in (payload.get("contributing_arms") or {}).items()
            },
            result_count=int(payload.get("result_count") or 0),
            duration_ms=float(payload.get("duration_ms") or 0.0),
            steering_applied=dict(payload.get("steering_applied") or {}),
            stages=dict(payload.get("stages") or {}),
            failed=str(payload.get("failed") or ""),
        )


@dataclass
class VariantReplay:
    """A whole cohort under one variant."""

    variant: Variant
    cohort_id: str
    results: dict[str, QueryReplay] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def completed_ids(self) -> set[str]:
        return {qid for qid, result in self.results.items() if not result.failed}

    def latencies(self) -> list[float]:
        return sorted(r.duration_ms for r in self.results.values() if not r.failed)

    def as_dict(self) -> dict[str, Any]:
        """The checkpoint payload: everything a resumed run must not recompute."""

        return {
            "variant_id": self.variant.variant_id,
            "cohort_id": self.cohort_id,
            "results": [result.as_dict() for result in self.results.values()],
            "failures": dict(self.failures),
        }

    @classmethod
    def from_dict(cls, variant: Variant, payload: dict[str, Any]) -> VariantReplay:
        run = cls(variant=variant, cohort_id=str(payload.get("cohort_id") or ""))
        for item in payload.get("results") or []:
            replayed = QueryReplay.from_dict(item)
            if replayed.query_id:
                run.results[replayed.query_id] = replayed
        run.failures = {str(k): str(v) for k, v in (payload.get("failures") or {}).items()}
        return run


def shadow_records(candidates: list[dict[str, Any]], *, now: str) -> list[dict[str, Any]]:
    """Turn proposed steering candidates into rule records retrieval can read.

    Shaped exactly like a ``memory_records`` row so ``admits`` and ``parse_rule``
    treat them identically to stored rules -- the shadow must exercise the real
    predicate, not a copy of it. Nothing is written: these live for the length
    of one search call.

    Only ``alias``/``preference``/``exclusion`` candidates are returned. A
    proposed *fact* cannot be shadow-replayed at all: its text is not in the
    lexical or vector index, so no arm can return it, and pretending otherwise
    by scoring the candidate's own text against the query would measure string
    similarity and report it as retrieval. The runner records those candidates
    as ``not_shadow_replayable`` instead of quietly scoring them.
    """

    from pheasant.artifacts.steering import STEERING_KINDS

    out: list[dict[str, Any]] = []
    for candidate in candidates:
        kind = str(candidate.get("kind") or "")
        if kind not in STEERING_KINDS:
            continue
        out.append(
            {
                "record_id": str(candidate.get("id") or candidate.get("record_id") or ""),
                "scope": str(candidate.get("scope") or "org"),
                "subject": candidate.get("subject"),
                "kind": kind,
                "text": str(candidate.get("text") or ""),
                "valid_from": str(candidate.get("first_seen") or now),
                "valid_until": None,
                "supersedes": None,
                "written_by": candidate.get("written_by"),
                "tier": "hot",
            }
        )
    return out


class ReplayEngine:
    """Runs cohorts through the real search path, one variant at a time."""

    def __init__(
        self,
        searcher: Any,
        kb_id: str,
        *,
        graph: Any = None,
        security: Any = None,
        max_results: int = 10,
        mode: str = "hybrid",
        shadow: list[dict[str, Any]] | None = None,
        explain: bool = False,
    ):
        self.searcher = searcher
        self.kb_id = kb_id
        self.graph = graph
        self.security = security
        self.max_results = max(1, int(max_results))
        self.mode = mode
        self.shadow = list(shadow or [])
        # Ask retrieval to report what each stage did. Off for the evaluation
        # plane, which measures *outcomes* and whose checkpoints must stay
        # small enough to reload wholesale; on for the tuning plane, which
        # cannot propose a parameter without knowing which stage lost the
        # document.
        self.explain = bool(explain)
        from pheasant.artifacts.migration import retrieval_source_ids

        self.artifact_sources = retrieval_source_ids(
            getattr(getattr(searcher, "store", None), "state", None)
        )

    def _policy(self, variant: Variant, query: EvaluatedQuery) -> dict[str, Any]:
        """The memory policy for one (variant, query) pair.

        A synthetic invariant case may carry its own ``as_of`` or
        ``current_only``: those cases exist to assert temporal behaviour, and
        the case has to be able to ask for the behaviour it asserts. Everything
        else takes the variant's policy unchanged, which is what keeps a pair
        differing in exactly one thing.
        """

        policy: dict[str, Any] = {"mode": variant.memory_results}
        if variant.tiers:
            policy["tiers"] = list(variant.tiers)
        expectation = query.expectation or {}
        if expectation.get("as_of"):
            policy["as_of"] = expectation["as_of"]
            policy["current_only"] = False
        return policy

    def replay_query(self, variant: Variant, query: EvaluatedQuery) -> QueryReplay:
        result = QueryReplay(
            query_id=query.query_id, variant_id=variant.variant_id, text=query.text
        )
        expectation = query.expectation or {}
        excluding = bool(variant.excluded_record_ids)
        fetch = self.max_results * OVERFETCH if excluding else self.max_results
        started = time.monotonic()
        try:
            payload = self.searcher.search_context(
                self.kb_id,
                query.text,
                mode=self.mode,
                max_results=fetch,
                graph=self.graph,
                security=self.security,
                principal=expectation.get("principal"),
                memory=self._policy(variant, query),
                exclude_sources=self.artifact_sources if variant.memory_results == "off" else None,
                steering_kinds=variant.steering_kinds,
                explain=self.explain,
                extra_steering_records=(
                    shadow_records(self.shadow, now=query.occurred_at or "")
                    if variant.candidate_ids
                    else None
                ),
            )
        except Exception as exc:  # noqa: BLE001 - one query must not fail a run
            # Recorded, never silently dropped: a partial replay has to name
            # the queries it could not complete, because excluding them from a
            # paired comparison without saying so changes the denominator.
            logger.warning(
                "evaluation: replay failed for %s under %s", query.query_id, variant.variant_id
            )
            result.failed = f"{type(exc).__name__}: {exc}"
            result.duration_ms = (time.monotonic() - started) * 1000.0
            return result
        result.duration_ms = (time.monotonic() - started) * 1000.0

        items = list(payload.get("results") or [])
        if excluding:
            excluded = set(variant.excluded_record_ids)
            items = [
                item
                for item in items
                if str((item.get("memory") or {}).get("record_id") or "") not in excluded
            ]
        items = items[: self.max_results]

        for item in items:
            node_id = str(item.get("node_id") or item.get("chunk_id") or "")
            if not node_id:
                continue
            result.ranked_ids.append(node_id)
            path = str(item.get("relative_path") or item.get("path") or "")
            result.ranked_paths.append(path)
            score = item.get("score")
            if score is not None:
                result.scores[node_id] = float(score)
            retrieved_by = str(item.get("retrieved_by") or "")
            if retrieved_by:
                result.contributing_arms[node_id] = retrieved_by.split("+")
            record_id = str((item.get("memory") or {}).get("record_id") or "")
            if record_id:
                result.memory_record_ids.append(record_id)
        result.result_count = len(result.ranked_ids)
        counts = payload.get("counts") or {}
        stages = payload.get("stages") or {}
        result.stages = dict(stages)
        # With `explain` on these are the arms' real ranked lists; without it
        # there is nothing to fill them with, and the arm *names* (which is
        # all `counts` can give) are still worth recording, because "the
        # vector arm contributed nothing" is a fact a metric may want.
        surviving = stages.get("surviving") or {}
        result.arms = {
            name: [str(item) for item in surviving.get(name, [])]
            for name in ("text", "vector", "graph")
            if int(counts.get(name) or 0) > 0 or surviving.get(name)
        }
        result.steering_applied = dict(payload.get("memory_steering") or {})
        return result

    def replay_variant(self, cohort: Cohort, variant: Variant) -> VariantReplay:
        run = VariantReplay(variant=variant, cohort_id=cohort.cohort_id)
        for query in cohort.queries:
            replayed = self.replay_query(variant, query)
            run.results[query.query_id] = replayed
            if replayed.failed:
                run.failures[query.query_id] = replayed.failed
        return run

    def replay_matrix(self, cohort: Cohort, variants: list[Variant]) -> dict[str, VariantReplay]:
        return {variant.variant_id: self.replay_variant(cohort, variant) for variant in variants}


def paired_ids(
    baseline: VariantReplay, treatment: VariantReplay
) -> tuple[list[str], dict[str, int]]:
    """Query ids that completed in **both** runs, plus why the rest were dropped.

    A paired comparison is only defined over queries both sides answered. The
    excluded count and its reasons travel with every delta metric, because a
    treatment that improved by 0.2 on the 40 queries it did not crash on is a
    different claim from one that improved by 0.2 on all 50.
    """

    both = sorted(baseline.completed_ids & treatment.completed_ids)
    reasons: dict[str, int] = {}
    for qid in set(baseline.results) | set(treatment.results):
        if qid in both:
            continue
        if qid in baseline.failures and qid in treatment.failures:
            reasons["failed_in_both"] = reasons.get("failed_in_both", 0) + 1
        elif qid in baseline.failures:
            reasons["failed_in_baseline"] = reasons.get("failed_in_baseline", 0) + 1
        elif qid in treatment.failures:
            reasons["failed_in_treatment"] = reasons.get("failed_in_treatment", 0) + 1
        else:
            reasons["absent_from_one_run"] = reasons.get("absent_from_one_run", 0) + 1
    return both, reasons
