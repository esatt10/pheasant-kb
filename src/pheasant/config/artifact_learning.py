"""Typed artifact learning policies; Memory* class names are import-compatible aliases."""

from __future__ import annotations

from dataclasses import dataclass, field

from pheasant.config.model import ModelMixin


@dataclass
class MemorySynthesisSettings(ModelMixin):
    """L3 compaction (Phase 4): abstractive merge of a near-duplicate
    cluster deterministic methods cannot resolve — complementary partial
    facts, progressive refinement, or genuine abstraction across records.
    See `docs/memory-system.md` §8 for what deterministic clustering (L1/L2)
    already handles and why this tier exists for what it cannot.

    Mirrors `AssistantSettings` field-for-field on purpose: an operator who
    has already configured the assistant's model recognizes every knob
    here, and the fields feed the exact same `assistant.llm`/
    `assistant.catalog` machinery — no second provider stack.

    **Off by default and never on an automatic beat.** Unlike consolidation
    or compaction (Phase 3), which are pure metadata operations, this makes
    a network call — CLAUDE.md rule 1 forbids an LLM on the indexing path,
    and the scheduler's maintenance beat is exactly that path. Synthesis
    runs only through the explicit `memory_synthesize` MCP tool /
    `POST /memory/synthesize`, never automatically, so `pytest` stays
    network-free by construction (the default `provider` resolves to
    nothing reachable) rather than by mocking.
    """

    enabled: bool = False
    provider: str = "auto"  # auto | anthropic | openai | gemini | none
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    max_output_tokens: int = 1024
    request_timeout_seconds: float = 60.0
    #: Hard cap on model calls in one pass. The content-addressed cache
    #: (a cluster's member-id set + model id + rule id, checked against the
    #: `memory_compactions` ledger before any call) already makes a repeat
    #: pass over unchanged clusters cost zero regardless — this bounds the
    #: *first* pass over a large store, or one after many new clusters
    #: appeared at once.
    max_calls_per_pass: int = 20
    #: Conservative char-based proxy for input size — no tokenizer
    #: dependency, matching the no-extra-dependency posture the rest of
    #: this module keeps. A cluster whose combined member text exceeds this
    #: is skipped rather than truncated silently into a partial merge.
    max_input_chars: int = 6000
    #: A cluster below this size is not a synthesis candidate — medoid
    #: promotion (L2, Phase 3) already resolves anything smaller cleanly
    #: and losslessly; synthesis is reserved for what that tier cannot.
    min_cluster_size: int = 3
    #: The other half of that gate, and the one that decides *what the model
    #: is asked to do* without a model. A cluster whose members are already
    #: near-identical (high Jaccard over normalized tokens) is what medoid
    #: promotion solves losslessly and for free; synthesis exists for the
    #: opposite shape — records about one subject that say genuinely
    #: *different* things (complementary partials, progressive refinement,
    #: abstraction across instances). Above this similarity a cluster is
    #: skipped, so spend goes only where deterministic compaction provably
    #: cannot help. Matters most when `compaction_enabled` is off (the
    #: default): nothing has been demoted, so without this gate every
    #: near-duplicate bucket would reach the model.
    max_jaccard: float = 0.55


@dataclass
class MemoryFormationSettings(ModelMixin):
    """Turning recorded interactions into memory candidates, deterministically.

    The observation plane (``observability.interactions``) records what was
    asked and what came back. This is the half that reads it and proposes
    records — and, deliberately, only *proposes*: a candidate becomes a
    record through :meth:`pheasant.artifacts.store.MemoryStore.append` like
    every other write, so nothing here is a second ingestion path.

    **No model runs in this path.** Rules are counting and string matching
    over recorded inputs, so a pass is reproducible and a candidate's id is
    a deterministic hash of what produced it. Every decision is ledgered
    with its ``rule_id`` and ``params_hash``, exactly as compaction's
    already is, so a repeat pass over unchanged observations under
    unchanged parameters writes nothing.

    See ``docs/memory-formation.md``.
    """

    #: Master switch. Off, nothing reads the observation plane and no
    #: candidate is ever minted — the region behaves exactly as it did
    #: before formation existed.
    enabled: bool = False
    #: Maintain one record per session, refined through dialog: scope
    #: ``session``, subject the session id, each refinement naming the
    #: previous one in ``supersedes``. `current_only` (on by default) then
    #: returns exactly one record per session and `as_of` reads its
    #: history — the validity model doing what it was built for, with no
    #: new primitive. Only meaningful when `enabled`.
    session_digest: bool = True
    #: Admit a candidate above threshold without review.
    #:
    #: **Off by default**, the same posture `compaction_enabled` and
    #: `supersede_retention_days` take: it changes what a *default* query
    #: returns, which is a decision an operator should make rather than
    #: inherit. Left off, candidates accumulate for review in the Memory
    #: tab and nothing is written until a person promotes one. An
    #: auto-admitted record carries the `formed` tag and its candidate row
    #: records `admitted_by`, so a machine-formed record is always
    #: distinguishable from a written one.
    auto_admit: bool = False
    #: How many times a pattern must be observed before it is a candidate.
    #: Counts run over a stream that is *sampled under load* (the log tier
    #: drops rather than blocks), so a busy region reaches these thresholds
    #: later — not incorrectly.
    min_observations: int = 3
    #: …and across how many distinct sessions. One session repeating itself
    #: is a habit; several sessions agreeing is a signal. Guards against a
    #: single loop minting steering that reorders results for everyone.
    min_sessions: int = 2
    #: Hard cap on candidates minted in one pass, bounding a first pass over
    #: a large ledger the way `synthesis.max_calls_per_pass` bounds its own.
    max_candidates_per_pass: int = 50
    #: A pending candidate nobody promoted expires after this many days.
    #: Rejections are remembered separately and are never re-proposed.
    candidate_ttl_days: int = 30
    #: Rules to run, by id. Each is versioned so a rule's decisions stay
    #: attributable after its logic changes: a new version is a new
    #: `rule_id`, never an edit to an existing one.
    rules: list[str] = field(
        default_factory=lambda: [
            "session-digest-v1",
            "alias-cooccurrence-v1",
            "path-affinity-v1",
            "retrieval-gap-v1",
        ]
    )


@dataclass
class MemorySettings(ModelMixin):
    """Agent-memory consolidation policy (Step 33.2).

    Consolidation archives superseded records (an explicit correction is the
    only default trigger); per-scope TTL decay is opt-in — ``None`` means the
    scope never expires. Archived record files are renamed in place
    (``.md.archived``) and never deleted.
    """

    consolidation_enabled: bool = True
    session_ttl_days: int | None = None
    user_ttl_days: int | None = None
    org_ttl_days: int | None = None
    #: Days a superseded or TTL-expired record stays indexed — hidden from
    #: default results by the existing `valid_until` query-time predicate,
    #: but still reachable via `as_of` / `current_only=False` — before
    #: consolidation actually archives its file (Phase 2). `0` (the
    #: default) reproduces the pre-Phase-2 behavior: archive the instant a
    #: record is no longer current.
    #:
    #: **Opt-in, not on by default, and that is a measured trade-off, not
    #: caution for its own sake.** Retaining a corrected record's near-
    #: duplicate text alongside its correction gives the hybrid RRF fusion
    #: two close competitors for one query instead of one — `stale_leak_rate`
    #: stays 0.0 (the query-time `valid_until` predicate does correctly
    #: exclude the old record from every result set), but `update_accuracy`
    #: was observed to swing 0.75-1.0 run to run on the same seed in
    #: `tests/test_memory_benchmark.py` at the default `retention=7`, from
    #: exactly this near-duplicate ranking competition. A region that wants
    #: the documented `as_of` guarantee — "what did we believe last week" —
    #: sets this explicitly and accepts that cost; one that does not is
    #: unaffected.
    supersede_retention_days: int = 0

    # --- retrieval (Steps 33.6-33.9) -------------------------------------
    #: How memory takes part in a search that does not say: ``auto`` (like any
    #: other source), ``off``, ``only`` or ``prefer``. A per-call ``memory``
    #: argument always wins over this.
    default_policy: str = "auto"
    #: Let ``alias``/``preference``/``exclusion`` records steer *ranking*, not
    #: just be retrievable (Step 33.8). Off by default: a memory that silently
    #: re-orders results is a surprise unless it was asked for.
    steering_enabled: bool = False
    #: Count which memories retrieval actually returns, so salience can reflect
    #: use (Step 33.9). Off by default — it is a write on the read path, and
    #: recording what a person looks up is a choice an operator should make.
    usage_tracking: bool = False
    #: Archive the least salient records once the store exceeds this many.
    #: ``None`` = unbounded, which is the pre-33.9 behavior. Runs as the
    #: **backstop** over whatever the per-scope/per-subject caps below leave
    #: behind (Phase 5) — those isolate their own pools first, this cap
    #: cleans up anything still over budget after that.
    max_records: int | None = None

    # --- per-scope budgets (Phase 5) ----------------------------------------
    #: Mirrors ``session_ttl_days``/``user_ttl_days``/``org_ttl_days`` above,
    #: but for count rather than age. ``max_records`` alone ranks the whole
    #: store as one pool, so with the default ``SCOPE_WEIGHT`` a session
    #: flood only ever *outranks* org facts by a fixed multiplier — it never
    #: fully isolates them. These three cap each scope's own pool
    #: independently, before the global backstop runs. ``None`` = that
    #: scope is unbounded (the pre-Phase-5 behavior).
    session_max_records: int | None = None
    user_max_records: int | None = None
    org_max_records: int | None = None
    #: Cap on live records sharing one ``subject`` (across scopes), grouped
    #: the same way graph bridging already groups by subject. Records with
    #: no ``subject`` are exempt — there is no single entity to cap them
    #: against. ``None`` = unbounded.
    max_records_per_subject: int | None = None

    #: Cap on `about` edges drawn per record by the graph bridge (Step 33.7).
    #: Total `about` edges stay bounded by this times the record count — the
    #: ceiling the retired concept layer never had.
    about_max_targets: int = 3

    # --- compaction (Phase 1) ---------------------------------------------
    #: L0 write-path admission: a write whose *normalized* text already
    #: matches a live record in the same (scope, subject, kind, ACL
    #: partition) bucket reinforces that record (bumps `observations`,
    #: records the surface form as a `variant`) instead of creating a new
    #: file — collapsing exact repeats and paraphrases alike, which today
    #: are indistinguishable "new" writes. **On by default**, unlike
    #: `usage_tracking` above: this is a write-path counter with none of
    #: that flag's read-path privacy argument (recording what an agent
    #: *wrote* is not recording what anyone *looked up*), and shipped off it
    #: would be exactly as inert as `uses` is while `usage_tracking` stays
    #: at its own default.
    reinforcement_enabled: bool = True

    # --- clustering (Phase 3) ----------------------------------------------
    #: L1/L2 near-duplicate clustering and medoid promotion, on the same
    #: consolidation pass as archival and capacity pruning. Off by default —
    #: unlike reinforcement, this changes what a *default* query sees
    #: (subsumed records drop to the cold tier and stop appearing in results
    #: a plain `current_only=True` query returns), so it is an explicit
    #: opt-in rather than an assumed-safe default, the same posture
    #: `supersede_retention_days` takes.
    compaction_enabled: bool = False
    #: Exact-Jaccard threshold (over normalized content tokens — see
    #: `pheasant.artifacts.normalize`) above which two records in the same
    #: (scope, subject, kind, ACL) bucket link into one cluster.
    compaction_similarity_threshold: float = 0.6
    #: A cluster below this size is left alone. `2` is the floor — anything
    #: less is not a cluster.
    compaction_min_cluster_size: int = 2

    # --- synthesis (Phase 4) -----------------------------------------------
    #: L3: abstractive merge for what L1/L2 cannot resolve deterministically.
    #: See `MemorySynthesisSettings` — off by default, never on an automatic
    #: beat.
    synthesis: MemorySynthesisSettings = field(default_factory=MemorySynthesisSettings)

    # --- formation (observation -> candidate -> record) --------------------
    #: Reading the observation plane and proposing records. Off by default;
    #: see `MemoryFormationSettings` and ``docs/memory-formation.md``.
    formation: MemoryFormationSettings = field(default_factory=MemoryFormationSettings)
