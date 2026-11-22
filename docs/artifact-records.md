# The memory system

How agent memory works in pheasant, end to end — what a memory *is*, how it
gets written, indexed, filtered, ranked and forgotten, and which invariants the
subsystem must not break.

This is the explanation. For the task-oriented version — enable it, write a
record, recall it — see [Agent memory](how-to/agent-memory.md). For the config
keys, see [Configuration](configuration.md#memory-deprecated-compatibility-settings).

---

## 1. The load-bearing decision: a memory is source content

A memory record is **one append-only Markdown file** under a built-in
`type: memory` filesystem source. Frontmatter above, the remembered text below.

Everything else follows from that. Indexing a memory is the ordinary
deterministic chunk → embed → graph pipeline that indexes any other file, so:

- **recall is search** — there is no separate retrieval path to keep in sync,
  and a memory competes for a slot on the same ranking as a document;
- **no LLM ever touches the memory path**, because no LLM touches the indexing
  path (design pillar; see [CLAUDE.md](https://github.com/esatt10/pheasant-kb/blob/main/CLAUDE.md) rule 1);
- **idempotency comes for free** — the engine's pre-read sha256 skip means
  re-syncing unchanged records is zero work;
- **the bytes are never destroyed.** "Forgetting" is a rename to
  `<id>.md.archived`, which stops matching the source's `**/*.md` include glob.

The write path only ever *creates files*. It never reaches into the index
directly.

```
memory_write / POST /memory
        │
        ▼
  MemoryStore.append ──────► <root>/<scope>/mem-<instant>-<digest>.md
        │                                    │
        │ (sync=true, the default)           │  ordinary filesystem source
        ▼                                    ▼
   incremental sync ────► chunks + FTS5 ─┬─► text arm
                     └──► vectors ───────┼─► vector arm
                     └──► graph nodes ───┴─► graph arm
                     └──► memory_records (projection)
                                │
                                ▼
                        MemoryPolicy / Steering
```

---

## 2. The modules

| Module | Owns |
|---|---|
| `memory/store.py` | The record file format, id derivation, append-only writes, archiving, consolidation |
| `memory/projection.py` | Deriving the `memory_records` SQLite rows from the files |
| `memory/policy.py` | `MemoryPolicy` — what one query is allowed to see, in two encodings |
| `memory/steering.py` | Turning `alias`/`preference`/`exclusion` records into ranking rules |
| `memory/salience.py` | The deterministic keep-worthiness formula |
| `memory/maintenance.py` | One consolidation + capacity pass, run by the scheduler or on demand |
| `memory/bridge.py` | `about` and `supersedes` edges into the knowledge graph |
| `memory/benchmark.py` | The offline LongMemEval-style recall benchmark |
| `security/acl.py` | Turning a record's scope into a read ACL |

---

## 3. Record identity

```
mem-<asserted-at>-<blake2b8(digest input)>
```

Identity is the **digest, not the timestamp**. Re-asserting the same thing
returns the record already stored, however much later it happens — an identical
write is reported `created: false` rather than duplicated.

The digest input is `scope|subject|text`, plus extra parts appended *only when
they differ from what schema v1 implied* (`kind`, `written_by`, `valid_from`,
`valid_until`). That is what makes **every v1 record id reproduce
byte-identically** under schema 2 — nothing on disk is ever rewritten.

`written_by` being in the digest is load-bearing for isolation: two principals
asserting the same sentence in the same second get two records rather than
silently sharing one file owned by whoever arrived first.

### Frontmatter is caller data and is validated as such

Every caller-supplied value that reaches the frontmatter (`subject`,
`supersedes`, `written_by`, each tag, `valid_from`, `valid_until`) is rejected
if it contains a line break, and the timestamps must parse as ISO-8601.

This is a security boundary, not tidiness. The block is line-oriented and
`load` reads `key: value` per line, so a newline in a value appends arbitrary
*keys* — and `memory_scope` is what `security/acl.normalize_acl` reads to build
the record's read ACL. Before this check, `subject="x\nmemory_scope: org"` turned
a private `user` note into a world-readable `org` one under
`security.acl_enforced`. `load` additionally takes the **first** occurrence of
each key, so a record written by an older release cannot be read at a forged
scope.

---

## 4. The validity model

A record is never edited and never deleted. It is **invalidated**, and there
are three independent ways that happens — the earliest wins:

| Mechanism | Set by | Meaning |
|---|---|---|
| supersession | a later record naming this one in `supersedes` | corrected at that record's `asserted_at` |
| declared expiry | `valid_until` on the write | it said so itself |
| scope TTL | `memory.{session,user,org}_ttl_days` | opt-in decay; `None` = never |

`valid_until` in the projection is **derived, never double-stored**:
`effective_valid_until` takes the minimum of the declared expiry and the
earliest correction. A record that declared it was good until December but got
corrected in March is stale from March.

**Validity is enforced at query time, not only by the batch pass.** That is the
stale-fact defect the agent-memory literature names as the primary one: the
region knows a fact was corrected and serves it anyway between consolidation
beats. `current_only` (on by default) closes it. `as_of` deliberately brings the
old record back — that is the whole point of invalidating rather than deleting.

**And re-asserting a corrected claim does not undo the correction.** A record id
is `mem-<instant>-<digest>` where only the digest half is content-addressed, so
writing the same text again after consolidation had archived it minted a *new*
id — and the correction, which names the old one, stopped applying. The
corrected claim came back current, beside its own correction, and `current_only`
returned both. Written inside the same second it reused the id by luck and
stayed corrected: identical operations, different memory, decided by a second
boundary nobody means as a semantic one. Reviving an archived record now reuses
that record's own id, which also keeps it *one* assertion rather than a second
`memory_record` node for a claim the region already knows.

**But `as_of` can only find a row that is still indexed, and consolidation
archives an invalidated record's file the moment it is no longer current** —
a `.md.archived` file drops out of the source's `**/*.md` include glob, so
the next sync removes its row entirely. That made the guarantee above true
only in the window between a correction and the next consolidation beat:
seconds at agent write rates, since an agent can call `memory_consolidate`
itself. `memory.supersede_retention_days` (Phase 2, default `0`, opt-in)
keeps an invalidated record's file — and so its row — around for that many
days past the instant it stopped being current, before archiving it. No new
column and no policy change: the *same* `valid_until` predicate that already
hides a superseded record from default results is what makes it reachable
through the window via `as_of` / `current_only=False`; only *when* the file
leaves the index moves, never what a query sees while it is still there. Left
at its default of `0`, behavior is unchanged from before Phase 2 — a
deliberate trade-off, not an oversight: keeping a corrected record's
near-duplicate text indexed alongside its correction gives hybrid RRF fusion
two close competitors for one query instead of one, and that measurably
affects ranking in a small corpus (see `tests/test_memory_benchmark.py`'s
comment on `update_accuracy`). A region that wants "what did we believe last
week" to stay answerable sets this explicitly and accepts that cost.

---

## 5. Query-time policy

`MemoryPolicy` is one knob, spelled identically on MCP `search_context`,
`POST /search`, `POST /relevant-files` and `POST /assistant/chat`.

| Field | Default | Meaning |
|---|---|---|
| `mode` | `auto` | `auto` / `off` / `only` / `prefer` |
| `scopes` | all | restrict to `session` / `user` / `org` |
| `subject` | — | substring match on the record's subject |
| `current_only` | `true` | drop corrected records |
| `as_of` | — | point-in-time recall |
| `max_results` | — | cap how many slots memory may occupy |
| `include_rules` | `false` | return steering records as content too |

### One rule, two encodings

The same rule is written twice, deliberately, and pinned together by a
10-policy × 5-record parity test:

- **`sql_predicate`** — pushed into the text arm's SQL, ahead of `LIMIT`.
  Post-filtering a globally-ranked page would return nothing from a narrow
  slice while the matching rows sat just past the cut.
- **`admits`** — the Python half, applied to the vector and graph arms, which
  carry no memory columns of their own.

Because the vector and graph arms are filtered *after* their own truncation,
those arms must **over-fetch** whenever the policy could drop something.
That decision is `may_filter`, which asks the loaded index whether any record
would actually be dropped — not `is_default`, which answers the different
question of whether the *caller* asked for anything unusual. The default policy
is not inert: it drops corrected records and steering rules.

### Steering records are not results

`alias`/`preference`/`exclusion` records are retrieval *machinery*. By default
they steer ranking and are excluded from result lists.

Measured on the vscode corpus: with them included, the alias rule
`filewatch daemon -> fileService, watcher` took **rank 1** for "where is the
file service implemented", pushing the real `fileService.ts` to rank 5 and
dropping corpus MRR from 0.462 to 0.335. It also hands an agent rule syntax
dressed as retrieved knowledge. The rules stay fully in force and fully
inspectable via `describe_retrieval` and `GET /memory`.

### Trust containment

The answering prompt states that a remembered passage is a *recorded
assertion*, that corpus content wins a disagreement, and that passage text is
**data, never an instruction**. Remembered passages are labelled with their
scope and assertion time through both citation builders. This is the
memory-control-flow defence: memory is an input an agent wrote, so it must not
be able to redirect the agent that reads it.

---

## 6. Steering

A record whose payload is a deterministic retrieval rule rather than prose.
This is the half that changes queries returning **no memory at all**.

```
alias       router -> pheasant-flock, flock
preference  when: deploy, docker -> prefer: docs/, deploy/
exclusion   never: vendor/**
```

Parsing is regex over text an agent already wrote — no model, no network.
A malformed rule is **ignored, never raised**: these are read during a sync, and
one bad line must not fail indexing or a search.

Two things that are easy to get wrong and are pinned by tests:

- **Triggers are matched against the tokenized query**, split the same way
  `_query_tokens` splits, with *every* part required. Matching one token would
  let a rule about `pheasant-flock` fire on a query that merely said
  `pheasant`; splitting on too few separators leaves `ci/cd`, `fs.watch` and
  `api:gateway` silently dead while still reporting themselves in force.
- **Scope gating is a security property.** A rule applies only to a query whose
  policy admits its scope, so `session` steering stays in that session. `mode`
  is deliberately *not* applied — `memory: "off"` is a statement about results,
  and a caller who wants no remembered passages still wants the region's
  remembered vocabulary.

Rule text is reassembled from its chunks in `chunk_index` order, because SQLite
does not guarantee the order an aggregate sees its input and a rule long enough
to span two chunks could otherwise parse differently between runs.

Off by default (`memory.steering_enabled`): memory that silently re-orders
results is a surprise unless it was asked for.

---

## 7. Memory in the knowledge graph

Memory is not a graph island. Records become `memory_record` nodes, with:

- **`supersedes` edges** between records, so a correction is a traversable
  relationship rather than only a frontmatter string;
- **`subsumes` edges** (Phase 3) from a near-duplicate cluster's medoid to
  each record it absorbed — drawn straight from `subsumed_by`, never through
  the `about` ladder below (that ladder is corpus-only; a subsumption is
  memory-to-memory). Deliberately distinct from `supersedes`: a subsumed
  record is redundant but still *true*, so this edge (and the column it is
  drawn from) never feeds `effective_valid_until`. See §8.
- **`about` edges** to what the record is *about*, drawn by a precedence
  ladder — **reference → symbol → heading → entity**, first rung wins, capped
  at `memory.about_max_targets`.

The ladder exists because `entity` cannot be the bridge on its own: entity
extraction runs only for `.md/.txt/.html/.xml`, so an entity-only bridge looks
fine on Markdown and is a silent no-op on PDFs or non-Python code.

The lexical/BM25 rung is deliberately **not** materialized — the search index
answers it at query time, and materializing it is how the retired concept layer
reached 98.6% of all edges.

Coverage is reported, not silent: `describe_retrieval` carries
`graph:{records, bridged, unbridged, by_signal}`, computed live.

---

## 8. Salience, reinforcement and bounded growth

At agent write rates a fact is rarely asserted once — it is restated,
paraphrased and re-derived, and each restatement used to be indistinguishable
from a new fact. **Reinforcement** (`memory.reinforcement_enabled`, on by
default) closes that gap on the write path: before creating a file, a write is
checked against a *normalized* form of its text (`pheasant.memory.normalize` —
Unicode-folded, whitespace-collapsed, stripped of leading articles and framing
clauses like "note that", but never token-sorted and never stripped of
quantifiers like "not" — see that module for why) scoped to the same
`(scope, subject, kind, ACL partition)` bucket a reader could legally see as
one thing. A match — whether the write is byte-identical or merely a
paraphrase — does not create a new record: it bumps that record's
`observations` counter and `last_seen` timestamp, and remembers the submitted
surface form as a `variant` (bounded to 8) when it differs from what is
stored. `memory_write`'s response gains `outcome: "created" | "reinforced"`
alongside the existing `created` boolean, and `submitted_text` when what was
written differs from what is now stored.

**A fold never targets a record a default query cannot return.** Reinforcement
answers a write with `created=False` / `outcome="reinforced"` — "we already
hold this" — and that is only true if the record it folded into is one
retrieval will actually surface. Two ways a record fails that test, and the
write path refuses both:

- **Superseded or expired.** Re-asserting a claim a later record corrected is
  a *new* assertion about the present, not a restatement of history, so it
  becomes its own record and retrieval can see the conflict. Folding instead
  would report success while the claim stayed unreachable —
  `supersede_retention_days` (§4) keeps corrected records queryable for days,
  which is exactly how long that window would otherwise stand open.
- **Demoted to `cold`.** Here the fold is right but the *target* is not: the
  cluster's canonical record is what a default query returns, so `subsumed_by`
  is followed to it and the observation credit lands there.

With no projection to consult at all (a cold or absent `/state`) a
byte-identical re-write still dedups on the file, because a missed *fold*
costs a counter where a missed *dedup* costs a duplicate record.

Two principals can never reinforce each other's `user`/`session`-scope
records — the ACL partition is `written_by`, mirroring
`security.acl.normalize_acl`'s own partition exactly — because a shared record
could only carry one writer's ACL. `org` records, being shared by design, do
reinforce across writers.

Salience is the one thing memory learns from *being used*. A documented
deterministic formula over recorded inputs — no model, no sampling — so a
pruning pass is reproducible rather than a judgement call:

```
evidence = 1 + 0.5·log1p(uses) + 0.5·log1p(observations)
salience = recency(90-day half-life, anchored on the most recent of
           asserted_at/last_seen/last_used_at) × evidence × scope_weight
scope_weight: org 1.25, user 1.0, session 0.6
```

`uses` is counted **after truncation**, so a record is credited for being
*served*, not merely considered, and usage tracking stays off by default — it
is a write on the read path. `observations` carries no such argument (it
records what was *written*, not what anyone *looked up*) and is on by default;
in a region that never enables usage tracking, `observations` is what keeps
salience from degenerating to age-and-scope-only ranking.

`memory.max_records` archives the least salient beyond the cap, via the same
in-place rename consolidation uses, with the score written back so a prune is
explainable. Unbounded by default.

**Per-scope and per-subject budgets** (Phase 5) run *before* that global cap,
each its own isolated pool: `session_max_records`, `user_max_records` and
`org_max_records` mirror the existing `session_ttl_days`/`user_ttl_days`/
`org_ttl_days` trio, and `max_records_per_subject` caps how many live records
can accumulate about one named entity, across scopes. This closes a real gap
`max_records` alone leaves open — ranking the whole store as one pool means a
`session`-scope flood only ever *outranks* an `org` fact by the fixed
`scope_weight` multiplier above, it never fully protects it, and a large
enough flood still eventually prunes it. Each cap defaults to unbounded; a
record can be doomed by more than one rule (its own scope's cap, its
subject's cap, and the global backstop), and is archived once regardless.

**Steering records are exempt from every cap here in both directions** — they
neither consume slots nor get archived. Ranking a deliberate operator-written
rule against ordinary facts, on a formula built for facts, meant crossing a
cap could silently switch off an `exclusion` and change ranking for every
future query. The number of rules actually in force is bounded separately
by `steering.MAX_RULES`.

### Compaction: clustering near-duplicates, not just deduplicating them

Reinforcement (above) only folds *exact equivalents* — the same claim,
different casing or framing. It cannot fold two records an agent phrased
genuinely differently but which still assert the same thing; that is a
*similarity* judgement, not an equivalence, and the write path is
deliberately never allowed to make one (nothing lossy happens before a
caller sees the result of their own write). `memory.compaction_enabled`
(off by default — see below) runs that judgement offline, on the same
consolidation pass as archival and capacity pruning:

1. **Bucket** live (`tier='hot'`) records by the exact same
   `(scope, subject, kind, ACL partition)` key reinforcement uses — never
   across scope, never across a steering kind, never across principal at
   `user`/`session` scope.
2. **Cluster** within a bucket by exact Jaccard similarity over normalized
   content tokens (`memory.compaction_similarity_threshold`, default `0.6`),
   using a postings index for candidate generation — the same shape as the
   corpus's own (retired, dormant) `_similarity_edges` pass, reused here
   because a memory bucket is small enough that an inverted index plus exact
   Jaccard needs no probabilistic signature.
3. **Promote a medoid**: within each cluster of at least
   `memory.compaction_min_cluster_size` (default `2`) members, the record
   maximizing summed similarity to every other member becomes canonical —
   ties break on the lowest `record_id`. Nothing is machine-authored: the
   "summary" is a real record a real writer wrote, so provenance stays
   exact.
4. **Demote the rest**: every other member gets `tier='cold'` and
   `subsumed_by=<canonical>`. **No file is renamed or deleted** — a demoted
   record stays on disk, stays indexed, and is fully reachable through an
   explicit `tiers=("cold",)` policy or the same `current_only=False`/
   `as_of` that already reaches retained superseded history (`allowed_tiers`
   widens to `("hot","cold")` under the same signals that widen the validity
   window — §5). The canonical record's `observations` absorbs the cluster
   (each demoted member's own count, plus one for having existed at all), so
   importance concentrates on the survivor instead of splitting across
   near-duplicates.
5. **Ledger**: every subsumption is appended to `memory_compactions`
   (`op`, `member_id`, `canonical_id`, `rule_id`, `params_hash`, `at`) — a
   permanent, append-only answer to "why is this record cold" that survives
   independently of the `subsumed_by` pointer itself. Idempotent: the row id
   is a deterministic hash of its own fields, so a second pass over
   unchanged content with unchanged parameters writes nothing new.

**Off by default, unlike reinforcement.** Reinforcement only ever changes
what `created` means for an exact restatement; compaction changes what a
*default* query returns (a subsumed near-duplicate stops appearing), which
is the same class of decision `supersede_retention_days` is — an explicit
opt-in, not an assumed-safe default.

### Synthesis: the one non-deterministic tier

Deterministic compaction folds *redundancy*. It cannot **merge**: three
concrete cases it provably does not cover —

1. **Complementary partials** — "runs in us-east-2" and "owned by ada" are
   not near-duplicates, both are true, and the compact form is one record
   stating both. No selector can emit a sentence present in neither input.
2. **Progressive refinement** — ten records each adding one detail to the
   same subject compact correctly only into one record stating all ten.
   Medoid promotion (L2) picks whichever single member overlaps most and
   leaves the other nine cold, carrying detail the canonical record lacks.
3. **Abstraction across instances** — "deploy failed Monday", "…Tuesday",
   "…Wednesday" → "deploys have been failing all week" is generalization,
   not selection.

`memory.synthesis` (Phase 4) is a model call for exactly this gap, and
nothing more. **The model is a writer, never an indexer** — it produces one
ordinary record through the same `MemoryStore.append` path a human or
agent write uses, and the deterministic pipeline then indexes it
identically to every other record. No LLM call ever happens on the
indexing path (CLAUDE.md rule 1), and it is **never invoked automatically**
— not from the scheduler beat, not from consolidation — only through the
explicit `memory_synthesize` MCP tool / `POST /memory/synthesize`. `pytest`
stays network-free by construction: the default `provider: auto` resolves
to nothing reachable when no API key is set, so a pass degrades to
`{"skipped": "no model reachable"}` rather than needing a mock.

Configuration mirrors `AssistantSettings` field for field — an operator who
has already pointed the assistant at a model recognizes every knob:

```yaml
memory:
  synthesis:
    enabled: false          # opt-in, and never automatic regardless
    provider: auto           # auto | anthropic | openai | gemini | none
    model: null               # explicit model id; null = provider default
    max_calls_per_pass: 20
    max_input_chars: 6000     # combined member text; over this, skip the cluster
    min_cluster_size: 3
    max_jaccard: 0.55         # above this, medoid promotion already has it
```

Candidates are subject-tagged buckets (the same `(scope, subject, kind, ACL
partition)` isolation reinforcement and clustering already enforce — a
cluster never crosses scope, kind, or principal) still `tier='hot'` after
L1/L2 has run, **and below `max_jaccard`**. That last gate is what decides
what the model is asked to do without a model: a bucket of near-identical
rephrasings is precisely what medoid promotion resolves losslessly and for
free, so it is skipped, and spend goes only to the shape L1/L2 provably
cannot handle. It matters most with `compaction_enabled` off (the default),
where nothing has been demoted and every near-duplicate bucket would
otherwise reach the model. Similarity is measured with the same tokenizer
and normalizer L1 clusters with, so the gate and the tier it defers to
cannot disagree about what "near-duplicate" means. A successful merge **subsumes its members** through the
identical mechanism medoid promotion uses — `tier='cold'`,
`subsumed_by=<new record>`, a `memory_compactions` row — not `supersedes`:
the inputs are not corrected, they are redundant-but-still-true, exactly
compaction's own category. The synthesized record inherits its cluster's
`written_by` (never a synthetic identity — a `user`/`session` cluster
shares one writer by construction, and using anything else would make the
merge unreadable by the very principal whose facts it summarizes), carries
the tag `llm-synthesized`, and its ledger row's `rule_id`/`params_hash`
records the model id — a generated record is always distinguishable from a
written one.

**Efficiency, cheapest first:** the model never sees anything L1/L2 already
resolved; a repeat pass over an already-subsumed cluster costs nothing
because its members are no longer `tier='hot'` and drop out of candidacy
entirely (the primary guarantee); a content-addressed `params_hash` (model
id + sorted member ids) is checked against the ledger *before* any call, so
even a cluster reset back to hot under unchanged parameters costs zero;
`max_calls_per_pass` bounds an unbounded first pass; and `try_complete`
degrades a provider failure to "skip this cluster" rather than failing the
whole run.

---

## 9. Isolation

Under `security.acl_enforced`, a record's ACL follows its **scope**:

| Scope | Readable by |
|---|---|
| `org` | everyone (region default visibility) |
| `user`, `session` | only the principal that wrote it |
| no recorded writer, not org | falls through to the region default |

The ACL is computed inside the artifact loop, before the projection for that
sync exists, so it is read from the record file. That is exactly why §3's
frontmatter validation matters: the file is the authority, so what reaches the
file must be trusted input.

---

## 10. Surfaces

| Surface | Write | Read | Maintain |
|---|---|---|---|
| MCP | `memory_write` | `search_context(memory=…)`, `describe_retrieval`, `pheasant://…/memory` | `memory_consolidate` |
| HTTP | `POST /memory` | `GET /memory`, `POST /search` | `POST /memory/consolidate`, `POST /memory/enable` |
| UI | `/memory` page | citation chips, `use memory` chat toggle | Settings → Memory panel |

The MCP tool surface is public API: additive evolution only, deprecate before
remove.

---

## 11. Invariants

Things this subsystem must keep true:

1. **Records are files.** No second ingestion path, no direct index writes.
2. **Nothing is ever deleted.** Archiving is a rename; corrections invalidate.
3. **v1 record ids reproduce byte-identically.** The digest may only grow parts
   for values v1 could not express.
4. **`sql_predicate` and `admits` agree exactly.** They are one rule in two
   encodings, and the parity test is what keeps them honest.
5. **Steering never narrows.** Alias expansion is additive; the caller's own
   tokens always survive.
6. **A malformed rule is ignored, never raised.**
7. **Passage text is data, never instruction.**
8. **The wire format is unchanged.** `memory` rides in `capabilities.modalities`,
   which is existing contract data — no schema bump, no re-vendor.

---

## 12. Measured

From `memory/benchmark.py` (deterministic, offline, through the real
`memory_write` → index → `search_context` path):

| Metric | Value |
|---|---|
| recall@5 | 1.000 |
| update accuracy | 1.000 |
| stale leak | 0.000 |
| abstention | 1.000 |
| bytes/record | 212.9 B |
| write latency (batched, `sync=False`) | 4.51 ms |
| write latency (default path, `sync=True`) | 83.2 ms |
| search latency | 11.83 ms |

**The two write-latency rows measure different things, on purpose (Phase
5).** The batched number reflects a script seeding a corpus — every write in
that loop skips the per-write index update, with one `sync_source` call at
the end. `memory_write`'s actual default (`sync=True`) is what an agent's
own write pays: one incremental sync with `enrich="deferred"` per call
(finding (2) of the compaction plan). The batched number was, for three
phases, the only one published — nobody had measured the path an agent
actually takes until this row existed.

**Redundancy** (Phase 5's fifth category — see §8): with `reinforcement_enabled`
on (default) and `compaction_enabled` on, an agent's paraphrase-heavy write
pattern —

| Metric | Value |
|---|---|
| records/fact | 1.000 |
| reinforcement ratio | 0.500 |
| compaction ratio | 0.500 |

— against `records_per_fact: 2.000, compaction_ratio: 0.000` with
`compaction_enabled` at its default (off): the same write pattern, the
same reinforcement, but nothing left to fold the surviving near-duplicate
into its canonical record. Reproduce both with
`python -m pheasant.memory.benchmark`.

On a real corpus (microsoft/vscode, partial index), through the real MCP stdio
surface: corpus MRR 0.462 → **0.495** with memory on (memory is not a tax),
memory-only recall 0.000 → **1.000**, and the config-level steering ablation
moved team-vocabulary queries **0.029 → 0.467** while control queries moved
**+0.000**.

---

## 13. Known limits

- **Bridge and vector behaviour at ~2,000-file scale with a real embedder is
  still unmeasured.**
- **`max_records` bounds recallable facts, not rules.** A store that
  accumulates thousands of steering records grows unbounded on disk; only the
  first `MAX_RULES` are ever in force.
- **`supersedes` is not authorization-checked.** Any writer may supersede any
  record whose id it knows. Injection can no longer *forge* the field, but the
  API accepts it as a legitimate parameter, and cross-principal correction is
  currently by design rather than by decision.
- **Taxonomy is not published on the Synapse contract** — the outline is
  region-local retrieval structure, not routing signal.
- **Reinforcement, tier, and observation counters live only in `/state`.**
  A `/state` restore predating N reinforcements resets those counters —
  record *content* is never at risk (the `.md` files are the truth, and
  restoring an older `/state` cannot lose or alter a written record), but
  compaction's inputs (`observations`, `tier`, `subsumed_by`, the
  `memory_compactions` ledger) are covered by `pheasant backup`, not by the
  record files themselves.
