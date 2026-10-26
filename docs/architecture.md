# Architecture

pheasant is a local-first MCP context server with an admin API, source
registry, deterministic indexing pipeline, and text, vector, and graph search.
The single-container topology is the default. The role-split fleet is the
scale-out topology for a corpus or request load that has crossed the measured
single-process limits.

## Runtime flow

1. Load and validate `/config/pheasant.yaml`.
2. Register sources and repair missing search or graph state.
3. Discover files in a stable order and skip unchanged content by hash.
4. Prepare changed files concurrently: read, parse, extract, and chunk.
5. Batch embeddings, then commit relational rows, vectors, manifests, and
   graph mutations through one ordered writer.
6. Enrich and atomically publish the shard's graph snapshot.
7. Serve API, MCP, UI, and retrieval traffic from the published state.

No indexed repository code is executed. Stable IDs, content hashes, ordered
commits, and sorted graph serialization make retries and unchanged
incremental runs deterministic.

## Logical components

| Component | Responsibility |
|---|---|
| API/MCP/UI | Transport adapters. Parse a request, call one application service, marshal the answer. |
| Application services | One implementation per operation, shared by HTTP and MCP: retrieval criteria, over-fetch, memory policy, metrics and refusal text. |
| Source registry | Configured and runtime source metadata, lifecycle state, and audit history. |
| Indexer | Watch, schedule, drain durable work, coordinate preparation, and make the one authoritative commit per knowledge-base shard. |
| Preparation worker | Stateless read/parse/extract/chunk work over authenticated HTTP or gRPC. It has no database or connector credentials. |
| Graph-query service | Read-only graph search/traversal. Optional on `storage.graph_format: rows`, where any replica queries the graph tables directly; on `node_link_json` it is the one process that keeps the snapshot resident so fleet APIs need only a bounded proxy. |
| PostgreSQL/SQLite | Artifacts, chunks, lexical ranking, the knowledge graph, manifests, leases, queue state, and metadata. |
| Vector store | Optional semantic vectors; NumPy and LanceDB are supported. |
| Log tier | Optional. Persists the interaction ledger, rolls expired rows to Parquet, and drops expired partitions. Exists so none of that runs on a process serving requests or holding the indexer's sync lock. |
| NATS JetStream | Durable source-task transport between API and indexer in the fleet. Carries the log tier's batches on a separate stream when observation is enabled. |
| Decisions API (optional) | OpenAI's typed-judgement endpoint, asked by the answering loop at question time only (`assistant.decisions`): is the evidence sufficient, which follow-up search, is the answer acceptable. Never on the indexing path. |

## Layering

```
transport (api, mcp_server, cli)
    ↓
services      one implementation per operation, transport-neutral
    ↓
domain        search, graph, memory, ingestion, sync
    ↓
persistence   state store, graph store, manifests
```

No upward edges. HTTP and MCP are the same API exposed twice, and that is a
property the suite holds rather than an intention the docs state: the two
surfaces had already drifted on four of five shared operations before the
services layer existed, and the divergence that mattered was invisible —
`relevant_files` applied the memory policy over HTTP and not over MCP, so the
surface built for agents could serve a record the region knew had been
corrected.

An operation therefore lives in `src/pheasant/services/` once and owns its
refusal text as well as its behaviour; `api/app.py` and
`mcp_server/tools.py` are adapters. Refusals are `ServiceError` subclasses of
`ValueError` carrying an HTTP status *hint*, so neither transport's error
vocabulary leaks into the layer both of them call.

Two tests hold the boundary. `tests/test_surface_conformance.py` drives the
same operations through both surfaces against one corpus and asserts identical
results and identical refusal text. `tests/test_service_layering.py` is an
import-graph test: nothing imports a layer above its own, `services/` imports
no transport, and every package declares its layer — `cli.py` and the
benchmarks are named composition roots, because constructing a transport is
what they are for.

## Retrieval decision

The four request modes are different views over three physical retrieval
arms:

| Mode | What it contributes | Cost characteristic |
|---|---|---|
| `text` | Exact identifiers and lexical matches from database full-text ranking. | Can become the slowest arm for common terms in PostgreSQL. |
| `vector` | Semantic matches when wording differs. | Embedding plus vector scan. |
| `graph` | Symbols, entities, dependencies, and related nodes. | Graph-service query in the fleet. |
| `hybrid` | Runs text, vector, and graph concurrently and merges them with reciprocal-rank fusion. | Approximately the slowest healthy arm, not their sum. |

The scalable assistant fanout is `vector`, `graph`, and `hybrid`. A separate
`text` fanout was removed because `hybrid` already executes the same lexical
query, and stress testing measured high-frequency PostgreSQL text ranking as
the dominant search bottleneck. Paying for that query twice had weak recall
value. Standalone text search is not disabled: callers can still request
`mode=text` for an exact-identifier probe, and every hybrid request still
contains the lexical arm.

The explicit vector and graph modes remain in the scalable assistant fanout.
They are comparatively cheap on the measured corpus and preserve their
arm-specific top results when hybrid's fused result limit would otherwise
truncate them. Local-small and local-advanced defaults are unchanged.

## Answering loop

The agentic workflow (`assistant/workflows/agentic.py`) is a LangGraph state
graph: classify → retrieve or plan → expand → grade → (loop) → synthesize →
verify → accept. Measured on the lab region, writing the answer was 94% of an
answer's time, and the earlier combined path wrote a whole answer in order to
learn whether the evidence was sufficient.

With an OpenAI key the loop's *judgements* go to the Decisions API instead of
a model-written grade (`assistant/workflows/decision_gates.py`, default
`assistant.decisions.mode: beta`):

| Gate | Replaces | Effect |
|---|---|---|
| `sufficiency` | the model grader / combined grade-and-answer | Nothing is written until the evidence is judged sufficient; a thin round costs a ~0.3 s judgement and a search. |
| `followup` | the grader's written `next_query` | A follow-up search is *chosen* from candidates the evidence names; "none" falls back to the model planner. |
| `accept` | nothing (new) | Judges the written answer against its cited passages, which lets the first draft be written at low reasoning effort and rewritten once, also at low effort, only when rejected. |
| `prune` (opt-in) | nothing (new) | Leaves passages judged irrelevant out of the answer's context. |

Every judgement that cannot be had falls back to the model-written path, and
the step says so; `mode: off` is the loop exactly as before. The same
judgements are exposed to agents doing their own retrieval as
`assess_evidence` (MCP) / `POST /assistant/assess` (HTTP). Thresholds are set
from labelled examples with `scripts/calibrate_decisions.py`. See
[configuration](configuration.md) for the measured effect.

## Persistence and consistency boundaries

- `/state` contains the state database (which holds the published graph on the
  default backend), graph snapshots, vector data, caches, and
  local state. `/exports` contains reproducible downstream exports.
- SQLite is the single-container backend. PostgreSQL is required when several
  pods serve one knowledge base.
- A knowledge-base shard has one elected indexer/commit authority. Extra
  indexers are failover, not write throughput.
- Graph-service replicas are interchangeable read-only copies of one complete
  shard. Individual edges are not hashed across replicas because paths and
  neighborhoods would become distributed transactions.
- True indexing scale-out uses whole-source knowledge-base shards, each with
  its own state, database scope, indexer, and graph snapshot. Synapse can fan
  retrieval across those shards.
- A remote graph failure makes API readiness fail. APIs never respond by
  materializing the whole graph and multiplying memory pressure during an
  outage.

## Deployment shapes and scaling scope

| Scope | API | Graph | Indexer | Workers | State boundary |
|---|---|---|---|---|---|
| Local/default | one all-role process | in process | in process | local threads/processes | one local `/state` |
| Docker Compose fleet | one published API endpoint by default | one replica by default | one leader; optional standby | four by default | PostgreSQL + NATS + shared volumes |
| Kubernetes shard | HPA on request CPU/latency | scale on graph latency/CPU and availability | StatefulSet, one active writer | KEDA/HPA on queue, in-flight, and preparation backlog | one namespace/release and RWX state per shard |
| Multi-shard | load-balanced/federated | at least one per shard | one authority per shard | independently elastic per shard | separate knowledge bases; Synapse fanout |

Keep the shipped worker, memory, CPU, queue, batching, and replica defaults
until measurements cross a boundary. Scale workers for preparation backlog,
API replicas for request traffic, and graph replicas for graph-query
saturation or availability. If graph save/enrichment or the single ordered
commit dominates, adding any of those replicas cannot help; shard whole
repositories or document collections instead.

## The observation plane

Optional and off by default. When enabled, every API and MCP call becomes a
row in an interaction ledger — never a file, never indexed, never returned by
a search — that [memory formation](memory-formation.md) reads to propose
memory candidates.

It scales on a fourth, independent axis, because observation volume tracks
request traffic rather than corpus churn. Two placements are load-bearing and
both are the opposite of the obvious one:

- **The request path appends to a bounded in-memory ring and nothing else.**
  Writing one ledger row per request puts a database write on the same
  PostgreSQL whose lexical ranking the retrieval section above names as the
  dominant measured search bottleneck. A flusher thread batches and publishes;
  a `--role logger` tier persists.
- **The hot-to-cold roll runs on that tier, never on the scheduler beat.**
  The beat holds `sync_lock` across all of its work, so a Parquet write over
  millions of rows there stalls incremental sync for every source in the
  region. In a single container the roll does run on the beat — bounded by
  `max_rows_per_pass`, which is what keeps it a slice rather than a stall.

The tier has its own table (`log_tasks`, never a `kind` column on
`index_tasks`), its own dead-letter, and its own scaling signal
(`pheasant_log_queue_depth`). Under saturation it drops observations rather
than slowing a request or growing an unbounded queue — the same posture the
API takes when it answers `429`.

The graph-query boundary is retained in the fleet because the measured corpus
reduced API memory and mixed-search wall time. It remains optional for local
profiles, where an HTTP hop and an extra process are unnecessary. See
[the measured decision](how-to/graph-query-service.md) and
[capacity planning](how-to/capacity-planning.md).

## Architecture regression gate

CI shallow-clones sparse portions of Spark, MLflow, VS Code, LangGraph, and
Deep Agents, then deterministically samples 200 real files from each. It runs
a full index, an unchanged incremental index, and vector/graph/hybrid searches
with a 64-dimension stub embedder and no assistant model or provider key. The
job fails on correctness, unexpected incremental embedding work, or committed
wall-time, throughput, search-p95, and peak-RSS guardrails. The JSON report is
uploaded for 30 days so a pull request can be compared with its predecessors.

These are shared-runner regression guardrails, not production capacity
promises. They deliberately test real repository shapes without making CI
depend on LLM availability, embedding quota, or secrets. See
[Architecture regression testing](how-to/architecture-regression.md).

## Design lineage

The fleet follows the useful boundaries described in
[GitHub's code-search architecture](https://github.blog/engineering/architecture-optimization/the-technology-behind-githubs-new-code-search/):
keep indexing separate from serving, make work durable and retryable,
distribute queries over independently replaceable search shards, and keep
serving state immutable between publications. Pheasant does not copy GitHub's
custom trigram index or split individual graph edges: this system also indexes
documents, semantic vectors, and relationship traversals, and current stress
tests show preparation and PostgreSQL lexical ranking—not substring lookup—as
the first bottlenecks.
