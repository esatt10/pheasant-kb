"""The read side: lineage, resolution, memory state, latency.

What a result has to be able to say about itself before an outside harness can
cite it — which snapshot, which profile, which memory state, and an id that
resolves back to the content it named.
"""

from __future__ import annotations

import time

from pheasant.readiness.probes import (
    PROBE_MARKER,
    NotRunnable,
    ProbeContext,
    ProbeResult,
    probe,
)
from pheasant.services import retrieval as retrieval_service
from pheasant.services.retrieval import memory_enabled


@probe("retrieval_lineage")
def _retrieval_lineage(context: ProbeContext) -> ProbeResult:
    """Every field the specification's retrieval contract requires is present.

    Checked as a field list rather than by eyeballing one response, because
    what goes wrong here is a key quietly disappearing when some optional
    subsystem is off — `memory_policy` was already reported only when the
    region held memory, which is right for that key and would be wrong for
    these.
    """

    payload = retrieval_service.search(
        context.services,
        retrieval_service.SearchRequest(
            query=PROBE_MARKER, mode="hybrid", max_results=5, principal="user:readiness"
        ),
    )
    lineage = payload.get("lineage") or {}
    state = lineage.get("state") or {}
    timing = lineage.get("timing") or {}
    required_top = ("query_id", "knowledge_base", "state", "timing")
    required_state = (
        "snapshot_id",
        "graph_generation",
        "ranking",
        "memory",
        "as_of",
        "principal",
        "acl_enforced",
        "criteria",
        "mode",
        "max_results",
    )
    required_timing = ("retrieval_ms", "truncated", "returned")
    missing = (
        [name for name in required_top if name not in lineage]
        + [f"state.{name}" for name in required_state if name not in state]
        + [f"timing.{name}" for name in required_timing if name not in timing]
    )
    ranking = state.get("ranking") or {}
    locators = [
        result
        for result in payload.get("results") or []
        if result.get("node_id") and result.get("relative_path")
    ]
    passed = not missing and "bundle_id" in ranking and bool(locators)
    return ProbeResult(
        "retrieval_lineage",
        passed,
        (
            "every lineage field is present and results carry locators"
            if passed
            else f"lineage is incomplete: {', '.join(missing) or 'no result carried a locator'}"
        ),
        observed={
            "query_id": lineage.get("query_id"),
            "missing": missing,
            "profile": {
                "bundle_id": ranking.get("bundle_id"),
                "provenance": ranking.get("provenance"),
            },
            "results_with_locators": len(locators),
        },
    )


@probe("resolve_result")
def _resolve_result(context: ProbeContext) -> ProbeResult:
    """A returned id resolves back to the persisted content it named."""

    from pheasant.services import graph as graph_service

    payload = retrieval_service.search(
        context.services,
        retrieval_service.SearchRequest(query=PROBE_MARKER, mode="text", max_results=5),
    )
    results = payload.get("results") or []
    if not results:
        raise NotRunnable("no probe document is retrievable yet; run ingest_roundtrip first")
    node_id = results[0]["node_id"]
    explained = graph_service.explain_node(context.services, node_id)
    rows = context.state.rows(
        "SELECT text FROM chunks WHERE artifact_id=? ORDER BY chunk_index LIMIT 1", (node_id,)
    )
    text = str(rows[0]["text"]) if rows else ""
    passed = bool(explained) and PROBE_MARKER in text
    return ProbeResult(
        "resolve_result",
        passed,
        (
            "a result id resolved to its persisted content"
            if passed
            else "a result id did not resolve to content carrying the marker"
        ),
        observed={
            "node_id": node_id,
            "resolved": bool(explained),
            "content_matched": PROBE_MARKER in text,
        },
    )


@probe("memory_state_reported")
def _memory_state_reported(context: ProbeContext) -> ProbeResult:
    """Every query says which memory state answered it.

    Reported even when the region holds no memory, which is the case a
    memory-off arm has to be able to *record*: an absent key is
    indistinguishable from a key nobody looked at, and a P0 result that cannot
    prove memory was off is not a baseline.
    """

    payload = retrieval_service.search(
        context.services,
        retrieval_service.SearchRequest(query=PROBE_MARKER, mode="hybrid", max_results=5),
    )
    memory = ((payload.get("lineage") or {}).get("state") or {}).get("memory")
    passed = isinstance(memory, dict) and "enabled" in memory
    return ProbeResult(
        "memory_state_reported",
        passed,
        (
            f"memory state reported as enabled={memory.get('enabled')}"
            if passed
            else "no memory state on the response"
        ),
        observed={"memory": memory},
    )


@probe("memory_export")
def _memory_export(context: ProbeContext) -> ProbeResult:
    """Memory records and candidates are enumerable for audit."""

    if not memory_enabled(context.services):
        raise NotRunnable(
            "this region has no enabled memory source, so there is nothing to "
            "export. Register one (POST /memory/enable) before the memory arm."
        )
    records = context.state.rows("SELECT COUNT(*) AS c FROM memory_records WHERE 1=1", ())
    candidates = context.state.rows("SELECT COUNT(*) AS c FROM memory_candidates WHERE 1=1", ())
    return ProbeResult(
        "memory_export",
        True,
        "memory records and candidates are enumerable",
        observed={
            "records": int(records[0]["c"]) if records else 0,
            "candidates": int(candidates[0]["c"]) if candidates else 0,
        },
    )


@probe("memory_as_of")
def _memory_as_of(context: ProbeContext) -> ProbeResult:
    """`as_of` names an instant, and the response reports which one answered.

    Deliberately modest. This establishes that the parameter travels and is
    echoed — the corpus half of temporal replay is declared *unsupported* in
    the contract, and a probe that implied otherwise by passing would be worse
    than no probe at all.
    """

    if not memory_enabled(context.services):
        raise NotRunnable(
            "this region has no enabled memory source, so validity has nothing to replay."
        )
    instant = "2000-01-01T00:00:00Z"
    payload = retrieval_service.search(
        context.services,
        retrieval_service.SearchRequest(
            query=PROBE_MARKER, mode="hybrid", max_results=5, as_of=instant
        ),
    )
    echoed = ((payload.get("lineage") or {}).get("state") or {}).get("as_of")
    return ProbeResult(
        "memory_as_of",
        echoed == instant,
        (
            "as_of travelled and is echoed on the response"
            if echoed == instant
            else f"as_of was not echoed (got {echoed!r})"
        ),
        observed={"requested": instant, "echoed": echoed},
    )


@probe("search_latency")
def _search_latency(context: ProbeContext) -> ProbeResult:
    """p95 search latency against the configured budget.

    Below ``latency_probe_queries`` this publishes nothing rather than a p95
    over four searches — the floor `tuning.health` already keeps, for the same
    reason: a confident number from too few samples invites exactly the wrong
    action.
    """

    settings = context.settings()
    budget = float(getattr(settings, "max_search_latency_ms", 0.0) or 0.0)
    if budget <= 0:
        raise NotRunnable("readiness.max_search_latency_ms is not configured")
    count = int(getattr(settings, "latency_probe_queries", 12) or 12)
    if count < 5:
        raise NotRunnable(
            f"readiness.latency_probe_queries is {count}; a p95 over that many "
            "searches is a statement about one of them"
        )
    samples: list[float] = []
    for index in range(count):
        started = time.perf_counter()
        retrieval_service.search(
            context.services,
            retrieval_service.SearchRequest(
                query=f"{PROBE_MARKER} case {index}", mode="hybrid", max_results=10
            ),
        )
        samples.append((time.perf_counter() - started) * 1000.0)
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(round(0.95 * (len(samples) - 1))))]
    p50 = samples[len(samples) // 2]
    return ProbeResult(
        "search_latency",
        p95 <= budget,
        (
            f"p95 {p95:.0f}ms against a {budget:.0f}ms budget"
            if p95 <= budget
            else f"p95 {p95:.0f}ms is over the {budget:.0f}ms budget"
        ),
        observed={
            "queries": len(samples),
            "p50_ms": round(p50, 1),
            "p95_ms": round(p95, 1),
            "budget_ms": budget,
        },
    )
