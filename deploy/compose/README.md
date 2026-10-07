# Pheasant configuration profiles

These files are generated from the live configuration schema with `pheasant
setup`; the JSON answer files are the editable source of truth. No secret is
stored in YAML.

| Profile | State and coordination | Search/assistant | Intended size |
|---|---|---|---|
| `local-small.yaml` | Local SQLite, no broker or workers | BM25/text search and extractive answers; MCP and durable memory remain enabled | Laptop, offline, small corpus |
| `local-advanced.yaml` | Single-node SQLite | Hybrid + graph retrieval by default, LanceDB, both WASM accelerators, `text-embedding-3-small`, and an agentic workflow using GPT-6 Luna for evidence grading and GPT-6 Sol for answers | One capable workstation/container |
| `fleet.yaml` | PostgreSQL, NATS JetStream, shared durable volumes, a dedicated graph-query service, and stateless gRPC preparation workers | Concurrent hybrid retrieval with bounded per-process answer admission; API replicas keep no full graph resident | Multi-container, horizontally scaled ingestion and serving |
| `swarm-lab.yaml` | Single-node SQLite, one container (`role: all`) | Keyword + graph retrieval with no model key; MCP over streamable HTTP; durable memory for the lab's P1 arm | The region pheasant-swarm-search's `docker-compose.yml` bundles (it vendors this file) |

The fleet chunks every source by plan (`chunk_strategy: auto`), including UI
uploads: code by top-level block, Markdown by heading, spreadsheets by row, and
PDFs/Word by a bounded structural scan that turns on only the heading rules a
document uses, under a 2,000-character ceiling. A PDF longer than 500 pages has
its text read by all the workers in page ranges. PDF and Office text extraction
is already included in the universal image; taxonomy is a built-in indexing
setting, not a separate package extra. A change to any processing setting
causes a full source pass on the next sync. See
[`docs/how-to/dynamic-chunking.md`](../../docs/how-to/dynamic-chunking.md).

The advanced and fleet presets use three retrieval rounds and three-hop graph
expansion. The agentic workflow grades evidence with `gpt-6-luna` and writes
the grounded answer with `gpt-6-sol`. This affects query-time cost and latency,
not indexing or stored state.

The fleet also provisions one durable `/memory` source and enables the
interaction ledger with a seven-day hot retention window. Start the
`observability` Compose profile so the dedicated logger drains its NATS queue.
Evaluation and tuning are enabled for manual baseline runs, but automatic
tuning and bundle application remain off. The ledger records queries and
principals; no observed interaction becomes memory without explicit admission.

The fleet mounts `fleet.yaml` read-only in every service. The UI can display
retrieval settings, but saving a persistent search-parameter change through
the Config page fails on this deployment; edit `answers/scalable.json`,
regenerate `fleet.yaml`, and redeploy instead. Applied tuning bundles are
stored separately from YAML and can be changed through the Tuning UI.

`worker.yaml` is the deliberately minimal trust-boundary config for the
fleet's stateless gRPC workers. It has no source list, database DSN, OpenAI key,
MCP server, or UI.

## Regenerate after changing an answer file

Run these from the repository root:

```bash
python -m pheasant setup --answers deploy/compose/answers/local-small.json --accept-defaults --plain --target local --output deploy/compose/local-small.yaml --force
python -m pheasant setup --answers deploy/compose/answers/local-advanced.json --accept-defaults --plain --target docker --output deploy/compose/local-advanced.yaml --force
python -m pheasant setup --answers deploy/compose/answers/scalable.json --accept-defaults --plain --target compose --output deploy/compose/fleet.yaml --force
python -m pheasant setup --answers deploy/compose/answers/worker.json --accept-defaults --plain --target compose --output deploy/compose/worker.yaml --force
python -m pheasant setup --answers deploy/compose/answers/swarm-lab.json --accept-defaults --plain --target compose --output deploy/compose/swarm-lab.yaml --force
```

`swarm-lab.yaml` is copied verbatim into pheasant-swarm-search as
`deploy/pheasant/pheasant.yaml`; copy it again after regenerating.
`tests/test_swarm_lab_profile.py` fails when the YAML and its answer file
disagree.

## Run the profiles

Copy the environment template once for any Compose profile:

```bash
cp deploy/compose/.env.example .env
```

Blank-canvas Docker, one container and no required key:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yml up -d --build
```

Small, entirely local without Docker:

```bash
pip install -e ".[mcp]"
pheasant start -c deploy/compose/local-small.yaml
```

Advanced single-node Docker with SQLite, LanceDB and OpenAI:

```bash
docker compose --env-file .env \
  -f deploy/compose/docker-compose.advanced.yml up -d --build
```

Scalable fleet:

```bash
# Set OPENAI_API_KEY and THREE distinct random values in .env:
#   PHEASANT_API_TOKEN            callers -> the region's API
#   PHEASANT_GRAPH_SERVICE_TOKEN  API replicas -> the internal graph API
#   PHEASANT_INDEX_WORKER_TOKEN   the indexer -> the preparation workers
# One `openssl rand -hex 32` per line. Compose used to reuse the worker token
# as the graph token; workers are the least-trusted tier and hold the first by
# necessity, so that handed every worker the credential for the whole graph.
# `serve` now refuses to start when those two resolve to the same value.
docker compose --env-file .env -f deploy/compose/docker-compose.scale.yml up -d --build \
  --scale indexer=1 --scale worker=4
```

Fresh UI-managed reset, when existing Pheasant volumes should be cleared:

```bash
docker compose -f deploy/compose/docker-compose.fresh.yml \
  up -d --build --force-recreate
```

The fresh manifest is intentionally destructive only to its named Pheasant
volumes. See [Run the UI](../../docs/how-to/run-the-ui.md#fresh-ui-native-reset)
before using it.

The UI is at <http://127.0.0.1:8765> and streamable HTTP MCP is at
`http://127.0.0.1:8765/mcp` for both Docker profiles.

## Assistant retrieval and Pheasant-lab

Hybrid already runs lexical, vector, and graph retrieval concurrently.
`multi_search` removes standalone vector and graph modes when hybrid is chosen;
listing `[hybrid, graph, vector]` does not create extra search arms. The lab
starts with one hybrid pass, keeps graph expansion, and runs at most one
bounded follow-up round when the evidence is insufficient. Search fanout
timings and failed arms are reported with the completed answer.

The lab answer settings live in
[`answers/pheasant-lab.json`](answers/pheasant-lab.json). Generate its runtime
YAML from the typed schema and validate the deployment with:

```powershell
python -m pheasant setup --answers deploy/compose/answers/pheasant-lab.json --accept-defaults --plain --target compose --output pheasant.yaml --force
python -m pheasant doctor -c pheasant.yaml --no-require-paths
docker compose --env-file .env -f deploy/compose/docker-compose.pheasant-lab.yml config --quiet
```

**Driving this fleet from pheasant-swarm-search.** The lab connects over
streamable HTTP MCP with the same `PHEASANT_API_TOKEN` - from the host as
`http://127.0.0.1:8765/mcp`, or in Docker with the lab's
`docker-compose.fleet.yml`, which joins this project's `pheasant-lab_default`
network and connects as `http://api:8765/mcp` (its console's `lab-fleet`
connection). That second form needs `http://api:8765` in
`server.api.cors_origins`, which `answers/pheasant-lab.json` now sets:
pheasant derives its MCP DNS-rebinding allow-list from that list, and a host
it does not name is answered **421 Misdirected Request** on every MCP call.
Regenerate `pheasant.yaml` from the answer file after pulling this change.
Its shipped `configs/pheasant-mcp.example.yaml`
targets 0.13.5: it submits through `submit_documents`, registers the landing
directory (`/state` is allow-listed above), waits on `get_index_queue` while a
queued sync awaits an indexer, and reads `describe_source` once the barrier is
crossed to compare the region's document count with its receipts. MCP
`sync_source` publishes to the queue only when `graph.query_service_url` is
set, as it is here; a hand-built role-split region without a graph service
indexes MCP syncs in the call even on `--role api`, while HTTP `/sync` refuses.

Before regeneration, preserve existing secret values, source registrations,
named volumes and workspace mounts. Keep one API, graph service and active
indexer with four preparation workers and one logger until measurements justify
a resource change. Worker scaling affects ingestion preparation, not answer
latency; do not add indexer or graph replicas as an unmeasured answer fix.

Use `scripts/benchmark_assistant_latency.py` for completed-answer timings. It
separates HTTP completion, SSE workflow progress, first provisional answer
text, and final-answer events, and supports MCP. Build separate development and
held-out manifests from the deployment's
own corpus; label expected facts and acceptable supporting passage IDs, then
review claim support separately from citation-reference validity. Keep
benchmark questions and reports outside indexed source roots and preserve the
readiness denylist. A four-way, ten-repeat HTTP run can be started with:

```powershell
python scripts/benchmark_assistant_latency.py --base-url http://127.0.0.1:8765 --token-env PHEASANT_API_TOKEN --cases <held-out-cases.json> --concurrency 4 --repeats 10 --transport http --output-dir <report-directory>
python scripts/benchmark_assistant_latency.py --base-url http://127.0.0.1:8765 --token-env PHEASANT_API_TOKEN --cases <held-out-cases.json> --concurrency 4 --repeats 10 --transport sse --output-dir <report-directory>
```

The harness marks reports invalid if the corpus or effective configuration
changes during a run. Do not treat first progress, retrieval-only timing,
cached-only results, incomplete answers, or unreviewed lexical matches as a
latency/quality pass.

## Throughput and durability notes

Scale only the tier that owns the constrained work:

| Signal | Compose action | What it cannot fix |
|---|---|---|
| API request saturation | Put a load balancer in front, then scale `api` | indexing or graph-query CPU |
| Graph-query CPU/latency | scale `graph`, after measuring host headroom | graph save/enrichment |
| Preparation backlog | scale `worker` | embedding quota or ordered commits |
| Indexer failure | start a standby `indexer`; one lease stays active | write throughput |
| Save/enrichment/commit dominance | create another whole knowledge-base shard | a single shard's global graph |

One Docker host has one CPU, disk, and PostgreSQL resource pool. The measured
single-host graph test became slower at two replicas, so the shipped Compose
default remains one graph service, one active indexer, and four workers. A
second complete Compose project needs distinct project/volume names,
`pheasant.name`, database scope, ports, and graph/worker tokens; treat it as a
knowledge-base shard rather than another writer for the same graph.

- The fleet profile batches 128 chunks per embedding request and permits 8
  embedding requests in flight. That is an aggressive but bounded ceiling;
  the OpenAI account's RPM/TPM tier is the actual limit. If logs show repeated
  429 responses, reduce `max_parallel_embeddings` before reducing batch size.
- gRPC workers accelerate file decoding, parsing, extraction and chunking.
  They do not accelerate the external embedding API. Scale workers for
  preparation and create another shard for another commit authority. Extra
  indexers for one shard are elected hot standbys, not throughput replicas.
- PDF, DOCX and other offline document extraction is dispatched to the worker
  tier. Each document is its own bounded task envelope, and native MuPDF work
  is serialized within a worker process while replicas continue in parallel.
- URL-managed repositories use the persistent `pheasant-workspace` volume by
  default. The indexer mounts it read/write so its persisted clone recipe can
  fetch and fast-forward before each sync; the API mounts it read-only. Set
  `PHEASANT_FLEET_WORKSPACE_PATH` to use a specific host directory instead.
- The memory volume is mounted read/write by both API and indexers, but the
  fleet does not create or schedule a memory source until `POST /memory/enable`
  is called. Once enabled, calls to MCP `memory_write` (or `POST /memory`) index
  immediately, while watcher and scheduler settings provide recovery if a
  write or process is interrupted.
  Ordinary chat questions and answers are not silently recorded as memory;
  an agent must explicitly choose what to remember.
- PostgreSQL and NATS make the source queue and manifests durable. LanceDB
  remains under the shared `/state/vectors` volume; the API reads it and the
  indexer tier writes it.
- The `graph` service is the only serving tier that owns `graph.latest.json` in
  RAM. It refreshes after indexer commits and exposes authenticated, bounded
  operations over the internal network. API/MCP replicas have a 2 GiB limit
  and never fall back to loading the graph locally. Scale API for request
  traffic, graph replicas for graph-query traffic, workers for preparation,
  and whole stacks for knowledge-base sharding.
- Keep the extra service only when it buys something: API replicas are
  multiplying graph RAM, a 3 GiB API remains above roughly 60-70% steady
  memory, or graph-query traffic needs an independently scalable tier. It does
  not accelerate graph save/enrichment; when those dominate, shard whole
  repositories or document collections into separate knowledge bases. Below
  the residency/query thresholds, the default single-container/local-graph
  profile is simpler and usually faster because it avoids an HTTP hop.
