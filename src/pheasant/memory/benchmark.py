"""LongMemEval-style memory-recall benchmark (Product Framework Step 33.4).

Deterministic and fully offline: a seeded generator writes synthetic memory
histories through the **real** write path (``memory_write``) and scores
recall through the **real** search path (``search_context``) — no mocked
retrieval anywhere. Categories follow the LongMemEval taxonomy where it
maps onto region memory:

- **single-hop recall** — a fact written once must surface in the top-k
  for its natural-language question;
- **multi-session interference** — recall holds against a large body of
  distractor memories written across other sessions/subjects;
- **knowledge updates** — a superseding write followed by consolidation
  must make the *current* fact retrievable and the stale one absent;
- **abstention** — questions about never-stored entities must not surface
  confident hits.

Phase 5 adds a fifth, non-LongMemEval category — **redundancy** — because
none of the four above says anything about the failure mode the compaction
plan exists for: an agent restating the same fact in fresh words every time
it comes up. `run_memory_redundancy_benchmark` measures how much of that L0
(reinforcement) and L1/L2 (clustering) actually absorb, through the same
real `memory_write` / `memory_consolidate` paths.

A true LongMemEval run (LLM-judged answers over the published dataset,
scored against Mem0/Zep) needs network + an LLM and is tracked as manual
release-channel work; this harness is the runnable, regression-gated
proxy. Reproduce the recorded numbers with::

    python -m pheasant.memory.benchmark
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WORDS = (
    "amber quartz falcon delta harbor juniper krypton lumen meadow nova "
    "onyx prism quill rustic saffron timber umber vertex willow zephyr "
    "basalt cedar dune ember flint garnet"
).split()

REGIONS = ["us-east-2", "eu-west-1", "ap-south-1", "us-west-2", "sa-east-1"]
OWNERS = ["ada", "bhaskara", "curie", "darwin", "euler", "fermi", "gauss"]


@dataclass(frozen=True)
class MemoryBenchSpec:
    n_facts: int = 30
    n_distractors: int = 120
    n_updates: int = 10
    n_abstain: int = 10
    k: int = 5
    seed: int = 1337
    mode: str = "hybrid"
    #: Phase 5: a small, bounded sample of writes made through the real
    #: default path (`memory_write(sync=True)`) to measure
    #: `write_latency_sync_ms`. Kept separate from, and much smaller than,
    #: `n_facts`/`n_distractors` — those are batched with `sync=False` on
    #: purpose (see `write_latency_ms`'s own docstring below), so mixing a
    #: `sync=True` write into that loop would make neither number honest.
    n_sync_probes: int = 8


@dataclass
class MemoryBenchReport:
    recall_at_k: float
    update_accuracy: float
    stale_leak_rate: float
    abstention_accuracy: float
    spec: MemoryBenchSpec
    #: Step 33.11 additions. Recall alone says whether the right memory came
    #: back; these say what it cost and how healthy the store is — the layers
    #: the agent-memory literature calls memory quality and efficiency.
    contradiction_rate: float = 0.0
    staleness_p50_days: float = 0.0
    staleness_p95_days: float = 0.0
    bytes_per_record: float = 0.0
    #: Batched: every fact/distractor write in this run uses `sync=False`,
    #: with one `sync_source` call at the end — realistic for a script
    #: seeding a corpus, but *not* what a single interactive `memory_write`
    #: costs, because Phase 0's per-write deferred-enrichment work never
    #: runs per write here. See `write_latency_sync_ms` for that number.
    write_latency_ms: float = 0.0
    #: Phase 5: the default path — `memory_write(sync=True)`, one call, one
    #: incremental sync with `enrich="deferred"` — timed over
    #: `spec.n_sync_probes` writes. This is what finding (2) of the
    #: compaction plan is about, and what the pre-Phase-0 `write_latency_ms`
    #: above could never show because it never took this path.
    write_latency_sync_ms: float = 0.0
    search_latency_ms: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "recall_at_k": self.recall_at_k,
            "update_accuracy": self.update_accuracy,
            "stale_leak_rate": self.stale_leak_rate,
            "abstention_accuracy": self.abstention_accuracy,
            "k": self.spec.k,
            "n_facts": self.spec.n_facts,
            "n_distractors": self.spec.n_distractors,
            "n_updates": self.spec.n_updates,
            "n_abstain": self.spec.n_abstain,
            "seed": self.spec.seed,
            "mode": self.spec.mode,
            "contradiction_rate": self.contradiction_rate,
            "staleness_p50_days": self.staleness_p50_days,
            "staleness_p95_days": self.staleness_p95_days,
            "bytes_per_record": self.bytes_per_record,
            "write_latency_ms": self.write_latency_ms,
            "write_latency_sync_ms": self.write_latency_sync_ms,
            "search_latency_ms": self.search_latency_ms,
        }


def _codename(rng: random.Random) -> str:
    # One atomic token (no hyphens/shared parts): distinct entities stay
    # distinct in FTS *and* in graph entity nodes, mirroring real entity
    # names; interference pressure comes from the shared template
    # vocabulary and the distractor corpus, not from entity-name collisions.
    return f"{rng.choice(WORDS)}{rng.choice(WORDS)}{rng.randrange(100, 999)}"


def generate_cases(spec: MemoryBenchSpec) -> dict[str, list[dict[str, str]]]:
    """Seeded, deterministic case set (same spec → identical cases)."""
    rng = random.Random(spec.seed)
    seen: set[str] = set()

    def fresh_codename() -> str:
        while True:
            name = _codename(rng)
            if name not in seen:
                seen.add(name)
                return name

    facts = []
    for _ in range(spec.n_facts):
        service = fresh_codename()
        region, owner = rng.choice(REGIONS), rng.choice(OWNERS)
        facts.append(
            {
                "service": service,
                "text": f"The {service} service runs in {region} and is owned by {owner}.",
                "query": f"where does the {service} service run",
                "region": region,
            }
        )
    distractors = [
        {
            "text": (
                f"Note {i}: the {fresh_codename()} experiment logged "
                f"{rng.randrange(10, 500)} samples during review."
            )
        }
        for i in range(spec.n_distractors)
    ]
    updates = []
    for case in facts[: spec.n_updates]:
        new_region = rng.choice([r for r in REGIONS if r != case["region"]])
        updates.append(
            {
                "service": case["service"],
                "old_text": case["text"],
                "new_text": (
                    f"The {case['service']} service now runs in {new_region} after the migration."
                ),
                "query": case["query"],
            }
        )
    # Abstention queries share NO vocabulary with facts/distractors beyond
    # the unheard codename — exact-match abstention: FTS must return nothing.
    abstain = [{"query": f"{fresh_codename()} deployment status"} for _ in range(spec.n_abstain)]
    return {"facts": facts, "distractors": distractors, "updates": updates, "abstain": abstain}


def _hit_paths(
    tools: Any, kb: str, query: str, k: int, mode: str, timer: list[float] | None = None
) -> list[str]:
    """Record ids of the memory hits, plus paths for anything else.

    Step 33.11: a memory hit now *says* it is one, so this reads the typed
    `memory.record_id` instead of substring-matching a record id out of the
    relative path. That hack was the only way to tell a memory apart before
    33.6 and it would silently start counting any file whose name happened to
    contain a record id.
    """
    started = time.perf_counter()
    payload = tools.search_context(kb, query, mode=mode, max_results=k)
    if timer is not None:
        timer.append(time.perf_counter() - started)
    out: list[str] = []
    for item in payload["results"]:
        memory = item.get("memory")
        if isinstance(memory, dict) and memory.get("record_id"):
            out.append(str(memory["record_id"]))
        else:
            out.append(str(item.get("relative_path") or ""))
    return out


def run_memory_recall_benchmark(
    tools: Any, spec: MemoryBenchSpec | None = None, *, knowledge_base: str | None = None
) -> MemoryBenchReport:
    """Run the four categories against a live ``PheasantTools`` facade.

    The facade's config must include a ``type: memory`` source. Writes go
    through ``memory_write`` (sync deferred to one batch index), updates
    through ``supersedes`` + ``memory_consolidate`` — the exact surfaces an
    agent uses.
    """
    from pheasant.memory.maintenance import run_memory_maintenance
    from pheasant.memory.store import memory_source

    spec = spec or MemoryBenchSpec()
    kb = knowledge_base or tools.config.pheasant.name
    source = memory_source(tools.config, tools.state)
    if source is None:
        raise ValueError("benchmark needs a `type: memory` source in the config")
    cases = generate_cases(spec)

    # Latency is measured, not declared: a field reporting 0.0 because nobody
    # timed it is worse than no field, since it reads as "measured, and fast".
    writes = 0
    write_seconds = 0.0
    search_times: list[float] = []

    fact_ids: dict[str, str] = {}
    for case in cases["facts"]:
        started = time.perf_counter()
        record = tools.memory_write(kb, case["text"], scope="org", sync=False)["record"]
        write_seconds += time.perf_counter() - started
        writes += 1
        fact_ids[case["text"]] = record["record_id"]
    for idx, distractor in enumerate(cases["distractors"]):
        scope = ("session", "user", "org")[idx % 3]
        started = time.perf_counter()
        tools.memory_write(kb, distractor["text"], scope=scope, sync=False)
        write_seconds += time.perf_counter() - started
        writes += 1
    tools.sync_source(kb, source.name, "incremental")

    recalled = sum(
        1
        for case in cases["facts"]
        if any(
            fact_ids[case["text"]] in path
            for path in _hit_paths(tools, kb, case["query"], spec.k, spec.mode, search_times)
        )
    )

    update_ids: dict[str, str] = {}
    for case in cases["updates"]:
        new = tools.memory_write(
            kb,
            case["new_text"],
            scope="org",
            supersedes=fact_ids[case["old_text"]],
            sync=False,
        )["record"]
        update_ids[case["new_text"]] = new["record_id"]
    tools.sync_source(kb, source.name, "incremental")
    run_memory_maintenance(tools.engine)

    updated_ok = 0
    stale_leaks = 0
    for case in cases["updates"]:
        paths = _hit_paths(tools, kb, case["query"], spec.k, spec.mode, search_times)
        if any(update_ids[case["new_text"]] in p for p in paths):
            updated_ok += 1
        if any(fact_ids[case["old_text"]] in p for p in paths):
            stale_leaks += 1

    abstained = sum(
        1
        for case in cases["abstain"]
        if not _hit_paths(tools, kb, case["query"], spec.k, spec.mode, search_times)
    )

    health = _store_health(tools)
    health["write_latency_ms"] = round(1000.0 * write_seconds / max(writes, 1), 3)
    health["write_latency_sync_ms"] = _measure_sync_write_latency(tools, kb, spec)
    health["search_latency_ms"] = round(1000.0 * sum(search_times) / max(len(search_times), 1), 3)
    return MemoryBenchReport(
        recall_at_k=recalled / spec.n_facts,
        update_accuracy=updated_ok / max(spec.n_updates, 1),
        stale_leak_rate=stale_leaks / max(spec.n_updates, 1),
        abstention_accuracy=abstained / max(spec.n_abstain, 1),
        spec=spec,
        details={"recalled": recalled, "updated_ok": updated_ok, "stale_leaks": stale_leaks},
        **health,
    )


# ---------------------------------------------------------------------------
# Redundancy (Phase 5)
# ---------------------------------------------------------------------------


#: Four writes per fact, each exercising a different layer of compaction:
#: [0] the original assertion; [1] a byte-identical repeat (the exact-digest
#: dedup that predates Phase 1, `outcome="duplicate"`); [2] the same claim
#: behind a framing prefix + different casing (`memory.normalize` strips
#: both, so this folds via L0, `outcome="reinforced"`); [3] the same claim
#: with one genuinely added clause — normalizes to a *different* string (L0
#: cannot fold it, `outcome="created"`) but shares enough tokens for L1's
#: Jaccard clustering (`memory.compaction`) to link it to [0]'s record once
#: a maintenance pass runs, if `compaction_enabled`.
def _redundant_writes(service: str, region: str, owner: str) -> list[str]:
    base = f"The {service} service runs in {region} and is owned by {owner}."
    return [
        base,
        base,
        f"Note that the {service} SERVICE runs in {region} and is owned by {owner}.",
        f"The {service} service runs in {region} and is owned by {owner}, per the latest "
        "inventory.",
    ]


@dataclass(frozen=True)
class RedundancyBenchSpec:
    n_facts: int = 15
    seed: int = 4242


@dataclass
class RedundancyBenchReport:
    #: Live (`tier='hot'`) records per distinct fact after one maintenance
    #: pass. `1.0` means every paraphrase collapsed onto one canonical
    #: record; the pre-Phase-0 system has no mechanism that could ever move
    #: this off the write count, so it would read `2.0` here (see
    #: `_redundant_writes`: two of the four writes per fact are always
    #: `created`) regardless of how long the store ran.
    records_per_fact: float
    #: Fraction of all writes (including the exact-digest `duplicate`) that
    #: L0 folded into an existing record rather than creating a new file.
    reinforcement_ratio: float
    #: Of the writes that *did* create a distinct record (survived L0),
    #: the fraction L1/L2 subsumed on the maintenance pass that followed.
    #: `0.0` whenever `memory.compaction_enabled` is off — nothing ran, not
    #: a claim that nothing could have.
    compaction_ratio: float
    spec: RedundancyBenchSpec

    def as_dict(self) -> dict[str, Any]:
        return {
            "records_per_fact": self.records_per_fact,
            "reinforcement_ratio": self.reinforcement_ratio,
            "compaction_ratio": self.compaction_ratio,
            "n_facts": self.spec.n_facts,
            "seed": self.spec.seed,
        }


def run_memory_redundancy_benchmark(
    tools: Any, spec: RedundancyBenchSpec | None = None, *, knowledge_base: str | None = None
) -> RedundancyBenchReport:
    """How much of an agent's paraphrase-heavy write pattern the compaction
    tiers actually absorb — finding (1) of the compaction plan, made
    falsifiable. Every write goes through the real `memory_write` path, so a
    region with `reinforcement_enabled`/`compaction_enabled` off shows that
    honestly as `reinforcement_ratio`/`compaction_ratio` reading `0.0` —
    nothing here assumes a setting the caller's config did not choose.
    """
    from pheasant.memory.maintenance import run_memory_maintenance
    from pheasant.memory.store import memory_source

    spec = spec or RedundancyBenchSpec()
    kb = knowledge_base or tools.config.pheasant.name
    source = memory_source(tools.config, tools.state)
    if source is None:
        raise ValueError("benchmark needs a `type: memory` source in the config")

    rng = random.Random(spec.seed)
    seen: set[str] = set()

    def fresh_codename() -> str:
        while True:
            name = _codename(rng)
            if name not in seen:
                seen.add(name)
                return name

    outcomes: list[str] = []
    for _ in range(spec.n_facts):
        service = fresh_codename()
        region, owner = rng.choice(REGIONS), rng.choice(OWNERS)
        for text in _redundant_writes(service, region, owner):
            # `subject=service`: without it every fact lands in the same
            # (scope, subject="", kind, acl_class) bucket (Phase 3's
            # `_bucket_key`), and the shared template wording — "service
            # runs in ... and is owned by ..." — is enough token overlap on
            # its own to clear the default Jaccard threshold *between*
            # unrelated facts, not just between one fact's paraphrases.
            #
            # `sync=True`, unlike the recall benchmark's batched
            # `sync=False`: reinforcement (Phase 1) folds a write against
            # the *projected* canonical record in SQL
            # (`memory.reinforcement.StateReinforcementIndex`), so a
            # paraphrase written before its predecessor's own sync has run
            # cannot find anything to fold into — this category exists
            # specifically to measure whether folding happens, so it has to
            # take the path that actually lets it.
            result = tools.memory_write(kb, text, scope="org", subject=service, sync=True)
            outcomes.append(result["outcome"])
    tools.sync_source(kb, source.name, "incremental")

    total = len(outcomes)
    created = outcomes.count("created")
    reinforced = outcomes.count("reinforced")

    maintenance = run_memory_maintenance(tools.engine) or {}
    compaction = maintenance.get("compaction") or {}
    subsumed = len(compaction.get("subsumed") or ())

    hot_records = tools.engine.state.rows(
        "SELECT 1 FROM memory_records WHERE COALESCE(NULLIF(tier, ''), 'hot') = 'hot'"
    )
    return RedundancyBenchReport(
        records_per_fact=round(len(hot_records) / max(spec.n_facts, 1), 4),
        reinforcement_ratio=round(reinforced / max(total, 1), 4),
        # Denominator: writes that survived L0 as a distinct record — the
        # only ones L1/L2 could ever have subsumed.
        compaction_ratio=round(subsumed / max(created, 1), 4),
        spec=spec,
    )


def _measure_sync_write_latency(tools: Any, kb: str, spec: MemoryBenchSpec) -> float:
    """`write_latency_sync_ms`: `spec.n_sync_probes` writes through the real
    default path (`memory_write`'s `sync=True`), one at a time. Distinct
    `session`-scope notes so nothing here competes for reinforcement or
    recall scoring with the facts/distractors above — this measures cost,
    not recall.
    """
    if spec.n_sync_probes <= 0:
        return 0.0
    seconds = 0.0
    for i in range(spec.n_sync_probes):
        started = time.perf_counter()
        tools.memory_write(kb, f"Write-latency probe note number {i}.", scope="session", sync=True)
        seconds += time.perf_counter() - started
    return round(1000.0 * seconds / spec.n_sync_probes, 3)


def _store_health(tools: Any) -> dict[str, float]:
    """Memory-quality and efficiency numbers, measured off the live store.

    Recall says whether the right memory came back. These say whether the
    store is *healthy*: how much of it contradicts itself, how old what it
    serves is, and what it costs per record. All read from `memory_records`
    and the record files, so they cost one query and a stat.
    """
    from pheasant.memory.store import memory_source

    try:
        rows = tools.engine.state.rows("SELECT asserted_at, valid_until FROM memory_records")
    except Exception:  # pragma: no cover - store older than 33.5
        return {}
    if not rows:
        return {}

    total = len(rows)
    # A record whose validity was ended by a *later* record is a contradiction
    # the store is carrying: it was asserted, then corrected.
    contradicted = sum(1 for row in rows if row["valid_until"])

    now = datetime.now(tz=UTC)
    ages: list[float] = []
    for row in rows:
        try:
            moment = datetime.fromisoformat(str(row["asserted_at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        ages.append(max(0.0, (now - moment).total_seconds() / 86400.0))
    ages.sort()

    def percentile(fraction: float) -> float:
        if not ages:
            return 0.0
        return round(ages[min(len(ages) - 1, int(len(ages) * fraction))], 4)

    source = memory_source(tools.config, tools.state)
    total_bytes = 0
    if source is not None:
        for path in Path(source.path).rglob("mem-*.md"):
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue

    return {
        "contradiction_rate": round(contradicted / total, 4),
        "staleness_p50_days": percentile(0.5),
        "staleness_p95_days": percentile(0.95),
        "bytes_per_record": round(total_bytes / total, 1),
    }


def _membench_config(root: Path, *, compaction_enabled: bool = False) -> Path:
    (root / "memory").mkdir()
    config_path = root / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: membench
  defaults: standalone
  state_path: {root / "state"}
  exports_path: {root / "exports"}
  workspace_root: {root}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
memory:
  compaction_enabled: {"true" if compaction_enabled else "false"}
sources:
  - name: agent-memory
    type: memory
    path: memory
""",
        encoding="utf-8",
    )
    return config_path


def main() -> None:  # pragma: no cover - manual reproduction entry point
    import tempfile

    from pheasant.config.loader import load_config
    from pheasant.mcp_server.tools import PheasantTools

    with tempfile.TemporaryDirectory(prefix="pheasant-membench-") as tmp:
        root = Path(tmp) / "recall"
        root.mkdir()
        tools = PheasantTools(load_config(_membench_config(root)))
        try:
            report = run_memory_recall_benchmark(tools)
        finally:
            tools.engine.close()
        for key, value in report.as_dict().items():
            print(f"{key}: {value}")

        # A dedicated store, `compaction_enabled: true`, so the printed
        # `compaction_ratio` shows what L1/L2 actually does rather than the
        # `0.0` a default region would report (see RedundancyBenchReport's
        # own docstring on that being an honest reading, not a bug).
        redundancy_root = Path(tmp) / "redundancy"
        redundancy_root.mkdir()
        redundancy_tools = PheasantTools(
            load_config(_membench_config(redundancy_root, compaction_enabled=True))
        )
        try:
            redundancy_report = run_memory_redundancy_benchmark(redundancy_tools)
        finally:
            redundancy_tools.engine.close()
        print("--- redundancy ---")
        for key, value in redundancy_report.as_dict().items():
            print(f"{key}: {value}")


if __name__ == "__main__":  # pragma: no cover
    main()
