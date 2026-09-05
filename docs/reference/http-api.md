# HTTP API reference

pheasant serves a FastAPI admin/retrieval API on port `8765` (configurable via
`server.port`). When `server.api.openapi` is enabled, interactive docs are
available at `/docs` and the schema at `/openapi.json`.

The routes below are the consolidated surface defined in
`src/pheasant/api/app.py`.

**Authentication.** None by default, which is right for one container on
loopback. When `security.api_auth.token_env` resolves to a value, every route
below needs `Authorization: Bearer <value>` and answers `401` without it —
except `security.api_auth.public_paths` (`/health`, `/ready`, `/metrics`) and
`/internal/*`, which enforces its own per-boundary tokens. Every role but
`all` refuses to start on a bind other machines can reach without either that
token or `security.api_auth.behind_authenticating_proxy`; see
[the fleet trust model](../security.md#a-fleet-is-the-other-case-and-it-is-enforced).
A process with `server.api.enabled: false` (the preparation workers) serves
only the probes, `/metrics` and `/internal/*`, and `404`s the rest.

## Health & ops

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness probe. Reports `role`, so a pod can be identified from the response, and `graph_generation.loaded` — the graph generation this process is answering from. Does no I/O at all, which is why the staleness comparison lives on `/ready`. Stays 200 when the state store is unreachable — restarting a pod does not bring a database back. |
| GET | `/ready` | Readiness probe. Reports the role and what it does (`watcher`, `scheduler`, `drains_queue`, `indexes_locally`), plus `graph_generation` — `loaded` beside `published`, so a replica that missed a graph reload is detectable rather than inferred. Returns **503** when the state store is unreachable so the replica leaves the Service without being restarted. Deliberately not gated on the index being populated: a replica held unready through a multi-hour first index would take the whole Service down for that time. |
| GET | `/metrics` | Prometheus exposition text — index queue depth, per-source throughput/ETA/stall, search latency, graph size. See [Monitor indexing](../how-to/monitor-indexing.md). |
| POST | `/internal/indexing/prepare` | Opt-in stateless remote preparation worker. Disabled unless `sync.concurrency.remote_worker_enabled`; requires `Authorization: Bearer` matching the environment variable named by `remote_worker_token_env`. Intended for pheasant coordinators, not public clients. |
| POST | `/internal/indexing/prepare-batch` | Several preparation tasks in one request. Same gate and token as above. Honours `deadline_seconds` (or the `X-Pheasant-Deadline-Seconds` header) by stopping between tasks rather than finishing work whose caller has given up, and answers a repeated `idempotency_keys` entry from a bounded cache instead of re-parsing. `408` when the deadline has already passed, `413` over `MAX_PREPARE_BATCH` tasks or the per-file size limit, `422` when a task is unacceptable (the coordinator then prepares it locally). A worker predating this route returns `404`, and the coordinator falls back to the single-task path. |

## Synapse region

| Method | Path | Purpose |
|---|---|---|
| GET | `/contract` | The region's published semantic contract (read-only). |

See [Attach to a Synapse fleet](../how-to/attach-to-synapse.md).

## Knowledge bases & sources

| Method | Path | Purpose |
|---|---|---|
| GET | `/knowledge-bases` | List knowledge bases. |
| GET | `/overview` | One call for a UI cold start: knowledge base, sources, node counts, whether anything is indexed. Each source in `sources[]` carries live `syncing`/`sync_error` (see Sync below). |
| GET | `/sources` | List configured + runtime sources. Each entry carries `syncing: bool` (a background sync — `wait: false` — is running now) and `sync_error: string \| null` (error from the most recent *background* sync, independent of `last_status`), plus `job` — the running job behind `syncing`, with its phase and counter (see Jobs below). |
| GET | `/sources/types` | Registerable source types — built-ins plus installed connector plugins — each with a `path_role` of `required` or `unused`. |
| POST | `/sources/quick-add` | One-field setup: a path, URL, glob or connector name is detected, named, registered and (by default) synced. Same inference as `pheasant up`. `sync_now` (default `true`) gates syncing at all; `wait` (default `true`) gates whether the response blocks on it — see Sync below. `wait: false` returns `sync_results: []` and `syncing: [names]` immediately; poll `GET /sources` for progress. |
| POST | `/sources/upload` | **multipart.** Upload documents; they land under `/state/uploads/<name>/`, which is registered as an ordinary `document_folder` source and indexed through the normal pipeline — no second ingestion path. Fields: `files` (repeatable), `source_name` (default `uploads`), `sync_now`, `wait`. A second upload into the same name adds to it. One over-sized or empty file is reported in `rejected[]` without losing the rest. |
| POST | `/sources` | Register a runtime source (full schema). Accepts plugin types; a plugin source needs no local path. Same `sync_now`/`wait` fields as quick-add. |
| PUT | `/sources/{source_id}` | Update a source. |
| POST | `/sources/{source_id}/disable` | Disable a source. |
| DELETE | `/sources/{source_id}` | Remove a source. |
| POST | `/sources/{source_id}/promote` | Promote a runtime source to durable config. |
| GET | `/sources/{source_id}/repo-map` | Repository map for a source. |
| GET | `/sources/{source_id}/history` | Source sync/audit history. |

## Sync

| Method | Path | Purpose |
|---|---|---|
| POST | `/sync` | Sync all sources (`mode` in body). |
| POST | `/sync/{source_id}` | Sync one source. |
| GET | `/sync/status` | Current sync status. |

`/sync` and `/sync/{source_id}` both accept `wait` in the JSON body
(default `true`, preserving the original blocking contract: the request
holds open until the sync finishes and returns its full
indexed/skipped/graph counts). Set `wait: false` to return immediately —
`{"status": "syncing", ...}` — while the sync runs in a background thread
(the same `sync/worker.py` subprocess path `wait: true` uses; nothing
about *how* the sync runs changes, only whether the caller waits for it).
This exists because a large source's first sync (clone + full index) can
run well past what a browser tab or reverse proxy will hold a connection
open for — the pheasant UI hit exactly this as a 504 on `/sources/
quick-add` even though the sync went on to succeed server-side. Poll
`GET /sources` (or `/overview`) for `syncing`/`sync_error` to track a
background sync to completion; a source is only ever "unknown" (404) if
it exists in neither `config.sources` nor the state registry — a source
registered by quick-add/`POST /sources` and never written to YAML is
still valid here, resolved through the same state-registry fallback
`SyncEngine._source` uses.

## Jobs (background work)

| Method | Path | Purpose |
|---|---|---|
| GET | `/jobs` | Every job, newest first; running ones sort ahead of finished. `?active=true` for running only. |
| GET | `/jobs/{job_id}` | One job: phase, counter, log tail, terminal outcome, and a `sources[]` breakdown. |
| GET | `/jobs/stream` | Server-sent events, one per job update, primed with current state on connect. |

Every source row (`/sources`, `/overview`) also carries `syncing`, `sync_error`,
`job` — the live job behind the boolean — and `progress`, **this source's own
slice** of that job: phase, counter, observed throughput, ETA, `stalled`, and
the indexed/unchanged counts. Under a `sync_all` the job-level counter is an
aggregate over every source, so `progress` is what tells you which one is
actually behind. See [Monitor indexing](../how-to/monitor-indexing.md).

## Search & retrieval

| Method | Path | Purpose |
|---|---|---|
| POST | `/search` | Search (`mode`: `text` / `graph` / `vector` / `hybrid`). Also takes `source_name`, `source_types`, `exclude_source_types`, `exclude_sources`, `node_types`, `min_score`, `section` and `memory`. Every hit reports `provenance.source_type` — the kind of source it came from. The response carries `graph_generation`: which graph answered, so a diagnosis can tell "not indexed" from "this replica has not picked up the index that has it". |
| POST | `/relevant-files` | Rank relevant files for a task/query. |
| GET | `/files/summary` | Summarize a file node. |
| GET | `/nodes/content` | Fetch a node's content. |
| GET | `/nodes/explain` | Explain why a node matched / its provenance. |

### The `memory` argument

`POST /search`, `/relevant-files` and `/assistant/chat` all accept `memory`:
one of `"auto"` (default), `"off"`, `"only"`, `"prefer"`, or an object with
`scopes`, `subject`, `current_only`, `as_of`, `max_results`,
`include_rules` and `tiers`.

`include_rules` defaults to `false`: `alias`/`preference`/`exclusion` records
steer ranking but are not themselves returned as passages. Set it true to see
them in results.

Records a later record corrected are excluded automatically — you do not have
to wait for a consolidation pass. Pass `{"current_only": false}` or an `as_of`
instant to see them. Hits that came from memory carry a `memory` block naming
the record, its scope, subject, when it was asserted, and its tier.

`tiers` (`["hot"]` default) reaches records demoted by compaction
(`memory.compaction_enabled`) — `["cold"]` or `["hot","cold"]`; `current_only:
false` and `as_of` widen to both tiers automatically, same as they already
widen the validity window.

## Agent memory

| Method | Path | Purpose |
|---|---|---|
| POST | `/memory/enable` | Provision the `type: memory` source. Idempotent; the only way to turn memory on without editing `pheasant.yaml`. |
| POST | `/memory` | Append one record. Body: `text`, `scope` (`session`/`user`/`org`), `subject`, `supersedes`, `tags`, `kind`, `principal`, `valid_until`, `sync`. Response adds `outcome` (`"created"` \| `"reinforced"` \| `"duplicate"`) and, when a reinforcement changed what is stored, `submitted_text`. |
| GET | `/memory` | List records. Query: `scope`, `current_only`. Each record carries `tier` (`hot`/`cold`) and `subsumed_by`. |
| POST | `/memory/consolidate` | Archive superseded/expired records, prune past `memory.max_records`, then re-index. Returns `{"skipped": …}` when consolidation is off — not an error. |
| GET | `/memory/candidates` | Memory formation has **proposed** but nothing has admitted. Not memories: nothing here is retrievable until promoted. `status` (default `pending`), `rule_id`, `principal`, `limit`. |
| POST | `/memory/candidates/{id}/promote` | Admit one proposal. Writes through the ordinary `MemoryStore.append`, so the result is an ordinary indexed record. `409` if already decided — a decision is final, and re-deciding would write a second record. |
| POST | `/memory/candidates/{id}/reject` | Decline one proposal, permanently. The rule that proposed it will not suggest it again. |
| POST | `/memory/synthesize` | LLM-merge a near-duplicate cluster deterministic compaction could not resolve into one canonical record, subsuming the inputs. Off by default (`memory.synthesis.enabled`) and never automatic — only this call runs it. `{"skipped": …}` when disabled, no memory source, or no model reachable; otherwise `{"attempted","synthesized","cached","records"}`. |

See [Agent memory](../how-to/agent-memory.md).

## Knowledge effectiveness (evaluation plane)

Off by default (`evaluation.enabled`). Everything these write lands in the
`evaluation_*` tables and is **never indexed, chunked or returned by a search**:
a region must not retrieve its own measurements as knowledge.

| Method | Path | Purpose |
|---|---|---|
| POST | `/evaluation/evidence` | Record one typed observation about one result. Body: `query`, `target_id`, `event_type`, optional `target_type`, `interaction_id`, `principal`, `session_id`, `position`, `outcome_reference`. Returns the derived polarity, strength, weight **and the four multipliers behind it**. `400` on an event type outside the taxonomy — a proof row naming an unweighted event is a row no metric can read. |
| GET | `/evaluation/taxonomy` | Every event type, its polarity and strength, and what it licenses, plus this deployment's two decisive defaults (`unknown_is_negative`, `non_selection_is_negative`). |
| POST | `/evaluation/run` | Start a batch as a background job; returns `job_id`. Body: `mode` (`current_state` \| `historical`), `as_of`, `force`. `400` when evaluation is disabled and `force` is not set. Watch it via `GET /jobs/{id}`. |
| GET | `/evaluation/report` | The latest report, or `?run=<run_id>`. The whole document: health vector, gates, attribution, generalization, candidate decisions, limitations, and all three explanations. |
| GET | `/evaluation/runs` | Recent runs with status, mode and whether the gates passed. |
| GET | `/evaluation/trend` | One metric across snapshots. `metric`, `cohort` (default `anchor` — the only cohort whose membership is frozen), `variant` (default `B5`), `limit`. |
| GET | `/evaluation/status` | What a batch is doing **right now**, read from `/state` rather than from the process running it — so it answers for a run this replica did not start, and for one whose container has since stopped. Returns `status`, `phase`, `phase_detail`, `completed_units`/`total_units` (cohort/variant replays), `fraction`, `attempts` and `error`. A run whose heartbeat expired reports `interrupted`, never a spinner nobody will stop. `?run=` for a specific one. |
| GET | `/evaluation/cohorts` | The query sets recent runs used, with purpose, size and whether frozen. An empty cohort explains an `insufficient_evidence` better than the metric can. |
| GET | `/evaluation/metrics` | Per-query rows behind an aggregate: the audit trail, on demand. `run`, `metric`, `cohort` (by purpose), `variant`, `limit`. Each row carries the full result payload — formula, substituted calculation, operands, proof references, limitation. |

Running a batch is a background job because it replays every cohort under every
variant through the real search path — minutes of work on a real corpus. It
takes the `__evaluation__` lease, so several API replicas produce one run rather
than N, and it never runs inside `sync_lock`.

See [Knowledge-effectiveness evaluation](../knowledge-effectiveness.md).

## Retrieval performance tuning (tuning plane)

Off by default (`tuning.enabled`), and read-only unless a bundle is applied.
Where the evaluation plane says *how well* retrieval is doing, this says **which
step is failing** — and after the merge a lexical miss, a filtered-out document,
a fusion demotion and a truncation all look identical, because they all produce
an absent result.

Everything these write lands in the `tuning_*` tables and under
`/exports/tuning`, and none of it is ever indexed, chunked or returned by a
search: a region must not retrieve its own experiments as knowledge.

| Method | Path | Purpose |
|---|---|---|
| POST | `/tuning/run` | Start a batch as a background job; returns `job_id`. Body: `force`, `apply`, `diagnose_only`. `diagnose_only` runs the first movement and stops — it attributes every miss to the stage that lost it and proposes nothing. `apply` lets a winner that passed every gate become the fleet's live ranking; separate from `force` on purpose, because "run it anyway" must not imply "change what every replica serves". `400` when tuning is disabled and `force` is not set. |
| GET | `/tuning/status` | What a batch is doing **right now**, read from `/state` rather than the process running it — so it answers for a batch this replica did not start and one whose container has stopped. Returns `status`, `phase`, `completed_units`/`total_units`, `progress`, `searches`, `attempts`, `error`. An expired heartbeat reports `interrupted`, and the next attempt resumes from its stored trials. |
| GET | `/tuning/report` | The latest report, or `?experiment=<id>`: the stage diagnosis, the trials with each one's motivating stage and rationale, the paired comparisons, every gate, the decision, and the bundle if one was produced. |
| GET | `/tuning/experiments` | Recent batches with status, phase and searches spent. |
| GET | `/tuning/parameters` | What this region ranks with, whether the values come from `config` or an applied `bundle`, the full tunable space (each parameter's stage, ladder and bounds), and the equivalent `search.ranking` block to paste into `pheasant.yaml`. |
| GET | `/tuning/bundles` | Configuration bundles this region has produced, and which one is live. |
| POST | `/tuning/bundles/apply` | Make one bundle the region's live retrieval overlay. Body: `bundle_id`, optional `applied_by`. `404` on an unknown id — a silent success would leave ranking unchanged while reporting that it changed. |
| POST | `/tuning/bundles/rollback` | Stand the active overlay down; the region returns to its configured values. |

**Applying is fleet-scoped by construction.** The active bundle is one row in
`/state`; every replica resolves it on a short TTL, so a fleet converges without
a rolling restart. There is deliberately no per-request and no per-principal
override, and nowhere in the schema for one to live: retrieval parameters that
varied by caller would make two agents disagree about what the region contains,
and would make every number the evaluation plane publishes a measurement of
whoever happened to ask.

Running a batch is a background job because it replays a cohort through the real
search path. It takes the `__tuning__` lease (so several replicas produce one
batch), never takes `sync_lock`, and stands down while the index queue has work
in it — indexing is somebody waiting, and this is a measurement.

See [Retrieval performance tuning](../retrieval-tuning.md).

## Stress-test readiness

| Method | Path | Purpose |
|---|---|---|
| GET | `/readiness/contract` | What this build supports, with a digest a harness pins to detect the region changing shape between arms. Served whether or not `readiness.enabled` is set — answering "can this region be measured" with a 404 is indistinguishable from an old build that has no contract — and reports `readiness_enabled` so the caller can tell. |
| POST | `/readiness/check` | Probe this region and return the go/no-go verdict per gate set. **Gated on `readiness.enabled`**, because a check writes: it submits documents to a scratch source it owns, indexes them and seals snapshots. Returns `409` when the region has not opted in. |
| POST | `/ingest/submit` | Persist documents with an idempotency key and one receipt per item. A retry under a known key folds onto the receipt it wrote. Acceptance is not searchability. |
| GET | `/ingest/status` | Receipts by `idempotency_key` or `submission_id`: `accepted`, `indexed`, `rejected` or `failed`. |
| POST | `/ingest/acknowledge` | Cross the index barrier for receipts whose artifacts now exist. |
| GET | `/ingest/reconcile` | Submitted against held, with `silent_loss` named. |
| POST | `/snapshots/seal` | Seal the current state as a run's reference snapshot. Idempotent over an unchanged region. |
| GET | `/snapshots` | Every snapshot this region holds, saying which are sealed. |
| GET | `/snapshots/{snapshot_id}` | One snapshot's manifest plus a live drift verification. |

`POST /search` accepts `snapshot_id`, `as_of` and `trace_id`. A pinned search is
verified **before** the arms run and refused with `409 SNAPSHOT_DRIFTED` if the
corpus has moved — the caller is going to attribute whatever comes back to the
snapshot it named, so the only safe order is to establish that the name is still
true first.

Every search response carries a `lineage` block: `state` (snapshot, ranking
bundle, memory policy, principal, `as_of`, criteria — all functions of the
request and the region) and `timing` (`retrieval_ms`, `truncated`, `returned`).
`retrieval_ms` is the only non-deterministic field a search returns.

Refusals carry `code` and `retryable` beside the unchanged `detail` text. The
full code table is in the readiness contract.

See [Stress-test readiness](../stress-test-readiness.md).

## Assistant (grounded chat)

| Method | Path | Purpose |
|---|---|---|
| GET | `/assistant/status` | Whether a model is reachable, from where (config env var or session key), and the resolved provider/model. |
| POST | `/assistant/key` | Hand the server an API key for this session only. Held in process memory behind an opaque token; never written to config, `/state`, or logs. |
| DELETE | `/assistant/key` | Revoke a session key immediately. |
| GET | `/assistant/workflows` | Available answering workflows, which one `auto` currently resolves to, whether the `[agent]` extra is installed, and each workflow's option defaults. |
| POST | `/assistant/chat` | Ask a question. Returns the answer, numbered citations, graph facts, the nodes to focus, and the workflow's step trace. Accepts `workflow` and `options` overrides. |

See [Ask your knowledge base](../how-to/chat-and-ui.md) and
[Customize the answering workflow](../how-to/agent-workflows.md).

## Answering effort (`assistant.retrieval`)

How hard the answering workflows look before they answer. Not to be confused
with the **retrieval tuning plane** above, which tunes how the region *ranks*;
these knobs decide how much it *fetches* per question.

| Method | Path | Purpose |
|---|---|---|
| GET | `/assistant/retrieval` | The typed `assistant.retrieval` settings, what is *effective* once `workflow_options` is layered on, and one line of help per knob. |
| PUT | `/assistant/retrieval` | Change one or more knobs. Omitted fields are left alone. Query-time only, so it applies to the next question — no restart, no re-index. |

## Semantic search (embeddings)

| Method | Path | Purpose |
|---|---|---|
| GET | `/search/embeddings` | Embeddings settings, vector coverage, and which vector backends are installed here. |
| PUT | `/search/embeddings` | Enable/configure embeddings in the live process; `persist: true` also writes the `search.embeddings` / `search.vector_store` keys back to the config file. Refuses a backend whose optional extra is missing. |
| POST | `/search/embeddings/reindex` | Embed already-indexed content without re-reading sources. Idempotent; `?drop_existing=true` discards vectors left in a stale embedding space. |

See [Vector self-search](../how-to/vector-search.md).

## MCP

| Method | Path | Purpose |
|---|---|---|
| GET | `/mcp/info` | Transports, a ready-to-paste client config, and the tool list an attached agent gets. |

## Graph

| Method | Path | Purpose |
|---|---|---|
| GET | `/graph` | Full graph. Filter with `types` / `exclude_types` / `source` before the node limit applies. |
| GET | `/graph/slice` | Subgraph around a node. |
| GET | `/graph/neighbors` | Neighbors of a node (depth + edge filters). |
| GET | `/graph/export/node-link-json` | Export graph as node-link JSON. |
| GET | `/graph/export/cytoscape-json` | Export graph as Cytoscape JSON. |
| GET | `/graph/diagnostics` | Structural health: node/edge type histograms, hubs by degree, orphan count, density. Walks the whole graph — do not poll it. |
| GET | `/graph/path` | Shortest path between two nodes (`source`, `target`, `max_depth`). Edges are followed in **both** directions: relatedness is not a question about which way an import points. |

## Filesystem & config

| Method | Path | Purpose |
|---|---|---|
| GET | `/fs/list` | List filesystem entries under allowlisted roots (for the directory picker). |
| GET | `/fs/host-path` | Can pheasant see this host path? Answers `native` / `visible` / `not_mounted` / `unknown`, and for `not_mounted` returns the exact remedy — compose volume, `docker run` flag, and the `allow_workspace_roots` entry it also needs. |
| GET | `/config/sections` | Every config section with whether the running process can pick a change up. |
| PATCH | `/config/section/{section}` | Validate, persist and (where safe) hot-apply **one** section. Reports `applied` vs `restart_required` honestly rather than saying "saved" for a value the process is still ignoring. |
| GET | `/knowledge-base` | This knowledge base's identity and paths. |
| PUT | `/knowledge-base` | Edit name/description. A **rename** changes `kb_id` — the graph root node and every stable artifact id — so it reports the full re-index it implies instead of silently orphaning the graph. |
| GET | `/config` | Current config. |
| GET | `/config/effective` | Resolved config after profile + YAML + overrides. |
| PUT | `/config` | Update config. |

## Example: search

```bash
curl -X POST http://localhost:8765/search \
  -H "content-type: application/json" \
  -d '{"query": "billing owner", "mode": "hybrid", "max_results": 5}'
```
