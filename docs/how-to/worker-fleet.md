# Running a worker fleet

## Split the process first

Before any of the below matters, decide what each process *is*. One pheasant
process serves search, serves the UI, watches directories, runs the scheduler
and indexes — right for one container, wrong for several, because three
replicas of it all watch the same directories and all try to index the same
source.

```bash
pheasant serve                    # all: today's behavior, the default
pheasant serve --role api         # serve; publish index work to the queue
pheasant serve --role graph       # serve read-only graph operations
pheasant serve --role indexer     # watch, schedule, drain the queue
pheasant worker --transport grpc  # preparation only
```

`api` replicas scale with request traffic and never index; `graph` replicas
serve a complete read-only snapshot; one `indexer` per shard does the
indexing; `worker` pods do the parsing. The hand-off between api and indexer is
the [queue](#queue-the-backlog), which is why `--role api`
refuses to start without it — a sync request that is accepted and then goes
nowhere is worse than one that is refused.

`GET /health` and `GET /ready` report the role, so you can tell pods apart
from a probe response. Full table in
[configuration](../configuration.md#process-roles-serverrole).


Indexing is CPU-bound Python. One container can only parse as fast as its own
cores, and while it does, it is holding the GIL against the request path. A
**preparation worker** is a second process — usually a second container — that
does the parsing and chunking and nothing else.

This page is about running several of them safely. If you only want one
container to go faster, [speed up indexing](indexing-performance.md) is the
shorter answer.

## What a worker is, and is not

A worker receives a file's bytes and a whitelisted parse config, and returns
chunks. That is the whole contract, and everything else follows from it:

* it **never writes** SQLite, the graph, manifests or vectors — the coordinator
  commits, in discovery order, so stable IDs and graph bytes stay deterministic;
* it **never receives connector credentials**. Only parsing inputs cross the
  boundary, so a compromised worker cannot reach your Notion or Drive;
* it holds no state between requests, so workers are interchangeable and
  disposable.

That last property is what makes the durability below possible: a request that
fails can simply be sent somewhere else.

## Start a fleet

Each worker is an ordinary pheasant container with the worker role turned on
and the shared token in its environment:

```yaml
# worker container
sync:
  concurrency:
    remote_worker_enabled: true
    remote_worker_token_env: PHEASANT_INDEX_WORKER_TOKEN
```

```bash
# HTTP workers are the API server
pheasant serve

# gRPC workers are their own process
pheasant worker --transport grpc --port 8766
```

The coordinator names them:

```yaml
sync:
  concurrency:
    file_executor: remote
    remote_worker_urls:
      - http://worker-a:8765
      - http://worker-b:8765
    remote_worker_token_env: PHEASANT_INDEX_WORKER_TOKEN
    remote_worker_batch_size: 8
    worker_transport: http    # or grpc
```

!!! warning "`/internal/indexing/prepare*` must not face the public internet"

    It is bearer-authenticated and refuses to run at all unless
    `remote_worker_enabled` is set, but it exists to accept work from your own
    coordinator. Keep it on an internal network or behind an ingress that
    does not route to it.

## What happens when a worker dies

Nothing you have to configure, which is the point. Dispatch is durable by
default:

| Failure | What happens |
|---|---|
| One worker refuses connections | Three attempts, then its circuit opens for 30 s and the rest of the sync goes elsewhere |
| A worker is slow | The request carries the caller's remaining budget; the worker declines work whose caller has already given up |
| A worker returns 429 or 503 | Retried with full jitter, honouring `Retry-After` |
| A rolling deploy replaces every pod | Endpoints fail over; if none answer, the coordinator prepares locally |
| **Every worker is down** | The sync **still completes**, with an identical index — only slower |

That last row is the design rule: remote preparation is a throughput
optimization, so it can never fail a sync. A test asserts the artifacts are
byte-identical between a healthy fleet and a completely dead one.

A retried request carries the same content-addressed idempotency key, so a
worker that already did the work answers from cache rather than parsing twice.

### Watching it

```promql
pheasant_worker_up{endpoint="http://worker-a:8765"}
```

`0` means that endpoint's breaker is open. Sync throughput
(`pheasant_index_files_per_second`) is the number that tells you whether it
mattered — see [monitor indexing](monitor-indexing.md).

## HTTP or gRPC

Start with HTTP. It needs no extra, and every durability property above is
identical on both transports because they are implemented once, above the
transport.

Choose gRPC (`pip install 'pheasant[grpc]'`, `worker_transport: grpc`) when:

* **your corpus is large and your network is not free.** JSON base64s file
  content, a flat 33% inflation on the only large field; gRPC sends it raw.
* **you have slow outliers.** `PrepareBatch` streams, so results come back as
  they are computed and one refused file does not fail its whole batch — over
  HTTP a batch answers with one body, so a single unacceptable file sends the
  rest back for local preparation.

The `.proto` ships in the package
(`pheasant/sync/proto/pheasant_worker.proto`) and is compiled at import time,
so it is a real contract you can implement a worker against in any language.

## Batch size is a memory knob

`remote_worker_batch_size` amortizes per-request overhead, but every task in a
batch holds its file's bytes in memory on **both** sides. With the default
1 GiB file limit, a batch of 8 can be an 8 GiB worst case per in-flight batch,
and there are `max_parallel_files` of those. Raise it for a corpus of small
files; leave it alone if your sources contain large documents.

## Queue the backlog

Separately from workers: with several sources, the list of what is left to
index lives in the coordinator's memory. A process killed nine sources into ten
has lost the tenth.

```yaml
sync:
  queue:
    enabled: true
    backend: local      # this knowledge base's own database — no broker
```

Now the backlog is rows. A restart resumes it, `pheasant_index_queue_depth` is
a number other processes can read, and a source that keeps failing is
dead-lettered after `max_attempts` instead of consuming the fleet forever.

```bash
pheasant queue status         # backlog, in-flight, and why anything died
pheasant queue drain          # index everything currently queued
pheasant queue requeue-dead   # replay dead letters once the cause is fixed
```

Redelivery is safe: indexing is idempotent by content hash and stable ID, so a
task delivered twice re-indexes to identical state.

Use `backend: nats` (`pip install 'pheasant[queue]'`) when indexers on
different machines must share one backlog and you would rather not point them
all at one database:

```yaml
sync:
  queue:
    enabled: true
    backend: nats
    nats_servers: ["nats://nats:4222"]
```

It buys fan-out, not correctness the local queue lacks — and it is one more
thing to run.

## Running the whole fleet

The pieces above assemble into four tiers. Both runtimes ship a working
version, and both are the *other* trade from the single container — take them
only past the point where one container stops being enough
([capacity planning](capacity-planning.md)).

### Four secrets, not one

Before either runtime: the fleet has four trust boundaries and each needs its
own value.

```bash
export PHEASANT_API_TOKEN=$(openssl rand -hex 32)               # callers -> the API
export PHEASANT_GRAPH_SERVICE_TOKEN=$(openssl rand -hex 32)     # API -> the graph service
export PHEASANT_INDEX_WORKER_TOKEN=$(openssl rand -hex 32)      # indexer -> the workers
export PHEASANT_INGESTION_SERVICE_TOKEN=$(openssl rand -hex 32) # API -> the landing service
```

One `openssl rand` per line, and that is the whole point. Workers are the
least-trusted tier in the fleet — no database, no keys, no volumes, and the
one place third-party parse code runs — and they hold the indexing token by
necessity. A value shared with the graph boundary would mean any compromised
worker also held the credential for the internal graph API, which serves the
whole graph. The shipped Compose file used to do exactly that; `serve` now
**refuses to start** when the two resolve to the same value.

The fourth is what makes **manual upload** work here at all, and it is worth
knowing why it needs one. An api replica mounts `/state` read-only, because the
indexer is the sole writer of committed state — so the UI drop zone, an agent's
`ingest_submit` and the readiness probe have nowhere to put their bytes. They
are forwarded to the indexer, which performs the identical local write; the
serving tier still writes nothing. Holding that token means being able to put
bytes into the corpus, so it is a distinct value and `serve` refuses it
colliding with the worker token as well. It is configured by
`ingestion.landing_service_url` — already set in `fleet.yaml` and the scaled
ConfigMap — and is a no-op in a single container, where the process that
accepts an upload is the one that indexes it.

### Compose

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.scale.yml up -d \
  --scale indexer=1 --scale worker=4
```

Postgres, NATS JetStream, one API, one graph-query service, one elected indexer
and four gRPC workers. The API keeps no persisted graph resident; authenticated
graph and hybrid operations go to the graph service over the Compose network.
The indexer owns watcher, scheduler, queue drain and the global commit stream;
those producers share one lock, while a `sync --all` child can still prepare
several sources concurrently. Starting extra indexers creates hot standbys:
one database lease elects the leader and promotes a standby after failure.
It does not add write throughput because the graph file, manifests, vectors
and graph FTS must reach one end state. The gRPC coordinator keeps one
multiplexed channel to Docker's
scaled service name and selects `round_robin`, so concurrent preparation
batches reach distinct worker replicas instead of gRPC's `pick_first` default
pinning the source to one container. Scale API replicas only after putting a
load balancer in front of them; Compose deliberately publishes one API endpoint.

### Kubernetes

```bash
kubectl apply -f deploy/kubernetes/namespace.yaml
kubectl -n pheasant create secret generic pheasant-secrets \
  --from-literal=PHEASANT_DATABASE_URL='postgresql://…' \
  --from-literal=PHEASANT_API_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=PHEASANT_INDEX_WORKER_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=PHEASANT_GRAPH_SERVICE_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=PHEASANT_INGESTION_SERVICE_TOKEN="$(openssl rand -hex 32)"
kubectl apply -f deploy/kubernetes/scaled/
```

`deploy/kubernetes/scaled/networkpolicy.yaml` is applied by that last line: a
default-deny on ingress, then one allowance per real caller. The API tier is
the only one anything outside the namespace may reach; the graph service and
the workers accept traffic from pheasant's own pods and the monitoring
namespace, and nothing else. A CNI that does not enforce NetworkPolicy makes
the file inert — check before relying on it, because the tokens are then the
only control.

| Workload | Kind | Scales on |
|---|---|---|
| `pheasant-api` | Deployment | CPU (HPA) |
| `pheasant-graph` | Deployment | graph-query CPU/latency; one per shard minimum |
| `pheasant-indexer` | StatefulSet, 1 replica | not autoscaled |
| `pheasant-worker` | Deployment | queued sources + `pheasant_index_preparation_backlog` |

`deploy/kubernetes/scaled/README.md` lists what you must provide first. Two
requirements are easy to miss and neither is optional:

* **A ReadWriteMany volume for `/state`.** The indexer writes the graph; the
  graph-query service and API replicas read state/vector data. With RWO the
  volume attaches to one node, so replicas scheduled elsewhere cannot mount
  it. Most default StorageClasses are RWO.
* **Postgres.** SQLite permits one writer per file, and it is not one file
  across pods.

#### How a committed graph reaches the tier serving it

The indexer writes the graph; the graph service and any API replica holding a
local snapshot reload it and swap generations atomically. Two things trigger
that reload, and the cheap one is not the primary one.

An indexer **announces** each committed generation on the broker the fleet
already runs (`sync.queue.nats_graph_subject`, core NATS pub/sub, one subject
per knowledge base). Every replica hears it and reloads at commit latency
rather than up to `server.api.graph_refresh_seconds` later. The **poll is
kept** underneath as a backstop, which is what lets the announcement be
at-most-once and stateless: a dropped message, a broker restart, or a region
with no broker at all costs one poll interval and nothing else. A region on
the `local` queue backend behaves exactly as it did before this existed.

Each generation has a content-addressed id — a digest of the published bytes,
so two replicas agree on the name without coordinating and an unchanged graph
keeps its name across a re-save. It is published where staleness becomes
visible rather than inferable:

```console
$ curl -s localhost:8765/ready | jq .graph_generation
{ "loaded": "9f2c41b0a7e35d18", "published": "9f2c41b0a7e35d18", "current": true }
```

`loaded` is the generation this process is answering from; `published` is what
is on `/state` now. When they differ, that replica has not picked up the
latest commit — the condition that used to be silent by construction.

On `/ready` rather than `/health`, and read off the event loop beside the
state-store probe: comparing the two means reading the publication record, and
`/health` is the liveness probe whose entire design is that it does no I/O (a
busy pod that fails it gets *restarted* by the thing meant to protect it).
`/health` carries the in-memory half, `loaded`, alone. Every
`/search` response carries the same id, so a retrieval diagnosis can tell "the
document is not indexed" from "this replica has not picked up the index that
has it". Two metrics go with it: `pheasant_graph_reloads_total{trigger}`
(`event` or `poll` — a region where every reload is `poll` is one whose
announcements are not arriving) and `pheasant_graph_generation_age_seconds`.

API readiness includes an authenticated graph-service probe; a broken
dependency removes that API replica from routing instead of silently serving
stale graph results.

### Scale on the backlog, not on CPU

CPU is a lagging signal for the worker tier. Source queue depth rises before a
source is claimed, then falls to zero while one large source may still have
thousands of files to prepare. Scale from both signals.

```promql
(max(pheasant_index_queue_depth) or vector(0))
  + (max(pheasant_index_inflight) or vector(0))
  + ceil((max(pheasant_index_preparation_backlog) or vector(0)) / 500)
```

The in-flight term is required for scale-to-zero. Pending depth drops as soon
as an indexer claims a source; without the claimed-work signal the scaler can
remove every worker before preparation starts and strand an otherwise healthy
task until redelivery.

`deploy/kubernetes/scaled/worker-hpa.yaml` ships both a KEDA `ScaledObject`
(preferred — reads Prometheus directly and can scale to **zero**, which
matters because idle workers are pure cost) and a plain HPA for clusters
without it.

The API tier scales on CPU with a five-minute scale-down window to avoid churn
under bursty assistant fanout. It keeps only a bounded graph proxy. The graph
tier owns snapshot residency; scale it independently on graph-query latency or
CPU, and give every graph replica enough memory for the old and new snapshot
during an atomic refresh.

## The ceiling, and knowing when you have reached it

Scale workers, not indexers — extra indexers for one shard are elected hot
standbys, because the graph, the vectors and the graph FTS are one coordinated
commit stream. That is a real consequence of a globally consistent graph, and
it is fine right up until a team scales workers, watches ingest stop
improving, and has nothing to tell them which of the two problems they have:
the commit authority is full, or retrieval is mistuned. Those look identical
from outside — the queue drains more slowly than work arrives, and adding
workers changes nothing.

So the ceiling is a number:

```promql
pheasant_commit_authority_saturation   # 0..1, five-minute rolling window
```

The fraction of the window the sole commit authority spent indexing. Sustained
**above 0.8** means more workers will not help and the region should be split
(`pheasant shard plan`). Below it, a slow queue is a tuning problem — take it
to [retrieval tuning](../retrieval-tuning.md), not to the scaler.

Three things it deliberately is not. It is not queue depth
(`pheasant_index_queue_depth` already says work is waiting; this says *why*: a
deep queue behind an idle indexer is a claim problem, behind a saturated one it
is the ceiling). It is not an average since boot, or a region that indexed hard
this morning would report itself full all afternoon. And it publishes nothing
at all before it has seen a minute of wall time, because two busy seconds in a
pod's first four are not 50% saturation, and sharding a region that is doing
nothing is the most expensive possible response to a misread number.

`pheasant scan` warns before you get there, from the same measured
coefficients: a corpus whose first index would take more than twelve hours on
one indexer is one to plan differently, and it says so.

### Splitting a region

`pheasant shard plan` packs whole sources into regions —
[capacity planning](capacity-planning.md) covers why whole sources rather than
an even split. `--emit` turns the proposal into files:

```bash
pheasant shard plan --shards 2 --emit ./regions
```

For each region: a `pheasant.yaml` carrying its own knowledge-base id and only
its own sources (with retrieval settings copied verbatim — a split must not
quietly become a second product), a `docker-compose.yml` with its own project
name, volume names and port, and a `.env.example` with secret stubs. Plus a
`README.md` saying what the emission did **not** do, which is the part that
matters: no data moves, no router is configured, and each region indexes its
own sources from scratch. It refuses to overwrite existing files, so a re-run
against an edited region is a diff rather than a loss.

## Related

- [Speed up indexing](indexing-performance.md) — worker counts and executors.
- [Monitor indexing](monitor-indexing.md) — throughput, ETA, stall detection.
- [Capacity planning](capacity-planning.md) — when one region should become several.
- [Knowledge effectiveness](../knowledge-effectiveness.md) — the read-side
  evaluation plane. It claims the `__evaluation__` lease so N replicas produce
  one run, never takes `sync_lock`, and only auto-triggers where the scheduler
  runs (`all`, `indexer`) — an `api` replica must not spend its budget replaying
  cohorts.
