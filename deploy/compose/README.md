# Pheasant configuration profiles

**pheasant-kb is the default deployment.** It is [`compose.yaml`](../../compose.yaml)
at the repository root, started with `docker compose up -d`, and its
configuration is `pheasant-kb.yaml` in this directory. Its guide is
[`docs/pheasant-kb/`](../../docs/pheasant-kb/index.md); other shapes are in
[Deployment](../../docs/deployment.md).

These files are generated from the live configuration schema with `pheasant
setup`; the JSON answer files under `answers/` are the editable source of
truth. No secret is stored in YAML.

| File | What it is |
|---|---|
| **`pheasant-kb.yaml`** | pheasant-kb's config, mounted on every tier by the root `compose.yaml`: PostgreSQL, NATS JetStream, a graph service, gRPC workers, the logger, hybrid retrieval with a resident vector matrix, `gpt-6-luna` agentic answers with Decisions API judgements. One host, up to ~150k files by default; every service sized from `.env`. |
| `fleet.yaml` | The generic fleet CI's smoke tests run, with `docker-compose.scale.yml`. The same topology as pheasant-kb. |
| `worker.yaml` | The minimal trust-boundary config for the gRPC preparation workers: no source list, database DSN, model key, MCP server or UI, and `server.api.enabled: false`. |
| `docker-compose.scale.yml` | The generic fleet's compose file (`--scale indexer=1 --scale worker=4`). |
| `backup.sh`, `restore.sh` | pheasant-kb's `backup` and `restore` services. |
| `init-secrets.sh` | pheasant-kb's `secrets-init`: generates the four tokens on first start. |
| `foundry/` | The opt-in Azure AI Foundry overlay ([Foundry](../../docs/pheasant-kb/foundry.md)). |
| `ci/` | Configs and scripts the CI smoke tests use. |

Both fleet configs chunk every source by plan (`chunk_strategy: auto`),
including UI uploads, and read a PDF longer than 500 pages across the workers
in page ranges. A change to any processing setting causes a full source pass
on the next sync. See
[`docs/how-to/dynamic-chunking.md`](../../docs/how-to/dynamic-chunking.md).

Both mount their YAML read-only in every service. The UI can display retrieval
settings, but saving a persistent search-parameter change through the Config
page fails on these deployments; edit the answer file, regenerate, and
redeploy instead. Applied tuning bundles are stored in the database and can be
changed through the Tuning UI.

## Regenerate after changing an answer file

Run these from the repository root:

```bash
python -m pheasant setup --answers deploy/compose/answers/pheasant-kb.json --accept-defaults --plain --target compose --output deploy/compose/pheasant-kb.yaml --force
python -m pheasant setup --answers deploy/compose/answers/scalable.json --accept-defaults --plain --target compose --output deploy/compose/fleet.yaml --force
python -m pheasant setup --answers deploy/compose/answers/worker.json --accept-defaults --plain --target compose --output deploy/compose/worker.yaml --force
```

## Run them

pheasant-kb, from the repository root (the root `.env.example` lists every
setting it reads):

```bash
echo "OPENAI_API_KEY=sk-..." > .env && docker compose up -d
```

The generic fleet reads `deploy/compose/.env.example`:

```bash
cp deploy/compose/.env.example .env
# Set OPENAI_API_KEY and FOUR distinct random values (`openssl rand -hex 32`):
#   PHEASANT_API_TOKEN                callers -> the region's API
#   PHEASANT_GRAPH_SERVICE_TOKEN      API replicas -> the internal graph API
#   PHEASANT_INDEX_WORKER_TOKEN       the indexer -> the preparation workers
#   PHEASANT_INGESTION_SERVICE_TOKEN  API replicas -> the indexer's landing service
docker compose --env-file .env -f deploy/compose/docker-compose.scale.yml up -d --build \
  --scale indexer=1 --scale worker=4
```

`serve` refuses to start when the graph and worker tokens resolve to the same
value: workers are the least-trusted tier and hold the worker token by
necessity.

The UI is at <http://127.0.0.1:8765> and streamable HTTP MCP is at
`http://127.0.0.1:8765/mcp` for both.

## pheasant-kb notes

pheasant-kb was previously shipped here as `docker-compose.pheasant-lab.yml`
(project and knowledge base `pheasant-lab`). An existing lab region keeps its
volumes and index with `COMPOSE_PROJECT_NAME=pheasant-lab` and
`PHEASANT_KB_CONFIG=./pheasant.yaml` in the root `.env`; see
[Operate pheasant-kb](../../docs/pheasant-kb/operate.md#move-from-pheasant-lab).

Hybrid already runs lexical, vector and graph retrieval concurrently.
`multi_search` drops separate vector and graph modes when hybrid is chosen, so
listing `[hybrid, graph, vector]` does not create extra search arms.
pheasant-kb starts with one hybrid pass, keeps graph expansion, and runs at
most one bounded follow-up round when the evidence is insufficient.

**An agent in another container** reaches pheasant-kb over streamable HTTP MCP
with the API token at `http://api:8765/mcp` once it joins this project's
network. The MCP host guard is derived from `server.api.cors_origins`, so the
answer file lists `http://api:8765`; a config generated before it did answers
that container **421**, so regenerate it.

Back up and restore with `docker compose run --rm backup` and
`docker compose run --rm restore <name>`; see
[Operate pheasant-kb](../../docs/pheasant-kb/operate.md#back-up-and-restore).

Keep one API, graph service and active indexer with four preparation workers
and one logger until measurements justify a change; the knobs and the signals
for each are in [Scale up or down](../../docs/pheasant-kb/scale.md). Worker
scaling affects ingestion preparation, not answer latency. Answer latency is
measured with `scripts/benchmark_assistant_latency.py`
([Operate](../../docs/pheasant-kb/operate.md#measure-answer-latency)).

## Throughput and durability notes

Scale only the tier that owns the constrained work:

| Signal | Compose action | What it cannot fix |
|---|---|---|
| API request saturation | Put a load balancer in front, then scale `api` | indexing or graph-query CPU |
| Graph-query CPU/latency | scale `graph`, after measuring host headroom | graph enrichment or commits |
| Preparation backlog | scale `worker` | embedding quota or ordered commits |
| Indexer failure | start a standby `indexer`; one lease stays active | write throughput |
| Commit authority saturated | create another whole knowledge-base shard | a single shard's commit stream |

One Docker host has one CPU, disk and PostgreSQL resource pool. The measured
single-host graph test became slower at two replicas, so the default remains
one graph service, one active indexer and four workers. A second complete
Compose project needs distinct project and volume names, `pheasant.name`,
database, ports and tokens; treat it as a knowledge-base shard rather than
another writer for the same graph (`pheasant shard plan --emit` writes one).

- pheasant-kb batches 128 chunks per embedding request and permits 8 requests
  in flight. The OpenAI account's RPM/TPM tier is the actual limit; if logs
  show repeated 429 responses, reduce `max_parallel_embeddings` before
  reducing the batch size.
- gRPC workers accelerate decoding, parsing, extraction and chunking. They do
  not accelerate the embedding API. Extra indexers for one shard are elected
  hot standbys, not throughput replicas.
- URL-managed repositories use the `pheasant-workspace` volume. The indexer
  mounts it read/write so it can fetch and fast-forward before each sync; the
  API mounts it read-only.
- Typed records (`record_artifact`, `POST /artifacts/records`) go to the
  `artifact-records` source under `/state`, created by the first write. The
  api hands each record file to the indexer through the artifact outbox,
  because it mounts `/state` read-only. Ordinary chat questions and answers
  are never recorded as records; an agent chooses what to record.
- PostgreSQL and NATS make the source queue and manifests durable. LanceDB
  lives under the shared `/state/vectors`; the api reads it and the indexer
  writes it.
- The graph is rows in PostgreSQL; no tier holds it resident. The `graph`
  service answers graph reads for the api on its own connection pool, and the
  api never falls back to reading the graph itself when the service is down.
