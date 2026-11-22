# Give your agents durable records

Agents (and people) record scoped facts with `record_artifact` over MCP or
`POST /artifacts/records` over HTTP, and those facts become ordinary indexed
knowledge — searchable with the same `search_context` / `POST /search` surface
as everything else, provenance included. No separate database, no second
retrieval path. The first record creates the `artifact-records` source under
`/state`; nothing has to be configured first.

> For how the whole subsystem fits together — the validity model, the two
> encodings of the query policy, the graph bridge, salience and the invariants
> — see [Artifact records](../artifact-records.md).

## 1. Where records live

Nowhere you have to configure. The first `record_artifact` creates the
`artifact-records` source (type `artifact`) at `/state/artifact-records`. On a
fleet, where an api replica mounts `/state` read-only, the record file is
handed to the indexer through the artifact outbox and written there. A region
upgraded from 1.x that declared an `agent-memory` source (type `memory`) keeps
it, and records keep going there.

Either way it is a folder of Markdown: the scheduler, include globs and backups
treat it like any other source.

## 2. Write records

From an MCP agent (Claude Code, Cursor, …) via the `record_artifact` tool, or
over HTTP:

```bash
curl -s localhost:8765/artifacts/records -X POST -H 'content-type: application/json' -d '{
  "text": "The staging cluster lives in us-east-2.",
  "scope": "org",
  "subject": "infra",
  "tags": ["deploy", "aws"]
}'
```

Scopes are `session`, `user`, or `org`; `subject` identifies whose memory
it is (a session id, a user, a team). With `sync: true` (the default) the
record is indexed before the call returns — **read-your-writes**: the very
next `search_context` finds it.

**Field rules.** `subject`, `supersedes`, `written_by` and each tag must not
contain a line break, and `valid_from` / `valid_until` must be ISO-8601
instants; a write that breaks either rule is rejected with a `400`. The
frontmatter block is line-oriented and a record's `memory_scope` is what
decides its read ACL, so a newline in a field value would otherwise let a
caller forge frontmatter and escalate a private note to `org`. Everything else
— colons, unicode, punctuation — round-trips unchanged.

**Restating a fact reinforces it, it does not duplicate it.** An agent that
asserts the same thing in fresh words every time it comes up — the normal
case at agent write rates — does not grow the store one record per
paraphrase. `artifacts.learning.reinforcement_enabled` (on by default) checks a new
write's normalized text (case/whitespace/framing folded, nothing else — see
`docs/artifact-records.md` §8) against the same `(scope, subject, kind)` bucket
before creating a file; a match reinforces the existing record instead. The
response's `outcome` says which happened:

```jsonc
{"created": false, "outcome": "reinforced", "record": {"record_id": "mem-..."}, "submitted_text": "the staging cluster runs in us-east-2"}
```

`outcome` is `"created"` (a genuinely new record), `"reinforced"` (folded
into an existing one — exact repeat or paraphrase alike), or `"duplicate"`
(reinforcement disabled, exact repeat only — the pre-compaction behavior).
Two principals can never reinforce each other's `user`/`session`-scope
memories this way; see §8 of the memory-system doc.

## 3. Recall is just search

```bash
curl -s localhost:8765/search -X POST -H 'content-type: application/json' \
  -d '{"query": "where does staging run?", "mode": "hybrid"}'
```

Memory records rank alongside (and link into) the rest of the knowledge
graph. `GET /artifacts/records?scope=org` lists raw records for inspection.

## What a record looks like on disk

Append-only Markdown, one file per record, deterministic id — greppable,
diffable, human-readable, and versioned:

```markdown
---
schema_version: 1
record_id: mem-20260716T120000Z-9f2ab31c74d05e88
memory_scope: org
memory_subject: infra
asserted_at: 2026-07-16T12:00:00Z
tags: deploy, aws
---

The staging cluster lives in us-east-2.
```

Writing the identical fact again is a no-op (`created: false`, no
re-index). Re-asserting a fact later creates a new record — recency is
signal.

## Corrections, decay, and consolidation

Pass `supersedes: <record_id>` when a memory corrects an earlier one. The
old record is then no longer *current* — `GET /artifacts/records?current_only=true`
filters it immediately — and the next **consolidation pass** archives it:
the file is renamed `<id>.md.archived` in place (bytes preserved forever,
nothing deleted), so search stops surfacing the stale fact while the audit
trail remains on disk. Below a few hundred archived records the pass drops
just those records' indexed state directly; above that it falls back to a
full re-sync of the (small) memory source — either way the result is the
same, only the cost differs.

Consolidation runs automatically on the scheduler beat, or on demand via
the `consolidate_artifacts` MCP tool / `POST /artifacts/consolidate`. Per-scope
decay is opt-in:

```yaml
artifacts:
  learning:
    consolidation_enabled: true   # default — supersedes chains get archived
    session_ttl_days: 14          # opt-in: scratch session memories decay
    user_ttl_days: null           # null = the scope never expires (default)
    org_ttl_days: null
    supersede_retention_days: 0   # opt-in: keep a correction's old record
                                   # indexed (reachable via as_of) for this
                                   # many days before archiving it. 0 = archive
                                   # on the very next pass. See "point-in-time
                                   # recall" note below.
```

**Want `as_of` to reliably reach a value from last week?** Set
`supersede_retention_days` above `0`. It is opt-in rather than the default
because it is a real trade-off, not a free fix: a superseded record's file
stays indexed alongside its correction for that many days, and near-duplicate
text competing for the same query measurably affects ranking under hybrid
(RRF) fusion — see `docs/artifact-records.md` §4 for the numbers. At `0`
(default), a corrected fact is dropped from the index on the very next
consolidation pass, same as before this knob existed.

## Controlling memory at query time

Every retrieval surface takes a `memory` argument — MCP `search_context`,
`POST /search`, `POST /relevant-files` and `POST /assistant/chat`. One word
covers the common cases:

```jsonc
{"query": "rollout password", "artifact_policy": "off"}    // no memory in the results
{"query": "rollout password", "artifact_policy": "only"}   // memory and nothing else
{"query": "rollout password", "artifact_policy": "prefer"} // memory keeps a share of the slots
```

or an object for the rest:

```json
{
  "query": "rollout password",
  "artifact_policy": {
    "scopes": ["user"],
    "subject": "deploy",
    "as_of": "2026-01-01T00:00:00Z",
    "current_only": false,
    "tiers": ["cold"]
  }
}
```

`tiers` (Phase 3, `["hot"]` \| `["cold"]` \| `["hot","cold"]`) reaches
records demoted by [compaction](#compaction--folding-near-duplicates-without-losing-them) — omit it and a plain query
sees `hot` only, exactly like before compaction existed; `current_only:
false` or `as_of` widen to `["hot","cold"]` automatically, the same signal
that already widens the validity window.

**A corrected record is never returned by default.** Supersession is enforced
at query time, so you do not have to wait for a consolidation pass to stop
seeing a fact the region already knows was replaced. `as_of` deliberately
brings it back — that is the point of invalidating rather than deleting, and it
is how you ask what was believed at a past instant.

Results that came from memory carry a `memory` block:

```json
{"record_id": "mem-…", "scope": "user", "subject": "deploy", "kind": "fact",
 "asserted_at": "2026-07-16T12:00:00Z"}
```

`describe_retrieval` reports the memory source's name, its scopes and record
counts, how many records are wired into the graph, and any steering in force —
so an agent never has to guess.

## Steering — records that improve every query

Set `artifacts.learning.steering_enabled: true` and a record's `kind` becomes a retrieval
rule rather than an assertion to recall. This is the one part of records that
changes queries returning **no memory at all**.

| kind | Write | Effect |
|---|---|---|
| `alias` | `router -> ingress-gateway, gateway` | Expands the query, so asking about "the router" finds documents that only say "ingress-gateway" |
| `preference` | `when: deploy, docker -> prefer: docs/, deploy/` | Adds a path prior alongside the built-in depth/tests/samples ones |
| `exclusion` | `never: vendor/**` | Suppresses matching paths |

```bash
curl -X POST localhost:8765/artifacts/records -H 'content-type: application/json' \
  -d '{"text": "router -> ingress-gateway, gateway", "kind": "alias", "scope": "org"}'
```

Paths are matched against a source-relative `relative_path`, so a source rooted
at `docs/` uses `vendor/`, not `docs/vendor/`.

**Scope decides reach, and that is a security property.** A rule applies only
where its scope does: `session` steering is confined to that session, `user` to
that principal, `org` fleet-wide. A corrected or expired rule steers nothing —
the same validity predicate governs steering and retrieval, so the two cannot
disagree. Note that `artifact_policy: "off"` suppresses record *content* but still
honours steering: not wanting remembered passages is not the same as not
wanting the region's remembered vocabulary.

**Rules steer; they are not results.** A steering record is retrieval
machinery, so it is excluded from result lists by default — measured live, an
alias rule was taking rank 1 for the very query it was written to improve,
pushing the real answer down five places. It stays fully in force and fully
inspectable via `describe_retrieval` and `GET /artifacts/records`. Pass
`"artifact_policy": {"include_rules": true}` to see rules in results anyway.

**Triggers match the tokenized query.** Write them however reads naturally —
`filewatch daemon`, `ingress-gateway`, `ci/cd`, `fs.watch` all work — and every
part of the trigger must be present for the rule to fire, so a rule about
`ingress-gateway` does not fire on a query that merely said `ingress`.

## Salience and bounded growth

With `memory.usage_tracking: true`, retrieval counts which records it actually
returned. That feeds a deterministic salience score — recency (90-day half
life) × use (with diminishing returns) × scope weight — which decides what goes
first when `artifacts.learning.max_records` is set. Pruning archives with the same in-place
`.md.archived` rename consolidation uses: bytes are preserved, never deleted.

Both are off by default. Usage tracking is a write on the read path, and
bounded growth should be a decision, not a surprise.

`max_records` bounds **recallable facts**. Steering records are exempt in both
directions — they neither consume slots nor get archived — because ranking a
deliberate rule against ordinary facts, on a formula built for facts, meant
crossing the cap could silently switch off an `exclusion` and change ranking
for every future query.

`max_records` alone ranks the whole store as one pool, so a `session`-scope
flood only ever *outranks* an `org` fact by the fixed `scope_weight`
multiplier — it never fully protects it. `session_max_records`,
`user_max_records` and `org_max_records` (mirroring the `*_ttl_days` trio)
cap each scope's own pool independently, and `max_records_per_subject` caps
how many live records can pile up about one named entity, across scopes.
Each defaults to unbounded; where a record's own scope cap, its subject's
cap, and the global `max_records` backstop all apply, it is archived once,
not three times.

## Compaction — folding near-duplicates without losing them

An agent restates the same fact in fresh words every time it comes up.
Reinforcement (above the write examples) already folds *exact* restatements
at write time; `artifacts.learning.compaction_enabled` (off by default) additionally
clusters genuinely-different-wording near-duplicates **offline**, on the
consolidation pass:

```yaml
artifacts:
  learning:
    compaction_enabled: false            # opt-in
    compaction_similarity_threshold: 0.6 # exact-Jaccard, over normalized tokens
    compaction_min_cluster_size: 2
```

Within one `(scope, subject, kind, ACL partition)` bucket — never crossing
any of those, same as reinforcement's isolation — near-duplicate records
above the threshold cluster together, and the member with the highest
summed similarity to the rest of the cluster is promoted as the canonical
record, **as-is**: nothing is synthesized or machine-authored. Every other
member is demoted to `tier: cold` and gets `subsumed_by: <canonical id>` —
**never renamed or deleted**. A demoted record stays on disk, stays
indexed, and stays reachable:

```bash
curl -s localhost:8765/search -X POST -H 'content-type: application/json' -d '{
  "query": "staging cluster region",
  "artifact_policy": {"tiers": ["cold"]}
}'
```

Every subsumption is recorded in the `memory_compactions` ledger
(`op`, `member_id`, `canonical_id`, `rule_id`, `params_hash`, `at` — see
`docs/reference/export-schema.md`), so "why is this record cold" always has
an answer. Off by default because, unlike reinforcement, it changes what a
plain query returns — the same posture `supersede_retention_days` takes.

### When clustering isn't enough: synthesis

Clustering folds redundancy — many phrasings of one claim. It cannot
**merge** two records that are each true and each say something the other
doesn't ("runs in us-east-2" + "owned by ada"), or abstract across several
("deploy failed Mon/Tue/Wed" → "failing all week"). For that,
`artifacts.learning.synthesis` calls a model — opt-in, and unlike everything else on
this page, **never automatic**: only an explicit call runs it.

```yaml
artifacts:
  learning:
    synthesis:
      enabled: false      # opt-in
      provider: auto        # auto | anthropic | openai | gemini | none
      model: null
      max_calls_per_pass: 20
```

```bash
curl -s localhost:8765/artifacts/synthesize -X POST
```

The model only ever sees clusters clustering already tried and couldn't
fully resolve; a successful merge subsumes its inputs exactly like medoid
promotion (`tier: cold`, `subsumed_by`), tagged `llm-synthesized` and
recorded in the ledger with the model id — never `supersedes`, since the
inputs weren't wrong, just redundant. It is a writer, not an indexer: the
merged text becomes an ordinary record through the normal write path, so
no LLM call ever happens on the indexing path itself, and a repeat call
over an unchanged, already-subsumed cluster costs nothing.

## In the UI

The **Artifacts** page has a **Knowledge records** panel that lists what has
been recorded, with its scope. Corrections are made by *superseding* — never by
editing — so the history `as_of` reads stays intact; **Correct** pre-fills a
superseding write, and saving it keeps the earlier version and its evidence.
Proposed records wait under **Proposed** on the same page until someone
promotes or rejects them.

In chat, the **use artifacts** switch on the composer lets knowledge records,
saved reports and composed work inform the answer, and any passage that came
from a record is chipped with its scope so an answer never silently passes off
a recorded assertion as a document.

## Isolation

When `security.acl_enforced` is on, a record's ACL follows its scope: `org` is
shared, while `user` and `session` records are readable only by the principal
that wrote them (`record_artifact(..., principal="user:alice")`). Without a
recorded writer nothing is asserted and the region default applies.
