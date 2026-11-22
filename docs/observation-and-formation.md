# Memory formation

How a region turns **what agents and people actually do** into durable memory —
and, just as deliberately, what it refuses to turn into memory.

This is the design document for the observation plane, the log tier that
carries it, and the formation policy that decides what becomes a record. For
what a memory record *is* once written, see
[The memory system](memory-system.md). For the task-oriented version, see
[Agent memory](how-to/agent-memory.md).

---

## 1. The problem

pheasant has a mature memory *store* and nothing feeding it. Every record
arrives through an explicit `memory_write` — an agent has to decide, in the
moment, that something is worth remembering, and say so. That is a high bar,
and it means the region learns nothing from being used.

Meanwhile the region already knows a great deal it throws away. Every search
carries a query, a principal, a set of criteria and a ranked result list. Every
chat turn carries a question and the passages that answered it. None of it is
recorded: `search_context`, `ask_knowledge_base` and `get_relevant_files` write
no audit row and log nothing at all.

Two proposals addressed this from opposite ends:

1. **Logs fill a memory-creation queue** — trace every API call, store the
   result with a time-based retention policy, structure it in the graph, and
   mine it for memory and for evaluation sets.
2. **Passive, session-scoped memory** — a UI session's chats do *not* become
   knowledge unless a person says so; memories are skills and tidbits that
   promote truth about the corpus; every session has one memory refined
   through dialog.

They are not alternatives. The first describes an **observation plane**: what
happened. The second describes a **formation policy**: what becomes durable, at
what scope, by whose decision. A design needs both, and the interesting part is
the boundary between them.

---

## 2. Three ways to combine them

### Option A — widen the audit trail

`source_audit_events` already exists, already carries
`(action, actor, transport, client_id, details_json)`, and is already written
on every mutation. Widen it to cover reads, and mine it.

Cheapest possible answer: no new entity class, no new dependency, no new
storage tier.

**Rejected.** It conflates an audit trail with an observation plane. An audit
row answers "who changed what"; an observation answers "what was asked and what
came back" — different retention, different volume by two orders of magnitude,
different access rules. There is no span or trace concept to hang correlation
on, and no retention policy at all, so the table grows forever. And it puts
request-rate churn onto a table that is load-bearing for source lifecycle. Its
own deliberate exclusion from Parquet exports (`analytics.py`: *"identity and
audit data is not that"*) would then be inherited by the observation plane for
the wrong reason.

### Option B — two planes, one bridge  ← **chosen**

Observations are a **new entity class**: rows, not files. They are never
chunked, never indexed, never returned by `search_context`. Deterministic rules
read them and mint **candidates**. A candidate becomes a record only through an
**admission** — a person promoting it from the Memory tab (the default), or an
explicitly enabled rule (opt-in) — and admission goes through
`MemoryStore.append` exactly like every other write.

### Option C — observations *are* memory records

The purest reading of the existing invariants. Write each interaction as a
memory record with `kind: episode` in a TTL'd `session` scope. Recall of
episodes is then search, for free, with no second retrieval path and no new
table.

**Rejected, on measured grounds:**

- `docs/memory-system.md` §12 measures **83.2 ms** for a default `sync=True`
  write. One file per interaction at request rates is untenable, and the
  batched `sync=False` path defeats the read-your-writes guarantee that makes
  the write path worth using.
- Near-duplicate episode text competing for one query under RRF fusion is the
  **same ranking damage** `supersede_retention_days` documents — and there the
  effect was large enough to move `update_accuracy` between 0.75 and 1.0 on a
  small corpus.
- Every capacity pool (`max_records`, per-scope, per-subject) would be swamped
  by episodes, and the salience formula is built for facts.

**But C is right about one thing, at one rate.** A *session digest* — one
record per session rather than one per turn — is exactly option C's shape at a
survivable volume, and it is how this design delivers "each session has a single
memory refined through dialog". See §6.

---

## 3. The boundary that makes both plans true

> **An observation is evidence. A record is memory. Only an admission crosses
> the line, and it crosses through `MemoryStore.append`.**

That single sentence resolves the apparent conflict between the two plans. Plan
1 wanted every API call captured; plan 2 wanted a UI session's chats *not* to
persist into the knowledge base. Both hold, because capture and persistence are
different things:

- A UI chat is fully observed. The observation is a row with a TTL, invisible
  to retrieval, readable only by its own principal.
- Nothing from that chat becomes knowledge until someone promotes it, or an
  operator explicitly enables a rule that does.
- When it is promoted, it is an ordinary record in an ordinary file, indexed by
  the ordinary pipeline — so memory invariant 1 (*"records are files, no second
  ingestion path, no direct index writes"*) never bends.

Everything the memory system already guarantees therefore keeps applying to a
formed record with no special case: validity and supersession, scope-derived
ACLs, salience, reinforcement, compaction, the graph bridge, the export
schema.

---

## 4. The log tier

The observation plane is the highest-volume thing in the region — one row per
request, against a corpus that changes hourly at most. Two consequences drove
the design, and both were mistakes in an earlier draft of it:

**A ledger write must not touch the request path.** Writing one row per request
into the region's database puts a write on the same PostgreSQL the lexical
search arm contends on — and `docs/architecture.md` names high-frequency
PostgreSQL text ranking as the *dominant measured search bottleneck*. Making
the write fail-soft protects correctness and does nothing for latency.

**A rollup must not run on the scheduler beat.** `SchedulerService._run` holds
`self._sync_lock` across all of its work. Compacting millions of ledger rows to
Parquet inside that lock stalls incremental sync for every source in the
region.

So: **the request path does a bounded in-memory handoff and nothing else**, and
every other piece of log work happens on its own tier with its own queue, its
own workers and its own failure domain.

```
request ──► bounded ring buffer ──► batch ──► log_tasks queue ──► --role logger
             (drops under load,                                       │
              never blocks)                                    ┌──────┴──────┐
                                                               ▼             ▼
                                                        hot: /state     cold: /exports
                                                        days, SQL       Parquet, DuckDB
```

Per request: build a span, append one dataclass to a `deque`. No lock
contention, no I/O, no database. A flusher thread batches by size or interval
and publishes **one task per batch** — the fan-in mirror of the batched
fan-out `sync/worker_pool.py` already does for file preparation, down to the
content-addressed idempotency key.

### Three storage tiers

| Tier | Where | Default retention | Read path | Serves |
|---|---|---|---|---|
| **Hot** | `interaction_events` in `/state` | `hot_retention_days: 7` | SQL | formation rules, `GET /interactions`, active retrieval |
| **Cold** | Parquet at `<exports_path>/interactions/dt=YYYY-MM-DD/` | `cold_retention_days: null` (forever) | DuckDB over `read_parquet` | eval bootstrap, analytics, audit |
| **External** | the operator's OTLP collector | theirs | their stack | tracing, alerting |

`hot_retention_days: 0` is **cold-only mode**: batches go straight to Parquet
and `/state` never grows. Formation then reads cold on its own pass — slower,
batch-only, which is fine because formation is a beat, not a request. This is
the configuration for an operator who wants the audit trail and the eval corpus
without a query-time ledger.

**A day is deleted only once its partition exists.** The roll groups expired
rows by day, writes each day's Parquet file, and deletes exactly the days that
were written. So a `/exports` volume that fills or goes read-only mid-pass costs
one day's *delay*, not one day's data: the failed day stays hot and is retried
next pass (`disposition: cold_partial`, with `deferred_partitions` naming it),
while the days that succeeded are gone from `/state` and will not be written a
second time. Deleting the whole batch on a partial write lost the failed day;
refusing to delete any of it re-wrote the days that had already succeeded, and
duplicate rows in cold storage are duplicate rows in the one place an outside
reader reads. And when `cold_enabled` is on but nothing can write Parquet (the
`analytics` extra is absent), the rows are still dropped — an un-drainable hot
store is the unbounded growth the retention policy exists to prevent — but the
report says `disposition: dropped` with `cold_unavailable: true`, rather than
calling them archived when no partition holds them.

**This does not make DuckDB a storage backend.** Cold storage is `/exports`,
written by `analytics.py`'s already-tuned Parquet writer, on a worker that is
not the indexer, off the sync path. No artifact, chunk, manifest or lease lives
there, and the ledger is derived observation with a TTL rather than operational
truth. The exclusive-file-lock trap that rules DuckDB out as a backend does not
apply: the single log worker is the only writer and writes into distinct `dt=`
partitions, while every reader opens its own in-memory connection over
`read_parquet`, exactly as `analytics.query` already does.

### Backpressure is an invariant

> **A log tier falling behind degrades to data loss, never to request latency.**

The ring buffer is bounded; overflow drops the oldest and counts it. When the
log queue depth crosses `max_queue_depth`, the flusher stops publishing and
drops, and counts that separately. Nothing on the request path ever blocks on
the log tier, and no observation failure can fail a request.

This is the same posture `bound_concurrency` already takes when it answers
`429` under saturation: shed rather than degrade. It has a real consequence for
formation, stated plainly because it would otherwise be a surprise: **formation
thresholds are counts over a stream that is sampled under load**, so a busy
region forms memory more slowly — not incorrectly.

### Why a separate queue

`log_tasks` is its own table, not a `kind` column on `index_tasks`.
Request-rate churn in the same table the indexer claims from means vacuum
pressure on PostgreSQL and constant churn on `idx_index_tasks_claim` — exactly
the burden the tier exists to avoid. The cost of separating is small because
the abstraction was already right: `drain()` is fully task-agnostic and is
reused verbatim, `LocalQueue` takes a table and a row-mapper so the race-free
conditional-`UPDATE` claim stays one implementation, and `NatsQueue` takes its
own stream, subject and durable.

At-least-once redelivery is free rather than dangerous: a row's id is
`blake2b(trace_id|span_id)` and the insert is `ON CONFLICT DO NOTHING`, so a
replayed batch is a no-op.

### Why a separate role

`--role logger` scales on `pheasant_log_queue_depth`, independently of request
traffic and of ingest. `ALL` deliberately does **not** drain the log queue —
mirroring the existing decision that `ALL` does not drain the index queue, so
that a single container stays byte-identical when a queue is switched on. In
one container the roll runs inline on the maintenance beat, bounded by
`max_rows_per_pass` so it cannot stall sync, the same way
`MEMORY_TARGETED_ARCHIVE_MAX` picks targeted deletes over a full re-sync.

Honestly scoped: buffering and batching alone remove most of the burden. The
separate tier buys *independent scaling* and keeps the roll off `sync_lock`.
For a single container it is pure overhead, which is why it is fleet-only and
off by default.

---

## 5. The metamodel

Observations are dimensioned by four things, on every row:

| Dimension | Values | Where it comes from |
|---|---|---|
| **Identity** | `user:<id>` / `group:<id>` | the caller's asserted `principal`, as everywhere else |
| **Session** | opaque string | `X-Pheasant-Session`, or the `session` tool argument |
| **Modality** | `ui` · `mcp` · `a2a` · `cli` | the surface the call arrived on |
| **Criteria** | the query's own filter object | `source_name`, `node_types`, `section`, … |

Modality is the one that did not previously exist in any usable form.
`PheasantTools` has always accepted `actor`/`transport`/`client_id`, but the
MCP server never passed them, so every MCP audit row read literally
`("mcp", "mcp", NULL)`, and HTTP hardcoded `("ui", "http", None)`.

### What one row holds

```jsonc
{
  "id": "…",  "trace_id": "…", "span_id": "…", "parent_span_id": null,
  "modality": "ui",  "operation": "/search",  "status": "ok",
  "principal": "user:ada",  "session_id": "sess-7",  "client_id": null,
  "started_at": "2026-08-29T09:14:02.118431Z",  "duration_ms": 34.2,

  "query_text":    "filewatch daemon nightly",
  "answer_text":   null,                     // set for a chat turn, capped
  "criteria":      {"mode": "hybrid"},
  "result_ids":    ["file:docs:runbook.md:branch=none"],   // → graph_nodes, chunks
  "result_paths":  ["runbook.md"],                          // → what steering matches
  "result_count":  1,   "top_score": 0.0328,
  "attributes":    {"http_status": 200, "method": "POST"}
}
```

**Ids and paths are two lists, not one.** A rule that had to sniff whether a
value was an id or a path would behave differently depending on which surface
produced the row — the opposite of the determinism every rule here rests on.
Ids join to `graph_nodes` and through them to `chunks`, which is how
`alias-cooccurrence-v1` asks whether a retrieved document actually contains the
token that found it. Paths are `relative_path`, the same grammar steering
matches against, so a `preference` rule minted from them can actually fire.

**`result_count` is the real total; the lists are capped at 50.** That is what
lets `retrieval-gap-v1` tell "nothing matched" from "matched more than we
bothered to record".

### Timestamps and traces are guaranteed, not best-effort

`trace_id`, `span_id`, `started_at` and `status` are `NOT NULL` in the schema,
and an event missing any of them is rejected before the insert — counted as
`pheasant_interaction_events_dropped_total{reason="malformed"}` rather than
silently skipped, because a defect that only ever shows up as a slightly
smaller ledger is a defect nobody finds. `parent_span_id` is nullable on
purpose: a root span genuinely has no parent.

`duration_ms` is always set, including on a call that raised — how long a
request took before it failed is exactly as interesting as how long a
successful one took. It comes from a **monotonic** clock while `started_at`
comes from the wall clock: `started_at` has to be comparable across processes
and sortable in SQL, but subtracting two wall-clock readings makes an NTP step
mid-request emit a negative or wildly inflated duration, which is nonsense in
the one column an operator reads to find slow calls.

**A trace survives pheasant's own hops.** The trace of the call in progress is
ambient, and every outbound hop injects it:

```
agent (traceparent) ─► POST /sync ──► index_tasks.payload.traceparent
                          │                      │
                          │                      ▼  claimed, minutes later,
                          │              indexer adopts the trace
                          ▼                      │
                   graph-query hop        remote preparation hop
```

Without that, a trace stops dead at the region's boundary: an operator sees
"search took four seconds" and cannot see that most of it was one graph-query
call to another pod, or that a sync a person asked for is the same event as
the indexing that happened later in a different process.

Two details that matter. The task's `traceparent` is attached **after** the
task id digest, never before — the id is content-addressed over the payload so
that two replicas answering one double-click enqueue one task, and a
per-request value inside the digest would defeat that entirely. And a
malformed or all-zero inbound `traceparent` means "no trace", never an error:
the spec reserves all-zero ids as invalid, and a sync must not fail because a
header was garbled.

The ambient trace is a context variable, which follows asyncio but **not** a
raw thread. A role that indexes locally runs its sync on a background thread,
so the hand-off there is untraced; the fleet hand-off — an `api` replica
publishing to a queue an indexer drains — happens inline in the request and is
carried. Syncs do not yet start traces of their own, so every injection point
is a no-op until one is ambient.

**A streamed answer is a child row.** `/assistant/chat/stream` returns its
response object before the answer exists, so the request's own event is
already buffered by then; mutating it afterwards would be a race whose outcome
depends on flush timing. The answer gets its own event instead, sharing the
trace and naming the request's span — which is what a parent/child span
relationship is for. Formation reads rows that have a question, which is
exactly the child and never the content-free parent.

In the graph, formation fills in node and edge types that
[the graph model](graph_model.md) has **declared since the initial build and
never emitted** — `query`, `agent_action`, and the `retrieved_by` edge — plus a
new `session` node. This is the same situation `heading`/`has_heading` were in
until 2026-08-06 and `supersedes` until 2026-08-11, and it gets the same fix.

```
session:{kb_id}:{session_id}
query:{kb_id}:{blake2b8(normalized_query)}
agent_action:{kb_id}:{trace_id}:{span_id}
```

**Graph projection is off by default, and it never covers raw events.** Only
promoted candidates and the evidence behind them become nodes. That restraint
is the retired concept layer's lesson applied before the fact rather than after
it: concepts reached 87% of a 162k-node graph and 98.6% of its edges before
anyone measured that they contributed nothing.

---

## 6. Formation

### Determinism

No model runs anywhere in this path. Rules are counting and string matching
over recorded inputs, so a pass is reproducible and a candidate's id is a
deterministic hash of what produced it. The only non-determinism in the whole
pipeline remains where it already was: the optional embedder.

Every decision is ledgered the way compaction's already is — `rule_id`,
`params_hash`, a content-addressed row id, `ON CONFLICT DO NOTHING` — so a
second pass over unchanged observations under unchanged parameters writes
nothing, and "why does this candidate exist" always has an answer.

### The rules

| Rule | Fires on | Produces |
|---|---|---|
| `session-digest-v1` | a session's own observations | one `fact`, scope `session`, subject `<session_id>` |
| `alias-cooccurrence-v1` | a query token that repeatedly retrieves artifacts never containing it, alongside a term that is present | an `alias` steering candidate |
| `path-affinity-v1` | a query family that consistently lands in one path prefix | a `preference` steering candidate |
| `retrieval-gap-v1` | a query repeatedly returning nothing above `min_score` | a surfaced gap — what the region *cannot* answer |

Two of the four produce **steering**, which is the direct answer to "memories
should be skills or helpful tidbits, and passive as much as possible".
Steering is the one part of memory that improves queries returning no memory at
all, it is excluded from result lists by default, and it is already measured:
the config-level ablation moved team-vocabulary queries from 0.029 to 0.467
while control queries moved by 0.000. These rules find such rules without
anyone writing them by hand.

#### How each one decides

**`alias-cooccurrence-v1`** looks for a word the corpus does not use. If a
query token retrieves documents and appears in *none* of them, the match came
from the rest of the query — so the token is the team's word and the
documents' own shared term is the corpus's. On a fixture where the docs say
`pheasant-flock` and everyone asks about the `router`, it proposes exactly
`router -> pheasant-flock`.

It refuses two things. A term shared by *most* of the retrieved documents is a
coincidence, so the target must appear in **all** of them; and a token that is
merely a different form of a word they do use is an inflection, not a
vocabulary gap. Without that second guard the rule proposes things like
`coordination -> check`, because "coordination" is literally absent from
documents that say "coordinates" — found on a real fixture, not imagined.

**`path-affinity-v1`** proposes `when: <token> -> prefer: <dir>/` when a query
family consistently lands in one place. The prefix is cut at a directory
boundary: `docs/deploy.md` and `docs/deployment.md` share the characters
`docs/deploy`, which is not a directory and would match neither file the way an
operator expects.

**`retrieval-gap-v1`** proposes a record naming a question the corpus keeps
failing to answer. A gap is **no results at all**, never "nothing scored well":
fused RRF scores are small positive numbers whose scale depends on how many
arms contributed, so a threshold there is a tuning knob pretending to be a
fact. `org` scope and no writer — a gap is a property of the corpus, not of
whoever happened to hit it.

`retrieval-gap-v1` is the honest form of *"more usage of the knowledge base
expands its knowledge"*. Usage cannot conjure facts the corpus does not
contain; what it can do is say precisely which questions keep going unanswered.

### The session digest

"A session has a single memory, refined through dialog" needs no new primitive.
It is a supersession chain: scope `session`, subject `<session_id>`, each
refinement naming the previous one in `supersedes`. Then:

- `current_only` (on by default) returns **exactly one** record per session;
- `as_of` reads the session's history, which is the whole point of invalidating
  rather than overwriting;
- consolidation archives the chain on the ordinary beat;
- `session_ttl_days` decays it like any other session-scope memory.

The text is a fixed template over sorted inputs — not a summary in the model
sense, and it does not claim to be:

```
Session sess-alpha (ui): 4 interactions from 2026-08-29T…Z to 2026-08-29T…Z.

Asked about:
- filewatch daemon nightly
- invoices finance service
- where does staging run

Most-consulted:
- runbook.md (3)
- billing.md (1)

Found nothing for:
- who owns the kestrel rota
```

Questions stay in the order the session asked them (dialog order, and already
deterministic — the rows are read `ORDER BY started_at`). Paths sort by count
then by path, because `most_common` alone leaves ties in insertion order, and
the record's id is a digest of this string: two passes over an unchanged
session must produce the same bytes or every beat would supersede the last.
Every list is capped — a digest is a paragraph someone reads in the Memory tab,
and an unbounded one is a transcript, which is the thing this deliberately is
not.

**"Found nothing for" is the honest form of "usage expands the knowledge".**
Usage cannot conjure facts the corpus lacks; what it can do is say which
questions keep going unanswered.

#### Why this one is written automatically

Everything else formation produces is a *candidate* a person promotes. A
session digest is written directly, and the reason is scope: it is
`scope: session`, subject that session, written by that principal — so under
`security.acl_enforced` only its own writer can read it, and it decays with
`session_ttl_days` like any other session memory. It never becomes shared
knowledge. Reaching `user` or `org` scope takes an explicit promotion, which is
exactly the "nothing persists into the knowledge base unless a person adds it"
this design is built around. What is automatic here is a session's memory of
itself.

A digest is written only for a session with at least `min_observations`
recorded interactions. One question is not a dialog, and a record per drive-by
query is the unbounded growth the capacity rules exist to prevent.

**Two principals claiming one session id get separate digests.** A session id
is caller-asserted, so that can happen; a digest that mixed them would be
readable by whichever writer owned the record — an ACL leak reached through a
field nobody authenticates.

### Admission

| | Who decides | Default |
|---|---|---|
| **Review** | a person, in the Memory tab | **on** |
| **Auto-admit** | a rule, above threshold | off |

Promotion goes through `MemoryStore.append` — the same call a person or an
agent makes — so a formed record is an ordinary file indexed by the ordinary
pipeline. There is no second ingestion path for a formed record any more than
for a written one, which is the entire point of making admission the only
crossing.

A **rejection is permanent**. The rule that proposed it re-derives the same
proposal on the next beat and the store refuses to reopen it, because
re-suggesting what somebody just declined is the fastest way to make a review
queue worth ignoring. A pending proposal nobody acted on expires after
`candidate_ttl_days`; a rejection never does.

Surfaces: `GET /memory/candidates`,
`GET /memory/candidates/{id}/evidence`, `POST /memory/candidates/{id}/promote`,
`POST /memory/candidates/{id}/reject`, the MCP tools `list_memory_candidates` /
`promote_memory_candidate` / `reject_memory_candidate`, and a **Proposed**
section at the top of the Memory tab showing each proposal with the evidence
behind it.

Review is the default because plan 2 asked for it and because it is the
conservative reading: a candidate is a suggestion until a person agrees. Every
candidate shows the evidence that produced it, in four layers, because a
reviewer asks four questions in order and each one is narrower than the last:

| Layer | The question | What it shows |
|---|---|---|
| 1. the row | what is claimed | rule, kind, scope, how strong |
| 2. expand | on what basis | the calls: what was asked, what came back |
| 3. expand again | how do I check it | the spans: trace, parent, when, how long |
| 4. select a key | what is actually behind it | a side panel on that one call — the ids in full, the criteria the search ran under, and the content keys it returned, each resolving to the text it names |

Everything below layer 1 is fetched on open, and layer 4's content lookup only
on selection: a hundred proposals must not be a hundred evidence queries, and
one opened proposal must not be a chunk fetch per result id.

Layer 4 resolves a key through `GET /nodes/content`, which is ACL-guarded, so a
reviewer sees the text behind an id exactly where they could have searched for
it. A key that does not resolve is reported rather than hidden: result ids are
content-addressed, so a 404 means the text has changed or been removed since
the call — which is itself the answer, and the same posture layer 2 takes when
evidence has aged out of the hot window.

`auto_admit` is off for the same reason `compaction_enabled` and
`supersede_retention_days` are off: it changes what a default query returns,
which is a decision an operator should make rather than inherit. An
auto-admitted record carries a `formed` tag and its candidate records
`admitted_by`, so a machine-formed record is always distinguishable from a
written one — the posture `llm-synthesized` already establishes for synthesis.

---

## 7. Evaluation

The ledger is a real evaluation corpus: actual queries, from actual principals,
with the results the region actually returned and — where a promotion followed
— the record that answered it. `pheasant eval bootstrap` turns that into a case
set `memory/benchmark.py` consumes, alongside the synthetic generator that
exists today.

```bash
pheasant eval bootstrap            # → <exports_path>/eval/cases.json
```

Distinct *questions*, not rows: the same query asked forty times is one case
with `asked: 40`, which is both a smaller file and a better weight for a
harness that wants to know what matters rather than what repeated. A question
that found nothing is kept and counted — it is the most useful case in the set,
because it is the one the region fails.

**The export is derived and de-identified, never the raw table.**
`analytics.py` excludes `source_audit_events` and `idp_groups` from Parquet
exports on principle — *"who a principal is, which groups they are in, and what
they did. An export is a file people pass around; identity and audit data is
not that."* An interaction ledger is exactly that category, so principal and
session become **per-export pseudonyms**: stable within one file, so a harness
can still tell that two cases came from one conversation, and useless for
answering "what did Ada ask". The salt is random per export, so two exports
cannot be intersected to re-identify anybody. Nothing else leaves — no trace
ids, no client ids, no timings. Cold storage lives outside
`parquet/<kb_id>/` for the same reason.

---

## 8. Isolation

An observation inherits the same rule its subject does: a caller reads only
observations written under its own principal, unless `security.acl_enforced` is
off. A formed record's ACL is derived from its scope by
`security/acl.normalize_acl`, unchanged — `org` shared, `user` and `session`
readable only by their writer.

A session digest is `scope: session`, so it is confined to its own session and
its own principal by exactly the machinery that already confines session
steering.

---

## 9. Invariants

1. **An observation is never a memory record.** No file, no chunk, no index
   entry, never returned by search.
2. **Admission is the only crossing**, and it goes through
   `MemoryStore.append`.
3. **The request path never blocks on the log tier**, and no observation
   failure can fail a request.
4. **Under pressure the tier loses data, not latency.**
5. **Formation is deterministic** and every decision is ledgered with its
   `rule_id` and `params_hash`.
6. **A repeat pass over unchanged observations writes nothing.**
7. **Raw events never enter the graph.**
8. **Cold storage is `/exports` and enforces nothing** — put the access
   control on the directory.

---

## 10. Known limits

- **A session id is caller-asserted**, exactly like `principal`. One caller can
  claim another's session. Same category as *"`supersedes` is not
  authorization-checked"*: by design for now rather than by decision.
- **The ledger records query text, answers and principals.** `redact_text`
  drops all free text and `max_answer_chars: 0` drops answers alone; the honest
  default is that an operator enabling observation is choosing to record them.
- **Formation counts a sampled stream.** Under sustained load, thresholds are
  reached later, and a region that sheds heavily forms memory slowly.
- **There is no A2A protocol surface to instrument.** `modality: a2a` is
  reserved and is emitted only by contract traffic until one exists.
- **A gap candidate names a question, not an answer.** `retrieval-gap-v1` can
  tell an operator what the corpus is missing; nothing here can supply it.
