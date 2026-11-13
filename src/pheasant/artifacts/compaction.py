"""Phase 3/6 — deterministic near-duplicate clustering and medoid promotion.

L1 (cluster) + L2 (select): the offline, reversible half of compaction.
Unlike L0 (`memory.normalize`), which runs on the write path and may only
fold *exact* equivalents, this stage is allowed to be fuzzy — its output is
a **tier change**, never a deletion. A cluster's losers are marked
``tier='cold'`` and ``subsumed_by=<medoid>`` in SQL; their `.md` files are
never touched, never renamed, and stay fully indexed and reachable via an
explicit tier filter or `current_only=False`/`as_of` (`memory.policy`).
Nothing here removes text from the store.

Bucketing mirrors L0's ACL constraint exactly: never across scope, never
across kind (steering records are retrieval machinery, exempt from
clustering the same way `memory.maintenance._prune_to_capacity` exempts
them from the capacity cap), never across ACL partition
(`memory.normalize.acl_class_for` — the same partition
`security.acl.normalize_acl` enforces at read time).

Candidate generation reuses the postings + exact-Jaccard shape already
written — and proven — at `graph.enrichment._similarity_edges`, dormant
there only because it keys off the retired `concept_terms`. Reused here on
normalized content tokens instead: buckets are small by construction (one
region, one scope/subject/kind/ACL combination), so an inverted index plus
exact Jaccard needs no probabilistic signature (MinHash/LSH), and every
decision it makes is exact rather than approximate.

Determinism throughout: bucket keys and record ids are iterated in sorted
order, every similarity score is rounded to 6 decimals before comparison
(the same precision `memory.salience.salience` already commits to, since a
sum of ratios is order-dependent in the last bit otherwise), and every tie
— which pair links, which record is the medoid — breaks on the lowest
`record_id`. `datetime.now()` never appears here; `now` is always threaded
in by the caller, exactly as `MemoryStore.consolidate` requires.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pheasant.artifacts.normalize import NORMALIZER_VERSION, acl_class_for, normalized_text
from pheasant.artifacts.policy import STEERING_KINDS
from pheasant.artifacts.store import MemoryRecord

#: Recorded in every ledger row this module writes (`memory_compactions
#: .rule_id`) so a reader — or a later, different clustering rule — can
#: tell which algorithm produced a given subsumption.
RULE_ID = "jaccard-medoid-v1"

#: Same character class as `memory.steering._TRIGGER_SPLIT` (not imported —
#: that name is private to its module, and content tokenization is a
#: different concern from trigger tokenization even though the shape
#: happens to match). A run of anything that is not `[A-Za-z0-9]` splits
#: tokens; single characters are dropped as noise.
_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")

DEFAULT_SIMILARITY_THRESHOLD = 0.6
DEFAULT_MIN_CLUSTER_SIZE = 2


def _tokens(text: str) -> frozenset[str]:
    """The comparable token set for clustering: L0's normalized text,
    split. Reusing `normalized_text` means a cluster can never separate two
    records that reinforcement would already have folded into one — L1
    only ever sees genuinely distinct surface forms."""
    normalized = normalized_text(text)
    return frozenset(part for part in _TOKEN_SPLIT.split(normalized) if len(part) > 1)


def _bucket_key(record: MemoryRecord) -> tuple[str, str, str, str]:
    return (
        record.scope,
        record.subject or "",
        record.kind,
        acl_class_for(record.scope, record.written_by),
    )


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    shared = len(a & b)
    if not shared:
        return 0.0
    union_size = len(a | b)
    return round(shared / union_size, 6) if union_size else 0.0


def mean_pairwise_jaccard(texts: list[str]) -> float:
    """Average exact Jaccard over every pair in `texts`, or 0.0 below two.

    How alike a group already is, as one number. Lives here rather than in
    `memory.synthesis` so the L3 gate measures similarity with exactly the
    tokenizer and normalizer L1 clusters with — a gate reading "these are
    near-duplicates, medoid promotion has them" has to agree with the tier
    that actually does the promoting, or it would wave through the clusters
    L2 already solves and skip the ones it cannot.

    Rounded to 6 decimals like every other score in this module: a mean of
    ratios is order-dependent in the last bit, and a gate that flipped
    between runs on the same content would make a pass non-reproducible.
    """
    if len(texts) < 2:
        return 0.0
    token_sets = [_tokens(text) for text in texts]
    scores = [
        _jaccard(token_sets[i], token_sets[j])
        for i in range(len(token_sets))
        for j in range(i + 1, len(token_sets))
    ]
    return round(sum(scores) / len(scores), 6) if scores else 0.0


def _connected_components(pairs: set[tuple[str, str]], node_ids: list[str]) -> list[list[str]]:
    """Union-find over `pairs`. Deterministic regardless of `pairs`' own
    iteration order: rooting always attaches the lexicographically larger
    id under the smaller, so the same edge set always produces the same
    component roots."""
    parent = {node_id: node_id for node_id in node_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in sorted(pairs):
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        if ra < rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    groups: dict[str, list[str]] = {}
    for node_id in node_ids:
        groups.setdefault(find(node_id), []).append(node_id)
    return [sorted(members) for members in groups.values() if len(members) > 1]


@dataclass(frozen=True)
class ClusterPlan:
    """One near-duplicate cluster's compaction decision."""

    canonical_id: str
    member_ids: tuple[str, ...]  # excludes canonical_id, sorted
    #: Cluster-wide observation credit the canonical record absorbs: each
    #: member's own accumulated `observations` (Phase 1 reinforcement)
    #: plus one for the member itself having existed as a competing
    #: near-duplicate — importance concentrates on the survivor instead of
    #: being lost when its duplicates are demoted.
    absorbed_observations: int


def find_clusters(
    records: list[MemoryRecord],
    observations: dict[str, int],
    tiers: dict[str, str],
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
) -> list[ClusterPlan]:
    """Bucket, cluster, and pick one medoid per qualifying cluster.

    ``records`` should be every currently-live record from one parse of the
    store (the same list `memory.maintenance` already builds once per pass,
    per Phase 0 — clustering needs record *text*, which exists only in the
    files, never in SQL). Steering kinds and already-cold records are
    filtered out **here**, not left to the caller, so a caller cannot
    forget the exemption `_prune_to_capacity` already established for
    steering, or accidentally re-cluster something already subsumed.
    """
    live = [
        record
        for record in records
        if record.kind not in STEERING_KINDS and tiers.get(record.record_id, "hot") == "hot"
    ]
    buckets: dict[tuple[str, str, str, str], list[MemoryRecord]] = {}
    for record in live:
        buckets.setdefault(_bucket_key(record), []).append(record)

    plans: list[ClusterPlan] = []
    for key in sorted(buckets):
        members = buckets[key]
        if len(members) < 2:
            continue
        token_sets = {record.record_id: _tokens(record.text) for record in members}

        # Postings index -> sparse candidate pairs, the same shape as
        # graph.enrichment._similarity_edges. This is what keeps candidate
        # generation sub-quadratic in bucket size; it only needs to find
        # candidates, not compute the medoid (that step is exhaustive,
        # below, over the much smaller resulting components).
        postings: dict[str, set[str]] = {}
        for record_id, toks in token_sets.items():
            for tok in toks:
                postings.setdefault(tok, set()).add(record_id)

        candidate_pairs: set[tuple[str, str]] = set()
        seen: set[tuple[str, str]] = set()
        for record_id, toks in token_sets.items():
            candidates = {c for tok in toks for c in postings.get(tok, ()) if c != record_id}
            for candidate_id in candidates:
                pair = (
                    (record_id, candidate_id)
                    if record_id < candidate_id
                    else (
                        candidate_id,
                        record_id,
                    )
                )
                if pair in seen:
                    continue
                seen.add(pair)
                if _jaccard(token_sets[pair[0]], token_sets[pair[1]]) >= similarity_threshold:
                    candidate_pairs.add(pair)
        if not candidate_pairs:
            continue

        node_ids = sorted(token_sets)
        for component in _connected_components(candidate_pairs, node_ids):
            if len(component) < min_cluster_size:
                continue
            # Exhaustive pairwise similarity within the component for an
            # exact medoid — components are small by construction (one
            # region's one bucket), so this is cheap, and it is more
            # correct than summing only the sparse discovered edges: two
            # members can be transitively linked through a third with no
            # direct term overlap of their own.
            sums: dict[str, float] = dict.fromkeys(component, 0.0)
            for i, a in enumerate(component):
                for b in component[i + 1 :]:
                    score = _jaccard(token_sets[a], token_sets[b])
                    sums[a] += score
                    sums[b] += score
            # Medoid: max summed similarity, ties -> lowest record_id — the
            # same total-order tie-break `memory.salience.rank` uses.
            medoid = min(component, key=lambda rid: (-round(sums[rid], 6), rid))
            others = sorted(rid for rid in component if rid != medoid)
            absorbed = sum(observations.get(rid, 0) for rid in others) + len(others)
            plans.append(
                ClusterPlan(
                    canonical_id=medoid,
                    member_ids=tuple(others),
                    absorbed_observations=absorbed,
                )
            )
    return plans


def params_hash(*, similarity_threshold: float, min_cluster_size: int) -> str:
    """Stamped into every ledger row this pass writes. Changing a parameter
    (or the normalizer version clustering's tokens depend on) changes the
    hash, so a re-run under different settings is distinguishable from one
    that reproduces a prior pass's decisions rather than silently mixed
    with it — the same discipline `NORMALIZER_VERSION` itself follows."""
    key = (
        f"{RULE_ID}|norm={NORMALIZER_VERSION}|threshold={similarity_threshold}|"
        f"min_cluster={min_cluster_size}"
    )
    return hashlib.blake2b(key.encode("utf-8"), digest_size=8).hexdigest()


def run_compaction(
    engine: Any,
    records: list[MemoryRecord],
    settings: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One compaction pass: cluster, promote a medoid, demote the rest.

    Pure SQL write-back — no file is renamed or rewritten, so this needs no
    re-sync of the memory source afterward (contrast `MemoryStore.archive`,
    which does). Only the graph needs telling: a `subsumes` edge is drawn
    beside `supersedes` by the same idempotent `add_memory_edges` pass that
    already runs on every `_bridge_memory` beat, called directly here too
    so a subsumption is reflected in the graph without waiting for
    something else to mark the source enrichment-dirty (Phase 0) — nothing
    about a pure metadata write does that on its own.
    """
    instant = (now or datetime.now(tz=UTC)).astimezone(UTC)
    threshold = float(
        getattr(settings, "compaction_similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD)
        or DEFAULT_SIMILARITY_THRESHOLD
    )
    min_size = int(
        getattr(settings, "compaction_min_cluster_size", DEFAULT_MIN_CLUSTER_SIZE)
        or DEFAULT_MIN_CLUSTER_SIZE
    )
    rows = engine.state.memory_compaction_rows()
    observations = {str(row["record_id"]): int(row.get("observations") or 0) for row in rows}
    tiers = {str(row["record_id"]): str(row.get("tier") or "hot") for row in rows}

    plans = find_clusters(
        records,
        observations,
        tiers,
        similarity_threshold=threshold,
        min_cluster_size=min_size,
    )
    hash_ = params_hash(similarity_threshold=threshold, min_cluster_size=min_size)
    when = instant.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    subsumed: list[str] = []
    for plan in plans:
        demoted = engine.state.subsume_records(
            plan.canonical_id,
            list(plan.member_ids),
            absorbed_observations=plan.absorbed_observations,
            now=when,
            rule_id=RULE_ID,
            params_hash=hash_,
        )
        if demoted:
            subsumed.extend(plan.member_ids)

    if subsumed:
        try:
            engine.graph_builder.add_memory_edges(
                engine.state, engine.config.memory.about_max_targets
            )
        except Exception:
            pass  # graph bridging is best-effort everywhere else too

    return {"clusters": len(plans), "subsumed": sorted(subsumed)}
