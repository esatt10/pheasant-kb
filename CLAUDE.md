# CLAUDE.md — pheasant

Context hand-off for any agent working on this repository. Read it first; it is
intentionally dense. It describes the system **as it is now**. Anything not
here should be derivable from the code, `docs/`, or a single grep — and where
docs and code disagree, **the code is authoritative**.

---

## 1. What this project is

**pheasant** is a Docker-first, local-first **MCP context server** that turns
configured sources (git repositories, folders, single files, Obsidian vaults,
web collections, SaaS connectors, API/S3) into a queryable **knowledge graph**
with hybrid self-search, for agents and humans.

Design pillars — these are product guarantees, not preferences:

1. **Idempotent indexing** — re-syncing unchanged content produces the same
   state (content sha256 + stable IDs).
2. **Incremental by default** — connector checkpoints and manifests skip
   unchanged artifacts. A second sync of an untouched corpus does no work.
3. **Deterministic parsing** — no LLM calls in the indexing path; all
   enrichment is rule-based and reproducible. The only sanctioned network calls
   at sync time are the optional embedder, captioner and transcriber, and each
   keeps a stub/offline path so `pytest` stays network-free.
4. **Persistence split** — `/state` (operational truth: SQLite or Postgres,
   graph, manifests) and `/exports` (regenerable payloads). State dirs are
   **user data**.

### 1.1 pheasant's second role: a Synapse brain region

pheasant is also the **region** component of **Synapse**, a federated
knowledge-base system whose router lives in the sibling **pheasant-flock** repo.
Each container publishes a **semantic contract** derived from its own content;
the router scores contracts to decide which regions to query and fans out to
each region's self-search.

Two iron rules: the contract schema is canonical in pheasant-flock — this repo
only vendors the exported JSON Schema and fixtures under `contracts/` — and
there is **no Python dependency between the repos**. The boundary is contract
JSON over HTTP, and a router-less pheasant must keep working unchanged. See
`docs/SYNAPSE_INTEGRATION.md`.

---

## 2. Repository layout

```
pheasant-kb/
├── CLAUDE.md · AGENTS.md      ← agent hand-off (this file is canonical)
├── README.md                  ← product front door
├── pyproject.toml             ← extras: mcp, vector, agent, a2a, wasm,
│                                postgres, grpc, queue, tuning, docs, dev
├── pheasant.example.yaml      ← reference config, every section
├── Dockerfile                 ← one universal image: API + MCP + UI, port 8765
├── docker-compose*.yml        ← default / override / fresh reset / scaled
├── contracts/                 ← VENDORED Synapse schema + fixtures (never edit)
├── deploy/                    ← kubernetes/ (+ scaled/), helm/, compose/
├── docs/                      ← MkDocs Material site (see mkdocs.yml nav)
├── examples/                  ← demo-agent-framework, vscode MCP config
├── scripts/                   ← release_version.py, sync_version.py
├── src/pheasant/
│   ├── cli.py                 ← up/host/setup/mount/start/serve/worker/sync/
│   │                            scan/queue/shard/migrate/backup/restore/
│   │                            export/mcp/…
│   ├── setup_wizard.py        ← `pheasant setup`, defaults read off the schema
│   ├── quickstart.py          ← `pheasant up` config generation
│   ├── capacity.py            ← the one home for sizing coefficients
│   ├── analytics.py           ← Parquet exports + the DuckDB query surface
│   ├── evalset.py             ← de-identified eval cases from the ledger
│   ├── evaluation/            ← the evaluation plane: contracts, snapshots,
│   │                            proof, cohorts, variants, replay, metrics,
│   │                            gates, report, candidates, runner, store,
│   │                            benchmark (the capacity measurement)
│   ├── tuning/                ← the tuning plane: stages (which retrieval
│   │                            step lost the document), space, refusion,
│   │                            strategy, executor, gates, bundle, tracking,
│   │                            store, runner, objective (what "better"
│   │                            means), glossary (what every measure means),
│   │                            health (live stage rates)
│   ├── readiness/             ← the readiness plane: contract (what this
│   │                            build supports, derived), probes (executable
│   │                            demonstrations), gates (the four go/no-go
│   │                            sets), runner (the verdict)
│   ├── sharding.py            ← `pheasant shard plan`
│   ├── decision.py            ← the gate vocabulary both planes share: a
│   │                            GateSet that cannot be constructed empty
│   ├── jobs.py                ← per-source progress: phase, rate, ETA, stalled
│   ├── config/                ← schema.py (dataclasses), loader, profiles
│   ├── sync/                  ← engine, connectors, watcher, scheduler, locks,
│   │                            queue, log_queue, graph_events (commit
│   │                            announcements), saturation (the commit-
│   │                            authority ceiling), worker_pool,
│   │                            worker_transport, grpc
│   ├── connectors/            ← first-party SDK plugins: notion, gdrive,
│   │                            slack, confluence, imap
│   ├── ingestion/             ← pipeline, chunking, content_types, taxonomy,
│   │                            extractor (7 doc formats), captioner,
│   │                            transcriber, office, msdoc
│   ├── graph/                 ← model, simple (the indexer's working set),
│   │                            sql (the serving read surface), builder,
│   │                            enrichment, capacity, traversal
│   ├── search/                ← sqlite_store (FTS5/tsvector + BM25),
│   │                            graph_search, hybrid, fusion (the one RRF
│   │                            loop, two entry points), explain (the stage
│   │                            block's declared shape), criteria, vector,
│   │                            ranking (the tunable parameters, fleet-scoped)
│   ├── memory/                ← store, projection, policy, steering, salience,
│   │                            bridge, maintenance, formation, benchmark
│   ├── persistence/           ← state_store, backends (sqlite|postgres),
│   │                            schema, graph_store (file|rows), graph_rows
│   │                            (the delta write), graph_codec (rows to
│   │                            attributes, and the XOR generation fold),
│   │                            receipts (what happened to what a caller
│   │                            submitted), manifest, migrate, paths
│   ├── services/              ← the application layer: one implementation per
│   │                            operation, two transports. errors (refusals
│   │                            both surfaces spell the same, with a stable
│   │                            code and a retryable flag), retrieval,
│   │                            graph, assistant, ingestion (submission and
│   │                            receipts), snapshots (seal and the drift
│   │                            refusal)
│   ├── mcp_server/            ← server.py (MCPServer), tools.py (PheasantTools)
│   ├── api/app.py             ← the HTTP surface
│   ├── assistant/             ← grounded answering + workflows
│   ├── sandbox/               ← WASM runtime, sandboxed connector, accel/
│   ├── deployment/            ← roles, serving durability, mounts, host
│   ├── security/              ← path_policy (what may be read),
│   │                            corpus_policy (what may never be indexed),
│   │                            acl, idp
│   ├── synapse/               ← contract publisher, events, signing
│   └── telemetry/             ← metrics.py (Prometheus exposition),
│                                interactions.py (the observation plane)
├── ui/                        ← React + Vite workspace (baked into the image)
└── tests/                     ← 128 pytest modules, offline by design
```

Key entities: **knowledge base** (`kb_id` = `pheasant.name`) → **sources** →
**artifacts** (stable ID `file:{source}:{relpath}:branch={b}`) → **chunks**
(+ full-text index) and graph nodes (symbol / entity / heading / memory_record /
external_reference) with edges (contains / has_chunk / has_heading / mentions /
references / imports / calls / similar_to / supersedes / about). Full grammar:
`docs/graph_model.md`.

---

## 3. Canonical commands

```bash
pip install -e ".[dev,mcp]"
pytest -q                                  # offline by design
ruff check src tests && ruff format --check src tests
mkdocs build --strict                      # needs the [docs] extra

pheasant up [PATH...]                      # detect → config → index → serve
pheasant setup [--advanced|--accept-defaults|--answers F]
pheasant host ~/notes                      # config + compose file, then run it
pheasant mount <host-path> [--at /data/x]  # bind-mount + allow-list it
pheasant scan                              # project RAM/disk/time before indexing
pheasant validate && pheasant doctor
pheasant sync --source <name> --mode incremental|full|validate_only|repair
pheasant serve --role all|api|indexer|graph|worker|logger
pheasant worker                            # stateless preparation worker
pheasant queue status|drain|requeue-dead
pheasant shard plan [--emit DIR]           # split a corpus across regions;
                                           # --emit writes each region's files
pheasant migrate --to postgres             # one-shot, verified, preserves original
pheasant backup|restore
pheasant export parquet [--table NAME]      # /exports/parquet/<kb_id>/*.parquet
pheasant export query "SELECT …"            # SQL over an export directory
pheasant export tables [--schema]           # what is exportable; --schema for columns
pheasant eval bootstrap                     # de-identified eval cases from real traffic
pheasant eval taxonomy                     # the evidence taxonomy: what each event licenses
pheasant eval proof --query … --target … --event explicit_accept
pheasant eval run [--mode current_state|historical --as-of T]
pheasant eval report [--run ID] [--json]
pheasant eval trend --metric known_positive_reciprocal_rank
pheasant eval status [--watch]             # a batch's live phase/progress, from /state
pheasant tune diagnose                     # which retrieval stage is losing documents
pheasant tune run [--apply]                # diagnose, search the blamed stages, gate a winner
pheasant tune show [--yaml]                # the parameters in force, and where they came from
pheasant tune bundles|apply <id>|rollback [--to] # the fleet's retrieval overlay
pheasant tune lineage                      # every configuration ever served
pheasant tune explain [term]               # what a measure means, and does not
pheasant tune status [--watch] | report
python -m pheasant.evaluation.benchmark    # measure a batch against the capacity model
pheasant readiness contract                 # what this build supports, and what it does not
pheasant readiness check [--gate-set core]  # probe the region; exit 0 only on GO
pheasant mcp --transport stdio
pheasant client-config claude-code|cursor|vscode
pheasant config show                       # resolved config after profile+YAML+--set

docker compose --env-file .env -f deploy/compose/docker-compose.yml up
docker compose --env-file .env -f deploy/compose/docker-compose.scale.yml up --scale indexer=1 --scale worker=4
```

For deployment/configuration work, load
`.agents/skills/pheasant-deploy/SKILL.md` before changing files or containers.

---

## 4. Rules

1. **Never put an LLM call in the indexing path.** Determinism is a product
   guarantee. The optional embedder, captioner and transcriber must each keep a
   stub path so the suite stays network-free.
2. **Treat `/state` as user data.** Schema/layout changes ship a one-shot
   idempotent migration that preserves originals (`*.migrated` rename, never
   delete). New tables arrive via `CREATE TABLE IF NOT EXISTS` in `SCHEMA`.
3. **Stable IDs are contracts.** Changing the ID grammar in
   `docs/graph_model.md` breaks every persisted graph — it needs a migration
   and an explicit decision note.
4. **Idempotency tests are the spine.** `tests/test_sync_idempotency.py` must
   stay green; any sync change adds cases there.
5. **Keep house style:** argparse CLI, dataclass config schema, ruff
   format + lint, pytest, Python ≥ 3.11, type hints.
6. **Never import pheasant-flock.** The Synapse boundary is contract JSON over
   HTTP. Never hand-edit vendored files under `contracts/`.
7. **Standalone mode is sacred.** Every change must leave a router-less,
   infrastructure-free pheasant fully functional. Postgres, gRPC, the broker
   and the role split are *selectable backends*; SQLite, HTTP, no queue and
   `--role all` are the defaults, and each seam owes a test asserting the
   no-infrastructure path is unchanged.
8. **The MCP tool surface is public API.** Renaming or removing a tool breaks
   deployed agents — additive evolution only, deprecate before remove. One
   sanctioned exception to date: `export_obsidian_notes` was removed outright
   when the exporter behind it was deleted, because there was nothing left for
   the tool to do. That is a precedent for "the feature is gone", not for
   renames.
9. **Cross-repo work** (anything the Synapse spec marks `[x-repo]`) uses
   identical branch names in both repos, contract fixture parity (sha256), and
   both test suites green before either push.
10. **Verify against the real thing.** Postgres, NATS, wasmtime and a real
    container have each caught bugs a mock could not. When a change touches a
    backend, run that backend. When it touches the image, build the image.
    CI does the Postgres half for you on **every** PR (`ci.yml`'s
    `backend-parity` job, deliberately unfiltered); locally it is
    `PHEASANT_TEST_POSTGRES_DSN=… pytest -q`, which turns on the parity suite
    and the lease/queue differential. Neither replaces running the thing you
    changed.
11. **Config-schema changes owe the config surface an update.** Adding a
    *top-level* section to `src/pheasant/config/schema.py` needs three things:
    a mention in `docs/configuration.md`, a `Section` in
    `src/pheasant/setup_wizard.py`, and an entry in `LIVE_APPLICABLE_SECTIONS`
    (`api/app.py`) saying whether a running server can pick the change up.
    `tests/test_config_surface_freshness.py` fails CI on all three,
    mechanically. **Individual field defaults need no second edit** — the
    wizard reads them off the live dataclasses — and neither does **nesting**:
    `model_validate` derives it from the annotations, so a nested section is
    constructed because its type says it is one. That last edit used to be a
    fourth, hand-written branch that the freshness test did *not* cover, so a
    section added without it loaded silently as defaults.
12. **DuckDB is read-side only.** `src/pheasant/analytics.py` uses it as a
    Parquet writer and a query engine over `/exports`; it must never become a
    `storage.backend` or appear on the sync path. Three reasons, each measured
    or documented: the write path is single-row OLTP (per-artifact `DELETE` +
    re-`INSERT`, conditional-`UPDATE` lease claims, `UPDATE … RETURNING` queue
    claims), which is a bulk-columnar engine's worst case; DuckDB's FTS index
    is rebuilt wholesale rather than maintained, which would break pillar 2;
    and its exclusive *file* lock blocks other processes from opening the
    database at all, where SQLite's WAL is what lets `deploy/compose/docker-compose.scale.yml`
    mount `/state:ro` on the API replicas while the indexer writes. The export
    takes no lease and issues nothing but `SELECT`, which is what makes it safe
    to run during a sync.
13. **One operation, one implementation.** An operation both surfaces expose
    lives in `src/pheasant/services/` and nowhere else; `api/app.py` and
    `mcp_server/tools.py` parse a request, call it, and marshal the answer.
    Layering is transport → services → domain → persistence with no upward
    edges, and both halves are tests, not conventions
    (`tests/test_surface_conformance.py`, `tests/test_service_layering.py`).
    Adding an operation to one surface only is how the last four divergences
    started; if it genuinely belongs to one transport, say so in the adapter,
    where a reader can see it.

---

## 5. How the system works now

### Surfaces and layering

**transport → services → domain → persistence, no upward edges.** pheasant has
two public APIs — HTTP (whose largest consumer is the bundled UI) and MCP
(whose consumers are agents) — and they are one implementation exposed twice.
That is now true; it used to be documentation. `api/app.py` referenced
`PheasantTools` once, for introspection, and four of the five shared
operations had already drifted apart, including `relevant_files` applying the
memory policy over HTTP and not over MCP — an agent could be served a record
the region *knew* had been corrected.

An operation lives in `services/` once and owns retrieval criteria, the
over-fetch, the memory policy, metrics and **its refusal text**. The two
transports are adapters: parse a request, call one function, marshal the
answer. A behaviour that differs between the surfaces has to be a difference
in an adapter — a thing a reader can see — rather than a difference between
two implementations, which is a thing nobody sees until it is reported.

Refusals are `ServiceError(ValueError)` subclasses carrying a `status` hint, so
the MCP server's existing `ValueError`/`KeyError` → `ToolError` translation and
HTTP's 400 both keep working, and each adapter can be taught the richer mapping
independently.

Two tests hold it. `tests/test_surface_conformance.py` drives the same
operations through both surfaces against one corpus and asserts identical
results *and* identical refusal text — it is what found the `graph_generation`
bug below. `tests/test_service_layering.py` is an import-graph test: no module
imports a layer above its own, `services/` imports no transport, and every
package declares its layer, with `cli.py` and the benchmarks named as
composition roots because building a transport is their job.

### Ingestion

A connector lists items and reads their bytes; the engine skips anything whose
sha256 is unchanged **before** reading it, which is what makes a re-sync free.
Text is parsed by content type, chunked, and written to the state store and the
full-text index; the graph builder adds nodes and edges; enrichment resolves
cross-source references.

**Seven document formats** extract real text: `.pdf`, `.docx`, `.pptx`,
`.xlsx`, `.doc`, `.rtf`, `.epub`. Providers are `auto` (default), `native`,
`builtin` (stdlib only) and `sandboxed` (the PDF tokenizer inside a WASM guest
with no host imports). `DOCUMENT_EXTENSIONS` and `EXTRACTED_EXTENSIONS` are
asserted set-equal — that drift is exactly how a format gets accepted and then
silently indexed as nothing.

**Images and audio** are captioned/transcribed into indexable text that flows
through the normal path. Both default to a deterministic offline stub, and an
authored `<file>.caption.txt` / `.transcript.txt` sidecar always wins.

**Structural taxonomy** (`ingestion/taxonomy.py`) is opt-in per source. Six
rules detect headings across mixed conventions, ordinals are parsed and
reconciled so a document's two spellings of "four" are one number, and chunks
are cut at section boundaries so one chunk is one section.

**Connectors** resolve by `sources[].type` through entry points, so a
third-party plugin needs no dispatch code here. Five ship first-party: Notion,
Google Drive, Slack, Confluence, IMAP. `pheasant.testing.ConnectorConformance`
is the public quality bar.

### Where the graph lives

`storage.graph_format`: **`rows` (default)** or `node_link_json`. The graph was
one zstd node-link file that every commit re-serialized whole and every process
answering a graph query held resident. Measured with
`python -m pheasant.graph.capacity` at 100k files (630k nodes / 630k edges):

| | file | rows |
|---|---|---|
| commit after a one-file change | 6.15 s, growing with the graph | **1.1 ms, flat** |
| load before a replica can serve | 4.9 s | none |
| resident bytes to answer a query | 1.65 GB | none |
| 3-hop bounded walk | in-RAM | 0.12 ms |
| stored bytes | 17.9 MB | 1,033 MB |

The commit number is the point, and not because six seconds is slow: it was
**O(total graph)** for an O(one file) change, on the sole commit authority,
under the sync mutex — so corpus size, not write rate, was what saturated the
commit stream, and the only documented way past it was to shard. The trade is
disk, roughly 55×, stated as `capacity.GRAPH_ROW_BYTES_PER_NODE` (measured
1,637 at 20k files and 1,639 at 100k) and reported by `pheasant scan`.

- **Two objects, two jobs.** `SimpleMultiDiGraph` is the *indexer's working
  set* — the builder mutates it, the whole-graph enrichment passes walk it, and
  it belongs in RAM because that is what those passes need.
  `graph/sql.SqlGraph` is the *serving read surface*, and it holds nothing.
  `SyncEngine.serving_graph()` picks: a process that builds the graph serves
  its own copy, a process that only serves gets the store. So the residency
  the `graph` role exists to stop every replica paying is now paid by whoever
  writes, and by nobody else — which also makes that role optional rather than
  the only way out.
- **The delta, not the graph.** `SimpleMultiDiGraph` tracks dirty and removed
  nodes and endpoint *pairs* (a pair, because parallel edges have no identity
  of their own — `add_edge` keys them by arrival). `graph_delta()` reads and
  `clear_graph_delta()` forgets, deliberately split where `take_index_delta`
  claims on read: the search index is a cache and a lost flush costs a
  rebuild, while a lost graph delta is a node that reaches no disk and that
  nothing believes is dirty any more.
- **The generation id survives the move.** Still content-addressed, still
  clock-free, still identical on two replicas holding the same rows. Every row
  carries its own digest and the published id folds them with **XOR**, which
  is its own inverse — so a commit folds each changed row's old digest out and
  its new one in, in O(changed). `recompute_folds` re-derives both with a full
  scan, because an incrementally maintained aggregate nothing checks is one
  that drifts.
- **A serving replica cannot be stale.** Staleness is a property of *copies*.
  A row-backed api replica reads the rows the indexer committed, so `/ready`
  publishes `loaded == published` by construction and the refresher finds
  nothing to do. The whole-file backend still needs all of it, and still has
  it.
- **Snapshots stay files.** History is read whole or not at all and is
  interval-gated rather than per-commit, so materializing one from rows at
  O(N) is the right cost in the right place — and it keeps a snapshot the same
  document whichever backend produced it.
- **What did not change.** Artifacts and chunks were already rows and are
  untouched; they are the model this followed. Vectors stay in LanceDB, which
  is columnar and append-incremental and never had the whole-file-rewrite
  problem — they remain outside the graph's transaction, so repair is still
  what reconciles them. What *did* improve for free: the graph and the chunks
  it describes now commit in **one transaction** instead of a database write
  followed by a separate file rename.
- **Migration.** A region upgrading imports its existing file once at boot and
  parks it as `*.migrated` (rule 2). `node_link_json` stays selectable and
  working, so a region that hits trouble reverts with one config line.
- **The indexer still holds one, and that is now the only place residency is
  required.** The builder mutates it and the enrichment passes walk it. Those
  passes were written when "walk the whole graph" was free — because the whole
  graph was already a dict in front of them — and three of them were doing it
  for data they never read. Measured at 100k files and fixed:
  `add_cross_source_edges` copied `dict(attrs)` for **every** node to hand the
  resolver a list it immediately narrowed to 15% (2.95s and +160MB → 319ms and
  +24MB); `_bridge_inputs` built `{node_id: type}` for every node to answer one
  question about a handful of edge targets; and `remove_source_content` /
  `remove_artifact_nodes` called `nodes(data=True)`, materializing the whole
  graph before the filter looked at the first entry (1.16s, an order of
  magnitude more than the removal it was preparing for).
  `tests/test_graph_working_set.py` bounds what each pass may touch.
- **What is left, and what it would cost.** `remove_nodes_from` is still
  O(total edges) — an edge goes when *either* endpoint does and only the
  outgoing half is indexed. Two fixes were measured and neither ships: taking
  the outgoing half off `_out` came out at 122.5ms against 126.0ms, inside the
  noise, because the scan is what costs rather than the test inside it; and an
  in-adjacency index removes the scan properly for ~215 bytes per node, 15% of
  the working set, to save ~120ms on a call that fires once per full sync and
  on a memory-maintenance beat. The shape is O(total) and the constant is
  small, so the answer is to stop holding the graph rather than to index it
  further — see the next bullet for what that needs.
- **Measured against the pre-35.10 baseline, end to end.** An 8,000-file
  corpus (24,443 nodes / 72,441 edges), same corpus both sides, `834452b`
  against now:

  | | baseline | now | |
  |---|---|---|---|
  | graph publish per commit | 0.82 s | **0.0007 s** | 1,231× |
  | incremental sync, 1 file changed | 4.36 s | **3.32 s** | 1.3× |
  | RSS for a replica that answers | 142 MB | **29 MB** | 4.9× |
  | text search p50 | 49.6 ms | 47.4 ms | — |
  | graph arm p50 | 34.7 ms | 55.9 ms | 1.6× *slower* |
  | 3-hop walk, ordinary node | 0.24 ms | 1.7 ms | 7× *slower* |
  | 3-hop walk, hub (8,040 edges) | 12.6 ms | 46 ms | 3.7× *slower* |
  | full index | 25.9 s | 27.7 s | 1.1× *slower* |
  | `/state` on disk | 115 MB | 177 MB | 1.5× *larger* |

  It is **not uniformly faster**, and the shape of the trade is the point: the
  axes it was built for — commit cost and residency — moved by three orders of
  magnitude and five times; graph *read* latency got worse, because reading a
  graph you do not hold means reconstructing what you read. Text search and
  the unchanged re-sync are untouched, which is the check that nothing leaked
  sideways.
- **And measured again on the fleet's own shape: Postgres, five sources.**
  The table above is one SQLite region with one source, which is the
  *standalone* deployment. The fleet is `storage.backend: postgres` with
  several repositories in one region, so that was run too — five repositories
  of 1,600 files each (40,122 nodes / 112,111 edges / 88,000 cross-repo
  reference edges), same corpus and same Postgres both sides:

  | | baseline | now | |
  |---|---|---|---|
  | graph publish per commit | 1.32 s | **0.005 s** | 270× |
  | incremental sync, 1 file changed | 5.02 s | **3.55 s** | 1.4× |
  | boot before a replica can answer | 1.08 s | **0.07 s** | 17× |
  | RSS for a replica that answers | 131 MB | **~0** | — |
  | hybrid p95 | 173.3 ms | **77.0 ms** | 2.3× |
  | hybrid throughput, 4 threads | 18.6 qps | **20.8 qps** | 1.12× |
  | hybrid p50 | 60.4 ms | 61.5 ms | — |
  | text search p50 | 46.4 ms | 47.5 ms | — |
  | cross-source resolution | 2.75 s | 2.75 s | — |
  | graph arm p50 | 45.7 ms | 59.9 ms | 1.31× *slower* |
  | 3-hop walk off a 1,620-edge hub | 2.3 ms | 7.6 ms | 3.3× *slower* |
  | bounded slice | 4.8 ms | 9.8 ms | 2.0× *slower* |
  | five full indexes | 65.4 s | 69.7 s | 1.07× *slower* |
  | stored bytes (db + `/state`) | 141 MB | 240 MB | 1.7× *larger* |

  Same shape as the SQLite run, which is the point of running it: nothing
  about the trade is a SQLite artifact. Two things only the fleet shape shows.
  **Cross-source resolution did not get more expensive** — 88,000 reference
  edges across five repositories resolve in the same 2.75s, so narrowing that
  pass to `external_reference` + artifacts held up on a corpus where it has
  real work. And the **p50/p95 split reverses**: the median hybrid query is a
  wash and the tail is 2.3× better, because the baseline's tail *is* the graph
  it is holding. A serving replica wants that direction.

  The first version of this table was much worse — graph arm 82ms, walk 14.2ms,
  slice 17.1ms, and throughput at 0.64× of baseline across four threads. Four
  changes closed it, and the shape of all four is the same: **work that was
  free while the graph was a dict in front of you, and is not free behind a
  store.** One statement instead of seven for the arm's candidates; a
  materializing decoder for readers that want every attribute; one token pass
  per field instead of two generator expressions; and the walk's budget
  reaching the fetch. None of them changes an answer.
- **Threads scale now, and replicas are still the axis.** Hybrid search across
  1, 4 and 8 *threads* in one process: baseline 13.3 → 18.6 → 17.7 qps, now
  **15.3 → 20.8 → 19.4** — ahead at every count, where before these changes it
  was 12.0 → 11.6 → 10.2 and behind at every count. It scales because most of
  what the arm now does is wait on one statement rather than run Python under
  the GIL. As independent *processes* on a 4-core box both trees stop where
  the cores do: baseline 13.3 → 26.0 → 36.0 → 36.4 at 1/2/4/8, now 13.1 →
  25.6 → 32.1 → 32.0 — parity at one and two, ~11% behind at four (it was
  ~31%), with a better p95 throughout. Which is the fleet reading: **the same
  throughput per core, and no graph residency per replica.** Four file-backed
  replicas hold four copies of the graph — 524 MB here, 6.6 GB at 100k files —
  to serve 36 qps; four row-backed ones hold none to serve 32.
- **Serving concurrency on SQLite is the one caveat worth knowing.** Hybrid
  throughput held at 1 thread and fell 3.7× at 4. The cause is not the design:
  concurrent SQLite reads do not scale *in a container like this one* — the
  same probe against the **baseline's** database reading `chunks` went 486/s
  to 13/s across 1→4 threads, with per-thread connections and no WAL backlog.
  The row backend simply moved graph reads onto that resource, so the graph
  paths inherit it. Postgres does not share it: the same batched fetch scales
  41 → 86 → 160 qps across 1 → 4 → 8 threads, which is where the fleet runs
  and where replicas exist. A standalone SQLite container serving heavily
  concurrent *graph* queries is the one shape that is worse off than before,
  and `storage.graph_format: node_link_json` is still there for it.
- **The prerequisite for dropping the working set is batching, and Postgres
  sets the bar.** The builder does a read-modify-write per node (`upsert_node`
  reads `created_at`, `upsert_edge` reads the existing edge). Against rows
  that is a point lookup: **8.2µs batched / 13.1µs each on SQLite** (630k of
  them ≈ 5–8s, about 2% of a full index) but **46.5µs batched / 456µs each on
  Postgres** — a socket round trip is 35× an in-process one, so the same 630k
  is 29s batched and **287s unbatched**. Batching is therefore the whole
  feasibility argument, not an optimization, and the fleet is exactly where
  Postgres runs. The only irreducibly global structure left is cross-source
  resolution's `by_path` index — every artifact path in the region, because a
  reference resolves only once both sources are indexed — and that is ~15% of
  nodes held for one pass rather than for the process.

### Retrieval

Three arms — text (BM25 over an FTS5 or `tsvector` index), vector (LanceDB),
and graph — fused by **reciprocal rank fusion**, because the arms score on
incomparable scales and raw-score merging silently degraded to text-only.

Ranking carries deliberate structure: `chunks_fts.title` holds the file's
**basename** with BM25 column weights `8/3/2/1`, and structural priors divide
by path depth and by tests/samples membership. Query expansion drops framing
stopwords. Criteria (`source_name`, `exclude_sources`, `node_types`,
`min_score`, `section`, `principal`) are available identically on MCP and HTTP.

**One over-fetch.** When a post-filter will drop rows the arms fetch further,
so `max_results` keeps meaning "give me this many" — and that is
`ranking.filter_overfetch`, computed in `RankingParameters.overfetch` and
nowhere else. It governs the ACL, section, memory *and* criteria filters; each
surface used to carry its own `× 4` for the last of those, so the tunable
parameter half-governed the stage the glossary attributed it to.
`tests/test_ranking_parameters.py` fails if a second multiplier appears.

Concept extraction was **retired**: it was 87% of nodes and 98.6% of edges and
failed every test set for it. `graph.enrichment._add_concept` is a no-op whose
docstring carries the measurements.

### Memory

Memory records are **source content** — one frontmatter Markdown file per
record, indexed by the ordinary pipeline. Recall *is* search. On top of that:

- **Validity** — a correction supersedes rather than overwrites, and validity
  is filtered at query time. `as_of` deliberately brings the old record back.
- **Policy** — one `MemoryPolicy` (`mode`, `scopes`, `subject`, `current_only`,
  `as_of`, `max_results`, `include_rules`) spelled identically on MCP and HTTP,
  with `sql_predicate` and `admits` as two encodings of one rule.
- **Steering** — `alias`, `preference` and `exclusion` records change ranking
  for queries that return no memory at all. Steering records are excluded from
  result lists by default: an agent asking for code should not get a line of
  rule syntax dressed as retrieved knowledge.
- **Isolation** — `normalize_acl` keys on scope: `org` is shared, `user` and
  `session` are readable only by their writer.
- **Graph** — records get `memory_record` nodes, `supersedes` edges, and
  `about` edges via a precedence ladder (reference → symbol → heading →
  entity), capped at three targets.
- **Observation** (`observability.interactions`, off) — every API/MCP call
  becomes a **row with a retention policy**: never a file, never chunked,
  never indexed, never returned by a search. A UI session's chat does not
  become knowledge because it was observed. The only path from here into
  memory is a candidate that something *admits*, and admission goes through
  `MemoryStore.append` like every other write, so invariant 1 never bends.
  Dimensioned by identity / session / modality (`ui|mcp|a2a|cli`) / criteria.
  Trace and timestamp are guaranteed, not best-effort: `NOT NULL` in the
  schema, rejected-and-counted before the insert if absent, `duration_ms`
  always set and taken from a **monotonic** clock while `started_at` is wall
  clock. The trace is ambient for the call and injected into every hop
  pheasant makes of its own — the graph-query call, remote preparation, and
  `index_tasks.payload` (attached *after* the id digest, or the content-
  addressed dedup that makes two replicas enqueue one task would break).
  `docs/memory-formation.md`.
- **Formation** (`memory.formation`, off) — deterministic rules read the
  observation plane and produce memory. `session-digest-v1` is the first:
  one record per `(session, principal)`, refined by **superseding itself**,
  so `current_only` returns exactly one and `as_of` reads the session's
  history. Written automatically rather than proposed, and only because of
  scope: `session` scope + `written_by` means only its own writer can read
  it and it decays with `session_ttl_days` — it never becomes shared
  knowledge, which still takes an explicit promotion. Two guards keep a
  repeat pass free: a text short-circuit (cheap) and the store's own id
  dedup (sound, because `supersedes` is deliberately absent from the id
  digest). Three further rules **propose** rather than write:
  `alias-cooccurrence-v1` (a query word absent from everything it retrieved,
  guarded against inflections — `coordination -> check` was a real false
  positive), `path-affinity-v1` (prefix cut at a directory boundary) and
  `retrieval-gap-v1` (a gap is *no results*, never a score threshold: fused
  RRF scores have no absolute scale). A candidate crosses into memory only
  through `MemoryStore.append`; a rejection is permanent, because
  re-suggesting what someone declined makes a review queue worth ignoring.

### Evaluation

`evaluation.*`, off by default and read-only when on. A **third plane**:
observations are evidence, records are memory, and *measurements are neither* —
nothing the `evaluation_*` tables hold is a file, is chunked, is indexed, or is
returned by a search. A region must not answer a question with its own report.

- **Typed proof, or none.** Served/considered/included are **unknown**, weight
  zero; only a caller can say `cited`/`selected`/`explicit_accept`/
  `explicit_reject`/`downstream_*`/`deterministic_validation_*`. `not_selected`
  is unknown too — the reader may have found the answer at rank one, and
  treating silence as a negative manufactures negatives at exactly the rate the
  region serves results. Weight is a product of four **reported** multipliers.
  Positive and negative sums never cancel: `P`, `N`, `Net` and a conflict rate
  are all published.
- **Snapshot manifests** digest every input that can change retrieval (content,
  sources, graph, lexical/vector index, encoding, chunking, fusion, arm limits,
  memory, steering, ACL, evaluation policy). Computed identically on any
  replica, so two pods agree on a snapshot id without coordinating. **No clock
  in either id**: a snapshot addresses state and a run addresses
  `(state, config, mode, described instant)`, so two runs over an unchanged
  region are one run and one trend point. The clock-seeded version made runs a
  second apart two rows and runs *within* a second collapse into one.
- **Six cohorts.** anchor (frozen, the trend line), rolling, **learned**
  (queries that created the memory — *recall of learned experience*, never
  reported as generalization), **temporal holdout** (later, independent
  queries), control (no steering rule can fire), synthetic invariants.
  `generalization_gap = learned − holdout` is the memorization detector.
- **Paired ablations** `B0`–`B6`. `B0` (corpus-only) is not removable: every
  attribution number is a difference against it. `B2`–`B4` hold memory
  *content* off so a retrieved record cannot be counted as a rule's doing.
- **Every metric carries its denominator, formula, substituted calculation,
  operands, proof ids, exclusions and one limitation** — `MetricResult.validate()`
  withholds one that cannot. A missing input yields `insufficient_evidence`
  with `value: None`, never `0.0`.
- **Gates are not metrics.** ACL leak, stale-current leak, `as_of` correctness,
  abstention, known-positive exclusion, control regression and negative-exposure
  increase are evaluated *before* aggregation so a good score cannot offset them.
- **Candidates are shadowed.** A proposed steering rule is passed into the
  search call for the length of one query via `extra_steering_records` — the
  real `parse_rule`/`admits` path, nothing written. A proposed *fact* is
  `not_shadow_replayable` (its text is in no index; scoring it would measure
  string similarity). Promotion needs every gate, independent queries, and a
  holdout result: `allow_originating_query_only_promotion` is off, which is
  what keeps the self-rewarding loop closed.
- **Fleet-safe.** A run claims the `__evaluation__` lease in `source_leases`
  (N replicas → one run), **never** takes `sync_lock`, and the replay searcher
  is built with `usage_tracking=False` so evaluation cannot inflate the salience
  of the records it measures. Auto-trigger fires only where the scheduler runs.
- **Progress is a row, not a process.** `phase`, unit counters and a heartbeat
  live on `evaluation_runs`, so the UI, the CLI (`pheasant eval status
  [--watch]`), HTTP (`/evaluation/status`) and MCP (`get_evaluation_status`)
  all watch a batch none of them started — across a restart. A run whose
  heartbeat expires is reclaimed as **`interrupted`** (at API boot and on the
  beat), never left spinning.
- **A batch resumes rather than restarting.** Each (cohort, variant) replay is
  checkpointed to `evaluation_replays` as it finishes; the content-addressed
  run id makes a re-run load them and replay only what is missing. Checkpoints
  clear only *after* the report commits, and a resumed run computes numbers
  identical to an uninterrupted one — asserted by killing one of two identical
  regions mid-batch and diffing health vectors.
- **Sized, not guessed.** `capacity.project_evaluation` is the one home for
  evaluation coefficients; `pheasant scan` prints run time, steady-state and
  *peak* volume separately (the peak is checkpoints in flight, the number that
  decides whether a PVC fills mid-run). `python -m pheasant.evaluation.benchmark`
  measures a real batch against the model and CI publishes the comparison —
  the first two coefficients shipped were out by 2x and 3x, found exactly that
  way. `docs/knowledge-effectiveness.md`.

### Tuning

`tuning.*`, off by default and read-only when on. Where evaluation says *how
well* retrieval is doing, this says **which step is failing** — and after the
merge a lexical miss, a filtered-out document, a fusion demotion and a
truncation all look identical, because they all produce an absent result. Six
causes, one symptom.

- **A stage model, attributed to the first stage that lost the document.**
  `search_context(explain=True)` reports each arm's candidates, what each
  filter removed, and the fused order before truncation; `tuning.stages`
  attributes every miss. Not to *every* stage that could be blamed — a
  document the lexical arm never saw is not also a fusion failure, and counting
  it as both makes the totals exceed the misses and every stage look guilty.
  Arms are reconciled first: a target the vector arm missed and the text arm
  returned is not a failure at all.
- **Three refusals.** Absence from the corpus is never *inferred* from "no arm
  returned it" — that needs a lookup, and without one it reports
  `candidates_missing`. A query with no known positive is excluded from the
  denominator rather than counted as served. And no stage is ever blamed on a
  score threshold: fused RRF scores have no absolute scale.
- **It declines.** When the misses are in stages no parameter reaches, the
  batch proposes nothing and says why. That is the most valuable thing it
  produces; searching anyway and shipping the highest number is the failure
  mode the design exists to prevent.
- **Most trials cost no retrieval.** The fusion family acts *after* the arms
  produce candidates, so it is recomputed from cached candidate lists — a
  thousand points for one replay. That is a re-implementation of the merge, so
  `verify_equivalence` re-fuses at the parameters that actually ran and
  compares id for id before any cheap trial is trusted, and a degraded capture
  falls back to a real search rather than approximating.
- **Nothing is promoted by its own evidence.** A winner selected on one cohort
  must confirm on a holdout it never saw, with a control that must not regress.
  Gates sit outside the score, and an empty gate list is a failure — `all([])`
  is `True`, which the evaluation plane learned the expensive way.
- **The objective is configured, not assumed.** `tuning.objective` picks
  between reciprocal rank, recall@5/10, hit rate, a balanced composite, or
  custom weights, and every one publishes what it *trades away*: a region
  whose agents read one result and one whose agents read a page want opposite
  things, and a plane that silently assumed the first would make the second
  worse while reporting an improvement. A composite scores `None` rather than
  zero when a component is missing — a point that could not be measured is not
  one that measured badly.
- **Every measure carries its own explanation** (`tuning.glossary`): what it
  means with its denominator, what to do if it moves, and — the field that
  prevents the wrong action — the misreading it invites. Served over HTTP and
  MCP and rendered inline in the UI, because documentation a reader has to go
  and find arrives after the mistake. `tests/test_tuning_objective.py` fails
  when a stage, gate or parameter the plane emits has no entry.
- **The demo and CI benchmark on SciFact**, not on a fixture. One of the BEIR
  tasks; `scripts/fetch_benchmark_corpus.py` materializes 395 abstracts (a
  quarter as real PDFs) with 66 **expert relevance judgements**, fetched at
  benchmark time and never vendored. The point is the judgements: a fixture
  whose known-positives were written by the seeding script produces evaluation
  and tuning numbers that measure the seeding script. The subset rule is in the
  manifest so it cannot be quietly tuned until the charts look good.
- **Each mechanism is measured on its own.** The diagnosis ablates the arms —
  text, vector, graph alone against the merge — by re-fusing captured
  candidates with the others weighted to zero, so it costs no retrieval.
  "Hybrid is better" is an assumption most regions never test and is
  frequently false; when an arm alone beats the merge the report says so in
  words. Reported, never acted on.
- **Base, overlay, lineage.** The base is `search.ranking` in the mounted
  config (compose-settable, version-controlled); the overlay is one row; the
  active point is base + overlay, and all three are reported separately
  because "what would a rollback give me" is asked when nobody can go and look
  it up. `lineage` records what each promotion replaced, and rollback targets
  the base (default) or a named earlier bundle.
- **Fleet-scoped, and applying is a separate act.** A bundle is one row in
  `/state` and every replica resolves it on a 30s TTL. There is no per-request
  and no per-principal override, and nowhere in the schema for one. Producing a
  bundle changes nothing; `pheasant tune apply` changes what the fleet serves,
  and rollback restores what the bundle recorded that it replaced.
- **It yields.** One executor slot, the `__tuning__` lease, never `sync_lock`,
  and it stands down *between units* while the index queue has work — indexing
  is somebody waiting, and this is a measurement. Standing down is resumable,
  not a failure.
- **Hot/cold.** `/state` holds experiments, trial scores, decisions and
  bundles; per-query per-trial rankings go to `/exports/tuning` as zstd JSONL,
  because they are derivable and an operational database is not where 80,000
  ranked lists belong. MLflow is an optional mirror of `/state`, never
  load-bearing, defaulting to a local file store.
  `docs/retrieval-tuning.md`.

### Readiness

`readiness.*`, off by default. Where evaluation says *how well* retrieval is
doing and tuning says *which step is failing*, this says whether an outside
harness may trust either answer — whether every submitted write reconciles,
whether a result names the exact place it came from, whether an isolation
boundary holds, and whether the region will say so in a form a machine can read
**before** an experiment starts rather than after it has produced numbers
nobody can defend. `decision.py`'s docstring predicted a third plane; this is
it, and it uses `GateSet` rather than shipping a fourth copy of the `all([])`
guard.

- **The contract is derived, not written down.** Every capability names live
  symbols, MCP tools and probes, and `capability_status` resolves them; a row
  naming something absent reports `unsupported` with the reason.
  `tests/test_readiness_contract.py` fails when a name goes stale. Three wrong
  symbol names shipped in the first version and were caught by *running* a
  check — a hand-maintained list says what somebody believed when they last
  edited it.
- **`supported` and `proven` are different claims.** The implementation
  existing has never been evidence that it works in the deployment about to be
  measured. A capability is `proven` only once a probe demonstrated it here,
  against this corpus; with no probe run at all it is `declared_untested`.
- **`unsupported` is a first-class answer**, and two rows use it. Corpus-level
  `as_of`, because this region holds one version of its corpus. And
  claim-to-claim evidence stance, because evidence is typed at the record and
  proof levels and there is no stance edge in the graph — inventing one on a
  rule-based footing is how concept extraction was already retired. A harness
  needs to tell "this region cannot" from "this region did not mention it".
- **A sealed snapshot's guarantee is a refusal, not time travel.** A search
  pinned to a snapshot is answered from that state or it is not answered:
  `require_current` re-derives the manifest and raises `SNAPSHOT_DRIFTED`
  naming the sections that moved. Weaker than "ingestion cannot change a sealed
  snapshot's results", and sufficient for what a scored experiment needs — that
  two runs claiming one snapshot cannot silently have seen different corpora.
  Said in the module rather than smoothed over: a reader who assumes time
  travel will ingest during a run and call the result reproducible.
- **A receipt answers what `artifacts` cannot.** A row's absence is the same
  answer for "never submitted", "rejected" and "lost". One receipt per item
  keyed by the caller's idempotency key, with `accepted` and `indexed` as
  separate dispositions because they are separate facts; `reconcile` reports
  `silent_loss` as receipts claiming an artifact the region does not hold,
  never as a difference between two totals — two totals can agree while one
  item was lost and another double-written.
- **Contamination is refused at every door.** `readiness.corpus_denylist` is
  enforcement, not detection: a check that benchmark artifacts are absent can
  only run once they have been indexed, and by then they have been retrievable.
  The rule is `security/corpus_policy.py` (layer 1, because `sync/` cannot
  import `services/`) and both write paths call it — the submission path
  raises `CORPUS_DENYLISTED`, the sync path refuses the item *before reading
  it* and counts it in the report. Empty by default, one truth test per item.
  The gate goes further and scans `artifacts`, because enforcement stops new
  arrivals and says nothing about what was indexed before the denylist existed.
- **A skipped probe is not a pass, and neither is a partly-skipped gate set.**
  A verdict is tri-state: `True` only when every gate in the set was evaluated
  *and* passed. The demo region caught the first version reporting memory as
  PASS with three of four gates unevaluated — the `all([])` shape one level up,
  where the empty set is not the gate list but the part nobody could run.
- **A check writes, and only to a scratch source it owns.** It submits
  documents to `__readiness__probe`, indexes them, seals snapshots and runs
  searches; it never touches a configured source, never takes `sync_lock` and
  never writes memory. `POST /readiness/check` is gated on `readiness.enabled`
  for that reason; the *contract* is not, because a 404 there is
  indistinguishable from an old build that has none.
  `docs/stress-test-readiness.md`.

### Retrieval telemetry

Two signals, split by cost, because the tuning plane's stage attribution only
exists inside a *replay* — which left production retrieval with a duration
histogram and a counter, neither able to name a stage.

- **Always on: counters.** `pheasant_retrieval_arm_total{arm,outcome}`,
  `_arm_candidates`, `_filtered_total{filter,arm}`,
  `_fusion_contributions_total{arms}`, `_fusion_depth`, `_truncated_total`,
  and `_empty_total{stage}` — the live version of the stage histogram. An
  in-memory increment per search; **no database write reaches the request
  path**, which is the rule the observation plane's hot tier exists to keep.
  `empty` and `failed` are separate arm outcomes because "the vector index is
  down" and "it has nothing for this query" call for opposite responses.
- **Sampled: the stage digest.** `observability.interactions.stage_sample_rate`
  attaches a compact per-stage summary to a fraction of ledger rows —
  including the bundle the search ranked under, which is what lets a stage
  regression be traced to the configuration change that caused it. It
  annotates the row the handler is *already* writing rather than writing one
  of its own. Sampling is deterministic on the trace id (hashed whole, see the
  traps), so every hop of a call agrees and the sampled set can be joined.
- **`tuning.health` reads those digests** into per-stage rates with
  denominators, classified `structural`: it says what the pipeline did, never
  whether an answer was correct, because nobody judged those queries. Its
  honest use is a change detector — an empty rate that moves after an apply is
  a fact about the bundle, without waiting for a batch. Below
  `MINIMUM_SAMPLES` it publishes nothing rather than a rate over four
  searches, and it reports when its window spans two configurations rather
  than averaging across the change somebody is looking for.

### Scale

One container until it shouldn't be. Then four independent axes:

| Axis | Mechanism | Scales on |
|---|---|---|
| Request traffic | `serve --role api` replicas; publish instead of index | CPU / RPS |
| Ingest throughput | `--role indexer` claiming from a durable queue, `--role worker` preparing | `pheasant_index_queue_depth` |
| Corpus size | `pheasant shard plan` packs **whole sources** per region | graph nodes |
| Observation volume | `--role logger` draining its **own** queue (`log_tasks`, never `index_tasks`) | `pheasant_log_queue_depth` |

The second axis has a ceiling the others do not: one indexer is the sole commit
authority per shard, so extra indexers are elected hot standbys and the third
axis is the only way past it. What 35.10 changed is *where* that ceiling sits:
publishing a generation used to cost O(total graph) per commit, so the ceiling
moved down as the corpus grew. With the graph in rows a commit costs what the
change costs, and the third axis is a decision about the indexer's own working
set rather than about every replica's.

Selectable backends, dependency-free side first: `storage.backend`
sqlite|postgres, `sync.queue.backend` off|local|nats,
`sync.concurrency.worker_transport` http|grpc,
`observability.interactions.queue.backend` off|local|nats. One exception, and
it is deliberate: `storage.graph_format` defaults to `rows`, the side that is
*better*, because both sides need the same state store and neither adds a
dependency — the rule is about not requiring infrastructure, not about
preferring the older mechanism.

The fourth axis rises with **request traffic, not corpus churn**, which is why
it is a separate queue and a separate role rather than a `kind` column: sharing
`index_tasks` would put request-rate churn on the index claim path. Two things
it must keep true. **The request path only appends to a bounded ring** — a
ledger write per request puts a database write on the same Postgres the lexical
arm already contends on (`docs/architecture.md`'s measured bottleneck). **The
hot→cold roll never runs under `sync_lock`**, which the scheduler beat holds
across all its work; a multi-million-row Parquet write there stalls incremental
sync for every source. Under pressure the tier drops observations rather than
slowing a request, so formation thresholds count a stream thinned under load: a
busy region forms memory more slowly, not incorrectly.

Service-to-service traffic is durable by construction: pooled keep-alive
connections, batching, full-jitter retry honouring `Retry-After`, a per-endpoint
circuit breaker whose half-open slot admits one probe, failover to another
worker and then to **local preparation**, deadline propagation applied to the
live socket, content-addressed idempotency keys, and heartbeats that extend a
claim while the handler runs. Remote preparation is an *optimization*: no
arrangement of worker failures may change what a sync produces.

Serving durability: bounded request concurrency answering `429` +
`Retry-After` under saturation, and a SIGTERM drain that fails readiness and
keeps serving on a timer thread — never by sleeping on the event loop.

**The fleet's invariants are refusals, not conventions** (`deployment/roles.py`,
one pass at startup). A serving role other than `all` refuses a bind other
machines can reach with neither `security.api_auth.token_env` resolving nor
`behind_authenticating_proxy` set — `all` is exempt so a laptop and every
standalone container keep starting with no configuration at all, but a pod
binds `0.0.0.0` by necessity and the bind address is not a control there. The
graph and worker tokens must not name one variable or resolve to one value:
two boundaries, and workers hold the second by necessity. A `worker` refuses
to hold a DSN, a model key, the IdP token, the graph token, a source list or a
non-SQLite backend, and `server.api.enabled: false` is *enforced* — such a
process answers the probes, `/metrics` and its own `/internal` routes and 404s
the rest. `/internal/*` is exempt from the API token structurally, because
requiring it there would hand every worker the region's front-door credential.

**The graph handoff is announced, and the poll is the backstop.** Each commit
publishes a content-addressed `generation_id` in the publication record and,
on the `nats` backend, announces it (core NATS pub/sub, one subject per kb —
fan-out, since every replica must reload). A dropped message costs one
`graph_refresh_seconds`, which is what lets the event path be at-most-once and
stateless; a broker-less region keeps exactly the poll it had. `/ready` publishes `loaded` beside
`published` so a stale replica is *detectable rather than inferred* (on
`/ready`, off the loop, because comparing them reads `/state` and `/health` is
the liveness probe that does no I/O — it carries `loaded` alone), and every
search response carries the generation that answered it.

**The single-writer ceiling is a metric, not prose.**
`pheasant_commit_authority_saturation` (`sync/saturation.py`) is the busy
fraction of a five-minute window on the process that owns the commit stream;
sustained above `SHARD_THRESHOLD` (0.8) means more workers will not help and
the region should be split. It publishes nothing below a minute of observed
wall time, and nothing at all on a process that is not the commit authority —
a confident zero from an api replica would read as headroom. `pheasant scan`
warns from the same measured coefficients before a corpus reaches it, and
`pheasant shard plan --emit` writes the second region's config, compose file
and secret stubs so acting on a split is a reviewed diff rather than a weekend
of copying.

`src/pheasant/capacity.py` is the single home for sizing coefficients so
`pheasant scan`, `pheasant shard plan` and the docs cannot disagree.

### Sandboxing

Third-party connector plugins can run under `connector.runtime: sandboxed`:
one wasmtime guest per instance with deterministic fuel metering, a
linear-memory cap, and a capability-scoped host-fetch pair. A guest declaring an
import the sandbox never wires fails to **load at all**. Two hot loops
(`resolve_cross_source_edges`, `_scan_edges`) have opt-in WASM accelerators that
fall back to pure Python on any error — acceleration is a performance path,
never a correctness dependency.

---

## 6. Traps this codebase has already fallen into

Each of these cost real time. They are listed because the shape recurs.

- **An UNINDEXED FTS5 column in a `WHERE` clause is a full table scan.** A
  per-artifact `DELETE FROM chunks_fts WHERE artifact_id=?` made indexing
  O(N²); 8,000 files got 6.3× faster once it was skipped on full syncs. Treat
  the pattern as a smell.
- **Under Postgres READ COMMITTED only the *outer* `WHERE` is re-evaluated**
  after a blocking UPDATE's winner commits — not the subquery. The outer clause
  must be a predicate the winner's own write falsifies.
- **A declared FK a maintenance path deliberately violates is fine under
  SQLite and fatal under Postgres.** `memory_records` carried
  `FOREIGN KEY (artifact_id) REFERENCES artifacts(id)`; `delete_artifacts`/
  `delete_source_artifacts` delete the `artifacts` row while intentionally
  leaving the `memory_records` row (preserving earned `uses`/`salience`/
  `observations` — `replace_memory_records` rebuilds the row anyway).
  SQLite never enforces a declared FK (no `PRAGMA foreign_keys=ON`
  anywhere); Postgres enforces every one by default and aborted the whole
  transaction. Two siblings, found the same way: `PostgresBackend.statement()`
  discarded `cursor.rowcount`, so any caller reading it (`subsume_records`,
  `delete_artifacts`) raised `AttributeError`; and one `INSERT OR IGNORE`
  (SQLite-only) needed `INSERT … ON CONFLICT … DO NOTHING`, the portable
  form already used everywhere else in this file. None of the three
  surfaced in the offline suite; all three surfaced in one run against a
  real local Postgres server, first try (`tests/test_backend_parity.py`).
- **A dedup that reports success must dedup into something still reachable.**
  Memory reinforcement folded a write into whatever row carried its
  canonical key, answering `created=False` / `outcome="reinforced"` — "we
  already hold this". When that row was *superseded* or compaction-*demoted*
  it was true of the counter and false of the store: the assertion was
  unreachable through every default query while the caller believed it was
  recorded. `supersede_retention_days` widened the window from one scheduler
  beat to days, which is what turned a latent edge into a live one. The rule
  now: a fold only ever targets a record a default query can return —
  corrected claims become new records, demoted ones redirect through
  `subsumed_by` — and the fold's validity predicate is spelled *exactly* as
  `MemoryPolicy.sql_predicate` spells it, empty-string corner included.
- **A ratio derived from a public enum measures the enum, not the thing.**
  `pheasant_memory_reinforcement_ratio` was computed from
  `writes_total{outcome}`, but `outcome` is public API and deliberately does
  not distinguish "folded a paraphrase" (what reinforcement newly does) from
  "folded a byte-identical repeat" (free since long before it). With the
  feature *on*, exact repeats report `reinforced`, so the gauge counted the
  thing its own docstring said it excluded, and its test passed only by
  fabricating a state the default config cannot produce. A derived metric
  needs its own inputs at its own granularity, and its test needs the real
  write path.
- **A batch insert makes one bad row cost every good row beside it.** A
  queued batch of observations is written inside one transaction, so a single
  event carrying a null `trace_id` — a truncated spool line, a garbled
  payload — raised `IntegrityError` and rolled back the whole batch. The batch
  then nacked, retried, failed identically and dead-lettered: one bad line for
  hundreds of good observations. Validation has to happen *before* the
  statement (`InteractionEvent.is_writable`), because a rolled-back
  transaction cannot drop the one bad row and keep the rest. Found by a test
  written for the batch path, not by reading it.
- **Telemetry ids that are minted twice name two different calls.** The
  interaction ledger mints W3C trace/span ids itself, because they are a row's
  primary key and must exist without the `[otel]` extra. With the extra
  installed the SDK mints its own — so the row and the exported span
  disagreed, and an operator correlating a slow span in their collector to a
  ledger row found nothing, which is most of the reason to export spans at
  all. The span starts first now and the row adopts *its* ids. Caught by
  running against a real SDK, not by the offline suite, which had no opinion.
- **Progress that lives in a process disappears with the process.** An
  evaluation batch is minutes of work, and the first version put its progress
  in the in-memory job registry. That answers neither case that actually
  happens: a browser talking to an API replica that did not start the run, and
  a reader coming back after the container was restarted — where the row also
  said `running` forever, because nothing rewrites a row when a process is
  killed. Phase, counters and a heartbeat are columns now, reclamation runs at
  API boot and on the beat, and each (cohort, variant) replay is checkpointed
  as it finishes so a restart resumes. The same shape as `source_leases`, and
  for the same reason.
- **An index in `CORE_SCHEMA` that names a column a guarded ALTER adds runs
  first, and fails the whole migration.** `CREATE TABLE IF NOT EXISTS` no-ops
  against an existing table, so `idx_evaluation_runs_live` on
  `heartbeat_at` broke every `migrate()` over a `/state` written before that
  column existed — "no such column: heartbeat_at", on boot. It is created in
  `migrate()` after the ALTER now, exactly where `idx_memory_records_canon_key`
  already was for exactly this reason. Found by pointing the CLI at an older
  state directory, not by reading the code.
- **Two staleness clocks over one dead process will disagree, and the gap is
  a feature that lies.** A killed evaluation container releases nothing, so its
  run row *and* its `__evaluation__` lease row are both left behind — and they
  aged out on different windows: `evaluation.run_stale_seconds` for the run,
  `locks.SOURCE_STALE_SECONDS` (45s) for the lease. Set the first below the
  second — the CI region uses 20s so its smoke test need not wait out 90 — and
  the region reports a batch `interrupted`, which invites a resume and says so
  in the UI, then *skips* the resume because a lease nobody was holding claimed
  a live writer. The skip was not loud either: a skipped run carries no gates,
  and `all([])` is `True`, so it reported its gates passed — which
  `pheasant eval run` turns straight into an exit status. Reclamation frees the
  lease on its own evidence and in its own window now (the staleness test lives
  in the `DELETE`, so a legitimate successor survives), and `gates_passed`
  requires a non-empty gate list. Every in-process test killed a batch by
  *raising*, which unwinds the lease's `__exit__` and releases it, so the
  offline suite could not see this: it took a real `docker compose stop`.
- **A capacity coefficient nobody measures is a coefficient that rots.** The
  first two evaluation constants were guesses: seconds-per-replay was 2x over,
  and bytes-per-checkpoint 3x *under* — the dangerous direction, since that is
  the number deciding whether a volume fills mid-run.
  `python -m pheasant.evaluation.benchmark` runs a real batch and prints
  measured beside projected; CI publishes the diff. Same posture as
  `SECONDS_PER_1K_FILES`, which was a curve being quoted as a line until
  someone measured it at two scales.
- **A measurement derived from what a system chose to show measures its own
  confidence.** Mining "appeared at rank 1" out of the interaction ledger as a
  positive would produce a retrieval metric that improves whenever ranking gets
  more *confident*, regardless of whether it gets more correct — and every
  experiment run against it confirms itself. The ledger yields `served` only:
  polarity unknown, weight zero. Utility proof has to come from a surface where
  somebody said so. The same shape one level up: a replay that counted as a
  memory *use* would let the evaluation raise the salience of the records it is
  measuring, which is why the replay searcher is built with
  `usage_tracking=False`.
- **An inherited default that returns a constant is a defect waiting for its
  first caller.** `SqliteBackend` inherited `Backend.statement()`, whose own
  docstring said "nothing calls this default in practice" and which returned
  `rowcount=0` unconditionally. True while only `PostgresBackend` implemented
  it; a live defect the moment anything read that count on SQLite. Every lease
  and batch claim here turns on it (`UPDATE … WHERE status <> 'running'`), so
  the tuning plane's claim reported "I did not get it" on the default backend
  and worked correctly on Postgres — the mirror image of the three portability
  bugs above, and the worse direction, because the offline suite runs on
  SQLite. Found by a second `run_tuning` being refused as "already running"
  against a row that said `completed`.
- **An owner that identifies a *process* cannot arbitrate a race inside one.**
  `open_experiment` inserts `ON CONFLICT DO NOTHING` and reads the owner back
  to learn whether it won — sound only if the owner is unique per *attempt*.
  It was `host:pid`, which looks unique and is not: two threads in one replica
  share it, so both read their own name back and both concluded they had
  claimed the batch. The `__tuning__` lease did not separate them either,
  because `SourceLease` grants a lease its current holder already owns, which
  makes an in-process race indistinguishable from a re-entrant acquire. The
  shape is ordinary — a scheduled batch and a hand-started one live in the
  same process — and it produced two completed experiments where the design
  promises one. Owners carry a uuid now. Found by running three batches from
  three threads; every sequential test passed throughout.
- **A fixed temp filename is a collision waiting for a second writer.** Cold
  payloads were written to `<name>.partial` and renamed. Two batches racing
  wrote the same path, the first rename took it, and the second `os.replace`
  raised `FileNotFoundError` on a file that had just moved out from under it.
  Unique per writer now, and a failed write unlinks its own temp file —
  otherwise unique names would turn one orphan into one per attempt.
- **A sampler that slices a trace id measures the id's shape, not the traffic.**
  Stage sampling read the low four hex characters, reasoning that a W3C trace
  id is random so any slice is uniform. True of ids pheasant mints, false of
  ids arriving in an upstream `traceparent`: an SDK deriving them from a
  counter or a clock leaves the low bits nearly constant, and sampling
  collapsed to all-or-nothing — 100% at a requested 25%, looking exactly like
  a working sampler. Hashed over the whole id now.
- **`StaticFiles(html=True)` is not a single-page-app fallback.** It serves
  `index.html` at `/` and 404s everything else, so every deep link into the UI
  — `/evaluation`, `/memory`, `/tuning` — returned JSON to a browser. Not just
  a broken bookmark: it is what a *hard refresh* does, so anyone who reloaded
  the page they were on had to navigate back in from the root. The screenshot
  script had grown a documented workaround for it. Fixed at the 404 handler,
  which runs after routing has already failed and therefore cannot shadow an
  API route, the `/mcp` mount, or a real asset.
- **A resumed batch that *skips* is not a resumed batch.** Reusing stored
  trials by `continue`-ing past them left the decision with an empty comparison
  set, so a batch that had in fact evaluated everything reported
  `insufficient_evidence`. A checkpoint has to come back as a *comparable*
  result, which means the per-query rows have to come back too: an aggregate
  cannot be paired after the fact, because it has already lost which queries it
  covered. Expensive trials restore from cold storage; cheap ones are redone,
  because redoing them costs nothing.
- **A zero weight is a zero score, not an exclusion.** The mechanism ablation
  isolated an arm by weighting the others to zero — but their candidates stay
  in the merge and are ordered by `best_rank`, so with embeddings off "vector
  alone" returned the *text* arm's ranking verbatim and scored just under it.
  A plausible number measuring the wrong mechanism. Isolation now filters
  which arms contribute at all; zero-weighting keeps its old meaning, because
  an operator turning an arm down wants it to stop influencing the order, not
  to have its documents disappear.
- **A stub embedder scores like a lexical one, because it is one.** The
  offline `stub` provider is a bag-of-words hasher with planted synonyms. A
  "vector arm: 0.70" row from it reads as semantic retrieval and is not, so
  the mechanisms payload carries the provider and `semantic: false`, and the
  UI says so where the number is.
- **A test corpus where every query succeeds proves nothing about ranking.**
  The first tuning fixture had three documents and `max_results: 10`, so every
  query returned its known positive and the stage histogram was pure `served`.
  The plane correctly proposed nothing — and the batch tests passed while
  exercising none of the search. Decoys plus a narrow cut is what makes *rank*
  matter, which is the thing being tuned.
- **`wasmtime.Trap` and `wasmtime.WasmtimeError` are siblings**, not parent and
  child. Catch `guest_failures()`.
- **A mutation harness must `touch` the restored file and purge
  `__pycache__`.** A same-byte-length mutant restored within one mtime tick
  leaves Python running the mutated bytecode, producing a false CAUGHT.
- **A mutant that survives is a question, not a score.** Most survivors here
  turned out to be real test gaps or vacuous tests. One was correct to survive
  and is recorded as uncovered in its module docstring.
- **Test the parser you ship.** A hand-rolled `yaml.py` at the repo root
  shadowed the declared PyYAML for anything run from a checkout, so the suite
  validated against a different parser than the image used. It concealed four
  bugs before it was deleted.
- **`model_dump(mode="json")` must emit plain types.** A `str` *subclass*
  (`PluginSourceType`) is not plain data, and PyYAML's representer dispatches on
  the exact type.
- **Signal handlers run on the event loop.** Sleeping in one stops the process
  answering anything, including the readiness probe the drain exists to flip.
- **A live run finds what the suite cannot.** Container-only bugs have surfaced
  four separate times — read-only mounts, cross-process registry visibility,
  a crash on real-world malformed input. Run the real thing.
- **Deleting a `sys.path` hack breaks whatever was quietly relying on it.**
  The root `sitecustomize.py` put `src/` on the path for anything started from
  a checkout, so CI's container job ran `python -m pheasant` without ever
  installing the package. Removing the shim (right call) turned that into
  "No module named pheasant" in a job whose name says *container*, and the
  publish workflow then skipped silently because it only fires on green CI.
- **A version reference nothing rewrites is a version reference that rots.**
  `sync_version.py` rewrote the files it happened to list, so every compose
  file and manifest added later started outside the net — `deploy/compose/docker-compose.fresh.yml`
  sat three releases behind on a tag users actually pulled. The list of files
  to stage in the release commit is now derived from the script, not pasted
  into the workflow, and `tests/test_version_alignment.py` scans the files
  themselves rather than trusting either list.
- **An image tag the docs name must be a tag something pushes.** The README's
  headline `docker run … ghcr.io/esatt10/pheasant` means `:latest`, and only
  the UI image had ever published one.
- **Record a release only after the registry has it.** The publish job used to
  commit the new version to `main` — into every compose file and manifest —
  before logging in to GHCR, so a failed push left main naming an image that
  never existed. Pushing first inverts the failure into a harmless one: an
  unrecorded image is skipped, because the next increment is computed from
  `max(pyproject, highest published tag)`.
- **A release covers every commit since the last one, not just the last PR.**
  The increment was resolved from `workflow_run.head_sha` alone, so a merge
  that a red CI left unpublished contributed nothing to the version even
  though its code shipped in the next image — a `minor` silently became a
  `patch`. The range is now every commit since the last `chore: release`, and
  the strongest increment among those PRs wins.
- **A skipped job is invisible; a failing one is not.** `container.yml` gated
  its whole job on `workflow_run.conclusion == 'success'`, so a red CI on main
  published nothing and reported it as a *skipped* run — indistinguishable
  from having nothing to do. #52 sat live on main with no image for four days
  behind that skip.
- **A security default that keys off a value you override is not a default
  you have.** MCP SDK 2.x moved transport configuration off the server
  constructor and onto `streamable_http_app()`/`run()`, and with it the rule
  that auto-enables DNS-rebinding protection — which fires only when the bind
  address is loopback. 1.x read that from the *constructor's* own default, so
  pheasant got the guard whatever it bound; 2.x reads the real address, and
  pheasant binds `0.0.0.0`. The mechanical port compiled, served, and
  answered every client — with host checking silently off in every container
  deployment. `_transport_security()` now always builds the settings object
  explicitly. Caught by `tests/test_api_ui_routes.py` asserting an unlisted
  host still gets **421**, which is the only assertion in the suite that
  fails when the guard disappears.
- **An SDK that sorts your exceptions is deciding what your agents can read.**
  MCP SDK 2.x forwards the text of a deliberate `ToolError`/`ResourceError`
  and reports everything else as a bare "Error executing tool <name>", the
  exception's own text kept server-side — right for a crash. 1.x appended
  every exception's text regardless. `PheasantTools` refuses deliberately and
  informatively ("Unknown knowledge base: x", "Unknown source: y", the whole
  `PathPolicyError` remedy) but does it with plain `ValueError`/`KeyError`,
  because it is the HTTP surface's facade too and must not import the SDK. So
  the mechanical port blanked the reason on every refusal across 27 tools and
  11 resources at once: an agent that mistyped a source name was told only
  that something failed. `server.py` translates the anticipated types at the
  SDK boundary — and per *surface*, since `ToolError` raised inside a resource
  handler is stripped exactly like a crash. Found by walking every tool
  against a fake corpus and diffing the refusals against the 1.x server; no
  test had ever asserted on error *text*, so nothing went red.
- **A config flag nothing reads is a comment with a colon in it.**
  `deploy/compose/worker.yaml` declared `server.api.enabled: false` from the
  day the worker tier existed, and no code ever consulted it — so every
  preparation worker served the whole knowledge-base API: register a source
  over any allow-listed path, rewrite the config, read what it finds. On the
  tier designed to hold nothing, scaled hardest, and running third-party parse
  code. The flag looked like the trust boundary and was documentation of an
  intention. Enforced now at the one place that can be checked (a 404 for
  anything outside the probes, `/metrics` and `/internal/*`), and asserted by
  a test that drives a real client at a route the flag claims does not exist.
  The general shape: a setting with no reader is worse than no setting,
  because it stops anyone looking for the missing enforcement.
- **A convenience in a deployment file can undo a boundary the code got
  right.** The worker tier is deliberately given no database, no keys and no
  volumes, and it holds the indexing token by necessity. The shipped Compose
  file then wired `PHEASANT_GRAPH_SERVICE_TOKEN` to
  `${PHEASANT_INDEX_WORKER_TOKEN}` — one fewer value to generate — so any
  compromised worker also held the credential for the internal graph API,
  which serves the whole graph. Nothing in the Python was wrong. Two variables
  is the fix; the refusal when they resolve equal is what keeps it fixed, and
  it belongs beside the other startup checks rather than in a README.
- **A security posture that is right for one process is a default that ships
  into the fleet.** "Unauthenticated, on loopback" is defensible for a
  local-first container and was carried unchanged into the role split, where
  pods bind `0.0.0.0` because a Service cannot reach a loopback-bound one — so
  the control the docs named did not exist in the deployment they described.
  The fix is not a bigger warning: `all` stays exempt (rule 7), and every
  other role refuses to start without either a token or an explicit
  "an ingress authenticates this". The general shape: when a control is a
  *property of the deployment* rather than of the code, it must be re-derived
  per deployment shape, not inherited.
- **Two mechanisms for one idea, and only one of them tunable.** Retrieval
  over-fetches when a post-filter will drop rows, so `max_results` keeps
  meaning "give me this many". That existed twice: `ranking.filter_overfetch`
  governed the ACL/section/memory filters, and each *surface* carried its own
  hardcoded `max_results * 4` for retrieval criteria — with the vector arm
  holding a third copy and the assistant's retrieval path doing no over-fetch
  at all while still post-filtering. The parameter is declared tunable, bounded
  and mapped to the `filters` stage, and the tuning glossary tells an operator
  that a `filters` miss may mean it is too small; so a bundle could be promoted
  on a parameter that half-governed the stage it was attributed to, and an
  operator following that advice would see no effect. The arithmetic lives in
  one method now and a test greps for a numeric multiplier on a result count
  anywhere else — which immediately found a *sixth* site, in vocabulary
  publication. That one is genuinely a different concern, so it got a name
  (`VOCAB_OVERFETCH`) rather than the parameter: the guard's real output is
  forcing the distinction to be stated.
- **A lesson can propagate while the code does not.** `all([])` is `True`, so a
  skipped evaluation run reported that it passed the gates it never evaluated.
  The tuning plane, written afterwards, shipped its *own* copy of the guard,
  with a docstring citing the evaluation plane's incident as justification. Two
  implementations of one invariant, and a third plane would have started from
  zero. `pheasant.decision` owns the vocabulary now, and the invariant is a
  constructor precondition rather than a remembered check: `GateSet` refuses to
  be built empty, "no gates" is the *absence* of a GateSet, and absence has no
  `passed` to misread.
- **A lambda closes over the name, not the value.** Consolidating the memory
  policy into one rule with two renderers put each clause's SQL fragment in a
  lambda. Two clauses built a placeholder string into a local called
  `placeholders`, so the steering fragment rendered with the scope clause's
  placeholder count and SQLite refused the statement outright. Caught by the
  parity test the consolidation was supposed to make redundant — which is the
  point worth keeping: removing the drift *between* two renderings does not
  make either rendering correct, so the test that compares them stays.
- **`get_args(list[X])` is also `(X,)`.** Deriving config construction from the
  dataclasses meant asking "does this annotation name a nested section" and
  "does it name a Path". Both answers unwrap unions — and a `list[Path]`
  unwraps identically to `Path | None`, so the whole list went to `Path()`, and
  `list[SourceConfig]` looked like a section and was handed to a constructor
  with three required arguments. The origin has to be checked before the args.
  Both surfaced immediately here; the second was latent (the only such field is
  skipped by name), which is the kind that waits for the second one.
- **Backend coverage behind a path filter is coverage nobody can reason
  about.** Postgres — the scale-out backend, and the one the offline suite
  cannot see — ran only in the evaluation, memory and tuning workflows, each
  gated on `paths:`. So whether a change was tested against it depended on
  which files it happened to touch. Measured when the unfiltered job was
  added: three of the twenty-two modules carrying dialect branching matched no
  filter at all, and one was `sync/locks.py` — the per-source lease several
  indexers rest on, and the exact file where a discarded `cursor.rowcount`
  once made a claim fail on SQLite and pass on Postgres. `ci.yml` now runs the
  whole suite against a real Postgres on every PR, and
  `tests/test_workflow_coverage.py` fails if that job gains a filter, stops
  running Postgres, or stops setting the DSN. The second-order lesson is in
  that test: narrowing the run to a curated list of dialect-sensitive *tests*
  would have been the same staleness one level down, so it runs everything.
- **A startup refusal has to key on what a process does, not on what its
  config could describe.** The "unauthenticated API on a routable bind" check
  was applied wherever `validate_role` ran — including `pheasant worker
  --transport grpc`, which binds a gRPC port and never starts the HTTP app at
  all. It therefore demanded an API token for a surface that does not exist.
  The shipped `worker.yaml` sets `server.api.enabled: false` and so happened
  to satisfy it, which is why a real run and the whole suite both passed: it
  was one config away from refusing a working deployment, in the direction
  guards are most expensive to be wrong in. `serves_http=False` says so at the
  call site now. Found by writing the test for an unrelated worker bug, which
  is the usual way.
- **A ceiling stated only in prose cannot be reached by anyone reading a
  dashboard.** One indexer is the sole commit authority, which every design
  document said and no metric showed — so "we scaled workers and ingest
  stopped improving" and "retrieval is mistuned" produced identical symptoms.
  Two decisions made the gauge honest rather than merely present: it publishes
  `None` below a minute of observed wall time (two busy seconds in a pod's
  first four are not 50% saturation, and the response to a misread is to shard
  a region that is doing nothing), and it is absent entirely on a process that
  is not the commit authority, because a confident `0.0` from an api replica
  reads as headroom. Same posture as `tuning.health` below its sample floor.
- **"One facade, exposed twice" is a claim only a test can hold.** The HTTP and
  MCP surfaces were documented as one API and were two implementations of it:
  `api/app.py` mentioned `PheasantTools` once, for introspection, and two of
  its docstrings said a function "mirrors" a tool it had in fact drifted from.
  Four of the five shared operations differed, and the worst was silent —
  `relevant_files` applied the memory policy over HTTP and not over MCP, so the
  surface built for agents could serve a record the region *knew* had been
  superseded, while the surface a human watches could not. Nobody decided any
  of it; it is what happens when a fix lands on the surface whose bug report
  arrived. The extraction is only half the answer: `tests/test_surface_conformance.py`
  drives both surfaces over one corpus and asserts identical results *and*
  identical refusal text, because two spellings of one refusal is the same
  defect one level down.
- **A conformance test finds bugs in the thing it was written to protect.**
  The matrix's first run reported `graph_generation: null` on MCP and a real
  value on HTTP — not a divergence between the surfaces but a defect in the
  event-driven graph reload committed a week earlier: `loaded_graph_generation`
  was set when a replica *read* a graph and never when a process *published*
  one, so `role: all` — the default, every standalone container — reported
  "no generation loaded" forever, and the staleness signal `/ready` publishes
  was unusable exactly where most deployments live. The engine adopts the
  generation it just committed now. The general shape: a test that compares two
  independent paths reveals things neither path's own tests can, because each
  was written by someone who already believed the answer.
- **Not every divergence a comparison finds is a bug to fix in that commit.**
  The same matrix then found the graph arm ordering equal-scoring nodes one way
  from an in-memory graph and another from the same graph loaded off disk.
  Real, and *not* fixed there: a deterministic tie-break inside a retrieval arm
  is a ranking change, it moves results for every deployed region, and it needs
  its own evidence rather than a line in a refactor. The fixture equalises the
  two by reloading, the divergence is written down, and the change is a change.
  A refactor that quietly fixes ranking is a refactor nobody can review.
- **A package can span two layers, and the layer test is how you find out.**
  `assistant` was classified as application-level because it orchestrates
  retrieval to answer a question — true of `chat.py`, `retrieval.py` and
  `workflows/`, and false of `catalog.py`, `providers.py`, `llm.py` and
  `credentials.py`, which are model-access adapters that `memory/synthesis.py`
  and `setup_wizard.py` legitimately reach for. Declaring one layer per
  top-level package would have forced a choice between a false failure and
  deleting the rule. Layers resolve by longest declared prefix instead, so the
  split is stated where a reader can see it — and `memory` importing
  `assistant.chat`, the edge that would actually be wrong, still fails.
- **Extracting a layer inverts dependencies you did not know you had.** Two
  fell out of the same test run and neither is about HTTP: `services/graph.py`
  held the pure hierarchy-first walk *and* the ACL guard, so the assistant
  could not reach the walk without importing the service layer (split into
  `graph/traversal.py`), and `sync/worker.py` imported `pheasant.cli` for one
  progress-marker string, putting the whole CLI under the worker (the constant
  moved to the side that parses it, and `cli.py` re-exports it). Both had been
  invisible for as long as they existed, because nothing had ever asked which
  direction they pointed.
- **A tidy `WHERE` clause can be a full table scan, and the plan is the only
  way to know.** The row backend addresses edges by exact endpoint pair, which
  needs an `OR` chain — `source IN (…) AND target IN (…)` is a cross product
  and over-matches, which would fold digests out that were never replaced and
  delete edges nobody touched. Written the obvious way, `WHERE kb_id=? AND
  ((source=? AND target IN (…)) OR …)`, SQLite cannot use its multi-index-OR
  optimization at all: each branch has to be independently indexable, and
  `source=?` alone is not a prefix of `(kb_id, source, target, type, seq)`.
  `EXPLAIN QUERY PLAN` said `SEARCH … USING INDEX idx_graph_edges_target
  (kb_id=?)` — the whole table. Repeating `kb_id` inside every branch gives
  `MULTI-INDEX OR` with exact primary-key lookups. Measured: **300 ms per
  incremental commit at 100k files versus 1 ms**, and the tidy form was
  O(total graph) — which would have made the row backend cost exactly what the
  file backend cost, for exactly the same reason, one layer down. It shipped
  green: every test passed, because a fixture with twelve edges cannot tell a
  scan from a seek.
- **`COUNT(*)` on a commit path is the same mistake wearing a different hat.**
  The first version asked "is this knowledge base empty" with a count and
  published counts by counting. Two full scans per commit, 364 ms at 100k
  files and growing — again O(total) for an O(changed) write. The emptiness
  probe is a `LIMIT 1` (the trick `NodeIndex.populated` already documents for
  exactly this reason) and the counts are maintained from the exact number of
  rows each delta inserts and replaces, which the fold bookkeeping already
  knows. `recount()` is the honest scan, for repair and for the tests.
- **XOR is an involution, so folding a row out twice folds it back in.** The
  published generation id is a XOR over every row's content digest, which is
  what makes it maintainable in O(changed). It is also what makes a duplicate
  fatal: an edge reached both as a replaced pair and as the casualty of a
  removed endpoint would be folded out twice and silently return, and the
  published id would be wrong with nothing else to notice. `_digests_for`
  keys by primary key rather than accumulating a list, so identity — not
  arrival — decides.
- **An index with no reader is a config flag with no reader.** Two of the four
  indexes first written for `graph_edges`/`graph_nodes` looked obviously
  useful (`source_id` on edges, `artifact_id` on nodes) and nothing queried
  either: per-source deletion goes through the node table and cascades by
  endpoint. They measured 50 MB of a 1,085 MB database between them and, worse
  than the bytes, they looked like the mechanism — the next person to ask "how
  does per-source deletion stay cheap" would have found the wrong answer.
- **A test whose fixture cannot exhibit the failure is a test that passes.**
  Three separate defects above — the OR-chain scan, the `COUNT(*)`, and the
  graph service reading `graph_builder.graph` instead of the serving graph —
  were invisible to the offline suite and obvious the moment
  `python -m pheasant.graph.capacity` ran at 100k files. Two of the three were
  *performance* bugs of the exact class this change existed to remove, and the
  third returned a plausible zero. The benchmark is the test for them, which
  is why it is a module and not a script somebody ran once.
- **A timestamp nothing reads was the one input making a content-addressed id
  move.** `GraphBuilder.upsert_node` stamped `updated_at` on *every* upsert,
  so re-asserting an unchanged node changed its bytes — and the published
  graph generation is a digest of those bytes on the file backend and of that
  row on the row backend. An unchanged corpus therefore published a new id as
  soon as the clock ticked, and every replica reloaded a graph identical to
  the one it held, on every beat: 4.9s per replica per beat at 100k files, for
  nothing. It predated both backends and nothing had ever asserted the
  property `generation_id`'s own docstring promises. Only `graph_search`
  (which skips it) and the Parquet export read the field, so holding it steady
  is free — and makes the exported value mean "when this node last changed"
  rather than "when a sync last ran". The stamp moves on a real difference and
  not otherwise, which is the rule `created_at` already had.
- **The same fix exposed an in-place mutation the delta could not see.**
  `upsert_edge` updates an existing edge through the dict `get_edge_data`
  hands back, so the change never passes through `add_edge`. Free on a backend
  that re-serializes everything; a silently dropped write on one that writes
  only what it is told changed. `touch_edge` marks the pair. The general
  shape: moving from "write it all" to "write the delta" makes every
  mutation-by-side-effect a correctness question, and they do not announce
  themselves.
- **A `full` re-index still moves the generation id, and that is written down
  rather than fixed here.** It removes a source's nodes and rebuilds them, so
  `created_at` legitimately resets. Arguably wrong — "full" ought to mean
  "rebuild the same graph", not "rebuild it with new birthdays" — but
  `created_at` would have to come from the artifact rows rather than from a
  graph that was just wiped, which changes what a full sync *means*.
  `tests/test_graph_backends.py` asserts stability on `incremental`, where it
  genuinely holds, and says why.
- **A pass that walks the whole graph for a slice of it keeps the whole graph
  alive.** Three did, and none of them was written wrongly — they were written
  when the whole graph was already a dict in front of them, so narrowing had
  no visible benefit. It has one now: the walk *is* the reason the working set
  exists. `add_cross_source_edges` was the clearest, handing the resolver
  `dict(attrs)` for every node when both the Python and WASM resolvers filter
  arrivals to artifacts-with-a-path plus `external_reference` stubs — 15% of a
  real graph, and the copy roughly doubled peak memory to build a list whose
  first act was to throw three quarters of it away. The general shape: when a
  data structure stops being free, every consumer written against "it is free"
  becomes a question, and they do not announce themselves either.
- **Dead code that walks is worse than dead code that sits.**
  `add_similarity_edges` keyed off `concept_terms`, and concept extraction was
  retired — so `_base_concepts` returns an empty enrichment and nothing has
  carried a concept term since. The pass still walked every node *and copied
  every artifact's attributes* to emit zero edges, on every sync. The concept
  retirement's own third justification was already "the live graph contained
  zero `similar_to` edges"; what nobody went back for was the walk still being
  paid to produce them. `tests/test_graph_working_set.py` asserts both halves —
  the pass emits nothing, *and* no node carries a concept term — so if
  enrichment ever starts emitting them again the no-op is caught rather than
  silently dropping edges.
- **An efficiency claim needs a bound, not a stopwatch.** Every assertion in
  `tests/test_graph_working_set.py` counts what the code *touches* — the node
  types handed to the resolver, whether a whole-graph snapshot was taken — and
  none of them times anything. A timing test measures the CI runner and goes
  flaky; a bound fails exactly when someone puts the work back. Which also
  means the change that could not be bounded is the one that did not ship: the
  `remove_nodes_from` rework measured 122.5ms against 126.0ms, so it was
  reverted rather than kept for looking better.
- **A micro-benchmark that picks a convenient starting node measures the easy
  half.** `graph/capacity.py` timed a bounded 3-hop walk at **0.12 ms** and
  the same walk on a real corpus took **220 ms** — because the benchmark
  started from an arbitrary node with a handful of edges and a real walk
  starts wherever the caller points, often at a `source` node that indexes
  every artifact in the region. Three separate defects hid behind that gap and
  every one of them was invisible to the whole test suite: the walk prefetched
  attributes for every *reachable* target rather than the ones it would keep
  (8,040 node rows to return 100); every edge's JSON attribute blob was parsed
  to read `type`, which is a column and not in the blob (8,040 `json.loads`,
  42% of the walk); and the rows were grouped by endpoint pair twice, once in
  the store and again in the caller. The benchmark measures a hub explicitly
  now, next to the ordinary node, because reporting only the ordinary one
  described the case that was already fine.
- **The same N+1 appeared one layer up, in the graph search arm.**
  `graph.nodes.get(node_id)` is a dict lookup on a resident graph and a
  `SELECT` on a stored one, so the obvious loop over the index's candidates
  issued **~2,000 single-row queries per search** — and the cost scaled with
  how *unselective* the query was rather than with what it returned, which is
  the worst possible shape. Batched, that is 4 queries. The general lesson:
  moving a data structure behind a store turns every `.get()` in a loop into a
  round trip, and they are spread across modules that never mentioned storage.
- **A filter the caller applies afterwards is a filter the store should have
  applied, and only a store makes that visible.** `slice_` keeps a link only
  when *both* endpoints are inside the slice, and established that by fetching
  every out-edge of every node it held and discarding the rest. On a resident
  graph the discard is a comparison; against rows it is the whole cost — a
  51-node slice containing one `source` node read **3,580 edge rows to draw
  ~150 links**, 16.7 ms of a 46 ms call, and it scaled with the region rather
  than with the slice. The store answers the *induced sub-graph* now (`source
  IN (…) AND target IN (…)`, where the cross product is precisely the question
  being asked, unlike `_pair_clauses` where it would over-match): 0.96 ms, 80
  rows, byte-identical answer, and the whole call 46 → 17 ms. Found by
  counting rows per query on a real five-repository region, because the
  fixture's hub has four edges and cannot tell 3,580 from 150.
- **A bound is only sound when nothing downstream can reject what it counted.**
  A bounded walk fetched a whole level's out-edges before expanding it, so a
  hub returned 1,620 pairs to keep 100. The budget now reaches the fetch, and
  the two things that made it hard are both worth keeping:
  **the ordering has to be per-pair.** `_hierarchy_first` ranks an endpoint
  *pair* by whether **any** of its edges is `contains`, so a per-edge `CASE`
  splits a pair carrying both a `contains` and an `indexes` edge across two
  rank groups — and the store's one-pass grouping then emits that pair twice
  with half its edges each. It is a `MIN(CASE …) OVER (PARTITION BY source,
  target)` window aggregate, in both dialects.
  **And the budget is only enough when nothing between the fetch and the
  result can reject a pair.** `max_nodes + visited` pairs per source is
  provably sufficient because at most `visited` targets can be skipped as
  already-seen — but an `edge_types` filter, an `exclude_edge_types` or an
  `exclude_node_types` rejects for reasons the count knows nothing about, and
  a hub whose 1,600 `indexes` pairs are all filtered out needs every one of
  them to reach its 20 `contains` pairs. Those walks ask for **no** limit and
  stay exactly as slow as they were. `_frontier_budget` is that rule, in one
  function, with a test that fails if someone simplifies it away.
  Measured: 14.5 ms → 7.3 ms for the walk, 17.1 → 9.8 for a slice, and the
  same 20 walks over the fleet region return byte-identical results.
- **The statement was the wrong thing to time.** The ranked frontier is
  *slower* as a query — 2.96 ms against 2.54 ms, because the server sorts
  1,620 rows to return 120 — and 2.7× faster as an operation: **10.40 ms
  against 3.81 ms** once the rows are decoded and grouped. Rows that never
  arrive are rows nobody builds a `Row` dict for, decodes into `_LazyAttrs`,
  or groups into pairs, and that is where the time was. Timing the SQL alone
  said do not do it.
- **A lazy mapping is a cost to a reader that wants all of it.**
  `_LazyAttrs` exists because a traversal hop reads `type` and nothing else,
  and it is exactly wrong for the search arm, which reads every attribute of
  every candidate: `dict(lazy)` goes through the mapping protocol, one
  Python-level `__getitem__` per key, over 1,836 candidates a query.
  **8.0µs per node against 3.75µs** for the same dict built in one step. The
  fix is not to choose one — it is that the *caller* says which it wants
  (`prefetch_nodes(..., materialized=True)`), because only the caller knows.
  The streaming iterators, which feed exports that read everything, were on
  the wrong side of this too.
- **Two generator expressions per field, six fields per node, 1,836 nodes.**
  `_field_score` asked `all(token in low …)` and then `any(token in low …)` —
  the same scan twice in the "some but not all" case, and two generator
  objects created every time either way. The profile put ~1.1M generator
  `next` calls plus the `any`/`all` frames at a fifth of the whole graph arm.
  One counting loop is **10.85µs against 3.66µs** for a node's twelve fields
  and returns the identical ladder. The general shape: a comprehension is not
  free at the bottom of a loop that runs ten thousand times a request, and a
  profile is the only thing that says which loop that is.
- **Two tables in one database do not need a round trip between them.** The
  graph arm asked `graph_nodes_fts` for candidate ids, shipped up to 2,000 of
  them into Python, and sent every one straight back down as a bind parameter
  for `graph_nodes` — a semi-join the planner would have done, spelled as a
  round trip because the two tables live behind different objects. 22.0 ms
  against 14.6 ms, seven statements per search against three, and the gap
  grows with how *unselective* the query is, which is the shape that has now
  bitten three times here. `NodeIndex.candidate_query` hands over the
  `SELECT` unexecuted and `GraphRowStore.nodes_matching` runs it as a
  subquery; neither module imports the other.
- **`IN (?,?,?)` makes the statement text a function of the batch size.**
  psycopg3 prepares a statement after its fifth execution of the same text,
  so a key-set predicate that never repeats its spelling is one that is never
  prepared and re-planned on every call — **8.93 ms against 6.15 ms** for a
  500-key node fetch. Postgres gets `= ANY(?)` with the list as one array
  parameter: one text, one plan, every batch size. SQLite keeps the `IN`
  list, which is its reference spelling and has no array type to bind. It is
  `Dialect.in_clause`, so the choice is made once rather than at each of the
  call sites that batch.
- **Two fixes were built, measured and thrown away in the same pass.** An
  out-adjacency index for `remove_nodes_from` measured 122.5ms against
  126.0ms, and an exact short-circuit in `_node_score` — provably identical
  output, skipping the attribute scan when `label`/`name` already scored above
  everything that follows — did not fire on a real corpus, because labels are
  paths and the query words are in the body. Both were reverted. A change that
  cannot show a number is not an optimization, it is a diff; and one that
  touches ranking without showing a number is worse than that.
- **A counter derived from every write measures writes, not the thing its name
  promises.** The receipt ledger's `submissions` was supposed to prove a
  caller's retry had been absorbed — three writes under one key, one stored
  object, `submissions == 3`. Crossing the index barrier writes that row too,
  and the first version counted it, so three submissions plus one
  acknowledgement reported four: a number that moves when *the region* acts, in
  the field a harness reads to prove *it* retried. Exactly the shape
  `pheasant_memory_reinforcement_ratio` was already caught by, one plane over.
  `counts_as_submission=False` on the acknowledgement, and the probe that found
  it is now a test.
- **A flag nobody declared reads exactly like a flag nobody set.** The retrieval
  lineage reported `memory.enabled` by asking `config.memory.enabled`, which
  does not exist — memory is a *source*, and `memory_source()` is the predicate
  the rest of the region uses. `getattr(..., False)` made it always `False`, so
  a region with memory fully on reported it off, and the probe that checked the
  field agreed with it. Two mistakes that cancelled into a plausible answer. It
  also has to pass the state store, because a memory source enabled from the UI
  lives in the runtime registry and reaches `config.sources` only in the process
  that created it — a check reading the config alone reports "off" on every
  replica but one. The mirror of "a config flag nothing reads": a *reader* with
  no flag is just as silent.
- **A capability list that is written down says what somebody believed when
  they last edited it.** The readiness contract's first version named
  `MCPServer`, `Bundle` and `SourceType.DOCUMENT_FOLDER` — three symbols that
  do not exist, in a document whose entire purpose is telling a harness what it
  may rely on. All three survived review and died on the first `pheasant
  readiness check`, because the contract resolves every name it publishes. The
  general shape is `tests/test_config_surface_freshness.py`'s: a list that is
  checked against the code cannot rot, and one that is not will.
- **A digest over a *result* cannot be the id of a *shape*.** The capability
  snapshot's digest is what a harness pins to detect the region changing
  underneath it between arms. It was computed over the published capability
  rows — which carry `status`, which moves with a probe outcome — so the digest
  changed when a latency probe was a millisecond slower, and a harness watching
  it would have seen the region "change shape" on every check. Same family as
  the `updated_at` stamp that made a content-addressed graph generation move on
  an unchanged corpus: an id has to be a function of exactly the thing it
  names.
- **A refusal code outside the table is a code nobody can act on.** MCP's
  `ToolError` carries a string and nothing else, so the contract publishes the
  code table and an agent maps the text it received. `ContaminationRefused`
  lived in `services/ingestion.py` while the table is derived from
  `services/errors.py`, so `CORPUS_DENYLISTED` — the one refusal that means
  "this must never be in the corpus" rather than "try a smaller file" — was
  absent from the only place a client could learn it. Found by the test that
  asserts the table is derived rather than typed twice. A vocabulary with two
  homes has one home nobody reads.
- **"All the gates I could evaluate passed" is not "the gates passed".** The
  readiness plane refuses an empty `GateSet` — `decision.py` makes that
  structural — and then reported a gate set as **passing** when three of its
  four gates had been *skipped*, because the one that ran passed. The empty set
  had moved: it was no longer the gate list but the part of it nobody could
  run. Caught on the first check against a real region, where memory and ACL
  enforcement were both off. A verdict is tri-state now, `INCOMPLETE (1 of 4
  gates evaluated)` is a heading rather than a caveat further down, and the
  lesson generalises past gates: any invariant of the form "every X passed"
  needs to know how many X there were supposed to be.
- **A healthy region and a measurable one are different claims.** With
  `corpus_denylist` empty there is no benchmark boundary to prove, so the
  contamination probe skips and the *core* gate set is incomplete — on a region
  where every probe that ran passed. That surprised the author of the test that
  asserts it, which is the point: "nothing is wrong here" and "this is ready to
  be measured" are separate sentences, and a plane that conflated them would
  certify a region that had never been asked to hold an isolated experiment.
- **Extracting a plane inverts dependencies you did not know you had — again.**
  `services/ingestion.py` needed the placement rules in `api/uploads.py`, and a
  service importing a transport is the upward edge `test_service_layering.py`
  refuses. Nothing in that module was ever about HTTP — `safe_filename` defends
  a filesystem — and it was in `api/` only because the first caller was a
  route. It is `ingestion/landing.py` now. The same run also put three modules
  over the size ratchet, and in every case the split the ratchet asks for was
  the right change rather than the toll: the receipt ledger out of
  `state_store.py`, the readiness tools out of `mcp_server/tools.py`, and the
  readiness routes out of `api/app.py` — the last being the "one router per
  plane" that file's own ceiling comment names as its fix.
- **A control enforced at one of several doors is a door with a sign on it.**
  `readiness.corpus_denylist` refuses content that may never become
  retrievable — a benchmark answer key an experiment scores against. It was
  enforced in `services/ingestion.submit` and nowhere else, so a region with
  the denylist configured still indexed that file when it arrived through a
  folder source, the UI drop zone, a git repository or any connector; and the
  Core gate that exists to prove the boundary reported it intact, because the
  probe only knocked on the door that was locked. Two things fixed it and both
  generalise. The rule moved to `security/corpus_policy.py` at layer 1, because
  the other caller is `sync/` and `sync/` cannot import `services/` — a control
  that protects *the corpus* belongs where everything enters the corpus, not
  where the first caller happened to be. And the probe now asserts the
  *outcome* rather than the mechanism: it plants a file the way a folder source
  would, and it scans `artifacts` for anything the denylist forbids, so the
  gate is a statement about this corpus rather than about this code and can
  fail on a region whose code is correct. Verified by deleting the enforcement
  line and watching the check turn NO-GO — a gate nobody has seen fail is a
  gate nobody knows works.
- **A measured field in a response breaks every whole-payload comparison
  downstream.** `lineage.timing.retrieval_ms` is the only non-deterministic
  field a search returns, and adding it broke a parity test in
  `tests/test_taxonomy.py` that compared two responses outright. Keeping it is
  right — a harness needs retrieval latency separated from transport time — so
  the fix was to split `state` from `timing` in the payload, and then to assert
  in `tests/test_readiness_plane.py` that `retrieval_ms` stays the *only* such
  field. Otherwise the next one arrives as a test quietly ignoring one more key.
- **An incremental sync leaks a chunk node per edit. Open, pre-existing, not
  fixed here.** Chunk node ids embed the chunk's sha256, so editing a file
  produces *new* chunk nodes — and nothing removes the old ones on the
  incremental path: `remove_artifact_nodes` exists and does exactly the right
  thing, but its only caller is memory maintenance. A full sync is correct,
  because it clears the source first. Measured on a real region: one file
  edited three times ends with **four** chunk nodes and four `has_chunk`
  edges, and the count rises by one on every subsequent edit, so an actively
  edited corpus accumulates orphaned chunks without bound — graph memory,
  traversal budget and `graph_nodes` rows spent on content that no longer
  exists. Reproduced identically on the commit before the working-set changes,
  so it is not one of theirs. Left alone deliberately: wiring removal into the
  incremental path changes what an incremental sync *does*, which is a
  correctness change with its own blast radius and deserves its own evidence
  rather than riding along with an efficiency pass.

---

## 7. Pointers

- **Architecture:** `docs/architecture.md` · **Graph taxonomy:**
  `docs/graph_model.md` · **Config:** `docs/configuration.md`
- **Setup:** `docs/how-to/setup.md` (the wizard is `src/pheasant/setup_wizard.py`)
- **MCP:** `docs/mcp_tools.md`, `docs/mcp_client.md` ·
  **HTTP:** `docs/reference/http-api.md`
- **Scale:** `docs/how-to/capacity-planning.md`,
  `docs/how-to/worker-fleet.md`, `docs/how-to/indexing-performance.md`
- **Security:** `docs/security.md` — the trust model for one container, the
  fleet's three boundaries, and which startup refusals enforce them
- **Analytics/exports:** `docs/how-to/parquet-exports.md`,
  `docs/reference/export-schema.md` (the contract an outside reader gets)
  — `/exports` is a PVC/named volume an outside reader mounts; nothing is
  served over HTTP
- **Memory:** `docs/memory-system.md`, `docs/how-to/agent-memory.md`
- **Observation & formation:** `docs/memory-formation.md` — the two planes,
  the log tier, and the two combination designs that were rejected
- **Evaluation:** `docs/knowledge-effectiveness.md` — the evidence taxonomy,
  the cohort split, the ablation matrix, the gates, and what it refuses to claim
- **Tuning:** `docs/retrieval-tuning.md` — the stage model, why re-fusion makes
  a parameter search affordable, and the gates that keep a winner from being
  promoted by its own evidence
- **Readiness:** `docs/stress-test-readiness.md` — the capability contract, the
  probes that turn a claim into evidence, the four go/no-go gates, and the two
  capabilities this build declares it does not have
- **Synapse region spec:** `docs/SYNAPSE_INTEGRATION.md`
- **Deployment:** `docs/deployment.md`, `deploy/kubernetes/`
