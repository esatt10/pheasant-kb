# Pheasant configuration profiles

These files are generated from the live configuration schema with `pheasant
setup`; the JSON answer files are the editable source of truth. No secret is
stored in YAML.

| Profile | State and coordination | Search/assistant | Intended size |
|---|---|---|---|
| `local-small.yaml` | Local SQLite, no broker or workers | BM25/text search and extractive answers; MCP and durable memory remain enabled | Laptop, offline, small corpus |
| `local-advanced.yaml` | Single-node SQLite | Hybrid + graph retrieval by default, LanceDB, both WASM accelerators, `text-embedding-3-small`, and the `gpt-6-luna` agentic workflow | One capable workstation/container |
| `fleet.yaml` | PostgreSQL, NATS JetStream, shared durable volumes, a dedicated graph-query service, and stateless gRPC preparation workers | Vector + graph + hybrid assistant fanout with adaptive concurrency; API replicas keep no full graph resident | Multi-container, horizontally scaled ingestion and serving |

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
```

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

## Fleet retrieval fanout

Hybrid already runs lexical, vector, and graph retrieval concurrently. The
fleet therefore does not add a separate `text` assistant fanout: it repeated
the PostgreSQL lexical ranking query that stress testing identified as the
slowest arm for common terms. Text remains available as an explicit API/MCP
mode and remains part of every hybrid request. The explicit vector and graph
modes preserve arm-specific candidates that can be truncated by hybrid fusion.

This is a fleet-profile choice, not a schema-default change. The small and
advanced profiles and the setup wizard defaults are unchanged.

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
