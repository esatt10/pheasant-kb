# How big can one region get?

> [!TIP]
> **pheasant-kb is the default deployment**
>
> For pheasant-kb, [Scale up or down](../pheasant-kb/scale.md) turns these
> measurements into the `.env` knob for each service. This page is the
> measurement behind it.

Every scaling decision in pheasant comes back to one number: **how many nodes
the knowledge graph has**. The graph is rows in PostgreSQL (`graph_nodes`,
`graph_edges`), so it sets the database's size and the indexer's working set.
A serving replica (api, graph service) holds none of it.

The numbers below are **measured, not modelled**: `python -m
pheasant.graph.capacity` builds a real graph with realistic attribute payloads
at each scale and reports what it cost. Re-run it on your own hardware; the
shape will hold, the constants will move.

## Measured cost

Node and edge counts come from the ratio a real corpus produces (~6.3 nodes
and ~6.3 edges per file, taken from a 2,132-file demo corpus at 13,503 nodes /
13,502 edges).

| Corpus | Nodes | Indexer RSS (resident graph) | Commit, one file changed | Graph rows in the database |
|---|---|---|---|---|
| 2,000 files | 12.6k | 34 MB | 1.0 ms | 23 MB |
| 20,000 files | 126k | 314 MB | 1.0 ms | 206 MB |
| 100,000 files | 630k | 1.5 GB | 1.1 ms | 1,033 MB |
| 250,000 files | 1.58M | 4.3 GB | — | — |

- **Commit cost is flat.** A commit writes the rows that changed, so it is the
  same millisecond at every size.
- **RSS is linear**, about 2.4 KB per node, and it is the indexer's. A process
  that builds the graph resident (`--role all`, and every enrichment pass)
  pays the column; pheasant-kb's indexer reads its working set lazily and
  releases clean cache between tasks, so treat the column as its ceiling. Do
  **not** extrapolate far past the top row: a linear projection to 250k files
  predicted 3.8 GB and the measured answer was 4.3 GB.
- **Disk is the cost**: about **1,640 bytes per node** in the database
  (measured 1,637 at 20k files and 1,639 at 100k). `pheasant scan` includes
  it.

## Measured against 1.x's single-file graph

Five repositories of 1,600 files (40,122 nodes / 112,111 edges / 88,000
cross-repository reference edges), PostgreSQL, the same corpus on both sides.
"Before" held the whole graph as one zstd file every replica loaded:

| | before | now | |
|---|---|---|---|
| graph publish per commit | 1.32 s | **0.005 s** | 270× faster |
| incremental sync, one file changed | 5.02 s | **3.55 s** | 1.4× faster |
| boot before a replica can answer | 1.08 s | **0.07 s** | 17× faster |
| RSS for a replica that answers graph queries | 131 MB | **~0** | it holds none |
| hybrid p95 | 173.3 ms | **77.0 ms** | 2.3× faster |
| hybrid throughput, 4 threads | 18.6 qps | **20.8 qps** | 1.12× faster |
| hybrid p50 | 60.4 ms | 61.5 ms | unchanged |
| text search p50 | 46.4 ms | 47.5 ms | unchanged |
| cross-source resolution | 2.75 s | 2.75 s | unchanged |
| graph arm p50 | 45.7 ms | 59.9 ms | 1.31× slower |
| 3-hop walk from a hub (1,620 edges) | 2.3 ms | 7.6 ms | 3.3× slower |
| bounded slice | 4.8 ms | 9.8 ms | 2.0× slower |
| five full indexes | 65.4 s | 69.7 s | 1.07× slower |
| stored bytes (database + `/state`) | 141 MB | 240 MB | 1.7× larger |

Read it as a trade: commit cost and residency, the two things that decided
when a region had to be sharded, moved by orders of magnitude. Graph *reads*
pay, because reading a graph you do not hold means reconstructing what you
read. The p50/p95 split reverses: the median hybrid query is a wash while the
tail is 2.3× faster, because the old tail *was* the graph it was holding.

**Sizing replicas.** Per-process throughput is set by Python, not by the
database, so a replica saturates a core before it saturates Postgres. Measured
as independent processes against one Postgres on a four-core box: 13.1 → 25.6
→ 32.1 → 32.0 qps at 1/2/4/8 processes, stopping where the cores do. Budget
roughly **one core per ~13 qps, and no graph residency per replica**.

## Sizing the indexer

| Corpus | Give the indexer | Notes |
|---|---|---|
| < 5,000 files | 512 MB, 1 CPU | |
| 5,000–25,000 | 1 GB, 2 CPU | |
| 25,000–75,000 | 2 GB, 2–4 CPU | |
| 75,000–150,000 | 4 GB, 4 CPU | |
| 150,000–240,000 | **6 GB, 4 CPU** | pheasant-kb's default (`PHEASANT_INDEXER_MEMORY=6144m`). |
| 240,000–500,000 | 8–16 GB | Fine in the cloud; `graph.max_nodes` warns. |
| **> 500,000** | **Shard** | |

These are per **region**, and linear. Sharding is about operational sanity
(parallel indexing, independent re-indexes, blast radius) more than about a
hard wall.

pheasant warns once per sync when the graph passes `graph.max_nodes` (default
**1.5M nodes**, ≈240,000 files, the point at which a 6 GB indexer is genuinely
full). It is a notice, not a refusal: by the time it can fire the index
already exists. Raise it *and* the indexer's memory together, or shard.

```yaml
graph:
  max_nodes: 1500000   # null disables the warning
```

## How long the first index takes

Measured by `python -m pheasant.sync.benchmark --mode capacity`, one worker,
embeddings off, on uniform Markdown:

| Corpus | Time | Files/s |
|---|---|---|
| 500 files | 1.4 s | 356 |
| 1,000 | 2.9 s | 345 |
| 2,000 | 5.8 s | 344 |
| 4,000 | 12.3 s | 326 |
| 8,000 | 26.2 s | 305 |

**Linear**: each doubling of the corpus roughly doubles the time. So
`files ÷ 300` is a usable first estimate in seconds, and `pheasant scan` does
that arithmetic for you.

**Disk** tracks *content*, not file count. `pheasant scan` uses ~4.0 bytes of
storage per corpus byte for chunks and the full-text index (measured on 1.x's
SQLite store, where it was flat to within 3% across the sweep) plus the graph
rows above. Embeddings add `chunks × dimensions × 4` bytes under `/state`.

**Embedding time is not projected.** A network embedder's throughput is the
provider's rate limit, not pheasant's: one run measured ~20 files/min against
a real endpoint, two orders of magnitude below the offline figures above.
Benchmark your own, and note that the [worker fleet](worker-fleet.md) does not
help here: embedding is a network wait, not CPU.

## Checkpoints

A sync commits as it goes, so a crash loses at most the work since the last
commit. The interval is derived from Young's formula (`T = sqrt(2 x C x MTBF)`),
which minimizes the *sum* of checkpoint overhead and work redone after a crash.
With commits at about a millisecond the interval is short at every size, and
`storage.graph_checkpoint_max_seconds` (30 min) caps it. Lower
`checkpoint_mtbf_seconds` on spot or preemptible capacity, where interruptions
are more frequent.

```yaml
storage:
  checkpoint_mtbf_seconds: 86400      # expected time between interruptions
  graph_checkpoint_max_seconds: 1800  # ceiling on lost work
```

## The graph service

```yaml
graph:
  query_service_url: http://graph:8765
  query_service_token_env: PHEASANT_GRAPH_SERVICE_TOKEN
  query_service_timeout_seconds: 30
```

With it set, api replicas send graph reads to the `graph` tier. It is
**optional**: an api replica holds no graph either way, because it can query
`graph_nodes` directly. Keep it (pheasant-kb does) when you want graph reads on
their own connection pool and their own scaling curve, not to save memory. It
does **not** make enrichment or commits faster: those stay in the single
commit authority. There is no local fallback when it is down, so an outage of
the graph tier is a clear failure rather than every replica reading the graph
at once.

## When you cross the line: shard

Split the corpus across several pheasant regions. Each region is a whole
pheasant-kb, a separate knowledge base with its own database and `/state`, and
a query is answered within one region, so:

* memory and checkpoint cost divide by the number of shards, and both are the
  constraints above;
* regions index **in parallel**, so wall-clock time to first index divides too;
* a region can be re-indexed or replaced without touching the others.

Shard along a boundary that matches how people search — per repository, per
team, per document collection — rather than by hashing paths. Retrieval quality
depends on related content living in the same graph: splitting one repository
across two regions breaks the cross-source edges that make
`get_graph_neighbors` useful, while splitting *between* repositories costs
nothing, because those edges were never going to exist.

Sizing a sharded corpus is the single-region table divided by the shard count.
600,000 files across 6 regions is six indexers of the 100,000-file row, about
1.5 GB each, indexing in parallel, instead of one 10 GB indexer indexing
serially.

### Let pheasant propose the split

```bash
pheasant shard plan -c pheasant.yaml            # fewest regions that fit
pheasant shard plan -c pheasant.yaml --shards 4 # exactly four
```

It scans each source (a directory walk, not an index), packs whole sources
largest-first, and prints which sources go where with a memory request per
region:

```text
600,000 files across 3 region(s) (~3,780,000 graph nodes)

  shard-1: 220,000 files, ~1,386,000 nodes, ~5.5 GB RSS -> request 8Gi
      - platform-monorepo
  shard-2: 200,000 files, ~1,260,000 nodes, ~5.0 GB RSS -> request 8Gi
      - docs-site
      - runbooks
```

The count defaults to the fewest regions that keep every shard under
`graph.max_nodes`, so the planner and the runtime warning agree about what "too
big" means. A single source over budget on its own is reported rather than
split — no arrangement of whole sources fixes that, and the honest answers are
more memory for that region or a narrower `include`/`exclude`.

### One commit authority per knowledge-base shard

The graph's enrichment passes (cross-source references, OKF bundles, record
bridging) read and rewrite edges across sources, and the published generation
id folds every row. Several indexers committing different sources from
different views of the graph would overwrite one another's edges even though
the relational rows are source-scoped. Pheasant therefore
elects one indexer to own watcher, scheduler, queue drain and graph commits per
shard. Extra indexers are hot standbys. Parallelism stays in multi-source
preparation inside that authority and in the stateless worker tier.

That ceiling used to be prose, which made it useless at the moment it mattered:
a team scales workers, watches ingest stop improving, and cannot tell "the
commit authority is full" from "retrieval is mistuned" — both look like a queue
draining more slowly than work arrives, with more workers changing nothing.
It is now a gauge:

```promql
pheasant_commit_authority_saturation   # 0..1, five-minute rolling window
```

Sustained **above 0.8**, more workers will not help and the region should be
split. Below it, a slow queue is a tuning problem. It publishes nothing at all
before it has seen a minute of wall time — two busy seconds in a pod's first
four are not 50% saturation, and sharding a region that is doing nothing is
the most expensive possible response to a misread number — and it is absent
entirely on a process that is not the commit authority, rather than reporting
a confident zero from an api replica that could not saturate if it tried.

`pheasant scan` warns before you reach it, from the same measured
seconds-per-1k-files the rest of this page uses: a corpus whose first index
would take more than twelve hours on one indexer is one to plan differently,
and the report says so beside the memory numbers.

### Turning the proposal into files

`pheasant shard plan --emit ./regions` writes what the plan implies: per
region, a `pheasant.yaml` with its own knowledge-base id and only its own
sources, a `docker-compose.yml` with its own project name, its own PostgreSQL,
volume names and port, and a `.env.example` with secret stubs. Each region is
one process (`--role all`) with the queue, graph service and workers turned off
in its copy of the config; retrieval settings are copied unchanged. A region
that outgrows one process is a copy of pheasant-kb's `compose.yaml`. Acting on a split used to mean
hand-copying all of that and getting every name different from the first —
with a silent failure at the end of it, because two regions sharing
`pheasant.name` share a knowledge-base id, and every stable ID in this system
starts with that.

It applies nothing and moves no data: each region indexes its own sources from
scratch, and the emitted `README.md` says so. It also refuses to overwrite
existing files, so re-running against an edited region is a diff rather than a
loss.

## Ask pheasant instead of reading the table

`pheasant scan` walks a source without reading it, so every number above can
be projected **before** you commit to a first index — which is the moment it
is useful, rather than after an OOM kill:

```console
$ pheasant scan -c pheasant.yaml
platform-monorepo (/data/platform)
  would index 6000 files, 27.93 MB (scanned 6003 entries, pruned 0 directories)
  largest subtrees: services (13 MB), web (9 MB), docs (4 MB)
  within configured limits — sync would proceed
  projected: ~37,800 nodes, ~0.1 GB RAM, ~0.1 GB in /state, ~19.8s to index
  suggested container memory: 0.5Gi
```

The projection reflects *your* config — enabling embeddings adds the vector
store to the disk figure — and warns when a corpus needs sharding rather than
a bigger indexer. `--json` includes it as a `projection` object.

## Sizing the evaluation plane

`evaluation` scales on a **different axis** from the corpus: cohort size times
the ablation matrix, not file count. A region with a million files and forty
recorded queries has a large index and a trivial evaluation, and the reverse is
equally possible — so one "how big should this be" number would describe
neither, and `scan` reports it separately when evaluation is enabled:

```console
evaluation plane (region-wide)
  at full cohorts (200 queries x 6 variants x 6 cohorts): 7,200 replays/run, ~1.0 min
  storage: ~47 MB per run (~17.1 GB/yr at the configured cadence), 5.8 MB of replay
           checkpoints in flight
  suggested container memory for a batch: 0.5Gi
```

Three numbers, three different questions:

| Number | What it is | What it decides |
|---|---|---|
| **replays/run, minutes** | `queries × variants × cohorts` real searches | Whether a batch fits `maximum_runtime_seconds` |
| **MB per run / GB per year** | Metric rows and reports, which accumulate | How big the `/state` volume must be |
| **checkpoint MB in flight** | Replay checkpoints, deleted when the run completes | How much the volume must have **free** during a run |

The steady-state figure is an **upper bound**: per-query metric rows exist only
for queries carrying positive proof, so a region where a quarter of queries are
evidenced — the ordinary case — uses roughly a quarter of it. The bound points
that way deliberately; over-provisioning a volume is cheap and running out
mid-run is not.

### Levers, in order of effect

1. `evaluation.cohorts.maximum_queries_per_cohort` — linear in everything.
2. `evaluation.maximum_stored_per_query_results` — the per-query audit rows are
   most of the steady state.
3. `evaluation.minimum_interval_seconds`, or leaving `on_material_snapshot`
   off — a region indexing hourly will otherwise evaluate hourly.
4. Trimming `evaluation.variants` — `B0` is not removable, because every
   attribution number is a paired difference against it.

Adding workers is **not** a lever:
[replay is deliberately not distributed](../knowledge-effectiveness.md#why-replay-is-not-fanned-out-over-the-worker-transport).
Past the point where a batch will not fit a budget worth having, the answer is
`pheasant shard plan` — each shard then evaluates itself.

### Measure it here

```bash
python -m pheasant.evaluation.benchmark --output evaluation-capacity.json
```

Runs a real batch through the real search path and prints what it measured
beside what the model projected. CI runs it on every change to the plane and
publishes the comparison as a job summary — because a model nobody checks
against a machine is a model that quietly stops describing anything. The first
two coefficients shipped here were out by 2x (time) and 3x (peak volume), and
this is how that was found.

## Sizing a fleet

pheasant-kb is the shape: an api tier, a graph service, one indexer, gRPC
preparation workers and a logger ([Scale up or down](../pheasant-kb/scale.md)).
What the file count predicts:

| Corpus | Shards | Indexers | Graph services | API replicas | Workers | Graph/index memory each |
|---|---|---|---|---|---|---|
| < 25,000 | 1 | 1 | optional | 1 | 0–4 | 0.5–1 Gi |
| 75,000 | 1 | 1 | 1 | 2 | 3 | 1 Gi |
| 150,000 | 1 | 1 | 1 | 2 | 6 | 2 Gi |
| 250,000 | 2 | 2 | 2 | 2 | 16 | 4 Gi |
| 600,000 | 3 | 3 | 3 | 3 | 24 | 8 Gi |

Two of those columns are honest guesses and it is worth saying which:
**api replicas** track request traffic, which a file count cannot predict —
two is a floor so a rollout is not an outage. **Workers** are one per ~25,000
files per shard, capped at 8, because the 2026-08-13 benchmark measured 8 file
workers buying only 1.113x on a commit-dominated fixture; the tier helps when
parsing is expensive (PDFs, large documents), not uniformly.

Only **shards** and **memory** come straight from measurement.

Keep the scaling scopes separate:

| Constraint observed | Scale | Do not scale |
|---|---|---|
| HTTP/MCP request saturation | API replicas | indexers |
| Graph-query latency/CPU | Graph replicas, preferably across nodes | workers |
| Parse/extraction backlog | Stateless workers | graph replicas |
| Graph save, enrichment, or ordered commit | Whole knowledge-base shards | indexer replicas within one shard |
| Embedding 429s/provider latency | Provider quota or lower embedding concurrency | preparation workers |

Docker Compose shares one host, so extra replicas compete for the same CPU
and database; the shipped default stays at one graph service, one active
indexer and four workers. Container Apps scales the api, graph and worker apps
independently, but one environment should still describe one shard. Give each
shard its own name, database, NATS stream and subject, `/state` share and
tokens, rather than sharing a writable graph.

## Request concurrency and the thread pool

The table above sizes **api replicas** for traffic, but not the budget those
replicas actually enforce it with. `server.api.max_concurrent_requests`
(`docs/configuration.md`'s "Serving durability" section) bounds how many
requests the shedding middleware admits at once before answering 429. Every
sync HTTP route in this process — which is most of them — and every MCP tool
call made against the `/mcp` mount in the same process run on a *separate*
budget: anyio's shared worker-thread pool, 40 tokens by default. On startup,
if `max_concurrent_requests` is set, this process raises that pool's token
count to at least match it, so admitting a request the limiter is happy with
never means it silently queues for a thread instead. Size
`max_concurrent_requests` for HTTP *and* MCP traffic combined, not HTTP
alone, since both draw from the one pool. `GET /metrics`'s
`pheasant_threadpool_tokens_total` / `_tokens_available` show the pool's
current headroom next to `pheasant_requests_inflight`.

This is a fixed invariant, not a measured coefficient like the numbers
above it — it does not live in `src/pheasant/capacity.py`.

## Measure your own

```bash
python -m pheasant.sync.benchmark --mode capacity --sizes 500,2000,8000
python -m pheasant.graph.capacity --files 2000,20000,100000
python -m pheasant.sync.benchmark --workers 1,2,4,8
```

The first is what produced the tables above; it prints its own coefficients
beside the measurements so drift is visible. Its fixture is uniform markdown,
which is the caveat that matters most: a real corpus of PDFs or code moves
seconds-per-file a long way and nodes-per-file hardly at all. That is also why
the model's `nodes_per_file` comes from a **live** 2,132-file corpus (6.3) and
not from the synthetic sweep (3.0) — the fixture has no links, headings or
symbols, so adopting its figure would have halved every memory projection.

## Related

- [Monitor indexing](monitor-indexing.md) — throughput, ETA and stall detection.
- [Speed up indexing](indexing-performance.md) — worker counts and executors.
- [Separate graph queries](graph-query-service.md) — setup, failure semantics,
  and the measured 2026-08-26 fleet decision.
- [Knowledge effectiveness](../knowledge-effectiveness.md) — the evaluation
  plane's own sizing, restart semantics and progress surfaces.
