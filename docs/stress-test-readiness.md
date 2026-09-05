# Stress-test readiness

**What this document is.** A specification for whether pheasant may be used as
the knowledge substrate of a *scored* experiment — one whose numbers somebody
will later have to defend — and the executable check that decides it.

It began as an external pre-build gap analysis written without access to this
repository, in which every Pheasant-side capability was marked `Assumed` or
`Unverified`. That framing was right and is preserved: **these were evidence
gaps, not capability gaps.** "pheasant probably does this" is not evidence, and
the point of the document was never to guess but to say exactly what would
count as proof.

What has changed is that the proof is now executable:

```bash
pheasant readiness contract          # what this build supports, and what it does not
pheasant readiness check             # probe this region; exit 0 only on GO
pheasant readiness check --out readiness.md --json
```

and it is available over HTTP (`GET /readiness/contract`,
`POST /readiness/check`) and MCP (`get_readiness_contract`,
`run_readiness_check`), from one implementation.

> **Primary decision.** No scored experiment begins until the applicable gate
> in [§7](#7-gono-go-gates) passes. `pheasant readiness check` exits non-zero
> for anything short of `GO`, including `INCOMPLETE` — because an unchecked box
> and a failed one are equally disqualifying for a result somebody will publish,
> and only one of them is fixed by changing the region.

---

## 1. What pheasant is expected to be

Not a vector-search endpoint. For an experiment it is the persistent,
inspectable knowledge substrate of a hierarchical research swarm, and it must:

1. Accept source-derived information from independent agents through a
   discoverable MCP interface.
2. Persist artifacts, chunks, relations, verification state and provenance
   without silent loss or duplication.
3. Seal knowledge snapshots so an answer can be reproduced against the same
   corpus and retrieval configuration.
4. Retrieve evidence with enough identity and lineage to trace an answer back
   to exact source locations.
5. Keep namespaces, principals, arms, snapshots, memories and tuning states
   isolated.
6. Support a memory-off baseline, a memory-on pass, a search-tuned pass, and a
   combined pass.
7. Preserve positive, negative, unknown and conflicting evidence rather than
   collapsing exposure into success.
8. Surface failures so errors become concrete refinement candidates.
9. Permit deterministic evaluation of whether it improves an otherwise blind
   agent's answers.

pheasant-kb is one knowledge base; Synapse/Flock is the multi-region
orchestration concept. **A harness may implement the swarm itself** — no
production Flock service is required before a first experiment.

---

## 2. The experiment this must support

### 2.1 Topology

L0 portfolio orchestrator → L1 topic director → L2 facet lead → L3 workers,
plus a coverage auditor, a benchmark builder and evaluation agents.

**Only source-derived research material may enter the searchable corpus.**
Orchestrator summaries, benchmark questions, reference answers, arm outputs,
scores and evaluator feedback stay outside it — enforced by
`readiness.corpus_denylist`, which refuses the *write* rather than detecting
the contamination afterwards ([§3.6](#36-isolation-phe-iso)).

### 2.2 Arms

| Label | Memory | Tuned search | Purpose |
|---|---:|---:|---|
| `S0` | n/a | n/a | Source-aware specialist ceiling |
| `C0` | off | off | Prior-only control, no pheasant access |
| `P0` / `M0S0` | off | off | Frozen-corpus pheasant baseline |
| `P1` / `M1S0` | on | off | Isolated memory effect |
| `P2a` / `M0S1` | off | on | Isolated search-tuning effect |
| `P2b` / `M1S1` | on | on | Combined effect and interaction |

A single `P2` cannot separate tuning from its interaction with memory, so the
harness must run both `P2a` and `P2b` even if the report groups them.

Required comparisons: `P0−C0`, `P1−P0`, `P2a−P0`, `P2b−P1`, `P2b−P2a`, the
interaction `P2b−P1−P2a+P0`, and the gaps `P1−S0` and `P2b−S0`. Each of `P2a`
and `P2b` separates learned replay, temporal/constructed holdout, and a control
cohort — the split pheasant's own evaluation plane already uses
(`docs/knowledge-effectiveness.md`).

---

## 3. The capability contract

`pheasant readiness contract` emits this machine-readably, with a digest a
harness pins to detect the region changing shape between arms. The digest
covers the build's capability shape and its limits, and deliberately **not**
probe results — one that moved because a latency probe was a millisecond slower
would defeat the reason to pin it.

Every capability reports one of four statuses, and the distinction between the
middle two is the whole subject of this document:

| Status | Meaning |
|---|---|
| `proven` | A probe demonstrated it **here**, in this deployment, against this corpus. |
| `supported` | The implementation exists; no probe has demonstrated it in this region. |
| `declared_untested` | Present, and no probe has been run at all. |
| `unsupported` | This build cannot do it, with the reason attached. |

### 3.1 Logical operations, and what serves them

| Logical operation | pheasant | Gap |
|---|---|---|
| `health/capabilities` | `/health`, `/ready`, `/readiness/contract`, `get_readiness_contract` | `G-PHE-001` |
| `mcp/discovery` | MCP server + `tools/list` | `G-PHE-002` |
| `source.register` | `register_source`, `POST /sources` | `G-PHE-004` |
| `artifact.upload` | `submit_documents`, `POST /ingest/submit` | `G-PHE-004` |
| `ingest.status` | `get_ingest_status`, `acknowledge_ingest` | `G-PHE-008` |
| `ingest.reconcile` | `reconcile_ingest`, `GET /ingest/reconcile` | `G-PHE-006` |
| `snapshot.seal` | `seal_snapshot`, `POST /snapshots/seal` | `G-PHE-009` |
| `snapshot.get/list` | `get_snapshot`, `list_snapshots`, `GET /snapshots` | `G-PHE-009` |
| `search` | `search_context`, `POST /search` | `G-PHE-011` |
| `read/evidence.get` | `get_file_summary`, `explain_node`, `GET /nodes/content` | `G-PHE-013` |
| `memory.configure/status` | memory policy, reported per query | `G-PHE-018` |
| `memory.export` | `memory_list`, `list_memory_candidates` | `G-PHE-019` |
| `search_profile.configure/status` | tuning bundles, reported per query | `G-PHE-021` |

### 3.2 Durable ingestion — PHE-ING

A submission is **receipted per item**, keyed by the caller's idempotency key.
Three properties, each a gate:

- **A retry is visible and singular.** The same key folds onto the same receipt
  and increments `submissions`; the stored object stays one. A harness can tell
  "my retry was absorbed" from "my retry wrote a second copy" — the difference
  between a 400-document corpus and one that scores as 520.
- **Acceptance and indexing are different facts.** `accepted` means the bytes
  are persisted; `indexed` means a search can return them. Collapsing them is
  how a harness queries for content the region holds and has not yet indexed,
  then records the empty result as a retrieval failure.
- **Partial failure is per item**, with an error code and a retryability flag.

`reconcile_ingest` reports `silent_loss`: receipts claiming an artifact the
region does not hold. Deliberately not a difference between two totals — two
totals can agree while one item was lost and another double-written.

There is **no second ingestion path**: bytes land in a directory registered as
an ordinary `document_folder` source and flow through the normal connector →
parse → chunk → graph pipeline, so idempotency is the sha256 skip pheasant
already had rather than a mechanism invented for this.

### 3.3 Knowledge and provenance — PHE-KNOW

Immutable source identity and version, artifact content hash, chunks with exact
locators (`relative_path`, `start_line`, `end_line`, `heading_path`), the graph's
typed nodes and edges, and memory validity with supersession. The grammar of
every id is in `docs/graph_model.md` and is a contract (rule 3).

**One thing pheasant does not model**, declared `unsupported` rather than
approximated: a claim-to-claim stance edge (support / contradict / qualify /
unknown). Evidence is typed at the *record* level — validity, supersession,
correction, retraction — and at the *proof* level, where the evaluation plane's
taxonomy types what a caller asserted. A harness needing claim-level stance
holds it outside the region. Concept extraction was retired here for failing
every test set built for it; inventing a stance edge on the same rule-based
footing would repeat that.

### 3.4 Sealed snapshots and temporal semantics — PHE-SNAP

`seal_snapshot` freezes a manifest over every input capable of changing
retrieval — corpus, graph, lexical and vector index, chunking, fusion, memory,
steering, ACL — computed identically on any replica with no clock in the id.
Sealing twice over an unchanged region returns one snapshot.

A search pinned to `snapshot_id` is verified before the arms run. **The
guarantee is a refusal, not time travel:**

> a search pinned to a snapshot is answered from that snapshot's state, or it
> is not answered.

This region holds one version of its corpus. It therefore cannot serve a sealed
snapshot's results after the corpus behind it moves, and it refuses with
`SNAPSHOT_DRIFTED` (naming the manifest sections that changed) rather than
answering from a different corpus while the caller attributes the result to the
sealed one. That is weaker than "new ingestion cannot change a sealed
snapshot's results" and **sufficient for the property a scored experiment
needs** — that two runs claiming one snapshot cannot silently have seen
different corpora. It is stated rather than smoothed over, because a reader who
assumes time travel will ingest during a run and call the result reproducible.

`as_of` has the same shape. Memory genuinely replays at an earlier instant — a
correction supersedes rather than overwrites — and corpus *content* does not.
So `temporal.memory_as_of` is a probed capability and `temporal.corpus_as_of` is
declared `unsupported`, with the sealed snapshot standing in for it.

### 3.5 Retrieval response contract — PHE-RET

Every search response carries a `lineage` block, split in two on purpose:

- **`state`** — `query_id` (content-addressed over query, mode, criteria,
  snapshot, `as_of`, principal; **no clock**, so two runs of one query under one
  configuration are one measurement), `snapshot_id` and whether it is current,
  `graph_generation`, the ranking bundle and its provenance, memory state and
  policy, `as_of`, principal and groups, `acl_enforced`, criteria, mode,
  `max_results`.
- **`timing`** — `retrieval_ms`, `truncated`, `returned`.

Everything under `state` is a function of the request and the region, so two
replicas answering one pinned query agree on it exactly. `retrieval_ms` is the
**only** non-deterministic field in a search response, which
`tests/test_readiness_plane.py` asserts so a second one has to be a decision
somebody made.

Result rows carry the locator half they always did: artifact id, chunk id, line
span, path, heading path, score, `retrieved_by`, rank.

### 3.6 Isolation — PHE-ISO

Zero-tolerance, all four blocking:

- ACL leakage = 0 (`security.acl_enforced`, `normalize_acl`, probed with two
  principals).
- Namespace/principal crossing = 0.
- Current-only stale leakage = 0 (memory validity is filtered at query time).
- Benchmark artifacts in the searchable corpus = 0.

The last is **enforcement, not detection**, and it is enforced at *every* door
into the corpus. `readiness.corpus_denylist` is a list of fnmatch patterns
tested against an item's relative path and its bare filename; the rule is
`security/corpus_policy.py` and both write paths call it:

| Door | Behaviour |
|---|---|
| `POST /ingest/submit`, `submit_documents` | Refused per item with `CORPUS_DENYLISTED`, before the bytes are written — distinct from a validation failure so a caller cannot mistake it for a size limit and retry smaller. |
| Any source the engine syncs — folder, git, upload directory, every connector | Refused per item **before the item is read**, logged, and counted in the sync report's `refused` / `refused_total`. |

Both halves matter. The first version of this control was enforced on
submissions alone, so a region with a denylist configured still indexed an
answer key that arrived through a folder source, the UI drop zone or a git
repository — while this gate reported the boundary intact. A control on one of
several doors is a door with a sign on it.

And enforcement is still not the whole gate, because it only stops *new*
arrivals. The `contamination_refused` probe therefore asserts three things: the
submission door refuses, the indexing door refuses, and **no artifact this
region holds matches the denylist** — a scan of `artifacts`, which is what
makes the gate a statement about this corpus rather than about this code. It
can fail on a region whose code is correct and whose corpus was populated
before the denylist existed; `pheasant sync --mode full` is the repair.

An empty denylist means there is no boundary to prove, so the contamination
probe reports `skipped` and the **core gate set is incomplete**. A region can be
perfectly healthy and still not ready to be measured.

### 3.7 Memory — PHE-MEM (required before `P1`)

Memory records are source content: one frontmatter Markdown file per record,
indexed by the ordinary pipeline, append-only, exportable. Validity, scope
isolation (`org` shared; `user` and `session` readable only by their writer),
supersession, steering, and candidate promotion gates are described in
`docs/memory-system.md` and `docs/memory-formation.md`.

What this plane adds: **every query reports which memory state answered it,
including "off"**. An arm that ran with memory off has to be able to *record*
that it did, and an absent key is indistinguishable from a key nobody looked at.

### 3.8 Search tuning — PHE-TUNE (required before `P2a`/`P2b`)

Immutable bundle ids, base/overlay/active reported separately, lineage of every
promotion, shadow validation before promotion, rollback that does not rewrite
history — all in `docs/retrieval-tuning.md`. What this plane adds is that the
effective profile id is returned in `lineage.state.ranking` for **every** query,
so a result can be attributed to the configuration that produced it.

### 3.9 Concurrency and operational behaviour — PHE-OPS

Bounded concurrent ingestion and retrieval, backpressure (`429` +
`Retry-After`), retryable-versus-permanent error classification, cancellation,
reconnect without duplicate logical writes, queue-depth observability, and
readiness separate from liveness. The concurrency probe drives writers that
share a process on purpose: the trap this codebase already fell into was an
owner identifying a *process*, which could not arbitrate a race inside one, and
every sequential test passed throughout.

No broker, hosted database or separate observability service is required.

---

## 4. Gap status

Statuses below are what `pheasant readiness contract` reports on a region
configured for an experiment. Two gaps are closed by a **declared limitation**
rather than by an implementation, and both are marked as such.

| Gap | Capability | Status | How it is established |
|---|---|---|---|
| `G-PHE-001` | Running service | proven | `service_identity` probe; `/health`, `/ready` |
| `G-PHE-002` | MCP discovery | proven | `mcp_discovery` probe; every contracted tool exists on the facade |
| `G-PHE-003` | Capability negotiation | proven | the contract itself, derived from live symbols |
| `G-PHE-004` | Durable ingestion | proven | `ingest_roundtrip`: submit → receipt → index → retrieve at a locator |
| `G-PHE-005` | Idempotency | proven | `idempotent_write`: three writes, one key, one stored object |
| `G-PHE-006` | Partial failure | proven | `partial_failure`, `reconciliation` |
| `G-PHE-007` | Provenance | proven | `ingest_roundtrip`, `resolve_result` |
| `G-PHE-008` | Index barrier | proven | `index_barrier`: `accepted` and `indexed` are distinguishable, lag bounded |
| `G-PHE-009` | Immutable snapshots | proven | `snapshot_seal`, `snapshot_drift_refused` |
| `G-PHE-010` | Temporal replay | **partial, declared** | memory `as_of` probed; corpus `as_of` `unsupported` — see [§3.4](#34-sealed-snapshots-and-temporal-semantics-phe-snap) |
| `G-PHE-011` | Retrieval lineage | proven | `retrieval_lineage` |
| `G-PHE-012` | Evidence semantics | **partial, declared** | record- and proof-level typed; claim-to-claim stance `unsupported` — see [§3.3](#33-knowledge-and-provenance-phe-know) |
| `G-PHE-013` | Read/resolve | proven | `resolve_result` |
| `G-PHE-014` | Namespace/ACL isolation | proven | `principal_isolation` (needs `security.acl_enforced`) |
| `G-PHE-015` | Benchmark contamination | proven | `contamination_refused`: both doors refuse, and `artifacts` holds nothing the denylist forbids (needs `readiness.corpus_denylist`) |
| `G-PHE-016` | Structured errors | proven | `structured_errors`; codes published in the contract |
| `G-PHE-017` | Concurrent writers | proven | `concurrent_writers` |
| `G-PHE-018` | Memory toggle/version | proven | `memory_state_reported` |
| `G-PHE-019` | Auditable memory | proven | `memory_export` |
| `G-PHE-020` | Memory isolation | proven | `principal_isolation` |
| `G-PHE-021` | Search-profile versioning | proven | `retrieval_lineage` reports the bundle per query |
| `G-PHE-022` | Tuning lifecycle | supported | `tests/test_tuning_plane.py` — one promotion cycle is a batch, not a check |
| `G-PHE-023` | Performance SLO | proven | `readiness.max_*` are configured; `search_latency` and `index_barrier` measure against them |

### 4.1 Harness-side gaps

The `G-LAB-*` items — repository scaffold, cost ledger, MCP client, run
manifests, arm runners, metric engine — are the *harness's* build, not
pheasant's, and are out of scope for this repository. What pheasant owes them is
in [§3](#3-the-capability-contract): a discoverable contract, receipts, sealed
snapshots, lineage, isolation and structured errors. Everything under
`runs/<run_id>/` in the harness's layout is derivable from those.

---

## 5. Configuration

```yaml
readiness:
  enabled: true                     # publish the contract, allow checks
  corpus_denylist:                  # refused at the write, not detected later
    - "benchmark/*"
    - "*.answers.json"
    - "eval/**"
  max_search_latency_ms: 5000.0     # p95
  max_ingest_ack_ms: 30000.0
  max_index_lag_ms: 120000.0
  latency_probe_queries: 12         # below 5 the probe publishes nothing
  concurrency_probe_writers: 4
  concurrency_probe_items: 5
```

The thresholds are deliberately generous. A performance gate cannot pass or fail
against an unstated number — which the original analysis correctly called the
one *missing decision* rather than a missing capability — and a threshold that
is present and loose can be tightened from evidence, where an absent one turns
every latency observation into an argument.

A region also needs, for the gates beyond core:

- `security.acl_enforced: true` — otherwise isolation cannot be demonstrated.
- an enabled `type: memory` source — otherwise memory has nothing to export or
  replay.

---

## 6. What a check does to the region

It writes. Specifically it submits documents to a scratch source named
`__readiness__probe`, indexes them, seals snapshots and runs searches. It never
writes to a configured source, never takes `sync_lock`, and never writes memory.
`tests/test_readiness_check.py` asserts the isolation directly — a readiness
check that contaminated the corpus it was certifying would be the most expensive
possible bug in this plane.

What it leaves behind is append-only evidence that a check ran: receipts, and
sealed snapshots. The scratch source can be deleted like any other.

Because it writes, `POST /readiness/check` is gated on `readiness.enabled`. The
*contract* is not: answering "can this region be measured" with a 404 is
indistinguishable from an old build that has no contract at all, so it is always
served and reports `readiness_enabled` so the caller can tell.

---

## 7. Go/no-go gates

Four sets, evaluated through `pheasant.decision.GateSet`, which **cannot be
constructed empty** — the invariant that exists because `all([])` is `True` and
this codebase has paid for it twice.

A gate set's verdict is tri-state:

- `False` — an evaluated gate failed. Stands whatever else was skipped.
- `None` — every evaluated gate passed and something was skipped. **Not a
  pass.** This is the specification's own rule, "any unchecked item is a NO-GO
  for scored results", and it is the `all([])` shape one level up: the empty set
  is not the gate list but the part of it nobody could run.
- `True` — every gate in the set was evaluated and passed.

### 7.1 Core — before the first scored experiment

| Gate | Established by |
|---|---|
| `service_identity` | health, readiness and the exact server version captured |
| `mcp_discovery` | initialization and tool discovery pass |
| `ingest_to_retrieval` | one source travels registration → indexed receipt → retrievable locator |
| `idempotency` | three writes under one key produce one stored object |
| `partial_failure` | item-level disposition; counts stay reconcilable |
| `index_barrier` | acceptance and searchability distinguishable; lag inside budget |
| `reconciliation` | every submitted item reconciles; silent loss = 0 |
| `snapshot_immutable` | a seal resolves; ingestion after it cannot be silently served as that snapshot |
| `retrieval_lineage` | stable ids, locators, snapshot, profile, trace |
| `resolve_result` | a returned id resolves to exact persisted content |
| `benchmark_contamination` | evaluation artifacts refused entry to the corpus |
| `structured_errors` | stable codes and a retryability flag |
| `performance_slo` | latency and index-lag budgets configured **and** met |
| `memory_state_visible` | every query reports which memory state answered it |

### 7.2 Swarm

`concurrent_writers` — bounded concurrent ingestion loses, duplicates and
cross-links nothing; `reconciliation_under_load` — reconciliation still balances
afterwards.

### 7.3 Memory — before a `P1` score is evidence of memory improvement

`memory_state_visible`, `memory_auditable`, `memory_temporal`,
`principal_isolation`.

### 7.4 Tuning — before a `P2` score is evidence of generalizable improvement

`profile_reported`, `snapshot_shared`.

---

## 8. What pheasant supplies, and what the harness computes

pheasant supplies retrievable evidence, identity, state and traces. **The
harness computes every experimental metric** — fact precision/recall/F1,
evidence recall, citation precision, contradiction recall, unsupported-claim
rate, paired deltas and relative lift.

That division is not an implementation convenience. A region must not answer a
question with its own report: `docs/knowledge-effectiveness.md` is explicit that
nothing in the `evaluation_*` tables is a file, is chunked, is indexed, or is
returned by a search, and the readiness plane holds the same line — nothing it
produces enters the corpus.

Two measurement traps this codebase has already paid for, and which a harness
will meet in the same form:

- **A measurement derived from what a system chose to show measures its own
  confidence.** Mining "appeared at rank 1" as a positive produces a metric that
  improves whenever ranking gets more *confident*, regardless of whether it gets
  more correct. Utility proof has to come from a surface where somebody said so.
- **Corpus similarity, retrieval exposure and answer fluency are not truth.**
  Reporting any of them as success is the failure mode the whole apparatus
  exists to prevent.

---

## 9. Definition of ready

pheasant is ready when this statement can be made **and independently
verified**:

> For a named principal, namespace, sealed knowledge snapshot, memory state,
> search profile and `as_of` time, every source-derived write is reconciled,
> every returned result is traceable to an exact source location, no forbidden
> information crosses an isolation boundary, every failure affecting a
> denominator is visible, and every reported improvement or regression can be
> recomputed from preserved operands.

`pheasant readiness check` is the automated check for it. Until it exits `0`
with a `GO` verdict, pheasant may be operational and the experiment is not
valid.
