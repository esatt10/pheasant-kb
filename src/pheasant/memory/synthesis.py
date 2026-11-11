"""Phase 4/6 — L3 synthesis: the one non-deterministic compaction tier.

Deterministic methods (L0 normalize, L1 cluster, L2 select — `normalize`,
`compaction`) fold *redundancy*: many surface forms of one claim collapse
losslessly into one record. None of them can **merge**: turn "runs in
us-east-2" and "owned by ada" into one record stating both, fold ten
progressive-refinement records into one, or abstract "deploy failed
Monday/Tuesday/Wednesday" into "deploys have been failing all week". That
is genuinely generative, and CLAUDE.md rule 1 forbids an LLM on the
*indexing* path for exactly the reason this module respects: **the model
here is a writer, never an indexer.** It produces one ordinary record file
through the same append-only path a human or agent write uses; the
deterministic pipeline then indexes it identically to every other record.
No LLM call ever happens inside indexing, and `pytest` stays network-free
by construction — the default `provider` resolves to nothing reachable —
not by mocking.

Off by default (`memory.synthesis.enabled`) and never invoked from the
scheduler beat: only the explicit `memory_synthesize` MCP tool /
`POST /memory/synthesize` call it. Efficiency, in order of what actually
saves calls:

1. The model never sees anything L1/L2 already resolved — only clusters
   still `tier='hot'` after compaction, size-gated so a genuinely tiny
   group is left for L2 (which is lossless) rather than spent on a model
   call.
2. The gate decides *what the model is asked to do* without a model:
   `max_jaccard` skips any cluster whose members are already near-
   identical, because that is exactly the shape medoid promotion resolves
   for free. Spend goes only where deterministic compaction provably
   cannot help.
3. Content-addressed caching: a cluster's member-id set + model id + rule
   id hashes into `params_hash`, checked against the `memory_compactions`
   ledger *before* any call — a repeat pass over unchanged clusters costs
   zero.
4. `max_calls_per_pass` bounds what an unbounded first pass could spend.
5. `LLM.try_complete` degrades to "skip this cluster" on any failure — a
   pass always completes, never half-fails the rest of its work.

The synthesized record `subsumes` its members through the same mechanism
Phase 3's medoid promotion uses (`StateStore.subsume_records`, `op=
"synthesize"`) — not `supersedes`: the inputs are not corrected, they are
redundant-but-still-true, exactly compaction's existing category. Every
member keeps its `written_by`/scope, so the synthesized record inherits
the cluster's own writer (never a synthetic identity) and its ACL stays
exactly what a member's would have been. `tags` and the ledger's
`rule_id`/`params_hash` (which encodes the model id) keep a generated
record permanently distinguishable from a written one.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from pheasant.connections import environment as connection_environment
from pheasant.memory.compaction import _bucket_key, mean_pairwise_jaccard
from pheasant.memory.policy import STEERING_KINDS
from pheasant.memory.store import MemoryRecord, MemoryStore
from pheasant.telemetry import metrics

RULE_ID = "llm-synthesis-v1"

#: Mirrors `MemorySynthesisSettings.max_jaccard`, for callers reaching
#: `_candidate_clusters` directly (tests) rather than through `run_synthesis`.
DEFAULT_MAX_JACCARD = 0.55

#: Tag stamped on every synthesized record — the human-visible twin of the
#: ledger's machine-readable provenance.
SYNTHESIZED_TAG = "llm-synthesized"

_SYSTEM_PROMPT = (
    "You merge near-duplicate notes from an agent's memory store into one "
    "consolidated note. Rules: state every distinct fact present in the "
    "inputs exactly once; invent nothing that is not present in the "
    "inputs; do not add commentary, headers, disclaimers, or explanation; "
    "output only the merged note itself, as plain prose, one to three "
    "sentences."
)


def _prompt(members: list[MemoryRecord]) -> str:
    lines = [f"{i + 1}. {member.text.strip()}" for i, member in enumerate(members)]
    return "Merge these into one note:\n" + "\n".join(lines)


def _resolve_provider(settings: Any, env: dict[str, str]) -> dict[str, Any] | None:
    """`{provider, api_key, model, base_url, source}` or None.

    A scoped twin of `assistant.chat.resolve_provider` — that function
    reads `config.assistant` specifically and has no session-credential
    concept to offer here (this is a batch maintenance call, not a live
    chat turn), so this reads `memory.synthesis` directly instead of
    bending the assistant's resolver to a second settings shape.
    """
    from pheasant.assistant.providers import PROVIDERS, resolve_auto_provider

    if not getattr(settings, "enabled", False):
        return None
    configured = str(getattr(settings, "provider", "auto") or "auto")
    if configured == "none":
        return None
    if configured == "auto":
        configured = resolve_auto_provider(env)
        if configured is None:
            return None
    spec = PROVIDERS.get(configured)
    if spec is None:
        return None
    env_name = getattr(settings, "api_key_env", None) or spec.api_key_env
    api_key = env.get(env_name)
    if not api_key:
        return None
    return {
        "provider": configured,
        "api_key": api_key,
        "model": getattr(settings, "model", None),
        "base_url": getattr(settings, "base_url", None),
        "source": "environment",
    }


def _candidate_clusters(
    records: list[MemoryRecord],
    tiers: dict[str, str],
    *,
    min_cluster_size: int,
    max_jaccard: float = DEFAULT_MAX_JACCARD,
) -> dict[tuple[str, str, str, str], list[MemoryRecord]]:
    """Buckets worth a synthesis attempt: live, non-steering, subject-
    tagged records sharing (scope, subject, kind, ACL partition) — the
    same partition reinforcement and clustering already enforce.

    Subject-tagged only: synthesis merges records *about the same thing*,
    and `subject` is the one field a record uses to say what that is. An
    untagged bucket has no anchor to merge around, so it is left alone —
    L1/L2 still apply to it exactly as before.

    **`max_jaccard` is what decides what the model is asked to do, without
    a model.** A bucket whose members are already near-identical is the
    shape medoid promotion (L2) resolves losslessly and for free; synthesis
    exists for the opposite one — same subject, genuinely different
    content. Gating here rather than after the call is the difference
    between spending on what only a model can do and spending on what L2
    would have done anyway, and it matters most when `compaction_enabled`
    is off (the default): nothing has been demoted, so every near-duplicate
    bucket would otherwise reach the model.
    """
    buckets: dict[tuple[str, str, str, str], list[MemoryRecord]] = {}
    for record in records:
        if record.kind in STEERING_KINDS:
            continue
        if tiers.get(record.record_id, "hot") != "hot":
            continue
        if not record.subject:
            continue
        buckets.setdefault(_bucket_key(record), []).append(record)
    return {
        key: sorted(members, key=lambda r: r.record_id)
        for key, members in buckets.items()
        if len(members) >= min_cluster_size
        and mean_pairwise_jaccard([r.text for r in members]) <= max_jaccard
    }


def params_hash(*, model_id: str, member_ids: list[str]) -> str:
    """Stamped into the ledger row and checked before every model call —
    the content address the cache is keyed on. Two passes over the exact
    same member set under the exact same model produce the exact same
    hash, so the second one costs nothing (see module docstring, lever 2).
    """
    key = f"{RULE_ID}|model={model_id}|members={','.join(sorted(member_ids))}"
    return hashlib.blake2b(key.encode("utf-8"), digest_size=8).hexdigest()


def run_synthesis(
    engine: Any,
    records: list[MemoryRecord],
    settings: Any,
    source: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One synthesis pass. Never called automatically — see module
    docstring. Returns `{"skipped": reason}` when nothing ran, or
    `{"attempted", "synthesized", "cached", "records": [...]}`.
    """
    if not getattr(settings, "enabled", False):
        return {"skipped": "memory.synthesis.enabled is false"}

    from pheasant.assistant.llm import llm_from_selection

    selection = _resolve_provider(settings, connection_environment())
    llm = llm_from_selection(selection, settings)
    if llm is None:
        return {"skipped": "no model reachable"}

    instant = (now or datetime.now(tz=UTC)).astimezone(UTC)
    when = instant.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    rows = engine.state.memory_compaction_rows()
    tiers = {str(row["record_id"]): str(row.get("tier") or "hot") for row in rows}
    observations = {str(row["record_id"]): int(row.get("observations") or 0) for row in rows}

    min_size = int(getattr(settings, "min_cluster_size", 3) or 3)
    max_similarity = float(getattr(settings, "max_jaccard", DEFAULT_MAX_JACCARD))
    max_chars = int(getattr(settings, "max_input_chars", 6000) or 6000)
    max_calls = int(getattr(settings, "max_calls_per_pass", 20) or 20)

    buckets = _candidate_clusters(
        records, tiers, min_cluster_size=min_size, max_jaccard=max_similarity
    )
    store = MemoryStore(source.path)

    attempted = 0
    cached = 0
    written: list[dict[str, Any]] = []
    for key in sorted(buckets):
        if attempted >= max_calls:
            break
        members = buckets[key]
        if sum(len(m.text) for m in members) > max_chars:
            continue
        member_ids = [m.record_id for m in members]
        hash_ = params_hash(model_id=llm.model_id, member_ids=member_ids)

        if engine.state.memory_compaction_ledger(params_hash=hash_):
            # This exact cluster was already resolved under this exact
            # model — nothing new to attempt, whatever the outcome was.
            cached += 1
            metrics.REGISTRY.inc("pheasant_memory_synthesis_calls_total", outcome="cached")
            continue

        attempted += 1
        completion = llm.try_complete(_SYSTEM_PROMPT, _prompt(members))
        if not completion or not completion.strip():
            metrics.REGISTRY.inc("pheasant_memory_synthesis_calls_total", outcome="empty")
            continue

        scope, subject, kind, _acl_class = key
        # All members share one ACL partition by construction (the bucket
        # key), so for user/session scope they share one written_by too —
        # the synthesized record inherits it rather than a synthetic
        # identity, which is what keeps its ACL correct.
        writer = next((m.written_by for m in members if m.written_by), None)
        record, created = store.append(
            completion.strip(),
            scope=scope,
            subject=subject or None,
            kind=kind,
            tags=(SYNTHESIZED_TAG,),
            written_by=writer,
            # The caller's instant, not the wall clock. A record id embeds
            # its own second, so leaving this unthreaded made a pass with a
            # pinned `now` still stamp real time — the synthesized record
            # and the ledger row recording it would then disagree about
            # when the merge happened.
            now=instant,
        )
        if not created:
            # An exact-digest collision with something already on disk —
            # extraordinarily unlikely for model prose, but if it happens
            # there is nothing new to subsume with; skip cleanly.
            metrics.REGISTRY.inc("pheasant_memory_synthesis_calls_total", outcome="collision")
            continue
        metrics.REGISTRY.inc("pheasant_memory_synthesis_calls_total", outcome="synthesized")
        written.append(
            {
                "record_id": record.record_id,
                "member_ids": member_ids,
                "absorbed": sum(observations.get(m, 0) for m in member_ids) + len(member_ids),
                "params_hash": hash_,
            }
        )

    if not written:
        # Always the full stats, never a generic message: "nothing to do
        # because everything was already cached" and "nothing to do
        # because there were no candidate clusters at all" are different
        # facts, and `cached` says which one happened.
        return {"attempted": attempted, "synthesized": 0, "cached": cached, "records": []}

    # One sync for every new file at once, rather than per-cluster — the
    # same "batch, don't trickle" reasoning Phase 0 applies to memory
    # writes generally. `enrich="now"`: unlike a single interactive write,
    # this is a maintenance-time batch operation where the graph should be
    # current before subsume_records credits the new canonical rows below.
    engine.sync_source(source.name, "incremental")

    demoted_total = 0
    for entry in written:
        demoted_total += engine.state.subsume_records(
            entry["record_id"],
            entry["member_ids"],
            absorbed_observations=entry["absorbed"],
            now=when,
            rule_id=RULE_ID,
            params_hash=entry["params_hash"],
            op="synthesize",
        )
    if demoted_total:
        metrics.REGISTRY.inc("pheasant_memory_compactions_total", demoted_total, op="synthesize")

    try:
        engine.graph_builder.add_memory_edges(engine.state, engine.config.memory.about_max_targets)
    except Exception:
        pass  # graph bridging is best-effort everywhere else too

    return {
        "attempted": attempted,
        "synthesized": len(written),
        "cached": cached,
        "records": [entry["record_id"] for entry in written],
    }
