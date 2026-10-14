"""What each retrieval stage did, emitted from the live path.

The tuning plane can already attribute a miss to the stage that caused it —
but only inside a *replay*, with `explain=True`, over a cohort. That leaves
production retrieval with two signals (`pheasant_search_duration_seconds` and
`pheasant_search_total`), neither of which can name a stage, and it leaves a
region's diagnosis only as fresh as its last batch. A ranking regression
introduced by an applied bundle is then invisible until somebody runs another
one, which is exactly the window in which you would want to notice.

So stage observation is split in two, by cost:

**Always on: counters.** Every search increments in-memory counters for each
arm's outcome and candidate count, each filter's drops, the fusion's arm
agreement, and — when a search returns nothing — the last stage that still had
candidates. This is the live version of the tuning plane's histogram. It costs
a dict lookup and an integer add per search against the registry that already
serves `/metrics`; **no database write reaches the request path**, which is the
rule the observation plane's hot tier exists to keep (a ledger write per
request puts a write on the same Postgres the lexical arm already contends on).

**Sampled: the stage digest.** A fraction of searches attach a compact
per-stage summary to their interaction-ledger row. That gives the tuning plane
a *live* diagnosis source alongside replay, and gives an operator a real query
to look at when a counter moves. It is sampled rather than universal because
the digest is ~40x the bytes of the row it rides on, and because a ledger sized
for search traffic should not be resized by a diagnostic.

Two things this deliberately does **not** do.

It does not record what was retrieved beyond ids already in the row. The digest
is counts and stage names — never chunk text, never a new copy of the results.

It does not turn a sampled search into evidence. A stage digest says the
pipeline did something; it says nothing about whether the answer was useful.
Proof still only comes from a surface where somebody said so, and mining "this
was served at rank 1" out of these counters would produce a metric that
improves whenever ranking gets more confident regardless of whether it gets
more correct.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Arms in the order the merge walks them.
ARMS: tuple[str, ...] = ("text", "vector", "graph")

#: The stage an empty result is attributed to, in the order it is decided.
#: Mirrors `pheasant.tuning.stages.STAGES`, collapsed to what a *live* search
#: can know without a corpus lookup or a known positive: nobody has told this
#: query what the right answer was, so "no arm had it" is as far as it goes.
EMPTY_STAGES: tuple[str, ...] = ("no_candidates", "filters", "fusion")


def observe_search(payload: dict[str, Any], stages: dict[str, Any] | None) -> None:
    """Emit stage counters for one completed search. Never raises.

    Best-effort by construction: a telemetry failure must never cost a query,
    and the alternative — a metrics bug taking down search — is a worse outcome
    than a gap in a dashboard.
    """

    try:
        _emit(payload, stages)
    except Exception:  # noqa: BLE001 - telemetry must never fail a search
        logger.debug("retrieval telemetry failed", exc_info=True)


def _emit(payload: dict[str, Any], stages: dict[str, Any] | None) -> None:
    from pheasant.telemetry import metrics

    registry = metrics.REGISTRY
    counts = payload.get("counts") or {}
    results = payload.get("results") or []

    # --- arms ------------------------------------------------------------
    # `counts` is present on every search; `stages` only when a caller asked
    # to explain. So the arm counters work without the diagnostic, and the
    # diagnostic adds the detail counts cannot carry (which arm *failed*
    # rather than merely came back empty).
    failed = set((stages or {}).get("arms_failed") or [])
    ran = set((stages or {}).get("arms_run") or []) or {arm for arm in ARMS if arm in counts}
    for arm in ARMS:
        if arm not in ran and arm not in counts:
            continue
        size = int(counts.get(arm) or 0)
        outcome = "failed" if arm in failed else ("ok" if size else "empty")
        registry.inc("pheasant_retrieval_arm_total", arm=arm, outcome=outcome)
        registry.observe("pheasant_retrieval_arm_candidates", float(size), arm=arm)

    # --- fusion ----------------------------------------------------------
    for item in results:
        arms = str(item.get("retrieved_by") or "")
        if arms:
            registry.inc("pheasant_retrieval_fusion_contributions_total", arms=arms)

    if not stages:
        # Without the explain block there is no filter accounting and no
        # pre-truncation depth. Emit what `counts` alone supports and stop
        # rather than inventing the rest.
        if not results:
            registry.inc(
                "pheasant_retrieval_empty_total",
                stage="no_candidates" if not any(counts.get(a) for a in ARMS) else "fusion",
            )
        return

    # --- filters ---------------------------------------------------------
    for name, per_arm in (stages.get("filters") or {}).items():
        for arm, dropped in (per_arm or {}).items():
            if dropped:
                registry.inc(
                    "pheasant_retrieval_filtered_total",
                    float(len(dropped)),
                    filter=str(name),
                    arm=str(arm),
                )

    # --- fusion depth and truncation --------------------------------------
    fused = list((stages.get("fusion") or {}).get("ranked") or [])
    registry.observe("pheasant_retrieval_fusion_depth", float(len(fused)))
    if len(fused) > len(results):
        registry.inc("pheasant_retrieval_truncated_total")

    if not results:
        registry.inc("pheasant_retrieval_empty_total", stage=empty_stage(stages))


def empty_stage(stages: dict[str, Any]) -> str:
    """The last stage that still had candidates, for a search that returned none.

    Walked in pipeline order and stopped at the first stage that *had*
    something, so the attribution is to the step that lost it rather than to
    every step downstream — the same rule the tuning plane's attribution
    follows, and for the same reason: blaming all of them makes the totals
    exceed the misses and every stage look guilty.
    """

    candidates = stages.get("candidates") or {}
    if not any(candidates.get(arm) for arm in ARMS):
        return "no_candidates"
    surviving = stages.get("surviving") or {}
    if not any(surviving.get(arm) for arm in ARMS):
        return "filters"
    return "fusion"


def stage_digest(payload: dict[str, Any], stages: dict[str, Any] | None) -> dict[str, Any]:
    """A compact per-stage summary, small enough to ride on a ledger row.

    Counts and stage names only — never chunk text, and never a second copy of
    the result ids the row already carries. On a typical search this is a few
    hundred bytes against a row that is already ~2 KB, which is what makes
    sampling a rate rather than a hard choice between "on" and "affordable".
    """

    counts = payload.get("counts") or {}
    digest: dict[str, Any] = {
        "returned": len(payload.get("results") or []),
        "arms": {arm: int(counts.get(arm) or 0) for arm in ARMS if arm in counts},
    }
    if not stages:
        return digest
    digest["arms_failed"] = sorted(stages.get("arms_failed") or [])
    filters = {
        name: sum(len(dropped) for dropped in (per_arm or {}).values())
        for name, per_arm in (stages.get("filters") or {}).items()
    }
    if any(filters.values()):
        digest["filtered"] = {name: n for name, n in filters.items() if n}
    fused = list((stages.get("fusion") or {}).get("ranked") or [])
    digest["fused_depth"] = len(fused)
    digest["truncated"] = len(fused) > digest["returned"]
    parameters = stages.get("parameters") or {}
    # The bundle the search ranked under, so a stage regression can be joined
    # to the configuration change that caused it. This is the whole reason a
    # digest is worth storing rather than only counting.
    if parameters.get("bundle_id"):
        digest["bundle_id"] = parameters["bundle_id"]
    digest["provenance"] = parameters.get("provenance", "")
    if not digest["returned"]:
        digest["empty_stage"] = empty_stage(stages)
    return digest


def should_sample(rate: float, trace_id: str) -> bool:
    """Deterministic per-trace sampling.

    Keyed on the trace id rather than a random draw so that every hop of one
    call agrees about whether it is sampled. A random decision per hop would
    produce traces where the search is sampled and the assistant call wrapping
    it is not, which is the shape that makes a sampled dataset unjoinable.
    """

    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    if not trace_id:
        return False
    # Hashed over the **whole** id, not sliced out of its low bits.
    #
    # The first version read the last four hex characters, on the reasoning
    # that a W3C trace id is random and any slice of it is uniform. That is
    # true of ids pheasant mints and false in general: a trace id can arrive
    # from an upstream `traceparent` header, and an SDK that derives ids from a
    # counter or a timestamp leaves the low bits almost constant. Sampling then
    # collapses to all-or-nothing — which is the worst possible failure for a
    # sampler, because it looks like it is working.
    #
    # Found by a stress test feeding sequential ids: at rate 0.25 every one of
    # 400 was sampled.
    digest = hashlib.blake2b(trace_id.encode("utf-8"), digest_size=8).digest()
    return (int.from_bytes(digest, "big") / float(1 << 64)) < rate


def count_lexical(index: str, path: str) -> None:
    """Which lexical mechanism ranked a search. Never raises.

    The text arm has seven ways to answer and they return the same shape, so
    a region quietly ranking on ``tsvector`` (a bundle moved a column weight)
    or reaching the unfenced statement on every search looks, from outside,
    exactly like one on the BM25 index — until somebody times it.
    """

    try:
        from pheasant.telemetry import metrics

        metrics.REGISTRY.inc("pheasant_retrieval_lexical_total", index=index, path=path)
    except Exception:  # noqa: BLE001 - telemetry must never fail a search
        logger.debug("lexical telemetry failed", exc_info=True)
