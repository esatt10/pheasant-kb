"""The four go/no-go decisions, and the rule that stops them being vacuous.

The readiness specification's §7 is four checklists — core, swarm, memory,
tuning — and its primary decision is that no scored experiment begins until the
applicable one passes. That makes these gates the whole point of the plane,
and it makes the way they are *combined* the thing most worth getting right.

``pheasant.decision.GateSet`` is that combination, and it is used here rather
than reimplemented for a reason recorded in its own docstring: the evaluation
plane shipped ``all([])`` returning True for a run that never happened, the
tuning plane then shipped its own copy of the guard citing that incident, and
a third plane starting from scratch had a one-in-two chance of repeating the
original bug. This is that third plane. It constructs a `GateSet`, which
cannot be built empty, and "no gates were evaluated" is the *absence* of one.

**A skipped probe does not pass a gate.** A gate whose probes all reported
``skipped`` is itself skipped, and a gate set with nothing in it is `None`,
which has no ``passed`` to misread. That is the same failure the evaluation
plane's interrupted-run incident produced — a run that skipped reported that
its gates passed, straight into an exit status — and the reason it cannot
recur here is structural rather than remembered.

**A gate is not a metric.** Nothing here is averaged, weighted or offset. An
isolation crossing is not compensated for by good latency, and a plane that
allowed the arithmetic would be the plane the specification was written to
avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pheasant.decision import Gate, GateSet

#: A gate: which probes decide it, and what a failure means. `blocking` marks
#: the ones that may stop an experiment; the rest are published and do not veto.
#:
#: The zero-tolerance rows come straight from the specification's §3.6 and are
#: blocking without exception: ACL leakage, namespace crossing, temporal
#: failure and benchmark contamination are the four things a result cannot be
#: corrected for after the fact.


@dataclass(frozen=True)
class GateSpec:
    gate_id: str
    summary: str
    probes: tuple[str, ...]
    blocking: bool = True
    #: Whether every named probe must pass, or any one of them suffices. `any`
    #: is for a capability with two independent demonstrations, never for a
    #: convenient way to make a failing probe stop mattering.
    require: str = "all"


#: The gate sets, keyed as the specification names them.
GATE_SETS: dict[str, tuple[GateSpec, ...]] = {
    "core": (
        GateSpec(
            "service_identity",
            "Health, readiness and the exact server version are captured.",
            ("service_identity",),
        ),
        GateSpec(
            "mcp_discovery",
            "MCP initialization and tool discovery pass.",
            ("mcp_discovery",),
        ),
        GateSpec(
            "ingest_to_retrieval",
            "One source travels registration → indexed receipt → retrievable locator.",
            ("ingest_roundtrip",),
        ),
        GateSpec(
            "idempotency",
            "Three writes under one key produce one stored object.",
            ("idempotent_write",),
        ),
        GateSpec(
            "partial_failure",
            "A batch reports item-level disposition and stays reconcilable.",
            ("partial_failure",),
        ),
        GateSpec(
            "index_barrier",
            "Acceptance and searchability are distinguishable, and the lag is bounded.",
            ("index_barrier",),
        ),
        GateSpec(
            "reconciliation",
            "Every submitted item reconciles; silent-loss count is zero.",
            ("reconciliation",),
        ),
        GateSpec(
            "snapshot_immutable",
            "A sealed snapshot is resolvable, and ingestion after it cannot be "
            "silently served as that snapshot.",
            ("snapshot_seal", "snapshot_drift_refused"),
        ),
        GateSpec(
            "retrieval_lineage",
            "Results expose stable ids, locators, snapshot, profile and trace.",
            ("retrieval_lineage",),
        ),
        GateSpec(
            "resolve_result",
            "A returned id resolves to exact persisted content.",
            ("resolve_result",),
        ),
        GateSpec(
            "benchmark_contamination",
            "Evaluation artifacts are refused entry to the searchable corpus.",
            ("contamination_refused",),
        ),
        GateSpec(
            "structured_errors",
            "Refusals carry stable codes and a retryability flag.",
            ("structured_errors",),
        ),
        GateSpec(
            "performance_slo",
            "Latency and index-lag budgets are configured and met.",
            ("search_latency", "index_barrier"),
        ),
        GateSpec(
            "memory_state_visible",
            "Every query reports which memory state answered it — including 'off'.",
            ("memory_state_reported",),
        ),
    ),
    "swarm": (
        GateSpec(
            "concurrent_writers",
            "Bounded concurrent ingestion loses, duplicates and cross-links nothing.",
            ("concurrent_writers",),
        ),
        GateSpec(
            "reconciliation_under_load",
            "Reconciliation still balances after concurrent submission.",
            ("reconciliation",),
        ),
    ),
    "memory": (
        GateSpec(
            "memory_state_visible",
            "Memory off/on and its effective state are returned per query.",
            ("memory_state_reported",),
        ),
        GateSpec(
            "memory_auditable",
            "Inferred records and candidates are enumerable for audit.",
            ("memory_export",),
        ),
        GateSpec(
            "memory_temporal",
            "`as_of` travels and is echoed, so a replay can name its instant.",
            ("memory_as_of",),
        ),
        GateSpec(
            "principal_isolation",
            "Nothing one principal wrote appears for another.",
            ("principal_isolation",),
        ),
    ),
    "tuning": (
        GateSpec(
            "profile_reported",
            "The effective retrieval profile id is returned for every query.",
            ("retrieval_lineage",),
        ),
        GateSpec(
            "snapshot_shared",
            "Baseline and treatment can run against one frozen knowledge snapshot.",
            ("snapshot_seal",),
        ),
    ),
}


def evaluate_gates(
    gate_set: str, results: dict[str, Any]
) -> tuple[GateSet | None, list[dict[str, Any]]]:
    """Turn probe results into one gate set, or into ``None``.

    ``results`` maps probe id to a `ProbeResult`. A gate whose probes did not
    all run is *skipped* — reported, not counted — and a gate set with no
    evaluated gates returns ``None`` rather than an empty `GateSet`, because
    the constructor refuses to build one and because a caller holding ``None``
    cannot ask it whether it passed.

    The skipped list comes back separately so a report can say "four gates
    could not be evaluated, here is why" — which is the sentence an operator
    needs and the one a boolean cannot carry.
    """

    if gate_set not in GATE_SETS:
        raise KeyError(f"unknown gate set: {gate_set}")
    gates: list[Gate] = []
    skipped: list[dict[str, Any]] = []
    for spec in GATE_SETS[gate_set]:
        available = [results[name] for name in spec.probes if name in results]
        ran = [result for result in available if getattr(result, "ran", False)]
        if len(ran) != len(spec.probes):
            skipped.append(
                {
                    "gate_id": spec.gate_id,
                    "summary": spec.summary,
                    "reason": _skip_reason(spec, results),
                }
            )
            continue
        outcomes = [bool(result.passed) for result in ran]
        passed = all(outcomes) if spec.require == "all" else any(outcomes)
        gates.append(
            Gate(
                gate_id=spec.gate_id,
                passed=passed,
                summary=spec.summary,
                blocking=spec.blocking,
                observed={result.probe_id: result.summary for result in ran},
                threshold=spec.require,
                evidence={result.probe_id: result.observed for result in ran},
            )
        )
    return GateSet.of(gates), skipped


def _skip_reason(spec: GateSpec, results: dict[str, Any]) -> str:
    missing = [name for name in spec.probes if name not in results]
    if missing:
        return f"probes not run: {', '.join(missing)}"
    reasons = [
        f"{name}: {results[name].skipped_reason}"
        for name in spec.probes
        if name in results and not getattr(results[name], "ran", False)
    ]
    return "; ".join(reasons) or "no probe produced a result"
