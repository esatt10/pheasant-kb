# Configuration

pheasant reads YAML from `/config/pheasant.yaml` by default. Start from `pheasant.example.yaml` and mount it read-only into the container.

The local `pheasant.yaml` copy is intentionally ignored by git because it contains host-specific mount assumptions. Commit changes to `pheasant.example.yaml` when you want to update the shared pattern. Docker Compose env files generated under `.pheasant/` are ignored.

For a one-line local run, use a profile:

```bash
pheasant start --profile quickstart --config pheasant.yaml
```

Config is resolved as base defaults + profile + YAML + `--set` overrides. Inspect the result with:

```bash
pheasant config show --effective --profile dev --config pheasant.yaml
```

## How to use this guide

1. Copy `pheasant.example.yaml` to your runtime config path.
2. Keep the top-level sections in place and only edit values you need.
3. For each setting below, choose values based on your deployment mode and data sensitivity.
4. Validate by starting pheasant and checking startup logs for loaded source counts and enabled transports.

---

## Top-level sections

| Section | Purpose | Required |
|---|---|---|
| `deployment` | Image and mount hints used by deployment tooling/templates. | Recommended |
| `pheasant` | Instance identity, environment label, and core filesystem roots. | Yes |
| `server` | API/MCP/UI network bindings and feature toggles. | Yes |
| `storage` | Database/graph/manifests locations and state limits. | Yes |
| `search` | Retrieval modes and ranking behavior. | Yes |
| `ingestion` | Turning binary/markup files (documents, images, audio) into indexable text. | Optional |
| `sync` | Watcher, git polling, schedule, idempotency, and concurrency behavior. | Yes |
| `graph` | Knowledge-graph density (concept-node threshold, WASM acceleration). | Optional |
| `security` | Path allowlisting, source-read protections, and ACL enforcement. | Strongly recommended |
| `synapse` | Federation into a Synapse fleet (contract publishing, signing). | Optional, standalone-safe |
| `memory` | Agent-memory consolidation policy (TTL decay, supersede archiving). | Optional |
| `assistant` | Grounded chat over the index (the UI's chat layer). Query-time only. | Optional |
| `evaluation` | Knowledge-effectiveness measurement: cohorts, proof, ablations, gates. | Optional |
| `readiness` | Whether an outside harness may trust this region's answers: the capability contract, the go/no-go gates, and the SLO budgets they decide against. | Optional |
| `sources` | All indexed repositories/folders/files/URLs (incl. per-source `taxonomy`). | Yes |

> **Note:** this table's row order follows `PheasantConfig`'s field order in
> `src/pheasant/config/schema.py`, not necessarily the order sections appear
> below. `tests/test_config_surface_freshness.py` fails CI if a new top-level
> settings block lands in that file without a mention here and without being
> reachable from `pheasant setup` — see [Set pheasant up](how-to/setup.md).
> You rarely need to write any of this by hand: `pheasant setup` asks about
> every section, and the web UI's Settings page edits most of them live.

---

## `deployment`

### `deployment.compose`

| Key | Type | Example | What it controls |
|---|---|---|---|
| `image_repository` | string | `ghcr.io/esatt10/pheasant` | Container image registry/repository for Compose-based runs. |
| `image_tag` | string | `0.1.3` | Image version tag used by deployment helpers. |
| `workspace_path` | path-like string | `./workspace` | Host path mounted to pheasant `workspace_root`. |

---

## `pheasant` (core instance settings)

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | `local-pheasant` | Used as instance/knowledge-base identifier. |
| `description` | string | `Lightweight MCP knowledge graph and retrieval server` | Human-readable descriptor for operators. |
| `environment` | string | `local` | Label (for example `local`, `dev`, `staging`, `prod`). |
| `log_level` | string | `INFO` | Typical values: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `state_path` | absolute path | `/state` | Base path for sqlite, graph snapshots, and manifests. |
| `workspace_root` | absolute path | `/workspace` | Root path sources should live under. |
| `exports_path` | absolute path | `/exports` | Output path for generated artifacts, including [Parquet exports](how-to/parquet-exports.md) under `parquet/<kb_id>/`. |

---

## `server` (connection options)

### Network binding

| Key | Type | Default | Notes |
|---|---|---|---|
| `host` | string | `0.0.0.0` | Bind address. `pheasant up` generates `127.0.0.1`; containers keep `0.0.0.0` (loopback inside a container is unreachable from the host) and compose publishes them to `127.0.0.1` instead. |
| `port` | integer | `8765` | Primary service port. |
| `role` | string | `all` | Which jobs this process takes on. `pheasant serve --role` overrides it. See below. |

On one container the API is unauthenticated, so the bind address is a security
control rather than a networking detail — see
[security.md](security.md#trust-model-for-the-http-api). In a fleet the pods
must bind `0.0.0.0`, so the bind address stops being a control at all and
`security.api_auth` takes over: every role but `all` refuses to start on a
routable bind without it.

### Process roles (`server.role`)

One process doing everything is right for one container and wrong for a
fleet — not for performance reasons, but because of replicas. Run three
copies of the default process against one knowledge base and all three watch
the same directories, all three fire the same scheduled sync, and all three
try to index the same source. Roles say which jobs a process has:

| Role | Watches | Schedules | Drains the queue | Indexes | Typical deployment |
|---|---|---|---|---|---|
| `all` | yes | yes | no | in-process | one container — **the default** |
| `api` | no | no | no | **never** | N replicas behind a Service |
| `indexer` | yes | yes | yes | in-process | one per shard |
| `graph` | no | no | no | no | internal graph-query service |
| `worker` | no | no | no | no | M replicas, autoscaled |

```bash
pheasant serve                     # all — unchanged
pheasant serve --role api          # serve; publish index work
pheasant serve --role indexer      # watch, schedule, drain
pheasant serve --role graph        # authenticated graph reads + snapshot refresh
pheasant worker --transport grpc   # preparation only
```

On PostgreSQL, `indexer` replicas elect one orchestrator per knowledge-base
shard. Only the leader starts watcher, scheduler and queue drain; standbys
report `leader: false` and `/ready` returns 503 until promotion. Inside the
leader, all three work producers share one child-sync lock. Scale preparation
workers for throughput and shard the knowledge base for more commit capacity;
extra indexers provide failover, not parallel graph writers.

`all` deliberately does **not** drain the queue: a single container turns the
queue on for [crash resumption](#the-index-work-queue-syncqueue), not to
become a fleet member, and `sync_all` already drains what it publishes.

**`api` requires the queue.** It publishes index work rather than running it,
so without `sync.queue.enabled: true` and an indexer on the same queue a sync
request would be accepted and then go nowhere. `pheasant serve --role api`
refuses to start in that case rather than letting you find out later. A
blocking sync (`wait: true`) against an api replica returns **409** with the
fix, because publishing is a different promise from "run this and return the
result".

With `graph.query_service_url` set, API and mounted MCP replicas do not load a
graph of their own; the `graph` role answers for them. API readiness fails when
that service is unreachable. There is deliberately no fallback to a local
graph: fallback would duplicate the graph into every replica at the exact
moment the graph tier is unhealthy and memory headroom is most valuable.

On the default `storage.graph_format: rows` that tier is **optional** rather
than the only way out — a serving replica queries `graph_nodes` directly and
holds nothing, so a bounded walk costs a query instead of 1.65 GB of resident
graph (measured at 100k files). Keep the graph service when you want graph
reads on their own connection pool and their own scaling curve; drop it when
you do not.

Routes are *not* hidden per role. What keeps search traffic off an indexer is
the Service selector in front of it; a role whose `/search` returned 404 would
be much harder to debug than one that simply has no clients. What a role
changes is what the process does **on its own**.

`GET /health` and `GET /ready` both report the role, so a pod can be
identified from a probe response. `/ready` returns 503 when the state store is
unreachable — that should take a replica out of the Service, not restart it,
which is why it is separate from `/health`. It is deliberately **not** gated
on the index being populated: a replica that stayed unready through a
multi-hour first index would take the whole Service down for that time.

### Serving durability (`server.api`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `max_concurrent_requests` | integer | `0` | In-flight requests before the surplus is refused with **429 + `Retry-After`**. `0` disables it. |
| `drain_seconds` | integer | `0` | Seconds to keep serving after SIGTERM while `/ready` already reports 503. `0` disables the delay. |
| `graph_refresh_seconds` | integer | `30` | Backstop interval for re-reading a graph written by the indexer, on a legacy local-graph `api` or the dedicated `graph` role. Not the usual trigger: on the `nats` queue backend an indexer announces each commit and replicas reload at commit latency, and this is the ceiling on how long a *missed* announcement can go unnoticed. A remote-graph API ignores it. `0` disables it. |

Both default off, and that is a decision rather than caution.

**Shedding only makes sense when there is somewhere else to go.** With one
process, a burst piles up behind anyio's shared worker-thread pool (see
below) and every request gets slower — but waiting is still the best
available answer, and a 429 to the only user is worse. With N replicas
behind a load balancer a fast 429 is strictly better than a request that sits
for thirty seconds and times out anyway. So set this on replicas, not on a
laptop.

`/health`, `/ready` and `/metrics` are **never** shed. A pod that answers 429
to its own liveness probe gets restarted by the thing meant to be protecting
it, turning a busy replica into a crash-looping one. All three are `async
def` routes for the same reason: they answer without needing a worker-thread
token from the pool below, so a saturated pool no longer delays them either.

**`max_concurrent_requests` and the thread pool are two separate budgets.**
Every sync `def` HTTP route — most of them — and every MCP tool call made
against the `/mcp` mount in this same process (`mcp_server/server.py`'s
`@mcp.tool()` handlers are correctly synchronous; ingestion-path determinism
forbids an async LLM call on the pipeline they share) run on anyio's shared
worker-thread pool, 40 tokens by default and otherwise unrelated to
`max_concurrent_requests`. On startup, if `max_concurrent_requests` is set,
this process raises that pool's token count to at least match it (never
lowers it) — so a request the limiter admits never silently queues for a
thread instead, which would reintroduce the blocking behavior shedding
exists to replace. Size `max_concurrent_requests` for HTTP *and* MCP traffic
together, not HTTP alone, since both draw from the one pool.
`GET /metrics`'s `pheasant_threadpool_tokens_total` /
`_tokens_available` show the pool's current headroom next to
`pheasant_requests_inflight`.

**Draining exists because Kubernetes does two things at once.** SIGTERM and
endpoint removal happen concurrently, and endpoint propagation is not instant
— kube-proxy on every node has to be told. A process that exits promptly
therefore drops whatever was routed to it in the gap. `drain_seconds` fails
readiness *first*, keeps serving for that long, and only then shuts down. Keep
it comfortably shorter than the orchestrator's termination grace period, or
the pod is killed mid-drain. A second SIGTERM skips the wait.

Draining is "stop being sent work", not "stop doing work": a draining replica
keeps answering requests normally.

The MCP server is stateless (`stateless_http=True`), which is what makes
replicas safe — two requests from one agent may land on different replicas and
both answer correctly. That property is pinned by a test.

### MCP options (`server.mcp`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | Global MCP enable/disable toggle. |
| `transports.stdio` | bool | `true` | Enables local stdio transport (common for editor integrations). |
| `transports.streamable_http` | bool | `true` | Enables HTTP streaming MCP transport. |
| `transports.sse` | bool | `false` | Enables SSE transport if your client requires it. |

### API/UI options

| Key | Type | Default | Notes |
|---|---|---|---|
| `api.enabled` | bool | `true` | Enables REST API endpoints. |
| `api.openapi` | bool | `true` | Exposes OpenAPI schema/docs endpoints. |
| `api.cors_origins` | list[str] | localhost dev/UI origins | Browser origins allowed to call the API. The shipped UI proxies `/api/*` same-origin and needs no entry here. |
| `api.cors_allow_all_origins` | bool | `false` | Restores `Access-Control-Allow-Origin: *`. The API is unauthenticated — only enable behind an authenticating ingress. |
| `ui.enabled` | bool | `true` | Enables web UI routes (if packaged). |
| `ui.graph_visualization` | bool | `true` | Enables graph visualization features in UI. |

---

## `storage` (state + persistence)

### `graph_format`: where the published graph lives

`rows` (default) keeps the graph in the state database, next to the artifacts
it describes. `node_link_json` keeps the pre-35.10 single zstd file. Measured
with `python -m pheasant.graph.capacity` at 100k files (630k nodes, 630k edges):

| | `node_link_json` | `rows` |
|---|---|---|
| commit after a one-file change | 6.15 s, growing with the graph | **1.1 ms, flat** |
| load before a replica can serve | 4.9 s | none |
| resident bytes to answer a query | 1.65 GB | none |
| bounded 3-hop walk | in-RAM | 0.12 ms |
| stored bytes | 17.9 MB | 1,033 MB |

The last row is the cost, and it is real: the graph stops being something every
replica holds and becomes something the volume holds, at roughly 1,640 bytes
per node. `pheasant scan` includes it, so size the PVC from there rather than
from the old file.

Choose `rows` unless you have a reason not to. Choose `node_link_json` if disk
is your binding constraint and your corpus is small enough that a whole-graph
rewrite per commit is free.

Switching to `rows` imports an existing graph file once at boot and renames it
`*.migrated` — nothing is deleted, and switching back reads it again. Both
backends write snapshots as files, so `graphs/` and your backup procedure are
unchanged either way.


| Key | Type | Default | Notes |
|---|---|---|---|
| `graph_format` | string | `rows` | Where the published graph lives: `rows` (in `graph_nodes`/`graph_edges` in the state database) or `node_link_json` (one zstd file). See below. |
| `graph_snapshot_interval_seconds` | integer | `900` | Graph snapshot cadence. |
| `sqlite_path` | absolute path | `/state/pheasant.db` | Main SQLite database file. |
| `graph_path` | absolute path | `/state/graphs` | Directory for graph snapshots. |
| `manifest_path` | absolute path | `/state/manifests` | Directory for source manifests. Connector checkpoints are stored in SQLite. |
| `max_state_size_gb` | integer | `10` | Soft state budget for cleanup/policy logic. |
| `compression.enabled` | bool | `true` (example) | Optional compression toggle for persisted artifacts. |
| `compression.algorithm` | string | `zstd` (example) | Compression codec name. |
| `retention.keep_snapshots` | integer | `10` (example) | Snapshot retention count target. |
| `retention.keep_event_days` | integer | `30` (example) | Event retention age target in days. |

> Notes:
> - If `sqlite_path`, `graph_path`, or `manifest_path` are omitted, they are derived from `pheasant.state_path`.

---

### Where state lives (`storage.backend`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `backend` | `sqlite` \| `postgres` | `sqlite` | SQLite is a file and permits **one writer process per knowledge base** — pheasant's hard scaling ceiling. Postgres lifts it. |
| `dsn_env` | string | `PHEASANT_DATABASE_URL` | Name of the environment variable holding the libpq DSN. Only the variable *name* goes in YAML — a DSN carries a password, and there is deliberately no field to paste one into. |
| `pool_size` | integer | `10` | Server-side connections this process may hold. Unlike a SQLite file handle, each is a real process on the database. |

Postgres needs the extra: `pip install 'pheasant-kb[postgres]'` (the published
image already has it).

```yaml
storage:
  backend: postgres
  dsn_env: PHEASANT_DATABASE_URL
  pool_size: 10
```

**The default needs no infrastructure and is unchanged.** Leaving `backend`
alone gives you exactly the SQLite deployment you had.

#### Moving existing state across

```bash
export PHEASANT_DATABASE_URL='postgresql://pheasant:...@db:5432/pheasant'
pheasant migrate --to postgres -c pheasant.yaml
```

Copies every table, rebuilds `chunks_fts` for the target dialect, verifies the
row counts, and only then renames the SQLite file to `*.migrated` — it is never
deleted. Re-running is safe: a table that already holds rows is left alone.
Stable IDs carry over byte-identically, so no re-index is needed.

The knowledge, memory, observation and **evaluation** planes all come across —
including recorded proof, which is the one thing in the region that cannot be
re-derived, and the frozen anchor cohort, without which every trend point after
the migration would be measured against different questions than every point
before. Four kinds of row are deliberately left behind, and
`pheasant.persistence.migrate.NOT_MIGRATED` names each with its reason: derived
indexes (rebuilt for the target dialect), in-flight queue and lease claims
(meaningless once detached from the process that claimed them), and evaluation
*replay checkpoints* — a checkpoint is a cached retrieval result, and since the
two backends rank differently (see below), reusing one across the migration
would make a single run's numbers half FTS5 and half `ts_rank_cd`. An
interrupted batch simply replays from scratch on the new backend.

#### One difference worth knowing about

Ranking is not identical between the two. SQLite ranks with FTS5's `bm25()`;
Postgres uses `ts_rank_cd`, which has no inverse document frequency and no
term-frequency saturation of its own. pheasant supplies both — the rank is a
sum over query terms of `IDF x saturated rank`, and titles/paths are tokenized
the way FTS5's `unicode61` does so a search for `deploy` still matches
`deploy-gateway.md`. What remains is that BM25 normalizes each column by *that
column's* length while `ts_rank_cd` normalizes the whole vector once, so the
two can disagree about ranks 2-3 when a title match on a common word competes
with body matches on rare ones. The top hit agrees on the gold set;
`tests/test_backend_parity.py` is the gate.


## `search` (retrieval behavior)

| Key | Type | Default | Notes |
|---|---|---|---|
| `default_mode` | string | `hybrid` | Typical modes: keyword, path, graph, hybrid (implementation-dependent). |
| `keyword.enabled` | bool | `true` (example) | Enables keyword index/query path. |
| `keyword.engine` | string | `sqlite_fts5` (example) | Keyword backend engine. |
| `embeddings.enabled` | bool | `false` | Enables embed-on-sync + `mode=vector` self-search (Synapse 21.4). |
| `embeddings.provider` | string | `openai-spec` | `openai-spec` (OpenAI-compatible HTTP endpoint) or `stub` (deterministic, offline). |
| `embeddings.model` | string | `text-embedding-3-small` | Embedding model name (must match the Synapse fleet pin when federated). |
| `embeddings.base_url` | string | `https://api.openai.com/v1` | OpenAI-spec endpoint base; `POST {base_url}/embeddings`. |
| `embeddings.api_key_env` | string | `OPENAI_API_KEY` | Name of the env var holding the API key (key never lands in config/state). |
| `embeddings.dimensions` | integer \| null | `null` | Unset by default — the `dimensions` request field is simply omitted, so the provider returns the model's own native size (e.g. 1536 for `text-embedding-3-small`, 3072 for `text-embedding-3-large`). Set an explicit number only to shrink vectors for storage (OpenAI's `-3` models support this) or to pin an exact size across a Synapse fleet. |
| `embeddings.batch_size` | integer | `64` | Texts per embedding HTTP request. |
| `embeddings.max_retries` | integer | `4` | Retries for transient transport and 5xx failures. Authentication and malformed requests fail immediately. |
| `embeddings.retry_backoff_seconds` | number | `1.0` | Initial exponential-backoff delay. Locally chosen waits cap at 30 seconds and use jitter. |
| `embeddings.rate_limit_max_wait_seconds` | number | `300.0` | Cumulative wait budget for provider 429 responses before the durable source task is allowed to fail. Provider `Retry-After`/quota-reset headers are honored in full; concurrent embedding threads share one cooldown and reduce/ramp concurrency adaptively. Set `0` to use ordinary bounded retries. |
| `vector_store.provider` | string | `lancedb` | `lancedb` (optional `[vector]` extra) or `numpy` (always-available flat file). |
| `vector_store.path` | absolute path | `<state>/vectors` | Vector index root; vectors live under `<path>/<kb_id>/`. Created only when embeddings are enabled. |
| `ranking.prefer_exact_path_matches` | bool | `true` (example) | Boost exact path matches. |
| `ranking.prefer_recent_commits` | bool | `true` (example) | Boost content tied to recent commits. |
| `ranking.graph_neighbor_boost` | bool | `true` (example) | Boost graph-adjacent matches. |
| `ranking.max_results_default` | integer | `10` | Default result count cap. |
| `ranking.filter_overfetch` | float | `3.0` | How far past `max_results` the arms fetch when a post-filter will remove candidates afterwards — ACL, memory policy, section, **and** the retrieval criteria a caller passes (`exclude_sources`, `node_types`, `min_score`, `source_types`). One parameter for every over-fetch: the surfaces each carried a hardcoded `× 4` until 35.9, so raising this moved some filters and not others while the tuning glossary said it governed the `filters` stage. Clamped to `(1.0, 10.0)`; below 1.0 would turn an over-fetch into a truncation. |
| `wasm_relationship_search` | bool | `false` | Run `graph_search._scan_edges` through the vendored WASM accelerator (Synapse 34.5b) instead of pure Python. Needs the `[wasm]` extra; falls back to pure Python on any failure or if the extra is missing — never a correctness dependency. A consistent, growing win (2-8x at 34.4's benchmark scale) on the relationship-search query path. **The Docker image turns this on** in a config it generates itself, since it always installs the `[wasm]` extra. |

---

## `ingestion` (multi-modal: image captioning + audio transcription)

Only takes effect for a source whose `include` globs admit an image or
audio extension — a text-only region builds neither captioner nor
transcriber and stays byte-identical to a pre-25.4 config. Captions/
transcripts flow through the normal chunk → embed → graph path like any
other text; an authored sidecar (`<image>.caption.txt` /
`<audio>.transcript.txt`) always wins over the model.

| Key | Type | Default | Notes |
|---|---|---|---|
| `captioner.provider` | string | `stub` | `stub` (deterministic, offline, default — caption = template over file name + digest of bytes) or `openai-spec` (vision-capable chat model, `POST {base_url}/chat/completions` with an `image_url` part). |
| `captioner.model` | string | `gpt-4o-mini` | Vision model name (only used by `openai-spec`). |
| `captioner.base_url` | string | `https://api.openai.com/v1` | OpenAI-spec endpoint base. |
| `captioner.api_key_env` | string | `OPENAI_API_KEY` | Env var name holding the key; the key itself never lands in config. |
| `captioner.prompt` | string | `Describe this image in one concise sentence for search indexing.` | Prompt sent with each image. |
| `transcriber.provider` | string | `stub` | `stub` (deterministic, offline, default — no audio library, no network) or `openai-spec` (`POST {base_url}/audio/transcriptions`, multipart upload). |
| `transcriber.model` | string | `whisper-1` | Speech-to-text model name (only used by `openai-spec`). |
| `transcriber.base_url` | string | `https://api.openai.com/v1` | OpenAI-spec endpoint base. |
| `transcriber.api_key_env` | string | `OPENAI_API_KEY` | Env var name holding the key. |

See [Multi-modal ingest](how-to/multimodal-ingest.md).

---

## `sync` (orchestration + change detection)

### Startup policy

| Key | Type | Example | Notes |
|---|---|---|---|
| `startup.full_validation` | bool | `true` | Validate all sources on startup. |
| `startup.repair_missing_indexes` | bool | `true` | Rebuild missing index artifacts automatically. |

### Watcher policy (`sync.watcher`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | Enable file-system watching. |
| `max_watch_paths` | integer | `100` | Upper bound on watched roots. |
| `debounce_ms` | integer | `1500` | Debounce delay before processing change bursts. |
| `batch_window_ms` | integer | `5000` | Batch window for event coalescing. |

### Git policy (`sync.git`)

| Key | Type | Default / Example | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | Enable repository-aware sync behavior. |
| `detect_commit_changes` | bool | `true` | Detect HEAD updates. |
| `detect_branch_switch` | bool | `true` | Detect active-branch changes. |
| `reindex_on_commit` | bool | `true` | Re-index source content when commit changes are detected. |
| `reindex_on_branch_switch` | string | `validate_only` (example) | Branch-switch handling strategy. |

### Scheduler policy (`sync.scheduler`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | Enable periodic fallback sync job. |
| `interval_seconds` | integer | `900` | Scheduler interval. |

### Size guardrails (`sync.limits`)

A source may point at any readable path, which makes "I accidentally indexed
my home directory" a realistic mistake. These limits are checked **during**
traversal, before any file is read, so an oversized source is refused rather
than consuming memory until the process dies. Set any field to `null` to
disable that limit.

| Key | Type | Default | Notes |
|---|---|---|---|
| `max_files` | integer\|null | `50000` | Matching files, after include/exclude. |
| `max_file_size_mb` | integer\|null | `25` | Skip any single file larger than this. Skipped files are reported, not fatal. |
| `max_total_mb` | integer\|null | `4096` | Total matched content. |
| `follow_symlinks` | bool | `false` | Home directories routinely contain links that escape the root or loop. |

A source can override the whole block with `sources[].limits`.

**A source over budget indexes nothing.** A partial index would be
non-deterministic, and silently indexing the first N files of a home
directory is worse than a clear stop. The sync returns
`status: "limit_exceeded"` with a message naming the limit and the largest
subtrees. Your options are: narrow it (`max_depth`, tighter `include`, more
`exclude`), raise the limits, or sync once with `--full-scan`.

### Knowing the size first (`pheasant scan`)

```bash
pheasant scan -c pheasant.yaml            # every enabled source
pheasant scan -s notes --depth 2 --json   # one source, machine-readable
```

`scan` walks without reading or indexing anything and reports the file
count, total size, largest subtrees, oversized files, and a **files-by-depth
table** so a depth cap can be chosen from evidence rather than guessed. It
also reports whether the configured limits would refuse the sync. Also
available as `POST /sources/{id}/scan` and the `scan_source` MCP tool.

### Per-run traversal toggles

`--depth N` and `--full-scan` apply to one invocation and are never written
back to the source config, so a one-off wide sync cannot silently become the
standing behavior of a scheduled one.

| Surface | Depth cap | Full scan |
|---|---|---|
| CLI | `pheasant sync --depth N` | `pheasant sync --full-scan` |
| HTTP | `{"depth": N}` on `/sync`, `/sync/{id}` | `{"full_scan": true}` |
| MCP | `max_depth=N` on `sync_source`/`sync_all` | `full_scan=true` |

`--full-scan` lifts both the depth cap and the size budget — the explicit
"yes, index all of it" switch.

### Idempotency + concurrency

| Key | Type | Example | Notes |
|---|---|---|---|
| `idempotency.hash_algorithm` | string | `sha256` | File identity hashing algorithm. |
| `idempotency.compare_size_mtime_hash` | bool | `true` | Use multiple file properties before reprocess. |
| `idempotency.skip_unchanged_files` | bool | `true` | Skip ingestion when source file is unchanged. |
| `concurrency.max_parallel_sources` | integer | `4` | Concurrent source processing cap. |
| `concurrency.max_parallel_files` | integer | `8` | Per-source preparation workers. Workers read/parse immutable inputs; SQLite, graph, manifest and vector-store commits stay coordinated and deterministic. |
| `concurrency.max_parallel_embeddings` | integer | `4` | Provider-sized embedding requests allowed in flight. Set this to the provider/rate-limit capacity, not blindly to CPU count. |
| `concurrency.file_executor` | string | `thread` | `thread` for low-overhead I/O overlap; `process` for CPU-heavy plain-text parsing (capped by the process-visible CPU quota); `remote` for authenticated worker nodes. Document/modal/taxonomy/repair work that needs local handler state falls back to threads. |
| `concurrency.remote_worker_urls` | list[string] | `[]` | Worker base URLs used round-robin when `file_executor: remote`. The coordinator reads the connector payload, then sends that immutable text payload for parsing/chunking. |
| `concurrency.remote_worker_enabled` | bool | `false` | Expose this instance's authenticated `POST /internal/indexing/prepare` worker endpoint. It never writes SQLite, graph, manifests or vectors. |
| `concurrency.remote_worker_token_env` | string | `PHEASANT_INDEX_WORKER_TOKEN` | Environment variable containing the shared bearer token. Required on coordinators and workers; the token is never stored in config or task bodies. |
| `concurrency.remote_worker_timeout_seconds` | integer | `120` | Per-task remote-worker timeout, and the deadline sent with the request so the worker declines work whose caller has already given up. |
| `concurrency.remote_worker_batch_size` | integer | `8` | Files per request. A batch amortizes request overhead and carries one deadline for the group, but every task in it holds its file's bytes in memory on both sides — so treat this as a memory knob as much as a throughput one. |
| `concurrency.worker_transport` | string | `http` | `http` (stdlib, no extra) or `grpc` (needs the `[grpc]` extra). Changes only the wire format: retry, failover, breakers, deadlines and idempotency are transport-independent. |
| `concurrency.lock_timeout_seconds` | integer | `120` | How long an in-process sync waits for an already-active lock on the same source. The interactive engine lease remains fail-fast; server-owned subprocess workers use their existing explicit lease wait. |

### Worker durability

`file_executor: remote` used to pick a worker with `position % len(urls)` and
send one request per file, so a single dead worker failed its share of *every*
sync. Dispatch is now pooled and durable, and none of it is configurable
because none of it should be a decision an operator has to get right:

* keep-alive connections, reused for the whole source;
* bounded retry with full jitter, honouring `Retry-After`;
* a circuit breaker per endpoint — three consecutive failures take it out of
  rotation for 30 s, so a dead worker costs three requests rather than a share
  of every file;
* failover to another endpoint, then to **local preparation**. Remote
  preparation is a throughput optimization, so it can never fail a sync: with
  every worker down, the index is byte-identical, only slower;
* a `pheasant_worker_up{endpoint}` gauge per endpoint;
* content-addressed idempotency keys, so a retry after a timeout is answered
  from the worker's bounded cache instead of re-parsed.

A worker that predates the batch route answers 404 once and the coordinator
falls back to the single-task path, so a coordinator upgraded ahead of its
fleet keeps working.

Run a gRPC worker with `pheasant worker --transport grpc --port 8766`
(HTTP workers are served by `pheasant serve` with
`concurrency.remote_worker_enabled: true`). gRPC carries file content as raw
bytes rather than base64 — a flat 33% saving on the only large field — and
streams results, so one refused file no longer fails the whole batch.

### The index work queue (`sync.queue`)

| Key | Type | Example | Notes |
|---|---|---|---|
| `queue.enabled` | bool | `false` | Off by default. With it off, `sync_all` keeps its sources in an in-process list exactly as before. |
| `queue.backend` | string | `local` | `local` is the state store itself — no broker, works on SQLite and Postgres. `nats` needs the `[queue]` extra. |
| `queue.visibility_seconds` | integer | `300` | How long a claimed task is invisible to other workers. Heartbeats extend it, so this bounds *silence* from a claimer, not work. |
| `queue.max_attempts` | integer | `3` | Attempts before a task is dead-lettered. A dead task is kept, never deleted. |
| `queue.nats_servers` | list[string] | `[]` | JetStream URLs, e.g. `nats://nats:4222`. |
| `queue.nats_stream` / `nats_subject` / `nats_durable` | string | `PHEASANT_INDEX` / `pheasant.index.tasks` / `pheasant-indexers` | Stream, subject and durable consumer names. |
| `queue.nats_graph_subject` | string | `pheasant.graph.committed` | Subject prefix for graph-commit announcements; the kb id is appended so two regions sharing a broker do not wake each other. Core NATS pub/sub, not a JetStream stream: every replica must hear it, and a dropped message costs one `server.api.graph_refresh_seconds` poll because the poll is kept as the backstop. Only used on the `nats` backend — a region with no broker keeps polling exactly as before. |

Turn it on when a backlog needs to outlive the process holding it. Three
things follow that a list cannot give: a sync killed nine sources into ten
resumes on the tenth, `pheasant_index_queue_depth` becomes a number other
processes (and an HPA or KEDA) can read, and a source that keeps failing is
dead-lettered instead of retried forever.

At-least-once delivery is safe here because indexing is already idempotent by
design — content sha256 plus stable IDs — so a redelivered task re-indexes to
identical state.

```bash
pheasant queue status         # backlog, in-flight, dead letters and why
pheasant queue drain          # index everything currently queued
pheasant queue requeue-dead   # replay dead letters after fixing the cause
```

`max_parallel_sources` overlaps independent connectors and preparation. Shared
state commits and global graph enrichment remain serialized, so the setting is
a throughput cap rather than permission for competing writers. `sync_all()`
returns results in configured source order regardless of completion order.

Measure the useful worker count on the target filesystem/CPU/provider:

```bash
python -m pheasant.sync.benchmark --workers 1,2,4,8
python -m pheasant.sync.benchmark --workers 1,2,4 --executor process --embeddings
```

The benchmark is deterministic and offline (`stub` embeddings when requested).
After an unmeasured warm-up it reports median wall time, individual trials,
files/second and speedup for both a clean full index and its immediately
unchanged incremental pass (three trials by default). A 60-second cold-index
target is workload- and provider-dependent; use this output rather than assuming
that a larger cap is faster.

### Manual sync modes

| Mode | Behavior |
|---|---|
| `incremental` | Uses connector checkpoints and item/content hashes to skip unchanged artifacts. |
| `full` | Rebuilds artifact, chunk, graph, manifest, and checkpoint state for the selected source. |
| `validate_only` | Checks connector health and source readability without writing index artifacts or manifests. |
| `repair` | Rebuilds only missing or invalid artifact/chunk state detected from manifests and database rows. |

---

## `ingestion` (binary/markup files → indexable text)

Some files carry text that cannot be reached by decoding the bytes: a PDF
stores it in compressed content streams, a DOCX in zipped XML, an image or an
audio file not at all. This section configures the handlers that turn those
into text, which then flows through the **normal** chunk → embed → graph path
like any other document.

Every handler is **opt-in by source include**. A source whose `include` globs
admit only code/markdown/config builds none of them, and behaves exactly as it
would if this section did not exist. `pheasant setup` and `pheasant up` emit
`**/*` for detected mixed document folders; hand-written sources can use
explicit document extensions instead.

| Handler | Extensions | Built when `include` admits | Network? |
|---|---|---|---|
| `extractor` | `.pdf` `.docx` `.pptx` `.xlsx` `.doc` `.rtf` `.epub` (+ `.html` `.htm` `.xhtml` when `html_text`) | any of those | **never** |
| `captioner` | `.png` `.jpg` `.jpeg` `.webp` `.gif` | an image extension | only if `provider: openai-spec` |
| `transcriber` | `.wav` `.mp3` `.m4a` `.flac` `.ogg` | an audio extension | only if `provider: openai-spec` |

### `ingestion.extractor` (document text)

Without an extractor, a document is **accepted and then silently produces no
text**: the artifact is discovered, hashed, typed `document` and given a graph
node, but contributes **zero chunks** — findable by its path, invisible by its
content. Configuring the extractor is what makes the contents searchable.

Seven formats are handled:

| Format | Extension | How the text is reached | Notes |
|---|---|---|---|
| PDF | `.pdf` | content streams (`zlib` + operator scan) or pymupdf | Only format with a sandboxed option |
| Word (OOXML) | `.docx` | `word/document.xml` | Includes tables |
| Word (legacy) | `.doc` | OLE2 compound file → FIB → piece table | Word 97-2003; pre-97 layouts are refused, not guessed |
| PowerPoint | `.pptx` | `<a:t>` runs per slide, in slide order | **Speaker notes are indexed too** |
| Excel | `.xlsx` | sheets + `sharedStrings`, tab-separated rows | Sheet names included |
| RTF | `.rtf` | control-word tokenizer | Requires the `{\rtf` signature |
| EPUB | `.epub` | OPF **spine** order → XHTML → text | Spine, not filename order |

Unlike the captioner/transcriber, no provider here uses a model or makes a
network call — the text is already in the file — so every option is fully
offline and deterministic.

| Key | Default | Purpose |
|---|---|---|
| `provider` | `auto` | `auto` \| `native` \| `builtin` \| `sandboxed` (see below) |
| `html_text` | `false` | Strip markup from HTML/XHTML so prose indexes instead of tags |

**Providers**

| Provider | PDF path | DOCX path | Notes |
|---|---|---|---|
| `auto` | `pymupdf`, else builtin | `python-docx`, else builtin | Default. Keeps whichever yields text; never raises into a sync. |
| `native` | `pymupdf` | `python-docx` | Best fidelity: CID/Type0 fonts, custom encodings, complex layout. Both libraries are already core dependencies. |
| `builtin` | `zlib` + content-stream scan | `zipfile` + `xml.etree` | **Standard library only.** No third-party imports at all. |
| `sandboxed` | builtin tokenizer inside the WASM sandbox | same as `builtin` | Fuel + memory cap, zero host capabilities. Needs `pip install 'pheasant-kb[wasm]'`. |

`native` also reads **EPUB** through pymupdf, which lays the book out and walks
it in reading order — a genuine upgrade over the builtin spine walk.

For **PPTX, XLSX, RTF and legacy DOC**, `native` and `builtin` are the *same
code path*. No third-party reader for those formats exists in this project's
dependency tree (`python-docx` handles only OOXML Word; `pymupdf` does not open
them), and the builtin readers are complete for them — in the OOXML and EPUB
formats the XML *is* the text, and RTF is a text format by definition. Listing
`native` as an upgrade there would be a pretend distinction.

**Authored sidecars.** If `<file>.extract.txt` sits next to a document, its
contents are used **verbatim** and no extractor runs — the offline way to give
an image-only scanned PDF real searchable text (mirrors the `.caption.txt` /
`.transcript.txt` sidecars).

**Why `html_text` defaults to off.** `.html` and `.xml` have always been
indexed as *raw markup* (tags, `<script>`, CSS included). Stripping them is an
improvement, but it changes the indexed text and therefore chunk boundaries of
an existing knowledge base, so it is an explicit opt-in rather than a surprise
on upgrade.

**When to choose `sandboxed`.** PDF is a classic hostile-input parser target,
and PDFs arriving from connectors (Google Drive, Slack, Confluence, IMAP) are
not authored by you. In-process, that parse runs with the sync worker's ambient
authority — every configured connector's API token in the environment, a
writable `/state`, network egress. `sandboxed` runs the tokenizer under a fuel
cap, a linear-memory cap, and **no host capabilities at all**. It is *not* a
fallback-on-failure path: if `wasmtime` is missing it raises with an actionable
hint rather than quietly extracting unsandboxed, because an operator who asked
for isolation must not silently get none.

Fidelity trade-off: `sandboxed` and `builtin` handle uncompressed and
FlateDecode streams with single-byte font encodings — the large majority of
real text PDFs — but do not decrypt encrypted PDFs, decode LZW/CCITT streams,
or resolve Type0/CID font CMaps. `native`/`auto` handle those. Pick
`sandboxed` when the *input* is untrusted; pick `auto` when it is yours.

```yaml
ingestion:
  extractor:
    provider: auto
    html_text: false
```

### `ingestion.captioner` / `ingestion.transcriber`

See [Multi-modal ingest](how-to/multimodal-ingest.md) for the full walkthrough.

| Key | Default (captioner) | Default (transcriber) |
|---|---|---|
| `provider` | `stub` | `stub` |
| `model` | `gpt-4o-mini` | `whisper-1` |
| `base_url` | `https://api.openai.com/v1` | `https://api.openai.com/v1` |
| `api_key_env` | `OPENAI_API_KEY` | `OPENAI_API_KEY` |
| `prompt` | (caption instruction) | — |

`api_key_env` is the **name** of an environment variable; the key itself never
lands in config or on disk.

---

## `graph` (knowledge-graph density)

| Key | Type | Default | Notes |
|---|---|---|---|
| `query_service_url` | string \| null | `null` | Internal graph-service base URL. When set, API/MCP serving replicas hold a bounded proxy instead of the full graph. Use `null` for standalone. |
| `query_service_token_env` | string | `PHEASANT_GRAPH_SERVICE_TOKEN` | Environment variable containing the bearer token shared by graph clients and the `graph` role. |
| `query_service_timeout_seconds` | float | `30.0` | Deadline per graph operation, including graph/hybrid search. Transport failures are explicit; they never trigger a local full-graph fallback. |
| `memory_entity_bridging` | bool | `true` | Wire agent-memory records into the graph (`about` edges to what a record refers to, `supersedes` between corrections). A no-op without a memory source. |
| `wasm_cross_source_resolution` | bool | `false` | Run `resolve_cross_source_edges` (import/link resolution across sources) through the vendored WASM accelerator (Synapse 34.5a) instead of pure Python. Needs the `[wasm]` extra; falls back to pure Python on any failure or if the extra is missing. Conditional win per the 34.4 benchmark — loses to Python below roughly 1,300-2,500 edges, wins modestly above it; opt in for large/growing multi-source graphs, leave off for small ones. **The Docker image turns this on** in a config it generates itself, on the assumption that a container's graph grows past the crossover; set it to `false` in your config if you are indexing a small, static corpus. |

---


---

### `graph.max_nodes` — when one region is no longer enough

| Key | Type | Default | Notes |
|---|---|---|---|
| `max_nodes` | integer \| null | `1500000` | Warn once per sync past this many graph nodes (~240,000 files — a full 6 Gi container). `null` disables. |

A **notice, not a refusal**: unlike `sync.limits`, which stops a source before
any work happens, by the time this can fire the index already exists and
discarding it would help nobody. The sync still returns `healthy`, with the
advice under `details.capacity`.

The default is measured rather than chosen. `python -m pheasant.graph.capacity`
reports ~2.4 KB of process RSS per node, flat across four scales; the graph is
roughly 60% of process RSS, so 1.5M nodes is a full 6 Gi container — the limit
the shipped manifests now set. See
[capacity planning](how-to/capacity-planning.md) for the full table and when to
shard into several regions.


## `security`

| Key | Type | Example | Notes |
|---|---|---|---|
| `allow_workspace_roots` | list[path] | `[/workspace, /exports]` | Allowed root prefixes for registered source paths. |
| `read_only_sources` | bool | `true` | Prevent source mutation operations. |
| `deny_path_traversal` | bool | `true` | Block `..` traversal and unsafe resolution. |
| `allow_user_selected_source_paths` | bool | `true` | Let a source name any readable path, not just one under `allow_workspace_roots`. This is what makes "point it at anything" work; see the security notes on what compensates for it. |
| `default_exclude_secrets` | bool | `true` | **Always** union `SECRET_EXCLUDES` into every filesystem source's excludes. Unlike the rest of `DEFAULT_EXCLUDES`, supplying your own `exclude` list does not drop these. |
| `acl_enforced` | bool | `false` | Master toggle for principal-aware retrieval (Step 32.x). `false` = every pre-32 deployment stays byte-identical. When `true`, `search_context` filters candidates against each artifact's captured ACL before merge/return. |
| `default_visibility` | string | `public` | How an un-ACL'd artifact (no connector-captured ACL, e.g. a plain filesystem source) is treated once `acl_enforced` is on: `public` keeps it searchable by anyone, `private` requires an authenticated principal. |
| `groups` | map[str, list[str]] | `{}` | Config-mapped `principal -> [group, ...]` identities, unioned with any IdP-synced groups at query time. |
| `idp.enabled` | bool | `false` | Turn on SCIM 2.0 group-directory sync (Step 32.4). Disabled by default — `groups` above still works with zero env vars. |
| `idp.provider` | string | `scim` | Directory protocol. |
| `idp.base_url` | string | `""` | SCIM `/Groups` listing endpoint base. |
| `idp.api_key_env` | string | `IDP_TOKEN` | Env var holding the bearer token; never stored in config. |
| `idp.sync_interval_minutes` | integer | `60` | How often the scheduler beat (or `POST /security/idp/sync`) refreshes the mapping. |
| `idp.staleness_max_minutes` | integer | `1440` | SLA: a mapping older than this **fails closed** (grants nothing) until the next successful sync. |
| `api_auth.token_env` | string | `PHEASANT_API_TOKEN` | Name of the env var holding a static shared bearer token; never the value. When it resolves, every route outside `public_paths` (and outside `/internal/*`, which enforces its own per-boundary tokens) needs `Authorization: Bearer <value>` or answers `401`. |
| `api_auth.behind_authenticating_proxy` | bool | `false` | "An ingress already authenticates callers." Satisfies the startup check below without a token of pheasant's own. |
| `api_auth.public_paths` | list[str] | `[/health, /ready, /metrics]` | Answerable without a token. The probes must stay open or an orchestrator cannot tell a healthy pod from an unauthorized one. |

**Every role but `all` refuses to start** on a bind address other machines can
reach with neither of the first two set. One container is exempt on purpose —
a laptop and every existing standalone deployment start with no configuration
at all — but a fleet pod binds `0.0.0.0` by necessity, so there the bind
address is not the control it is in Compose. A process serving no
knowledge-base API (`server.api.enabled: false`, as the preparation workers
use) needs neither. See
[the fleet trust model](security.md#a-fleet-is-the-other-case-and-it-is-enforced).

Two further startup refusals live in the same pass. A serving process refuses
when `graph.query_service_token_env` and
`sync.concurrency.remote_worker_token_env` name one variable or resolve to one
value — two trust boundaries, and workers hold the second by necessity. And a
`worker` refuses to hold a database DSN, a model provider key, the IdP token,
the graph token, a source list, or a non-SQLite state backend, none of which it
can use.

---

## `synapse` (federation into a Synapse fleet, optional)

Standalone-safe by construction: every router-facing behavior no-ops with
the defaults below, so a region that never sets `router_url` behaves
exactly like a router-less pheasant. Read
[Attach to a Synapse fleet](how-to/attach-to-synapse.md) before enabling.

| Key | Type | Default | Notes |
|---|---|---|---|
| `publish` | bool | `false` | Gate contract publication + the NDJSON sync-event stream. |
| `router_url` | string \| null | `null` | Synapse router base URL, e.g. `http://synapse-router:8000`. When set, each successful sync POSTs `sync.completed` (with the inline contract) to `<router_url>/v1/synapse/events` — failures are logged, never raised. |
| `fleet_id` | string \| null | `null` | Fleet label stamped into the published contract. |
| `endpoint` | string \| null | `null` | This region's reachable base URL, e.g. `http://my-region:8765` (the router pulls `GET /contract` from here). |
| `webhook_timeout_seconds` | float | `5.0` | Timeout for the router-webhook POST. |
| `signing_key_ref` | string \| null | `null` | Secret *reference* — `env://NAME` or a bare env-var name — resolving to a base64 32-byte Ed25519 seed (Step 24.4). Unset (default): `integrity.signature` stays `null` and nothing changes. The plaintext key never lands in config or on disk. |

---

## `memory` (agent memory, optional)

Governs the built-in `memory` source type: agents write records via MCP
`memory_write` / `POST /memory`, which land as append-only frontmatter
Markdown files indexed by the ordinary pipeline — recall is just search.
This block controls **consolidation** (archiving), how memory takes part in
**retrieval**, and how the store is **bounded**; it does not register the
memory source itself. See [Agent memory](how-to/agent-memory.md).

| Key | Type | Default | Notes |
|---|---|---|---|
| `consolidation_enabled` | bool | `true` | Archive superseded records (an explicit correction) and per-scope TTL-expired records on the scheduler beat or via `memory_consolidate` / `POST /memory/consolidate`. Archiving renames `<id>.md` → `<id>.md.archived` in place — bytes preserved, never deleted — then the archived records' indexed state is dropped directly (or, above a few hundred in one pass, a full re-sync prunes them). |
| `session_ttl_days` | integer \| null | `null` | TTL for `session`-scoped records. `null` = never expires by age. |
| `user_ttl_days` | integer \| null | `null` | TTL for `user`-scoped records. |
| `org_ttl_days` | integer \| null | `null` | TTL for `org`-scoped records. |
| `supersede_retention_days` | integer | `0` | Days a superseded/TTL-expired record stays a live, indexed file — hidden from default results by the existing `valid_until` predicate, reachable via `as_of` / `current_only=False` — before consolidation archives it. `0` = archive on the very next pass (pre-Phase-2 behavior). A deliberate trade-off, not free: retaining near-duplicate corrected text alongside its correction measurably costs ranking in hybrid RRF fusion (see `docs/memory-system.md` §4). |
| `default_policy` | `auto` \| `off` \| `only` \| `prefer` | `auto` | How memory takes part in a search that does not say. A per-call `memory` argument (MCP `search_context`, `POST /search`, `POST /assistant/chat`) always wins. `auto` = like any other source; `only` = memory and nothing else; `prefer` = memory is guaranteed a share of the result slots. |
| `steering_enabled` | bool | `false` | Let `alias` / `preference` / `exclusion` records re-rank results rather than merely be retrievable. Off by default: a memory that silently re-orders searches is a surprise unless it was asked for. See [Agent memory](how-to/agent-memory.md#steering). |
| `usage_tracking` | bool | `false` | Count which records retrieval actually returns, so salience reflects use. Off by default — it is a write on the read path, and recording what someone looks up is an operator's decision. |
| `max_records` | integer \| null | `null` | Archive the least salient records once the store exceeds this many. `null` = unbounded. Pruning uses the same in-place `.md.archived` rename as consolidation; nothing is deleted. Runs as a **backstop** after the per-scope/per-subject caps below. |
| `session_max_records` | integer \| null | `null` | Cap on `session`-scoped records alone, mirroring `session_ttl_days`. Isolates the pool: `max_records` alone ranks the whole store together, so a session flood only *outranks* an org fact by `scope_weight`, never fully protects it. |
| `user_max_records` | integer \| null | `null` | Cap on `user`-scoped records alone. |
| `org_max_records` | integer \| null | `null` | Cap on `org`-scoped records alone. |
| `max_records_per_subject` | integer \| null | `null` | Cap on live records sharing one `subject`, across scopes. Records with no `subject` are exempt. |
| `about_max_targets` | integer | `3` | Cap on `about` edges the graph bridge draws per record. Total `about` edges stay bounded by this times the record count — the ceiling the retired concept layer never had. |
| `reinforcement_enabled` | bool | `true` | Before creating a file, check a write's normalized text against the same `(scope, subject, kind, ACL partition)` bucket; a match — exact repeat or paraphrase — reinforces the existing record (`observations`, `last_seen`, a bounded `variants` list) instead of creating a new one. On by default: unlike `usage_tracking`, this records what was *written*, not what was *looked up*. See [Agent memory](how-to/agent-memory.md) and `docs/memory-system.md` §8. |
| `compaction_enabled` | bool | `false` | Offline near-duplicate clustering and medoid promotion, on the consolidation pass. Demoted records go to `tier='cold'` (never deleted or renamed — reachable via an explicit tier filter or `current_only=False`/`as_of`) and their `observations` are absorbed by the surviving canonical record. Off by default: unlike reinforcement, this changes what a *default* query returns. See `docs/memory-system.md` §8. |
| `compaction_similarity_threshold` | float | `0.6` | Exact-Jaccard threshold over normalized content tokens above which two records in the same bucket link into one cluster. |
| `compaction_min_cluster_size` | integer | `2` | A cluster below this size is left alone. |
| `synthesis.enabled` | bool | `false` | LLM-merge (Phase 4) for what deterministic clustering cannot resolve — complementary partial facts, progressive refinement, abstraction across records. Opt-in *and* never automatic: only the explicit `memory_synthesize` tool / `POST /memory/synthesize` runs it, never the scheduler beat. Mirrors `assistant.*` field for field. |
| `synthesis.provider` | `auto` \| `anthropic` \| `openai` \| `gemini` \| `none` | `auto` | Same semantics as `assistant.provider`. |
| `synthesis.model` | string \| null | `null` | Explicit model id; `null` uses the provider's default. |
| `synthesis.max_calls_per_pass` | integer | `20` | Hard cap on model calls in one pass. A repeat pass over already-subsumed clusters costs zero regardless — this bounds a large first pass. |
| `synthesis.max_input_chars` | integer | `6000` | A cluster whose combined member text exceeds this is skipped rather than truncated into a partial merge. |
| `synthesis.min_cluster_size` | integer | `3` | Below this, medoid promotion (L2) already resolves the cluster losslessly. |
| `synthesis.max_jaccard` | float | `0.55` | Above this mean pairwise similarity (over normalized tokens, measured exactly as L1 clustering measures it) a cluster is skipped: near-identical rephrasings are what medoid promotion resolves for free, so spend goes only where deterministic compaction provably cannot help. Matters most with `compaction_enabled` off (the default), where nothing has been demoted. |
| `formation.enabled` | bool | `false` | Read the observation plane (`observability.interactions`) and mint memory **candidates** from it, deterministically. Off, nothing is read and no candidate is ever minted. A candidate is a proposal, not a record: it becomes one only through an admission, and admission goes through the ordinary `MemoryStore.append` write path. See [Memory formation](memory-formation.md). |
| `formation.session_digest` | bool | `true` | Maintain one record per session, refined through dialog — scope `session`, subject the session id, each refinement naming the previous in `supersedes`. `current_only` then returns exactly one record per session and `as_of` reads its history. No new primitive: this is the validity model doing what it was built for. Only meaningful when `formation.enabled`. |
| `formation.auto_admit` | bool | `false` | Admit a candidate above threshold without review. Off by default, the same posture `compaction_enabled` takes: it changes what a *default* query returns. Left off, candidates accumulate for review in the Memory tab. An auto-admitted record carries the `formed` tag and its candidate records `admitted_by`, so a machine-formed record is always distinguishable from a written one. |
| `formation.min_observations` | integer | `3` | How many times a pattern must be observed before it is a candidate. Counts run over a stream that is *sampled under load* — the log tier drops rather than blocks — so a busy region reaches these thresholds later, not incorrectly. |
| `formation.min_sessions` | integer | `2` | …and across how many distinct sessions. One session repeating itself is a habit; several agreeing is a signal. Guards against a single loop minting steering that reorders results for everyone. |
| `formation.max_candidates_per_pass` | integer | `50` | Hard cap on candidates minted in one pass, bounding a first pass over a large ledger. |
| `formation.candidate_ttl_days` | integer | `30` | A pending candidate nobody promoted expires after this many days. Rejections are remembered separately and never re-proposed. |
| `formation.rules` | list | all four | Which rules run: `session-digest-v1`, `alias-cooccurrence-v1`, `path-affinity-v1`, `retrieval-gap-v1`. Each is versioned so its decisions stay attributable — a new version is a new `rule_id`, never an edit to an existing one. |

**Corrected records are excluded from retrieval automatically**, at query time,
without waiting for a consolidation pass — pass `{"current_only": false}` or an
`as_of` instant to see them. That is a property of the retrieval path, not a
setting here.

---

## `observability` (tracing + the observation plane, optional)

Two independent things share this block because they share a source: one span
per API/MCP call feeds both an operator's OTLP collector and the region's own
**interaction ledger**. Neither requires the other, and both are off by
default.

The ledger is what [memory formation](memory-formation.md) reads. It records
queries, principals, sessions and result ids as **rows with a retention
policy** — never files, never chunked, never indexed, never returned by a
search. A UI session's chat does not become knowledge because it was observed;
only an admission crosses that line.

Exporting spans needs the `[otel]` extra (`pip install "pheasant[otel]"`).
Without it, spans still feed the ledger and the exporter is simply never
attached — `pytest` stays network-free by construction rather than by mocking.

| Key | Type | Default | Notes |
|---|---|---|---|
| `otlp_endpoint` | string \| null | `null` | OTLP collector endpoint. `null` attaches no exporter at all: spans are still created and still feed the ledger, they just go nowhere off-box. |
| `otlp_protocol` | string | `http/protobuf` | OTLP wire protocol. |
| `otlp_headers_env` | string | `PHEASANT_OTLP_HEADERS` | Environment variable holding `key=value,key=value` exporter headers. The **name**, never the value — same rule as `storage.dsn_env`. |
| `service_name` | string | `pheasant` | `service.name` on every exported span. |
| `sample_ratio` | float | `1.0` | Head sampling for *exported* spans only. It deliberately does not thin the ledger: sampling out a span your collector does not need should not also cost the region a data point formation counts on. |
| `interactions.enabled` | bool | `false` | Record interactions. Off, the region behaves exactly as it did before this existed. On, it is recording queries and principals — a deliberate choice, not a default. |
| `interactions.redact_text` | bool | `false` | Record no free text at all — neither the question nor the answer. Named for what it does rather than one of the two fields it covers: redacting a question while keeping an answer that quotes the corpus back at it would be incoherent. Identity, modality, criteria, result ids and result paths are still recorded, so `path-affinity-v1` and `retrieval-gap-v1` still work; only the lexical rule `alias-cooccurrence-v1` goes quiet. |
| `interactions.max_answer_chars` | integer | `4000` | Cap on a recorded assistant answer, in characters. **`0` records no answers at all** — the same "0 means off" shape `hot_retention_days` and `supersede_retention_days` use. A cap rather than an unbounded field because an answer is model output at 10-50x a question's bytes; left unbounded, chat traffic would dominate a ledger sized for search. A truncated answer is marked `answer_truncated` in `attributes`. |
| `interactions.buffer_size` | integer | `10000` | Events held in memory before a flush. **A backpressure knob, not a throughput one**: the buffer is bounded and overflow drops the oldest event rather than blocking a request. |
| `interactions.flush_interval_seconds` | float | `5.0` | Flush at least this often, even below `flush_batch_size`. |
| `interactions.flush_batch_size` | integer | `500` | Events per published batch. Batching is what keeps the ledger off the request path: one publish per N events rather than one write per request. |
| `interactions.max_queue_depth` | integer | `50000` | Stop publishing (and start dropping, counted separately) when the log queue is this deep. Without it, a stalled log tier turns into unbounded queue growth. |
| `interactions.hot_retention_days` | integer | `7` | How long events stay queryable in `/state`. **`0` is cold-only mode**: batches go straight to Parquet and `/state` never grows. Formation then reads cold on its own pass — slower, batch-only, which is fine because formation is a beat, not a request. |
| `interactions.cold_enabled` | bool | `false` | Roll hot rows past their retention into Parquet under `<exports_path>/interactions/dt=YYYY-MM-DD/` before deleting them. Off, they are simply deleted. |
| `interactions.cold_retention_days` | integer \| null | `null` | `null` keeps cold partitions forever. Set it and whole `dt=` directories are dropped once past it — never individual rows. |
| `interactions.max_rows_per_pass` | integer | `50000` | Upper bound on rows one roll pass moves. Load-bearing in a single container, where the roll runs on the scheduler beat **under `sync_lock`**: an unbounded roll there stalls incremental sync for every source. |
| `interactions.spool_path` | path \| null | `null` | Where a replica spools batches when `/state` is read-only and no queue is configured — the degraded path for a custom SQLite multi-process deployment. `null` means such a replica drops instead. The shipped fleet needs none of this: it runs PostgreSQL, so every replica writes directly. |
| `interactions.queue.enabled` | bool | `false` | Hand log work to a dedicated tier. Off, whoever produced a batch writes it. On, batches are published to **their own queue** — not `index_tasks` — and a `serve --role logger` drains them, so persistence, rolling and compaction never touch a request or the indexer's sync lock. |
| `interactions.queue.backend` | `local` \| `nats` | `local` | Same two backends the index queue has. `nats` takes its own stream, subject and durable so the two tiers cannot consume each other's work. |
| `interactions.queue.visibility_seconds` | integer | `120` | How long a claimed batch stays invisible before another worker may take it. |
| `interactions.queue.max_attempts` | integer | `2` | Lower than the index queue's `3` on purpose: a log batch is best-effort by construction, and retrying a poisoned one three times costs more than the data is worth. |
| `interactions.queue.nats_stream` | string | `PHEASANT_LOGS` | JetStream stream name. |
| `interactions.queue.nats_subject` | string | `pheasant.logs.batches` | JetStream subject. |
| `interactions.queue.nats_durable` | string | `pheasant-loggers` | Durable consumer name. |

### What one interaction row holds

| Field | Notes |
|---|---|
| `id`, `trace_id`, `span_id`, `parent_span_id` | Correlation, and `NOT NULL` apart from the parent (a root span genuinely has none). `id` is `blake2b(trace_id\|span_id)`, so a redelivered batch dedups instead of double-counting. An inbound W3C `traceparent` is adopted, so an agent's trace continues into this one. A streamed answer is a **child** row: it outlives the request that opened it, so it gets its own span under the same trace rather than a racing late edit of the request's. |
| `modality`, `operation`, `principal`, `session_id`, `client_id` | The four dimensions formation slices on. All caller-asserted, exactly like `principal` already is everywhere else in pheasant. |
| `started_at`, `duration_ms`, `status` | `started_at` and `status` are `NOT NULL`. `duration_ms` is always set, including on a call that raised, and comes from a monotonic clock so an NTP step cannot produce a negative one. `status` is `ok` \| `error` \| `shed`; a 429 under saturation is recorded, because it is the signal that says thresholds are being reached more slowly than traffic suggests. |
| `query_text` | The question, from MCP tool arguments or an HTTP request body. |
| `answer_text` | The assistant's answer, when there was one. Capped by `max_answer_chars`. |
| `criteria_json` | The filter object the caller passed — mode, source filters, `min_score`, section. |
| `result_ids_json` | Stable node ids, which join to `graph_nodes` and through them to `chunks`. |
| `result_paths_json` | Source-relative `relative_path` values — the same grammar [steering](how-to/agent-memory.md#steering) matches against, so a `preference` rule minted from these can actually fire. |
| `result_count`, `top_score` | The full result count (the id/path lists are capped at 50) and the best score. Together they are what lets a rule tell "nothing matched" from "matched more than we recorded". |
| `attributes_json` | Free-form: HTTP status and method, and the `answer_truncated` / `text_redacted` markers. |

Two lists rather than one, deliberately: a rule that had to sniff whether a
value was an id or a path would behave differently depending on which surface
produced the row, which is the opposite of the determinism every formation rule
downstream is built on.

**A log tier falling behind degrades to data loss, never to request latency.**
The buffer is bounded, the queue depth is bounded, and nothing on the request
path ever blocks on either — the same posture `server.api.max_concurrency`
takes when it answers `429` under saturation. `pheasant_interaction_events_dropped_total{reason}`
counts what was lost and why.

**Cold storage enforces nothing.** A Parquet directory has no access control,
and these rows carry principals and query text. Put the access control on the
directory, exactly as [the export schema](reference/export-schema.md) says of
exports.

---

## `evaluation` (knowledge-effectiveness measurement, optional)

Off by default, read-only when on, and it publishes no single "accuracy" score.
A run replays cohorts of recorded queries through the **real** search path
against a corpus-only baseline and the memory system, then reports what changed
with its denominator, its evidence and its limitation attached.

Full prose: [Knowledge-effectiveness evaluation](knowledge-effectiveness.md).

```yaml
evaluation:
  enabled: false
  on_material_snapshot: false
  minimum_interval_seconds: 3600
  max_results: 10
  mode: hybrid
  maximum_queries_per_run: 500
  maximum_runtime_seconds: 900
  retrieval_diagnostics: false
  composite_weights: {}
  proof:
    unknown_is_negative: false
    non_selection_is_negative: false
    temporal_decay_enabled: false
    positive_floor: 0.2
    minimum_evidenced_queries: 5
  cohorts:
    anchor: true
    anchor_minimum_queries: 20
    rolling_lookback_days: 30
    holdout_minimum_separation_days: 0.0
  variants:
    memory_content: true
    alias_only: true
    preference_only: true
    exclusion_only: true
    full_memory: true
    candidate_shadow: true
  gates:
    acl_leak_maximum: 0
    stale_current_leak_maximum: 0
    control_regression_tolerance: 0.0
    incomplete_snapshot_blocks_run: true
  promotion:
    enabled: false
    minimum_independent_queries: 3
    allow_originating_query_only_promotion: false
```

### `evaluation.enabled`

Master switch. Off, nothing here runs and the region behaves exactly as it did
before this existed. On, `pheasant eval run` and `POST /evaluation/run` produce
reports; the `evaluation_*` tables in `/state` hold them and are **never
indexed, chunked or returned by a search** — a region must not retrieve its own
measurements as knowledge.

Needs `observability.interactions.enabled` to have recorded queries worth
replaying. Without it the cohorts are empty and every demonstrated metric
reports `insufficient_evidence` rather than zero.

### `evaluation.on_material_snapshot`

Fire a run on the scheduler beat when the snapshot manifest shows a material
change (content, graph, retrieval configuration, memory or ACL policy digests
moving — not counts drifting). Off by default: a run costs one search per query
per variant, which is real work to start doing on a timer without being asked.

Fleet behaviour: the automatic trigger fires only where the scheduler runs
(`--role all`, `--role indexer`), a run takes the evaluation lease so several
replicas produce one run rather than N, and it **never runs inside
`sync_lock`** — a replay there would stall incremental sync for every source.

### `evaluation.run_stale_seconds`

How long a running batch may go without stamping its heartbeat before another
process may declare it dead and mark it `interrupted`.

A knob rather than a constant because the right value depends on how long a
single (cohort, variant) replay takes here, and that is a property of the
corpus. Too high and a stopped container shows a spinner for minutes longer
than it needs to. The default is 90s — six heartbeats.

Lowering it is safe: the heartbeat interval is **derived from this value**, so
narrowing the window speeds the beat up with it and a batch always gets at
least three beats to say it is alive. It did not always work that way — the
beat was fixed at 15s, so any window at or below that inverted the meaning of
"the heartbeat expired" and a slow-but-healthy batch was reclaimed out from
under itself, freeing the `__evaluation__` lease under a run that never
stopped. Windows below 3s are raised to 3s, which is the narrowest one a beat
can still satisfy.

Reclamation runs at API startup and on the scheduler beat, so `--role api`
replicas (which never run the beat) still close out a batch whose container
stopped. See
[Watching a batch](knowledge-effectiveness.md#watching-a-batch-and-what-happens-when-the-container-stops).

### `evaluation.proof.*`

How observed events become weighted evidence. The three defaults that matter:

* `unknown_is_negative: false` — an artifact served and neither selected nor
  rejected stays *unjudged*. Turned on, every metric changes meaning:
  precision improves whenever the region returns **fewer** results.
* `non_selection_is_negative: false` — the reader may have found the answer at
  rank one and stopped.
* `temporal_decay_enabled: false` — an operator who has not chosen a half-life
  should not discover that a year-old conclusive test result stopped counting.

`positive_floor` is the net weight below which a target is neither a known
positive nor a known negative. Deliberately not zero: one weak citation is not
a "known positive", and `known_positive_recall` counting one would over-claim
in its own name.

`minimum_*` are sufficiency conditions. A metric computed over less is
published with status `insufficient_evidence` and **`value: null`** — never
`0.0`, which would put a red bar on a dashboard describing an instrumentation
gap.

### `evaluation.cohorts.*`

Six cohorts, each with a different purpose:

| Cohort | What it is | Why it is separate |
|---|---|---|
| `anchor` | Frozen once, replayed at every snapshot | Longitudinal comparability |
| `rolling` | Recent traffic in a lookback window | Notices new questions; moves for two reasons at once |
| `learned` | Queries whose interactions created the memory | **Recall of learned experience — never reported as generalization** |
| `temporal_holdout` | Later queries that contributed no evidence | Forward generalization |
| `control` | Queries no steering rule can fire on | Finds unintended re-ranking. Paired **B1 vs B5** so content is held constant and steering varied — the cohort controls for steering, so only a steering-only pairing measures it |
| `invariants` | Deterministic ACL / validity / abstention cases | Gates, not scores |

`holdout_minimum_separation_days` defaults to `0.0` because "how long must a
holdout remain independent" is an open policy decision; a hidden non-zero
default would be answering it on your behalf.

### `evaluation.variants.*`

The ablation matrix. `B0` (corpus baseline) is not removable: every attribution
number is a paired difference against it, and a treatment score published
without one gets read as accuracy.

### `evaluation.gates.*`

Hard invariants, evaluated **before** any aggregation so a failure cannot be
averaged away. ACL leakage, stale-current leakage, temporal `as_of` correctness,
abstention and known-positive exclusion default to zero tolerance.

### `evaluation.promotion.*`

Off by default. With it off the same candidate decisions are computed and
recorded and **nothing is applied** — which is what to run first: read a month
of decisions before letting any of them take effect.

`allow_originating_query_only_promotion: false` is the anti-self-reward rule. A
candidate that improves only the query that created it has demonstrated recall
of its own evidence and nothing else.

---

## `tuning` (retrieval performance tuning, optional)

Off by default, and read-only when on unless you turn `auto.apply` on or apply
a bundle yourself.

The evaluation plane tells you *how well* retrieval is doing. This one tells
you **which step is failing** and, where a parameter can fix it, proposes the
parameter. Retrieval is a pipeline — query analysis, three candidate arms, three
filters, a fusion, a truncation — and after the merge every one of those
failures looks identical, because they all produce an absent result. Six causes,
one symptom.

A batch works in four movements:

1. **Diagnose.** Replay a cohort with per-stage capture and attribute every miss
   to the *first* stage that lost the document. The output is a histogram over
   stages, not a score. If it says 71% of your misses are documents that were
   never indexed, you have got the most valuable thing this plane produces and
   should stop — no ranking parameter will move that, and the batch says so
   rather than searching a space that cannot contain the answer.
2. **Propose.** Only parameters whose stage the diagnosis actually blames.
3. **Trial.** Most trials cost **no retrieval at all**: the fusion family
   (`rrf_k`, the arm weights) acts after the arms have produced candidates, so
   it is re-computed from cached candidate lists. Only parameters that change
   what the arms retrieve need a real search, and those are budgeted separately.
4. **Decide.** Gate the winner against a held-out cohort it was never selected
   on and a control cohort that must not regress, then package it as a bundle.

### What a bundle is, and why applying it is a separate step

A bundle is a `search.ranking` parameter set plus its whole provenance: the
snapshot it was measured against, the decision that produced it, the
comparisons and gates behind that decision, and the parameters it replaces.
Producing one changes nothing — it is a file describing a configuration.
**Applying** one changes what every replica in the fleet serves, which is why
it is `pheasant tune apply` (or `auto.apply: true`) rather than a side effect
of the batch.

Applying is **fleet-scoped by construction**. The active bundle is one row in
`/state`, every replica resolves it on a short TTL, and there is nowhere for a
per-request or per-principal override to live. Retrieval parameters that varied
by caller would make two agents disagree about what the region contains.

`pheasant tune rollback` stands the overlay down and returns the region to its
configured parameters. What the bundle replaced is stored on the bundle, so
reverting does not depend on anyone remembering what the config used to say.

### `tuning.enabled`

Default `false`. Turns the plane on. It needs `evaluation` to be producing
cohorts and proof, because it tunes against the same queries and the same
evidence the evaluation plane reports on — deliberately, so the two cannot
optimize for and measure different things.

### `tuning.auto.enabled` / `tuning.auto.apply`

Two switches, and the gap between them is the safety property. The first runs
batches automatically (only where the scheduler runs, so API replicas never
start one). The second lets a passing bundle change ranking unattended. Leave
`apply` off until you have read a few reports; the work still happens and the
bundle waits.

### `tuning.refusion_trials` / `tuning.requery_trials` / `tuning.max_searches`

The two trial budgets are separate because the two cost classes differ by
roughly three orders of magnitude, and a single "trials" number would be spent
entirely on whichever class the enumeration reached first. `max_searches` is
the backstop that keeps a large cohort from turning a modest trial budget into
the region's dominant workload.

### `tuning.max_index_queue_depth` / `tuning.yield_to_sync`

Backpressure. A running batch stands down while the index queue has work in it
or a sync holds a source lease, and it checks *between* units rather than once
at the start — a batch that began on an idle region and is still going when a
large re-index starts has to yield, not finish what it started. Standing down
is not a failure: trials are checkpointed, so the next attempt resumes.

The executor holds **one slot**, takes the `__tuning__` lease, and never takes
`sync_lock`.

### `tuning.pinned_parameters`

Names you have settled and do not want re-litigated. A pinned parameter is
never proposed.

### `observability.interactions.stage_sample_rate`

Default `0.0`. The fraction of searches that attach a **per-stage digest** to
their ledger row: arm counts, what each filter removed, the fused depth, and
the bundle the search ranked under.

The always-on Prometheus stage counters (`pheasant_retrieval_*`) do not depend
on this. What sampling buys is a *query you can look at* when one of those
counters moves, and a live diagnosis source for the tuning plane that does not
wait for a batch — which is what `GET /tuning/health` reads.

Sampled rather than universal because the digest is a few hundred bytes on a
row that is already a couple of kilobytes. Sampling is deterministic on the
trace id, so every hop of one call agrees and the sampled set can be joined; a
per-hop random draw produces traces that cannot be.

### `tuning.objective.*`

What "better" means for this region. `metric` is `reciprocal_rank` (default),
`recall_at_5`, `recall_at_10`, `hit_rate` or `balanced`; `weights` overrides it
with a custom combination over collected metrics, normalized to sum to one.

Not a detail. A region whose agents read one result wants `reciprocal_rank`;
one whose agents fetch a page and synthesize wants `recall_at_10`, and would be
actively harmed by a parameter set that sharpens rank one at the cost of
dropping a document out of the list. Each objective publishes what it **trades
away** as well as what it optimizes, and every report names the one that
produced it.

### `tuning.tracking.*`

`backend` is `off` (default), `state`, or `mlflow`. `/state` is always the
source of truth; MLflow is a **mirror** of it, so losing the mirror loses a
dashboard rather than a result — and you can turn tracking on later and still
have every row.

With no `tracking_uri`, the MLflow sink writes a **local file store** under
`<exports>/tuning/mlruns`: no server, no network, no credentials. Open it later
with `mlflow ui --backend-store-uri <that path>`. Needs the `[tuning]` extra; a
region without it logs a warning and the batch runs exactly as before.

### `search.ranking.*` — the parameters this tunes

The knobs themselves live under `search.ranking` (BM25 column weights, the
structural priors, the RRF constant, the per-arm fusion weights, the filter
over-fetch multiplier). Every default is the value the 2026-08-03 retrieval
overhaul measured, so a region that never opens the block ranks exactly as it
always has. You can set them by hand and pin them; the tuning plane is a way of
choosing them with evidence, not the only way to change them.

---

## `readiness` (stress-test readiness, optional)

Off by default and read-only when on — except for a check, which writes to a
scratch source it owns. This is the plane that answers whether an outside
harness may trust this region's answers: it publishes a machine-readable
capability contract and runs the go/no-go gates in
[Stress-test readiness](stress-test-readiness.md).

```yaml
readiness:
  enabled: true
  corpus_denylist:
    - "benchmark/*"
    - "*.answers.json"
  max_search_latency_ms: 5000.0
  max_ingest_ack_ms: 30000.0
  max_index_lag_ms: 120000.0
  latency_probe_queries: 12
  concurrency_probe_writers: 4
  concurrency_probe_items: 5
```

| Key | Default | What it does |
|---|---|---|
| `enabled` | `false` | Publish the contract and allow `pheasant readiness check` / `POST /readiness/check`. The contract endpoint is served either way — answering "can this region be measured" with a 404 is indistinguishable from an old build that has none. |
| `corpus_denylist` | `[]` | fnmatch patterns that may **never** enter the searchable corpus, tested against an item's relative path and its bare filename. A matching submission is refused with `CORPUS_DENYLISTED`. |
| `max_search_latency_ms` | `5000.0` | p95 search latency the readiness gate accepts. |
| `max_ingest_ack_ms` | `30000.0` | How long a submission may take to reach `accepted`. |
| `max_index_lag_ms` | `120000.0` | How long after acceptance an item may take to become searchable. |
| `latency_probe_queries` | `12` | Searches the latency probe issues. Below 5 it publishes nothing rather than a p95 over four searches. |
| `concurrency_probe_writers` | `4` | Concurrent writers the swarm probe drives. |
| `concurrency_probe_items` | `5` | Items each concurrent writer submits. |

**`corpus_denylist` is enforcement, not a report.** A check that evaluation
artifacts are absent can only run once they have been indexed, and by then they
have been retrievable — so the write is refused. An empty list costs one truth
test per submitted item and changes nothing else; it also means there is no
boundary to prove, so the contamination probe reports `skipped` and the core
gate set is **incomplete** rather than passing.

**The thresholds are deliberately generous.** A performance gate cannot pass or
fail against an unstated number, and one that is present and loose can be
tightened from evidence where an absent one turns every latency observation
into an argument.

A region also needs two things outside this section before the gates beyond
core can be evaluated: `security.acl_enforced: true`, without which isolation
cannot be demonstrated, and an enabled `type: memory` source, without which
memory has nothing to export or replay. Both report as `skipped` with the
sentence that says what to turn on.

---

## `assistant` (grounded chat)

Powers the UI's chat panel and `POST /assistant/chat`: retrieve from your own
index, cite the passages, surface graph facts, then ask a chat model to write
the answer from those passages alone.

**This is a query-time surface only.** No LLM ever runs during indexing, so
enabling it does not affect determinism — re-syncing unchanged content still
produces byte-identical state. With no provider reachable the assistant still
answers *extractively* (top passages + citations + facts), which is the
default and works fully offline.

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | `false` makes `/assistant/chat` return 403. |
| `provider` | str | `auto` | `auto` \| `anthropic` \| `openai` \| `gemini` \| `none`. `auto` picks the first provider whose key env var is set, in the order Anthropic → OpenAI → Gemini. |
| `model` | str \| null | `null` | Provider default when unset (`claude-sonnet-5`, `gpt-5.6-luna`, `gemini-2.5-flash`). |
| `base_url` | str \| null | `null` | Point at a gateway or self-hosted OpenAI-spec endpoint. |
| `api_key_env` | str \| null | `null` | Read the key from a differently-named variable. Defaults to the provider's own (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`). |
| `allow_session_keys` | bool | `true` | Let a UI user paste a key for their browser session. Held in server memory behind an opaque token — never written to config, `/state`, or logs; dropped on expiry, revoke, or restart. Set `false` to require the env var. |
| `session_key_ttl_minutes` | int | `720` | Lifetime of a session-supplied key. |
| `max_context_chunks` | int | `8` | Passages retrieved and offered to the model. |
| `max_output_tokens` | int | `4096` | Per-answer output cap sent to the provider. |
| `request_timeout_seconds` | float | `90.0` | Provider HTTP timeout. A timeout degrades to the extractive answer rather than erroring. |
| `max_facts` | int | `12` | Graph facts surfaced per answer, collected round-robin across the cited sources. |
| `workflow` | str | `auto` | Which agent workflow answers a question: `auto` \| `knowledge-summary` \| `agentic` \| `simple` \| any registered plugin name. `auto` = `agentic` when the `[agent]` extra is installed *and* a model is reachable, else `simple`. An unknown or failing workflow degrades to `simple` with the reason attached to the answer. |
| `workflow_options` | dict | `{}` | Per-workflow tuning, keyed by workflow name, merged over that workflow's defaults. Callers may override any key per request. |
| `retrieval` | block | see below | Typed retrieval criteria — the same knobs, with names, validation and a UI. |

### `assistant.retrieval` — how hard to look before answering

These knobs already existed as untyped keys inside `workflow_options`,
documented only in a workflow module's `DEFAULTS` dict. This block is their
typed home, which is what makes them validated, editable from the UI
(Settings → Retrieval tuning), and readable by an agent over MCP
(`describe_retrieval`).

| Key | Type | Default | Notes |
|---|---|---|---|
| `max_rounds` | int \| null | `2` | plan → retrieve → grade turns before answering with what is in hand. `1` disables the re-plan loop. |
| `per_query_results` | int \| null | `6` | Passages fetched per query per search mode. |
| `max_context_passages` | int \| null | `10` | Total passages offered to the answering step. |
| `retrieval_modes` | list \| null | `["text", "vector"]` | Modes to fan out over. `vector` is dropped automatically when no vector index is built, so leaving it on is safe. |
| `expand_graph` | bool \| null | `true` | Walk the graph out of the best hits, reaching documents that share no vocabulary with the question. |
| `expand_depth` | int \| null | `1` | Hops to walk when expanding. |
| `expand_per_node` | int \| null | `3` | Neighbours taken per expanded node. |
| `grade_evidence` | bool \| null | `true` | Ask the model to grade its own evidence before answering. |
| `verify_citations` | bool \| null | `true` | Drop `[n]` markers that do not resolve to a real citation. |
| `max_facts` | int \| null | `12` | Graph facts surfaced alongside the answer. |

`hybrid` is already a concurrent fusion of text, vector, and graph. The
generated scalable profile deliberately uses `[vector, graph, hybrid]` and
omits a second standalone `text` fanout: stress testing found PostgreSQL
full-text ranking to be the slowest arm for high-frequency terms, so repeating
it had a weak cost/recall case. This does not disable text search. Explicit
`mode=text` still serves exact-identifier queries and hybrid still contains
lexical results. Vector and graph remain explicit to preserve arm-specific top
candidates that may fall beyond hybrid's fused result limit. The schema
default above and the local profile defaults are unchanged.

**Precedence is deliberately low.** Values merge in this order, later winning:

```
workflow DEFAULTS  <  assistant.retrieval  <  assistant.workflow_options  <  per-request options
```

So a config that already tuned `workflow_options` is completely unaffected by
this block's arrival, and an agent overriding a criterion for one call still
wins over both. A field left `null` is **not merged at all** — the workflow's
own default applies — which is what keeps this additive rather than a second
source of truth for values it does not care about.

Editable live: `GET`/`PUT /assistant/retrieval`. Retrieval is query-time only,
so a change applies to the next question with no restart and no re-index.

```yaml
assistant:
  retrieval:
    max_rounds: 3
    max_context_passages: 16
    retrieval_modes: ["text", "vector", "graph"]
```

**The key never lands in config.** Both routes are indirections: an
environment variable *name* here, or a runtime token in the browser. Nothing
in this file is a secret.

The `agentic` workflow is a LangGraph state graph (classify → plan → retrieve →
expand → grade → synthesize → verify) and needs the optional extra:

```bash
pip install 'pheasant-kb[agent]'
```

```yaml
assistant:
  workflow: agentic
  workflow_options:
    intent: auto            # auto | knowledge | procedural
    max_rounds: 3
    retrieval_modes: [hybrid, vector, graph]
    expand_depth: 2
    passage_chars: 6000     # how much of each cited file the model sees
```

`classify` reads the question as a **knowledge summary** ("what does this
repository do") or a **procedural** one ("how do I use this tool") and shifts
retrieval, the sufficiency bar and the answering prompt to match — breadth
over depth for the first, depth and real code examples for the second.
`knowledge-summary` is the same graph with that reading pinned. See
[Customize the question-answering workflow](how-to/agent-workflows.md).

Both workflows send the model whole **files**, rebuilt from their chunks with
line spans and metadata, rather than the 500-character search preview. Code and
config are never excerpted; large prose is cut to the matched neighbourhood.

Third-party workflows register under the `pheasant.agent_workflows`
entry-point group, the same plugin shape as the
[Connector SDK](reference/connector-sdk.md).

See the how-to guides: [Ask your knowledge base](how-to/chat-and-ui.md) and
[Customize the answering workflow](how-to/agent-workflows.md).

---

## `sources` (per-source configuration)

Each source item supports:

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | none | Unique source id/name. |
| `type` | enum \| plugin name | `single_file` | One of `repository`, `markdown_folder`, `obsidian_vault`, `document_folder`, `web_collection`, `single_file`, `s3`, `api` — or any installed connector plugin (`notion`, `gdrive`, `slack`, `confluence`, `imap`, or your own). `GET /sources/types` lists what this deployment accepts. |
| `path` | absolute path | none | Filesystem path for source root (or file). |
| `description` | string/null | null | Human-readable context for operators. |
| `enabled` | bool | `true` | Disable without deleting config. |
| `include` | list[glob] | code/text defaults | Inclusion patterns. |
| `exclude` | list[glob] | secure defaults | Exclusion patterns. |
| `repo.*` | object | see below | Repository-specific behavior. |
| `chunking.*` | object | see below | Chunking strategy/size overlap. |
| `sync.*` | object | see below | Source-specific trigger policies. |
| `connector.*` | object | see below | Connector feature flags and provider-specific options. |
| `urls` | list[string] | `[]` | URL list (mainly for `web_collection`). |

### `sources[].repo`

| Key | Type | Default | Notes |
|---|---|---|---|
| `branch_policy` | string | `current` | Branch selection policy for repository context. |
| `include_uncommitted` | bool | `true` | Include working tree changes. |
| `commit_trigger` | bool | `true` | Trigger sync on commit change events. |
| `dependency_graph` | object | `{}` | Optional language-specific dependency graph config. |
| `clone_url` | string/null | `null` | Remote cloned by URL quick-add. When set, every sync fetches and safely fast-forwards before indexing. Use `GITHUB_TOKEN`/`GH_TOKEN` for private GitHub repositories; never put credentials in this URL. |
| `clone_path` | string/null | `null` | Root of the managed checkout. This differs from `sources[].path` when a GitHub tree URL selects a repository subdirectory. |
| `clone_ref` | string/null | `null` | Branch or tag requested by a GitHub tree URL. Otherwise the clone's tracked default branch is used. |

URL-added repositories are managed checkouts. Before `incremental`, `full`,
`repair`, or `validate_only`, Pheasant fetches `origin`, resolves the tracked
revision, and permits only a fast-forward. It then records the remote, local,
and indexed commit IDs in the source checkpoint. `GET /sources` and the
Sources page report `remote current` only when all three commits match.

Pheasant will fail with `remote_error` instead of indexing stale content when
the fetch cannot authenticate, the checkout has working-tree changes or local
commits, the history diverged, or `origin` does not match `clone_url`. Local
repository sources (`clone_url: null`) are never fetched or modified.

Sources created with URL quick-add live immediately in operational state. Use
the Sources page's **promote** action to add the generated source block to
`pheasant.yaml` when scheduler/startup synchronization must survive a complete
server restart.

### `sources[].chunking`

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | When false, each artifact is indexed as one full-content chunk. |
| `strategy` | string | `semantic` | Chunking algorithm (semantic/heading/page-oriented, etc.). |
| `max_chars` | integer | `4000` | Maximum chunk size. |
| `overlap_chars` | integer | `400` | Overlap between adjacent chunks. |

### `sources[].sync`

| Key | Type | Default | Notes |
|---|---|---|---|
| `on_startup` | bool | `true` | Process source at service start. |
| `on_file_change` | bool/string | `debounce` | File-change trigger behavior. |
| `on_git_commit` | bool | `true` | React to git commits for this source. |
| `interval_seconds` | int/null | `null` | Source-specific scheduled sync interval. |

### `sources[].taxonomy`

Structural taxonomy extraction for **books, procedures and legal documents** —
the outline the document already declares (Part / Chapter / Article / Section /
`§ 12.3` / `1.2.3` / `(a)`), turned into retrieval structure.

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. Set it when registering the source. |
| `max_depth` | int | `6` | Deepest heading level to keep (clamped 1-6). |
| `detect` | list | `[]` (all) | Narrow the rule set: `markdown`, `keyword`, `code`, `numbered`, `lettered`, `caps`. |
| `graph_nodes` | bool | `true` | Emit `heading` nodes + `has_heading` edges. |
| `split_on_sections` | bool | `true` | Cut chunks at section boundaries so one chunk is one section. |

With it on, three things happen on every sync:

1. **Chunks are cut and labelled per section.** Each chunk carries its
   breadcrumb in `chunks.heading_path`, which `chunks_fts` indexes at BM25
   weight 2.0 — double the body text. A search hit then reports *which
   section* matched (`heading_path` on the result), not just which file.
2. **`heading` graph nodes and `has_heading` edges are emitted**, with a
   section `contains` its subsections — so the taxonomy is a traversable tree
   using the same `contains` edge the directory hierarchy uses.
3. **`GET /taxonomy`** renders the outline per document, with any numbering
   defects it found (gaps, duplicates, backwards numbering).

Retrieval can then be **restricted to one section**: `section` on
`POST /search` and MCP `search_context(section=...)` matches the breadcrumb, so
`§ 12.3`, `Article IV` or a section's wording all reach it, and naming a parent
returns everything nested under it. Graph hits are excluded under a section
filter — a symbol is not inside a document section.

```yaml
sources:
  - name: contracts
    type: document_folder
    path: /workspace/contracts
    include: ["**/*.pdf", "**/*.docx"]
    taxonomy:
      enabled: true
```

**Why it is off by default, and per source rather than global.** The numbering
rules are genuinely ambiguous on prose: `1. Introduction` in a standards
document is a section, `1. Buy milk` in a note is a list item, and nothing in
the line distinguishes them. Length and punctuation filters reduce the
confusion but cannot remove it. Enabling it per source is how you say "this
corpus really is structured documentation". It also changes what the FTS index
holds for that source, so it wants a deliberate `--mode full` re-sync.

**Ordinal reconciliation.** A heading's own number decides its parent wherever
it can, so mixed numbering works: `4.2` attaches to whichever heading *is* `4`
— including a roman `ARTICLE IV`, since `IV` parses to `(4,)` — while `§ 12.3`
refuses an ancestor whose ordinal is not a prefix of its own and climbs past the
Article to the unnumbered title above. `§ 12A` is treated as a *sibling* of
`§ 12`, because inserting a section is not nesting one. Lettered items (`(a)`,
`(iv)`) are positions among siblings and are placed by nesting only.

Each `heading` node stores its parsed ordinal (`ordinal_parts`,
`ordinal_series`, `ordinal_suffix`), so a section is queryable by citation.

**Sequence reconciliation.** `GET /taxonomy` also reports numbering defects per
document in an `issues` list — `gap` (with the `missing` numbers), `duplicate`
and `out_of_order`. For a contract or a procedure, "is anything missing?" is the
question people actually ask, and once ordinals are parsed it is nearly free to
answer. Only gaps *between observed siblings* are reported: a series starting at
3 is an excerpt, not a defect.

**Residual ambiguity.** Seven letters are also roman numerals. A lone
`(c)`/`(d)`/`(l)`/`(m)` is read as the letter and a lone `(i)`/`(v)`/`(x)` as
the numeral, which gets both conventions right in sequence but misreads a letter
list that runs as far as `(i)`. Bounded on purpose: lettered ordinals never
decide hierarchy, so the worst case is one spurious `issues` entry.

### `sources[].connector`

Experimental non-filesystem connectors are disabled until explicitly enabled per source.

| Key | Type | Default | Notes |
|---|---|---|---|
| `allow_experimental` | bool | `false` | Required for `web_collection`, `api`, and `s3` connector execution. |
| `request_timeout_seconds` | integer | `10` | HTTP/API request timeout. |
| `headers` | map[string,string] | `{}` | Optional HTTP headers for web/API requests. |
| `api_endpoint` | string/null | `null` | JSON item listing endpoint for `api` sources. |
| `api_items_field` | string | `items` | JSON field containing API item records. |
| `api_content_field` | string | `content` | JSON field containing inline item content. |
| `s3_bucket` | string/null | `null` | Bucket name for `s3` sources. |
| `s3_prefix` | string | empty | Object prefix for `s3` sources. |

Example `web_collection` source:

```yaml
sources:
  - name: public-docs
    type: web_collection
    path: /workspace
    urls:
      - https://example.com/docs/overview.md
    connector:
      allow_experimental: true
    include:
      - "**/*.md"
```

---


## Release/version alignment (important for merges)

When a PR changes deployable server behavior and is merged, the release/version check expects a **new image version**. In practice:

- `pyproject.toml` version and deployment image tags must remain aligned for a release.
- `deployment.compose.image_tag` in `pheasant.example.yaml` is one of the generated references that should be incremented to the new server version during release prep.
- Use `python scripts/sync_version.py --check` in CI/local validation to confirm all generated version references are synchronized.

If this check fails on merge/release automation, bump the project version and re-run the sync script so config and deployment manifests match.

---

## Deployment modality examples

### 1) Local developer workstation (Docker Compose)

Use this for single-machine local development with mounted host directories.

```yaml
deployment:
  compose:
    image_repository: ghcr.io/esatt10/pheasant
    image_tag: 0.1.3
    workspace_path: ./workspace

pheasant:
  environment: local
  log_level: INFO
  state_path: /state
  workspace_root: /workspace

server:
  host: 0.0.0.0
  port: 8765
  mcp:
    enabled: true
    transports:
      stdio: true
      streamable_http: true
      sse: false
```

### 2) Team shared VM / self-hosted service

Use this when multiple clients connect over network and you want stricter controls.

```yaml
pheasant:
  environment: prod
  log_level: INFO

server:
  host: 0.0.0.0
  port: 8765
  mcp:
    enabled: true
    transports:
      stdio: false
      streamable_http: true
      sse: true
  api:
    enabled: true
    openapi: false

security:
  allow_workspace_roots:
    - /workspace
    - /exports
  allow_user_selected_source_paths: true
  read_only_sources: true
  deny_path_traversal: true
  default_exclude_secrets: true
```

### 3) Indexing an existing Obsidian vault

pheasant reads an Obsidian vault as an ordinary Markdown source: wikilinks
resolve as references, and `.obsidian/` metadata is excluded. (pheasant no
longer *writes* a vault — the graph workspace at `/graph` in the UI replaced
that projection.)

```yaml
sources:
  - name: existing-obsidian-vault
    type: obsidian_vault
    path: /workspace/obsidian-vault
    enabled: true
    include: ["**/*.md", "**/*.canvas"]
    exclude: ["**/.obsidian/**", "**/.trash/**"]
```

### 4) Repository indexing at scale (many files)

Use this when you ingest larger repositories and want predictable performance.

```yaml
sync:
  watcher:
    enabled: true
    max_watch_paths: 300
    debounce_ms: 2500
    batch_window_ms: 10000
  scheduler:
    enabled: true
    interval_seconds: 900
  concurrency:
    max_parallel_sources: 6
    max_parallel_files: 12
    max_parallel_embeddings: 4
    file_executor: process
    lock_timeout_seconds: 180

sources:
  - name: primary-repository
    type: repository
    path: /workspace/repository
    include: ["**/*.py", "**/*.md", "**/*.yaml", "**/*.json"]
    exclude:
      - "**/.git/**"
      - "**/.venv/**"
      - "**/node_modules/**"
      - "**/dist/**"
      - "**/build/**"
    repo:
      branch_policy: current
      include_uncommitted: true
      commit_trigger: true
```

---

## Practical tuning checklist

- Start with the example file defaults.
- Use `pheasant init --profile <name>` to generate a focused starter config.
- Use `pheasant doctor --profile <name> --config pheasant.yaml` before long-running syncs.
- Confirm every `sources[].path` is under an allowed security root.
- Trim `include` patterns first; then harden `exclude` patterns.
- Keep `scheduler.interval_seconds` enabled as a safety net even when watcher is on.
- Enable vector settings only if you intend to run an embedding/vector stack.
- For production, disable OpenAPI/UI if not required.
