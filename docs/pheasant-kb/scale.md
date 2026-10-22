# Scale pheasant-kb up or down

Every service in pheasant-kb has one job, so each one is limited by one thing
and sized by its own knobs. Scaling is a matter of finding the part that is
limited and moving that part's number, not making everything bigger.

Sizes live in `.env` beside `compose.yaml` and take effect with
`docker compose up -d`. Each is a **limit**, not a reservation: a service uses
what it needs up to its limit, and the defaults add up to more than an idle
stack uses. One hard rule from Docker: no single `*_CPUS` value may exceed the
host's CPU count.

## At a glance

| Part | Limited by | Watch | Knobs |
|---|---|---|---|
| [Workers](#workers-ingest-throughput) | Parsing and extraction (PDFs, Office files) | `pheasant_index_preparation_backlog`, `pheasant_index_files_per_second` | `PHEASANT_WORKERS`, `PHEASANT_WORKER_THREADS`, `PHEASANT_WORKER_MEMORY`, `PHEASANT_WORKER_CPUS` |
| [Indexer](#indexer-the-single-writer) | Corpus size (memory) and commit rate | `pheasant_commit_authority_saturation`, container memory | `PHEASANT_INDEXER_MEMORY`, `PHEASANT_INDEXER_CPUS` |
| [Embeddings](#embeddings) | Your OpenAI rate limits | `429` responses in the indexer log | `sync.concurrency.max_parallel_embeddings`, `search.embeddings.batch_size` |
| [API](#api-requests-and-answers) | Concurrent requests and answers | `pheasant_requests_inflight`, `429` responses, `pheasant_search_duration_seconds` | `PHEASANT_API_MEMORY`, `PHEASANT_API_CPUS`, `server.api.max_concurrent_requests`, `assistant.latency.max_concurrent_answers` |
| [Vector store](#the-vector-store) | Chunks × dimensions | api memory, vector search time | `search.vector_store.resident`, `resident_max_mb`, `ann_index`, `PHEASANT_API_MEMORY` |
| [Graph service](#graph-service) | Graph-query latency | graph-arm and walk latency | `PHEASANT_GRAPH_MEMORY`, `PHEASANT_GRAPH_CPUS` |
| [Postgres](#postgres) | Corpus size and query load | query latency, connection count | `PHEASANT_POSTGRES_MEMORY`, `PHEASANT_POSTGRES_CPUS`, `PHEASANT_POSTGRES_MAX_CONNECTIONS` |
| [Storage](#storage) | Disk | volume sizes | The volumes; `pheasant scan` projects them |
| [NATS and logger](#nats-and-logger) | Queue traffic | `pheasant_index_queue_depth`, `pheasant_log_queue_depth` | `PHEASANT_NATS_*`, `PHEASANT_LOGGER_*` |

The metrics are on each service's `/metrics` (Prometheus text, no token
needed); the api's is at <http://127.0.0.1:8765/metrics>. The others are
reachable from inside the Compose network, for example
`docker compose exec indexer curl -s localhost:8765/metrics`.
[Monitor indexing](../how-to/monitor-indexing.md) lists them all.

## The defaults

| Service | Memory | CPUs | Replicas |
|---|---|---|---|
| `api` | 4096m | 1.0 | 1 |
| `graph` | 2560m | 1.5 | 1 |
| `indexer` | 2560m | 2.0 | 1 |
| `worker` | 512m each | 1.0 each | 4 (2 threads each) |
| `logger` | 1024m | 1.0 | 1 |
| `postgres` | 1280m | 2.0 | 1 |
| `nats` | 256m | 0.5 | 1 |

These were measured on the stack itself and suit a corpus up to roughly
150,000 files on a host with 16 GB of memory and 8 cores. The recipes at the
end give smaller and larger starting points.

## Workers: ingest throughput

Workers decode, extract and chunk files the indexer hands them. They are the
part to add when a first index or a large batch of documents is slow, and they
are stateless, so adding one is free of risk:

```bash
# .env
PHEASANT_WORKERS=8
```

or, for one run, `docker compose up -d --scale worker=8`. The indexer spreads
work across every replica, and a PDF longer than 500 pages is split by page
range across all of them.

- **Scale up** when `pheasant_index_preparation_backlog` stays high during a
  sync, and especially for PDF- and Office-heavy corpora, where parsing is
  most of the cost.
- **Diminishing returns** set in when the indexer's commits, not parsing,
  dominate: on a corpus of small text files eight workers measured only 1.1×
  faster than one. Plain code and Markdown gain little past two.
- `PHEASANT_WORKER_THREADS` is files in flight per worker. Raise it with
  `PHEASANT_WORKER_MEMORY`, since each file in flight is held in memory.
- **Scale down** to 1 or 2 on a small host. Workers do nothing between syncs.

Workers never speed up embeddings; those are bounded by your OpenAI quota.

## Indexer: the single writer

The indexer is the only service that writes: it claims syncs from the queue,
commits chunks, graph rows and vectors in order, and runs the enrichment
passes. Its memory holds the graph working set, which grows with the corpus:

| Corpus | Indexer memory |
|---|---|
| up to 75,000 files | 1–1.5 GB |
| up to 150,000 files | 2–2.5 GB (the default) |
| up to 250,000 files | 4 GB |
| beyond | 8 GB, or split the corpus (below) |

Raise `PHEASANT_INDEXER_MEMORY` before a large first index rather than after
an out-of-memory restart, which looks like an unexplained restart mid-sync.
`pheasant scan` projects memory, disk and time for a folder before you index
it.

There is exactly one active indexer. A second one is a **hot standby**, not more
throughput: it takes over within a lease interval if the first dies.

```bash
docker compose up -d --scale indexer=2
```

The ceiling of one writer is a metric, not a guess:
`pheasant_commit_authority_saturation` is the busy fraction of the last five
minutes. Sustained above 0.8, more workers will not help, and the answer is to
split the corpus into a second knowledge base. `pheasant shard plan --emit DIR`
proposes the split and writes the second stack's files. See
[Capacity planning](../how-to/capacity-planning.md#when-you-cross-the-line-shard).

## Embeddings

Every new or changed chunk is embedded by OpenAI during indexing. pheasant-kb
sends batches of 128 chunks with up to 8 requests in flight, which is
aggressive; your account's rate limits are the real ceiling.

- Repeated `429` responses in `docker compose logs indexer`: lower
  `sync.concurrency.max_parallel_embeddings` first, then
  `search.embeddings.batch_size`.
- A higher rate tier: raise `max_parallel_embeddings`.

Both are config settings ([Configure](configure.md#change-the-configuration)).

## API: requests and answers

The api service serves the UI, the HTTP API and MCP. It holds no graph, and
reads Postgres and the vector store.

- **Concurrent requests** are capped by `server.api.max_concurrent_requests`
  (32). Beyond it the API answers `429` with `Retry-After` instead of slowing
  down. `pheasant_requests_capacity_remaining` shows the headroom.
- **Concurrent answers** are capped separately by
  `assistant.latency.max_concurrent_answers` (4), because each one holds model
  calls open for seconds. Raise it with `PHEASANT_API_CPUS`.
- **Memory** (4096m) covers the in-memory vector matrix (below), result sets,
  and MCP uploads, which are parsed in memory up to
  `server.mcp.max_request_body_size_mb` (1024 in pheasant-kb). If agents do not
  upload large files, lower that setting and `PHEASANT_API_MEMORY` can drop to
  2048m; a sustained hybrid-search load measured about 1 GB.

One api replica is the right shape on one host, because it owns the published
port. More api replicas need a load balancer in front, which is what the
[Kubernetes manifests](../deployment.md) provide.

## The vector store

Vectors live in LanceDB on the `pheasant-state` volume. The indexer writes them
and the api reads them. Two settings decide how they are searched:

- `search.vector_store.resident: true` (pheasant-kb's setting) keeps every
  vector in the api service's memory as one matrix: an exact search in tens of
  milliseconds. It costs **4 bytes × 1,536 dimensions = 6 KB per chunk**:
  about 600 MB at 100,000 chunks. A table larger than
  `search.vector_store.resident_max_mb` (1024 by default, about 170,000 chunks)
  is not held, and the disk scan answers instead.
- `search.vector_store.ann_index: ivf_hnsw_sq` (also on) keeps an approximate
  index the indexer rebuilds after each sync. It answers when the vectors are
  not resident, much faster than an exact disk scan, at some cost in recall.
  `none` turns it off on the next query.

**Scaling up:** raise `PHEASANT_API_MEMORY` and `resident_max_mb` together,
leaving the 1–2 GB the api needs besides. **Scaling down:** turn `resident`
off and let the ANN index answer, which frees that memory at the cost of some
recall; measure your own corpus with
`python -m pheasant.search.vector_ann_benchmark`. Smaller vectors
(`search.embeddings.dimensions: 512`) cut memory by two thirds but re-embed
everything. See [Vector search](../how-to/vector-search.md).

## Graph service

The graph service answers graph walks and slices for the api tier. The graph
itself is stored as rows in Postgres, so this service holds no resident copy;
its memory covers query working sets. On one host a second graph replica
measured *slower* than one, because both share the same CPUs and database.
Raise `PHEASANT_GRAPH_CPUS` if graph queries are slow under load; scale it out
only across machines. Below about 75,000 files, `PHEASANT_GRAPH_MEMORY=1024m`
is ample.

## Postgres

Postgres holds operational state, every chunk, the graph's rows and the BM25
full-text index, so it grows with the corpus and carries every search.

- **Memory and CPU:** raise `PHEASANT_POSTGRES_MEMORY` and `_CPUS` as the
  corpus passes about 100,000 files or when search latency climbs under load.
- **Connections:** each pheasant service holds up to `storage.pool_size` (16)
  connections: api, graph, indexer and logger use 64 of the default 100. Add 16
  per extra indexer or api replica, and raise
  `PHEASANT_POSTGRES_MAX_CONNECTIONS` to match.
- `POSTGRES_PASSWORD` is fixed when the database is first created.

## Storage

| Volume | Grows with | Rough size |
|---|---|---|
| `pheasant-pg17` | Chunks, graph rows, full-text index | The bulk of the footprint. About 240 MB for 8,000 files of code and docs together with `/state`; graph rows cost ~1.6 KB per node. |
| `pheasant-state` | Vectors (6 KB per chunk), graph snapshots, images, uploads | |
| `pheasant-workspace` | Repositories added by URL | The size of the clones |
| `pheasant-exports` | Parquet exports, cold observation partitions | Regenerable; prune freely |
| `pheasant-nats` | Queued tasks | Small; acknowledged tasks are removed |

`pheasant scan` prints a projection of disk, memory and first-index time
before you add a large source. `docker system df -v` shows actual volume
sizes. To put a volume on a particular disk, replace it with a bind mount in a
`compose.override.yaml`.

## NATS and logger

Both are small and rarely need attention. The logger turns observations into
rows in batches of 500; one replica keeps up with heavy traffic. If
`pheasant_log_queue_depth` grows without bound, give it more CPU, or turn the
interaction ledger off and run `--scale logger=0`
([Configure](configure.md#features-you-can-turn-off)).

## Recipes

### Scale down: an 8 GB laptop

```bash
# .env
PHEASANT_API_MEMORY=2048m
PHEASANT_GRAPH_MEMORY=1024m
PHEASANT_GRAPH_CPUS=1.0
PHEASANT_INDEXER_MEMORY=1536m
PHEASANT_INDEXER_CPUS=1.0
PHEASANT_WORKERS=2
PHEASANT_LOGGER_MEMORY=512m
PHEASANT_POSTGRES_MEMORY=1024m
PHEASANT_POSTGRES_CPUS=1.0
```

In your `pheasant.yaml` copy, lower `server.mcp.max_request_body_size_mb` to
`64`. This suits a corpus of up to about 25,000 files. For less than this, run
the [single container](../other-deployments.md) instead: one process, SQLite,
no key needed.

### Scale up: a large corpus on one large host

For 150,000 to 250,000 files on 32 GB and 16 cores:

```bash
# .env
PHEASANT_WORKERS=8
PHEASANT_WORKER_THREADS=2
PHEASANT_INDEXER_MEMORY=4096m
PHEASANT_INDEXER_CPUS=4.0
PHEASANT_API_MEMORY=6144m
PHEASANT_API_CPUS=2.0
PHEASANT_POSTGRES_MEMORY=4096m
PHEASANT_POSTGRES_CPUS=4.0
```

Raise `search.vector_store.resident_max_mb` to fit the vectors (6 KB per
chunk), and `assistant.latency.max_concurrent_answers` with the api's CPUs.

### Beyond one host

- **More request capacity** or **machines for each tier:** the same services
  run on Kubernetes, with api, graph and worker replicas spread across nodes.
  See [Deployment](../deployment.md) and
  [Running a worker fleet](../how-to/worker-fleet.md).
- **More write capacity:** split the corpus into knowledge-base shards with
  `pheasant shard plan`, one pheasant-kb stack per shard.
- **Measured coefficients and the reasoning behind them:**
  [Capacity planning](../how-to/capacity-planning.md).
