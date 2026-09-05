# Interface matrix

pheasant exposes the same capabilities across several surfaces: the **CLI**, the
**HTTP API**, the **web UI**, and **MCP** (for agents). This page maps each
capability area to the concrete command, route, or tool on each surface so you
can pick whichever fits your workflow.

Legend: — means "not offered on this surface"; use one of the others.

## Configuration

| Capability | CLI | HTTP | Web UI | MCP |
|---|---|---|---|---|
| Generate starter config | `pheasant init --profile <p>` | — | — | — |
| Validate config | `pheasant validate <file>` | — | Config editor (diff preview) | — |
| Show resolved config | `pheasant config show --effective` | `GET /config`, `GET /config/effective` | Config editor | — |
| Edit config | (edit YAML) | `PUT /config` | Config editor (form + raw YAML) | — |
| Environment doctor | `pheasant doctor` | `GET /health`, `GET /ready` | — | — |
| Generate compose env | `pheasant compose-env <file>` | — | — | — |
| Generate VS Code MCP config | `pheasant client-config vscode` | — | — | — |

## Exploration (sources & sync)

| Capability | CLI | HTTP | Web UI | MCP |
|---|---|---|---|---|
| List knowledge bases | — | `GET /knowledge-bases` | — | `list_knowledge_bases` |
| List sources | — | `GET /sources` | Sources page | `list_sources` |
| Set up from one target (path/URL/glob) | `pheasant up <target>…` | `POST /sources/quick-add` | Sources → **+ Add source** | — |
| List registerable source types (built-in + plugins) | — | `GET /sources/types` | Sources → Advanced… (type picker) | — |
| Register a source (full schema) | (edit YAML) | `POST /sources` | Sources → **Advanced…** | `register_source` (`sync_now`, `wait`) |
| Update a source | (edit YAML) | `PUT /sources/{id}` | Sources → edit | — |
| Disable a source | (edit YAML) | `POST /sources/{id}/disable` | Sources page | `disable_source` |
| Remove a source | (edit YAML) | `DELETE /sources/{id}` | Sources page | `remove_source` |
| Promote runtime source to config | — | `POST /sources/{id}/promote` | Sources → promote | `promote_runtime_source_to_config` |
| Sync one source | `pheasant sync --source <name>` | `POST /sync/{id}` | Source manager | `sync_source` |
| Sync all sources | `pheasant sync --all` | `POST /sync` | Source manager | `sync_all` |
| Start/follow background sync | `pheasant sync --progress` | `POST /sync/{id}` (`wait=false`), `GET /jobs/{job_id}`, `GET /jobs/stream` | Source manager | `start_sync_source`, `get_job`, `list_jobs` |
| Repair state | `pheasant repair`, `pheasant sync --mode repair` | `POST /sync` (mode) | — | — |
| Sync status | — | `GET /sync/status` | Source manager | `get_sync_status` |
| Sync history | — | `GET /sources/{id}/history` | — | `get_sync_history` |

## Retrieval (search & graph)

| Capability | CLI | HTTP | Web UI | MCP |
|---|---|---|---|---|
| Search (text/graph/vector/hybrid) | — | `POST /search` | Search page | `search_context` |
| Ask a question (grounded answer + citations + facts) | — | `POST /assistant/chat` | Chat pane | `ask_knowledge_base` |
| List answering workflows | — | `GET /assistant/workflows` | Chat → **Workflow** | — |
| Supply an LLM key for a session | — | `POST /assistant/key` | Chat → **Connect model** | — |
| Relevant files for a task | — | `POST /relevant-files` | — | `get_relevant_files` |
| File summary | — | `GET /files/summary` | Node inspector | `get_file_summary` |
| Repo map | — | `GET /sources/{id}/repo-map` | — | `get_repo_map` |
| Node content | — | `GET /nodes/content` | Node inspector | — |
| Explain a node | — | `GET /nodes/explain` | — | `explain_node` |
| Graph neighbors | — | `GET /graph/neighbors` | Knowledge panel | `get_graph_neighbors` |
| Browse filesystem | — | `GET /fs/list` | Sources → Advanced… | — |

## Semantic search (embeddings)

| Capability | CLI | HTTP | Web UI | MCP |
|---|---|---|---|---|
| Inspect embeddings status + coverage | — | `GET /search/embeddings` | Settings → Semantic search | — |
| Enable / configure embeddings | (edit YAML) | `PUT /search/embeddings` | Settings → Semantic search | — |
| Embed already-indexed content | `pheasant sync --mode full` | `POST /search/embeddings/reindex` | **Build missing vectors** | — |

## Visualization

| Capability | CLI | HTTP | Web UI | MCP |
|---|---|---|---|---|
| Full graph (type / source filtered) | — | `GET /graph` | Knowledge panel + legend filter | — |
| Graph slice (around a node) | — | `GET /graph/slice` | Click a citation or node | — |
| Export node-link JSON | — | `GET /graph/export/node-link-json` | — | — |
| Export Cytoscape JSON | — | `GET /graph/export/cytoscape-json` | — | — |

## Federation (Synapse region)

| Capability | CLI | HTTP | Web UI | MCP |
|---|---|---|---|---|
| Inspect published contract | — | `GET /contract` | — | `get_contract` |
| Publish contract | (automatic on sync when `synapse.publish: true`) | — | — | — |
| Push event to router | (automatic webhook to `<router_url>/v1/synapse/events`) | — | — | — |

Routing, fan-out, merge, and global cross-region search live on the **router**
(pheasant-flock), not on the region. See
[Attach to a Synapse fleet](../how-to/attach-to-synapse.md).

## Knowledge effectiveness (evaluation plane)

| Capability | CLI | HTTP | Web UI | MCP |
|---|---|---|---|---|
| Record typed interaction proof | `pheasant eval proof` | `POST /evaluation/evidence` | — | `record_evidence` |
| List the evidence taxonomy | `pheasant eval taxonomy` | `GET /evaluation/taxonomy` | — | `pheasant://evaluation/taxonomy` |
| Run an evaluation batch | `pheasant eval run` | `POST /evaluation/run` (background job) | Effectiveness → **Run evaluation** | `start_evaluation` |
| Watch a batch in flight | `pheasant eval status [--watch]` | `GET /evaluation/status` | Effectiveness (live progress) | `get_evaluation_status` |
| Read the latest report | `pheasant eval report` | `GET /evaluation/report` | Effectiveness | `get_evaluation_report` |
| List past runs | — | `GET /evaluation/runs` | — | — |
| Inspect the cohorts a run used | — | `GET /evaluation/cohorts` | Effectiveness → Cohorts | — |
| Resolve an aggregate to its per-query rows | — | `GET /evaluation/metrics` | Effectiveness → click a tile | — |
| One metric's trend | `pheasant eval trend` | `GET /evaluation/trend` | Effectiveness → Trend | — |
| De-identified eval case set | `pheasant eval bootstrap` | — | — | — |

Starting a batch is available everywhere, and it is safe to ask twice: a run
takes the region's evaluation lease and is content-addressed, so a second
request joins the batch already in flight rather than starting a competing one.

Progress is the same row on every surface. It lives in `/state`, not in the
process running the batch, which is what makes a browser talking to a replica
that did not start the run — or a reader coming back after a restart — see the
same thing.

## Stress-test readiness

| Capability | CLI | HTTP | Web UI | MCP |
|---|---|---|---|---|
| Capability contract | `pheasant readiness contract [--json]` | `GET /readiness/contract` | — | `get_readiness_contract` |
| Go/no-go check | `pheasant readiness check [--gate-set X] [--out F]` | `POST /readiness/check` | — | `run_readiness_check` |
| Submit documents with a receipt | — | `POST /ingest/submit` | — | `submit_documents` |
| Receipt status | — | `GET /ingest/status` | — | `get_ingest_status` |
| Cross the index barrier | — | `POST /ingest/acknowledge` | — | `acknowledge_ingest` |
| Reconcile submissions | — | `GET /ingest/reconcile` | — | `reconcile_ingest` |
| Seal a snapshot | — | `POST /snapshots/seal` | — | `seal_snapshot` |
| Resolve a snapshot | — | `GET /snapshots/{id}` | — | `get_snapshot` |
| List snapshots | — | `GET /snapshots` | — | `list_snapshots` |

`pheasant readiness check` exits `0` only on a `GO` verdict — `INCOMPLETE`
counts as a no-go, because an unchecked box and a failed one are equally
disqualifying for a result somebody will publish. See
[Stress-test readiness](../stress-test-readiness.md).

## Server & MCP lifecycle

| Capability | CLI |
|---|---|
| Host a configured container (+ UI) for targets | `pheasant host <target>…` |
| Start HTTP API + MCP | `pheasant start` |
| Serve (container entrypoint) | `pheasant serve` |
| Standalone MCP server | `pheasant mcp --transport stdio\|streamable-http\|sse` |
| Backup state | `pheasant backup <out>` |
| Restore state | `pheasant restore <in> [--force]` |
| Export state as Parquet | `pheasant export parquet [--table NAME]` |
| Query an export | `pheasant export query "<SQL>"` |
| List exportable tables | `pheasant export tables` |
| Show the export schema | `pheasant export tables --schema [--json]` |

See [HTTP API](http-api.md) for the full route list and
[MCP tools & resources](../mcp_tools.md) for the full tool/resource list.
Parquet exports are CLI-only by design — they read state and write files, so
there is nothing for a server route or an agent tool to add. See
[Export a corpus as Parquet](../how-to/parquet-exports.md), and
[Parquet export schema](export-schema.md) for what an outside reader gets.
