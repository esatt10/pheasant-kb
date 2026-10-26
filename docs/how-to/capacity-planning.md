# How big can one region get?

!!! tip "pheasant-kb is the default deployment"
    For pheasant-kb, [Scale up or down](../pheasant-kb/scale.md) turns these
    measurements into the `.env` knob for each service. This page is the
    measurement behind it.
    Start with [Run pheasant-kb](../pheasant-kb/index.md).

Every scaling decision in pheasant comes back to one number: **how many nodes
the knowledge graph has**. The graph is held in memory during a sync and
written out as one compressed blob, so it sets the container's RAM, the time
each checkpoint costs, and how long a restart takes before it can serve.

The numbers below are **measured, not modelled** — `python -m
pheasant.graph.capacity` builds a real `SimpleMultiDiGraph` with realistic
attribute payloads at each scale and reports what it cost. Re-run it on your
own hardware; the shape will hold, the constants will move.

## Measured cost

Four scale points, on one container. Node and edge counts come from the ratio a
real corpus produces (~6.3 nodes and ~6.3 edges per file, taken from the live
2,132-file demo corpus at 13,503 nodes / 13,502 edges).

| Corpus | Nodes | Process RSS | Checkpoint write | Restart load | Edge scan |
|---|---|---|---|---|---|
| 2,000 files | 12.6k | **34 MB** | 0.13 s | 0.10 s | 1.8 ms |
| 20,000 files | 126k | **314 MB** | 1.3 s | 0.9 s | 16 ms |
| 100,000 files | 630k | **1.5 GB** | 6.7 s | 4.6 s | 95 ms |
| 250,000 files | 1.58M | **4.3 GB** | 19.9 s | 11.2 s | 240 ms |

Cost per node is flat across all four (~2.4 KB of RSS), so this is linear and
you can interpolate. Do **not** extrapolate far past the top row: a linear
projection to 250k files predicted 3.8 GB and the measured answer was 4.3 GB,
12% high.

**The table above is `storage.graph_format: node_link_json`, and it is no
longer the default.** On `rows` (the default since 35.10) the graph lives in
the state database, so those three costs land differently:

| Corpus | Nodes | RSS to *serve* | RSS to *index* | Commit | Database |
|---|---|---|---|---|---|
| 2,000 files | 12.6k | **0** | 34 MB | 1.0 ms | 23 MB |
| 20,000 files | 126k | **0** | 314 MB | 1.0 ms | 206 MB |
| 100,000 files | 630k | **0** | 1.5 GB | 1.1 ms | 1,033 MB |

Two things move. The commit stops growing with the graph — it is the same
millisecond at every size, because it writes only what changed — and the
residency is paid by the process that *builds* the graph rather than by every
process that reads one. An api replica on `rows` holds no graph at all, so the
"Give the container" table below applies to your indexer; serving replicas size
from request traffic.

What you pay for it is disk: about **1,640 bytes per node** in the database
(measured 1,637 at 20k files and 1,639 at 100k), against a compressed file
roughly 55× smaller. `pheasant scan` includes it.

## Measured against the pre-row-backend baseline

An 8,000-file corpus (24,443 nodes / 72,441 edges), the same corpus driven
through both versions:

| | before | after | |
|---|---|---|---|
| graph publish per commit | 0.82 s | **0.0007 s** | 1,231× faster |
| incremental sync, one file changed | 4.36 s | **3.32 s** | 1.3× faster |
| RSS for a replica that answers graph queries | 142 MB | **29 MB** | 4.9× smaller |
| text search p50 | 49.6 ms | 47.4 ms | unchanged |
| graph arm p50 | 34.7 ms | 55.9 ms | 1.6× slower |
| 3-hop walk, ordinary node | 0.24 ms | 1.7 ms | 7× slower |
| 3-hop walk from a hub (8,040 edges) | 12.6 ms | 46 ms | 3.7× slower |
| full index | 25.9 s | 27.7 s | 1.1× slower |
| `/state` on disk | 115 MB | 177 MB | 1.5× larger |

Read it as a trade, not a win: commit cost and residency — the two things that
decided when a region had to be sharded — moved by three orders of magnitude
and five times. Graph *read* latency got worse, because reading a graph you do
not hold means reconstructing what you read. Text search and an unchanged
re-sync are untouched.

## The same measurement on a fleet: Postgres, five repositories

The table above is one SQLite region with one source — the standalone shape.
The fleet shape is `storage.backend: postgres` with several repositories in one
region, so here it is: five repositories of 1,600 files (40,122 nodes / 112,111
edges / 88,000 cross-repository reference edges), same corpus and same Postgres
server on both sides.

| | before | after | |
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

The trade is the same shape it is on SQLite, which is the reason to run it —
none of it was a SQLite artifact. Two things only the fleet shape shows.
Cross-source resolution did **not** get more expensive with five repositories
linking to each other. And the p50/p95 split reverses: the median hybrid query
is a wash while the tail is 2.3× faster, because the file backend's tail *is*
the graph it is holding. A serving replica wants that direction.

Graph *reads* are still the side that pays — an arm and a walk that reconstruct
what they read cannot beat one that already holds it — but the gap is now
1.3–3.3× rather than the 1.8–5.9× the first row-backed release measured, and
serving throughput and tail latency are ahead rather than behind.

**Sizing replicas.** Per-process throughput is set by Python, not by the
backend, so a replica saturates a core before it saturates Postgres. Measured
as independent processes against one Postgres on a four-core box: before, 13.3
→ 26.0 → 36.0 → 36.4 qps at 1/2/4/8 processes; after, 13.1 → 25.6 → 32.1 →
32.0. Both stop where the cores do. So budget roughly the **same throughput per
core, and no graph residency per replica** — four row-backed replicas hold
nothing where four file-backed ones hold four copies of the graph (524 MB at
this size, 6.6 GB at 100k files). Adding a container is the axis; it is the
axis the file backend priced out of reach.

**One caveat, for standalone SQLite regions serving concurrent graph traffic.**
Hybrid throughput holds at one thread and falls about 3.7× at four. That is not
the row backend: concurrent SQLite reads do not scale in a containerised
filesystem — the same probe against a *pre-change* database reading `chunks`
fell from 486/s to 13/s across one to four threads. The change moved graph
reads onto that resource, so the graph paths inherit it. Postgres does not
share the problem (41 → 86 → 160 qps across one to eight threads), and the
fleet runs Postgres. If you serve heavily concurrent graph queries from a
single SQLite container, either put the `graph` role in front of it or keep
`storage.graph_format: node_link_json`.

## Sizing a region

| Corpus | Give the container | Notes |
|---|---|---|
| < 5,000 files | 512 MB, 1 CPU | |
| 5,000–25,000 | 1 GB, 2 CPU | |
| 25,000–75,000 | 2 GB, 2–4 CPU | |
| 75,000–150,000 | 4 GB, 4 CPU | |
| 150,000–240,000 | **6 GB, 4 CPU** | The shipped default. |
| 240,000–500,000 | 8–16 GB | Fine in the cloud; `graph.max_nodes` warns. |
| **> 500,000** | **Shard** | One container stops being sensible. |

These are per **region**, and linear — a cloud deployment can go well past the
bottom row on a single node. Sharding is about operational sanity (parallel
indexing, independent re-indexes, blast radius) more than about a hard wall.

!!! note "The shipped Kubernetes manifest allows 6 GB"

    `deploy/kubernetes/deployment.yaml` and the Helm chart limit memory to
    **6 Gi**, which covers roughly **350,000 files** on the table above. It was
    1 Gi (about 65,000 files) until 2026-08-16; a corpus past that was
    OOM-killed mid-sync and it presented as an unexplained restart rather than
    as a capacity problem. Raise it further for a cloud deployment — the
    numbers above are per-region and linear.

## How long the first index takes

Measured by `python -m pheasant.sync.benchmark --mode capacity`, one worker,
embeddings off, on uniform markdown:

| Corpus | Time | Files/s | /state |
|---|---|---|---|
| 500 files | 1.4 s | 356 | 3 MB |
| 1,000 | 2.9 s | 345 | 5 MB |
| 2,000 | 5.8 s | 344 | 11 MB |
| 4,000 | 12.3 s | 326 | 87 MB |
| 8,000 | 26.2 s | 305 | 173 MB |

**Linear** — each doubling of the corpus roughly doubles the time (measured
2.06x, 2.01x, 2.12x, 2.13x). So `files ÷ 300` is a usable first estimate in
seconds, and `pheasant scan` does that arithmetic for you.

!!! warning "This was superlinear until 2026-08-16"

    The same sweep on the previous release went 1.9 s → 164.7 s over the same
    range — **tripling** for every doubling, an ~O(n^1.7) curve that
    extrapolated to about 16 hours at 250,000 files.

    The cause: `chunks_fts.artifact_id` is an UNINDEXED FTS5 column, so the
    per-artifact `DELETE FROM chunks_fts WHERE artifact_id=?` was a full scan
    of a table growing with the corpus — and on a *full* sync it was scanning
    to delete nothing, because the source had already been emptied before the
    loop. Skipping a question whose answer is already known made an
    8,000-file index **6.3x** faster.

    If you have an older recorded timing, discard it. A per-file figure taken
    from a small corpus and multiplied up was extrapolating a curve as though
    it were a line.

    **SQLite only.** The Postgres backend never had this: `chunks_fts` there
    is an ordinary table with `idx_chunks_fts_artifact`, so the delete was
    always indexed. If you were already on Postgres, the times above are the
    ones you had.

**Disk** tracks *content*, not file count: measured at **~4.0 bytes of
`/state` per corpus byte**, flat to within 3% across the sweep, and dominated
by SQLite storing every chunk's text again plus its FTS index. Embeddings add
`chunks × dimensions × 4` bytes on top.

**Embedding time is not projected.** A network embedder's throughput is the
provider's rate limit, not pheasant's — the 2026-08-12 vscode run measured
~20 files/min against a real endpoint, two orders of magnitude below the
offline figures above, and that number describes the provider. Benchmark your
own, and note that the [worker fleet](worker-fleet.md) does not help here:
embedding is a network wait, not CPU.

## Checkpoint cost, and why it is no longer the ceiling

On the default `storage.graph_format: rows` this section is largely historical:
a checkpoint writes the changed rows and costs about a millisecond whatever the
graph weighs, so the interval below is spacing out something that is no longer
expensive. Leave the settings alone; they cost nothing and they are what
`node_link_json` still needs.

On `node_link_json` a checkpoint serializes the entire graph, so its cost grows
with the index. The
interval is **derived from that cost**, not fixed — pheasant uses Young's
formula (`T = sqrt(2 x C x MTBF)`), the standard HPC/ML-training result, which
minimizes the *sum* of checkpoint overhead and work redone after a crash rather
than either alone:

| Corpus | Checkpoint cost | Interval | Overhead | Worst-case rework |
|---|---|---|---|---|
| 2,000 files | 0.13 s | 150 s | 0.1% | 2.5 min |
| 20,000 files | 1.3 s | 8 min | 0.3% | 8 min |
| 100,000 files | 6.7 s | 18 min | 0.6% | 18 min |
| 250,000 files | 19.9 s | 31 min | 1.1% | 31 min |

Overhead stays around 1% at every size. The trade is bounded rework — at most
one interval of indexing is lost to a crash — capped by
`storage.graph_checkpoint_max_seconds` (30 min).

```yaml
storage:
  checkpoint_mtbf_seconds: 86400      # expected time between interruptions
  graph_checkpoint_max_seconds: 1800  # ceiling on lost work
```

Lower `checkpoint_mtbf_seconds` on spot or preemptible instances: interruptions
are more frequent there, and the formula responds by checkpointing more often —
which is exactly when that pays for itself.

So checkpoint cost is **not** what limits one region. RAM is, and it grows
linearly — the table at the top is the guide.

pheasant warns once per sync when the graph passes `graph.max_nodes` (default
**1.5M nodes**, ≈240,000 files — the point at which the shipped 6 Gi container
is genuinely full). It is a notice, not a refusal: by the time it can fire the
index already exists, and discarding it would help nobody. Raise it *and* the
container limit together, or shard.

```yaml
graph:
  max_nodes: 1500000   # null disables the warning
```

## When to separate graph queries from API replicas

The fleet profile can put the resident graph behind an authenticated internal
service:

```yaml
graph:
  query_service_url: http://graph:8765
  query_service_token_env: PHEASANT_GRAPH_SERVICE_TOKEN
  query_service_timeout_seconds: 30
```

The `graph` role loads and refreshes the snapshot; API/MCP replicas keep only a
bounded proxy.

On `storage.graph_format: rows` this is **optional**: an API replica already
holds no graph, because it queries `graph_nodes` directly. The residency
arguments below are the `node_link_json` case. Keep the graph service on `rows`
when you want graph reads on their own connection pool and their own scaling
curve — not to save memory, which it no longer does.

On `node_link_json` it is worth the extra network hop and service when either:

* API graph residency is regularly above roughly **60-70% of a 3 GiB limit**;
* two or more API replicas would otherwise duplicate a large graph; or
* graph-query CPU/latency needs to scale independently from text/vector/API
  traffic.

It does **not** make graph enrichment or graph commits faster: those remain in
the single commit authority. If save/enrichment dominates an indexing run,
split sources into multiple knowledge-base shards below so each indexer and
graph service owns a smaller snapshot. For one API below the memory threshold,
the default local graph is simpler, uses less total fleet memory, and avoids the
HTTP hop. There is no automatic local fallback in remote mode because an outage
must not cause every API replica to materialize the graph simultaneously.

## When you cross the line: shard

Split the corpus across several pheasant regions and let the
[Synapse router](attach-to-synapse.md) fan out across them. Each region is an
ordinary container with its own `/state`, so:

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

Sizing a fleet is the single-region table divided by the shard count. 600,000
files across 6 regions is six containers of the 100,000-file row — 1.5 GB and a
6.7 s checkpoint each — indexing in parallel, instead of one 10 GB container
indexing serially.

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

Postgres allows API replicas and durable coordination, but the persisted graph
is one whole-file snapshot and its node FTS is one global projection. Several
indexers committing different sources from stale graph snapshots can overwrite
one another even when relational rows are source-scoped. Pheasant therefore
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
sources, a `docker-compose.yml` with its own project name, volume names and
port, and a `.env.example` with secret stubs. Acting on a split used to mean
hand-copying all of that and getting every name different from the first —
with a silent failure at the end of it, because two regions sharing
`pheasant.name` share a knowledge-base id, and every stable ID in this system
starts with that.

It applies nothing and moves no data: each region indexes its own sources from
scratch, and the emitted `README.md` says so. It also refuses to overwrite
existing files, so re-running against an edited region is a diff rather than a
loss.

## Which storage backend

Independent of the graph, and a different question:

| | SQLite (default) | Postgres |
|---|---|---|
| Setup | none | a database to run |
| Writers | **one process per knowledge base** | **one elected indexer per shard**, with standby failover |
| Read replicas | one container | many |
| Ranking | BM25 | see [configuration](../configuration.md#where-state-lives-storagebackend) |

Use SQLite unless you need several replicas serving one knowledge base,
database-backed leader election, or a durable broker-backed fleet. It is not
slower for a single container, and it is one fewer thing to operate. Postgres
does **not** change the graph numbers above, because the graph is held in memory
either way.

## Ask pheasant instead of reading the table

`pheasant scan` walks a source without reading it, so every number above can
be projected **before** you commit to a first index — which is the moment it
is useful, rather than after an OOM kill:

```console
$ pheasant scan -c pheasant.yaml
platform-monorepo (/workspace/platform)
  would index 6000 files, 27.93 MB (scanned 6003 entries, pruned 0 directories)
  largest subtrees: services (13 MB), web (9 MB), docs (4 MB)
  within configured limits — sync would proceed
  projected: ~37,800 nodes, ~0.1 GB RAM, ~0.1 GB in /state, ~19.8s to index
  suggested container memory: 0.5Gi
```

The projection reflects *your* config — enabling embeddings adds the vector
store to the disk figure — and warns when a corpus needs sharding rather than
a bigger container. `--json` includes it as a `projection` object.

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

Past one container, the shape is
[four tiers](worker-fleet.md#running-the-whole-fleet). What the file count
predicts:

| Corpus | Shards | Indexers | Graph services | API replicas | Workers | Graph/index memory each |
|---|---|---|---|---|---|---|
| < 25,000 | 1 | 1 | — local | — single container | 0 | 0.5–1 Gi |
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

Docker Compose shares one host, so extra replicas can compete for the same CPU
and database; the shipped default stays at one graph service, one active
indexer, and four workers. Kubernetes makes API, graph, and worker scaling
independent, but one namespace/release should still describe one shard. Give
each shard its own name, database scope, NATS durable/subject, RWX state, and
tokens, then fan retrieval across shards rather than sharing a writable graph.

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
- [Attach to a Synapse fleet](attach-to-synapse.md) — running several regions.
- [Knowledge effectiveness](../knowledge-effectiveness.md) — the evaluation
  plane's own sizing, restart semantics and progress surfaces.
