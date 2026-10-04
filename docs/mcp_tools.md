# MCP Tools, Resources, and Prompts

MCP is the primary agent interface. Tool responses should be compact, ranked, and provenance-rich.

## Server transports

pheasant exposes MCP through the official Python MCP SDK (`mcp>=2.1,<3`) when the `mcp` extra is installed. The Docker image includes this runtime.

That SDK line speaks the **2026-07-28** protocol revision and still answers the earlier ones, so a client negotiates the newest revision both ends support and an older agent keeps working unchanged. Three transports are available — `stdio` (the default), `streamable-http`, and `sse` — selected with `--transport` and gated per deployment by `server.mcp.transports`. The streamable-HTTP endpoint is also mounted inside `pheasant serve` at `/mcp`; `GET /mcp/info` reports which transports this region offers and the URL to use.

```bash
pheasant mcp --config /config/pheasant.yaml --transport stdio
```

For VS Code, keep pheasant running with Docker Compose and let VS Code start the MCP protocol process inside that container:

```bash
pheasant compose-env pheasant.yaml --output .pheasant/compose.env
docker compose --env-file .pheasant/compose.env up -d
docker exec -i pheasant python -m pheasant mcp --config /config/pheasant.yaml --transport stdio
```

The command is intended to be owned by the MCP client, so it waits on stdio. Do not add Docker's `-d` detach flag to the MCP stdio command.

## VS Code client config

Create `.vscode/mcp.json` locally from the reusable template:

```bash
mkdir -p .vscode
cp examples/vscode/mcp.json .vscode/mcp.json
```

Or generate it from the pheasant CLI:

```bash
pheasant client-config vscode --output .vscode/mcp.json
```

The committed template contains no host-specific paths. `.vscode/mcp.json` is ignored because users often customize container names, images, volumes, or local environment values.

## Tools

!!! warning "Removed in 0.10.0: `export_obsidian_notes`"

    The Obsidian vault projection was removed, and with it the
    `export_obsidian_notes` tool and the `POST /obsidian/export` endpoint. The
    UI's graph workspace (`/graph`) covers what the vault was used for.

    This is a **breaking change to the MCP tool surface**, which is otherwise
    evolved additively — an agent still calling `export_obsidian_notes` will
    get an unknown-tool error rather than a deprecation warning. It was
    removed outright rather than deprecated because, with the exporter gone,
    there is nothing left for the tool to do.

    Indexing an Obsidian vault as a **source** is supported as
    `type: markdown_folder` (the retired `obsidian_vault` loads as that).

| Tool | Purpose |
|---|---|
| `list_knowledge_bases` | Return registered knowledge bases and status. |
| `register_source` | Add a source at runtime after path/include/exclude validation. Optional `sync_now`; `wait=false` returns a followable background job. For web pages pass `source_type="web_collection"` and `urls=[…]` with no `path`; only public http(s) URLs are accepted unless the operator set `security.allow_agent_private_urls`. |
| `start_sync_source` | Start one source sync and immediately return a job id. |
| `get_job` | Read one background job's phase, counters, log tail and terminal result/error. |
| `list_jobs` | List recent jobs, optionally active jobs only. |
| `list_sources` | List sources with filters, status, and pagination. |
| `disable_source` | Disable a source without deleting its indexed state. |
| `remove_source` | Remove a source and its indexed state. |
| `promote_runtime_source_to_config` | Return a deterministic YAML patch, or write one by policy, for runtime sources. |
| `sync_source` | Trigger `incremental`, `full`, `validate_only`, or `repair` sync for one source. |
| `sync_all` | Trigger sync for all enabled sources. |
| `memory_write` | Append one agent-memory record (`session`/`user`/`org` scope, optional `subject`/`supersedes`/`tags`) to the configured `type: memory` source and, by default, index it immediately — the memory is retrievable via `search_context` in the same session. Recall is ordinary search; there is no separate read path. |
| `memory_consolidate` | Run one consolidation pass now: archive superseded and TTL-expired memory records (files renamed `.md.archived`, never deleted) and re-sync the memory source so they leave the index. The scheduler runs this automatically; this is the on-demand edge. |
| `list_memory_candidates` | Memory this region has **proposed** from how it is used, awaiting a decision. These are not memories: nothing listed is retrievable, and nothing becomes retrievable until it is promoted. Each carries the evidence behind it — which rule, how many observations, across how many sessions. |
| `promote_memory_candidate` | Admit one proposal, making it an ordinary record through the same write path `memory_write` uses. |
| `reject_memory_candidate` | Decline one proposal, permanently. The rule that proposed it will not suggest it again. |
| `search_context` | Search graph/search state in `text` (SQLite full-text over chunk content and paths), `graph` (node/relationship labels, types and attribute values), `vector` (embedding similarity; requires `search.embeddings.enabled`, otherwise contributes nothing), or `hybrid` (merged and re-ranked) mode. Also accepts **retrieval criteria** an agent can set per call instead of relying on how the region was configured: `source_name`, `source_types`, `exclude_source_types`, `exclude_sources`, `node_types`, `min_score`. `source_types` scopes by the *kind* of source (`repository`, `gdrive`, `web_collection`, …) rather than by name, which is what you want when you do not already know every source in the region; every hit reports its own as `provenance.source_type`, and `describe_retrieval` lists the types present. All optional and additive — an existing caller is unaffected. `snapshot_id` pins the search to a sealed snapshot: the region answers from that state or refuses with `SNAPSHOT_DRIFTED`, naming the sections that moved. `as_of` is the instant memory validity is evaluated at, echoed into the lineage even where the region holds no memory — which is where a caller most needs to be told it did nothing. `expand` attaches each hit's **graph neighbourhood** under `hit.graph` — see [raw retrieval with graph expansion](#raw-retrieval-with-graph-expansion). `include_chunks` and `include_graph_neighbors` are accepted for compatibility and do nothing. |
| `search_context_batch` | Bulk context retrieval: up to 25 `queries` in one call, at most 1,000 hits in total (`queries × max_results`). Every other argument means exactly what it means on `search_context` and applies to every query, and each query is answered exactly as it would be alone. `results` is the merged context — each passage once, ordered **by rank across queries** (every query's best hit, then every query's second), so truncating it keeps coverage of every query; each hit's `batch` block names the query indexes that found it and its best rank. `searches` holds each query's own payload unless `per_query` is false. A `snapshot_id` pin is verified once for the whole batch. `counts.overlap` is how many hits a query shared with an earlier one — high overlap means the queries are paraphrases rather than facets. `expand` is applied once, to the merged `results`; the per-query `searches` are not expanded. |
| `describe_retrieval` | Report how this knowledge base retrieves and what an agent may override per call: default mode and result count, which modes actually work here (`vector` is only offered when a vector index exists), the sources present, the `assistant.retrieval` settings, and one line of help per knob. Call this before guessing at parameters for an unfamiliar region. |
| `preview_retrieval` | Run retrieval criteria and report how they differ from the standing configuration — both result sets plus the delta (added / dropped / kept). Lets an agent test a setting against real content before anyone writes it into `pheasant.yaml`. Read-only: nothing is persisted. |
| `get_relevant_files` | Return files likely needed for a coding task. |
| `ask_knowledge_base` | A synthesized, cited answer from the configured workflow (extractive with no model). Takes `history` (the conversation so far, `[{question, answer}]`, oldest first — the region keeps no chat state), `depth` (`short` / `medium` / `long`, or unset to read it off the question) and `visual` (`diagram` / `image` / `none`), plus the retrieval criteria and `memory` `search_context` takes. The answer carries `route`, numbered `figures` for `[fig:n]` markers, and `visual`. See [answer length, conversations, visuals and figures](how-to/conversations-and-visuals.md). |
| `create_visual` | A visual grounded in the knowledge base, in whatever shape the request needs, or the images it holds. `request` says what to draw and from what viewpoint; `node_ids` (up to 12 chunk or file ids) draws from exactly those passages — "visualize this passage", or redraw the same evidence as another shape — and without them the region searches for the request. `kind`: `flow`, `sequence`, `hierarchy`, `mindmap`, `concept`, `cycle`, `timeline`, `swimlane`, `layers`, `groups`, `table`, `quadrant`, `chart`, `canvas`, the UML `class`, `activity`, `state` (state machine) and `usecase`, or `image` (everyday names such as "org chart", "2x2" or "class diagram" work too). Every node, edge, lane and cell lists the passages (`cites`) that support it; an unsupported element is `inferred`, a chart value no cited passage states is unverified, and a mostly-inferred visual is declined with a reason. `visual.mermaid` is the Mermaid export where Mermaid has the shape (`visual.markdown` for a table); `visual.redraw` carries the `node_ids` to redraw it. With no model connected the visual is the graph's own edges. |
| `get_image` | An indexed image as MCP **image content** plus its caption and path, so a vision-capable agent can look at what a document shows. `node_id` comes from an answer's `figures`, an `image` search hit, or `create_visual`. Raster formats only. Also readable as the resource `pheasant://knowledge-bases/{kb_id}/media/{node_id}`. |
| `get_graph_neighbors` | Traverse graph neighbors with true depth-aware BFS along outgoing edges (two hops by default), structural `contains` edges first. `edge_types` keeps only those edges; `exclude_edge_types` and `exclude_node_types` prune the walk itself, as on `GET /graph/neighbors`; `max_nodes` bounds it (set it when starting from a `directory` or `source` hub). Any search hit's `node_id` or `chunk_id`, or an expanded neighbour's `node_id`, is a valid start. |
| `get_graph_slice` | The induced sub-graph around a node: `nodes`, every link between any two of them, each node's hop distance (`depths`) and `truncated` when `limit` was reached. The same operation as `GET /graph/slice` and the `graph-slices` resource, now callable with bounds and exclusions. |
| `get_file_summary` | Return a compact summary and provenance for a file. |
| `get_repo_map` | Return repository structure, important modules, and dependencies. |
| `explain_node` | Explain a graph node and why it matters. |
| `get_sync_status` | Return queue, lock, error, freshness, and connector checkpoint status. |
| `get_sync_history` | Return runtime registration, sync, promotion, disable, and removal audit events. |
| `record_evidence` | Record what came of a result this region returned: `cited`, `selected`, `explicit_accept`, `explicit_reject`, `downstream_success`/`failure`, or a `deterministic_validation_pass`/`fail`. The only way *proof* enters the region — retrieval already records what was served, and only the caller knows whether it helped. Being served is not evidence of usefulness and **not** selecting something is not evidence against it, so an agent that reports nothing lowers the evaluation's coverage rather than corrupting its conclusions. See `pheasant://evaluation/taxonomy`. |
| `start_evaluation` | Start an effectiveness batch and return a job id, not a report — a batch is minutes of work. Poll `get_evaluation_status`. Safe to call twice: the run takes the region's lease and is content-addressed, so a second call joins the batch in flight rather than starting a competing one. |
| `get_evaluation_status` | How far a batch has got: `phase`, cohort/variant replays done against planned, `attempts` (above 1 means an earlier attempt was interrupted and this one resumed it), and the terminal status. Read from `/state`, so it answers for a run this process did not start and for one whose container has stopped — a run whose heartbeat expired reports `interrupted` rather than pretending to still be working. |
| `get_evaluation_report` | Return the latest knowledge-effectiveness report: the health vector, the hard gates, learned-versus-holdout generalization, candidate decisions, the stated limitations, and the actions the report permits. Every number carries its denominator; one that could not be computed reports `insufficient_evidence`, never `0.0`. |
| `start_retrieval_tuning` | Find **which step** of retrieval is failing, and tune the parameters that reach it. Returns a job id, not a report. `diagnose_only=true` runs the first movement only — it attributes every miss to the stage that lost it (lexical arm, a filter, the fusion, the cut) and proposes nothing, which is the right first call: it can tell you the failures are somewhere no retrieval parameter reaches. `apply=true` lets a winner that passed every gate become the fleet's live ranking; off by default, because producing a bundle changes nothing and applying one re-ranks every replica. |
| `get_retrieval_tuning_status` | How far a tuning batch has got: `phase`, units done against planned, `attempts`, terminal status. Read from `/state`, so it answers for a batch this process did not start and one whose container has stopped; an expired heartbeat reports `interrupted` and the next attempt resumes from its trials. |
| `get_retrieval_diagnosis` | Where retrieval loses documents, by pipeline stage, with the denominator and — per stage — whether a parameter can reach it at all. A stage marked `reachable_by_tuning: false` (a document that was never indexed, say) is a statement that no ranking work will help; that is the most useful thing this reports. |
| `get_retrieval_parameters` | What this region ranks with, whether the values come from `config` or an applied `bundle`, and the full tunable space with each parameter's stage and bounds. A ranking nobody expects is most often a bundle somebody applied, and this is where that shows. |
| `list_tuning_bundles` | Configuration bundles this region has produced, and which one is live. Each carries the decision, gates and comparisons behind it. |
| `apply_tuning_bundle` | Make a bundle this region's live retrieval overlay. **Fleet-scoped**: one row in `/state`, resolved by every replica within its refresh window. There is deliberately no per-principal or per-request variant — parameters that varied by caller would make two agents disagree about what the region contains. Reversible. |
| `rollback_tuning_bundle` | Stand the active overlay down; the region returns to its configured values. What the bundle replaced is stored on it, so this does not depend on anyone remembering what the config used to say. |
| `get_readiness_contract` | What this build supports, with a digest a harness can pin. **Read this before hard-coding a tool name or assuming a capability.** Every row names the readiness gap it closes and reports `proven`, `supported`, `declared_untested` or `unsupported` — and an unsupported row carries its reason, because "this region cannot" and "this region did not mention it" call for different responses. Also publishes the refusal-code table: this transport carries a string and nothing else, so an agent maps a refusal's text onto a machine-readable code here. |
| `run_readiness_check` | Probe this region and return the go/no-go verdict per gate set. Performs real work — it submits documents to a scratch source it owns, indexes them, seals snapshots and runs searches — and never writes to a configured source or to memory. A gate set with skipped gates reports `null`, not `true`: an unchecked box and a failed one are equally disqualifying for a result somebody will publish. |
| `submit_documents` | Persist documents with an idempotency key and one receipt per item. Re-submitting under a key this region has already seen folds onto the receipt it wrote rather than making a second copy. **Acceptance is not searchability** — sync the source, then call `acknowledge_ingest`. |
| `get_ingest_status` | Receipts: `accepted`, `indexed`, `rejected` or `failed`, per submitted item, with the error code and retryability of anything refused. |
| `acknowledge_ingest` | Cross the index barrier for receipts whose artifacts now exist. Read from `artifacts` rather than from what a sync reported, because a sync's summary is a claim about what it did and this is a question about what the region holds. |
| `reconcile_ingest` | Submitted against held. `silent_loss` is the number to read: receipts claiming an artifact this region does not have — deliberately not a difference between two totals, which can agree while one item was lost and another double-written. |
| `seal_snapshot` | Seal the current state as a run's reference snapshot. Idempotent over an unchanged region, because the id is a digest of the state. Pin a search to the returned `snapshot_id` and this region answers from that state **or refuses** — it does not hold older corpus versions, so the guarantee is that two runs naming one snapshot cannot silently have seen different corpora. |
| `get_snapshot` | A snapshot's manifest, and whether the region still stands where it says. Drift names the manifest *sections* that moved: `corpus` means somebody indexed, `retrieval` means a tuning bundle was applied, `memory` means a record was written. |
| `list_snapshots` | Every snapshot this region holds, saying which are sealed. |

### Raw retrieval with graph expansion

`search_context` is retrieval with no model in the path — three arms fused by
reciprocal rank fusion, nothing planned, graded or written. `ask_knowledge_base`
is the region's own answering workflow on top of the same retrieval. A harness
that runs its own evaluation, reranking or synthesis wants the first, and
`expand` gives it the structure around each hit as well, so it can follow
imports, calls, references, headings and memory supersession without the
region deciding which neighbours matter.

| `expand` | Meaning |
|---|---|
| omitted / `false` / `0` | No expansion; the payload is unchanged (default) |
| `true` | 1 hop, up to 8 neighbours per hit |
| `1`–`3` | That many hops |
| `{"depth", "max_neighbors", "edge_types", "exclude_edge_types"}` | Any of them; `max_neighbors` 1–50 |

Each hit gains `graph: {seed, neighbors, truncated}`; each neighbour carries
`node_id`, `type`, `label`, `relative_path`/`source_id`/`artifact_id` where it
has them, a short `summary`, its `depth`, the `edge_types` it was reached by
and `via`, the node it was reached from. The response's `expansion` block
reports the settings in force and how many distinct hits were walked (at most
25; `seeds_skipped` counts the rest). `has_chunk` and `indexes` are skipped by
default — a file's own passages and the source-to-every-file shortcut; naming
`edge_types` drops those defaults and naming `exclude_edge_types` replaces them.

Expansion never changes retrieval — the same hits, in the same order, under
the same `lineage.query_id`. Under `security.acl_enforced` neighbours pass the
same artifact check the hits do, anything reached *through* a withheld node is
withheld too, and a node that belongs to no artifact is withheld. `POST /search`
and `POST /search/batch` take the same `expand`, with the same refusal text.
The [`pheasant-retrieval` skill](https://github.com/esatt10/pheasant-kb/blob/main/.agents/skills/pheasant-retrieval/SKILL.md)
and the `use_pheasant_for_raw_retrieval` prompt package the whole workflow.

### Agent memory in retrieval

`search_context` and `preview_retrieval` take a `memory` argument: one of
`"auto"` (default), `"off"`, `"only"`, `"prefer"`, or an object with
`scopes` / `subject` / `current_only` / `as_of` / `max_results` /
`include_rules` (default `false` — steering records steer ranking but are not
returned as passages) / `tiers` (`["hot"]` default; `["cold"]` or
`["hot","cold"]` reaches records demoted by compaction, `current_only: false`
and `as_of` widen to both automatically). Records a
later record corrected are excluded automatically — pass an `as_of` instant to
ask what was believed then. Hits that came from memory carry a `memory` block
naming the record, its scope, when it was asserted, and its tier.

`memory_write` takes `kind` (`fact` by default; `alias` / `preference` /
`exclusion` are retrieval rules), `principal` (who asserted it — part of the
record id, and what scopes it under `security.acl_enforced`) and `valid_until`.
Its response carries `outcome` (`"created"` \| `"reinforced"` \| `"duplicate"`)
alongside the existing `created` boolean — a write whose normalized text
already matches a live record in the same scope/subject/kind/ACL bucket folds
into it instead of creating a new file (`memory.reinforcement_enabled`, on by
default). A fold only ever targets a record a *default* query can return: a
claim a later record corrected becomes its own new record rather than folding
into the record it contradicts, and a write matching a compaction-demoted
record folds into that cluster's canonical one. See `docs/memory-system.md` §8
for reinforcement and compaction.

`describe_retrieval` reports the memory source's name, its scopes and counts,
how many records are wired into the graph, and any steering in force, so an
agent never has to guess the source name to exclude it.

`memory_synthesize` LLM-merges a near-duplicate cluster deterministic
compaction could not resolve (complementary partial facts, progressive
refinement, abstraction) into one canonical record, subsuming the inputs the
same way medoid promotion does. Off by default (`memory.synthesis.enabled`)
and **never automatic** — only this explicit call runs it, so the scheduler
beat never makes a network request. Returns `{"skipped": reason}` when
disabled, no memory source is configured, or no model is reachable.

## MCP Apps

`ask_knowledge_base`, `create_visual` and `get_image` declare
`_meta.ui.resourceUri: "ui://pheasant/knowledge-view.html"` (plus the deprecated
flat `ui/resourceUri`). The resource is served as `text/html;profile=mcp-app`
(MCP Apps, protocol 2026-01-26): a host that supports apps renders the answer,
diagram or image in a sandboxed iframe, and one that does not reads the same
JSON. The view is self-contained, loads nothing from the network, never parses
a result as HTML, and calls back only through the host: `get_image` for an
image, `create_visual` for its **Redraw as** row. pheasant's web UI hosts the
same file.

**Expanding a visual.** The view can ask its host for the whole window with the
standard `ui/request-display-mode` request (`fullscreen`, and `inline` to go
back). It shows its **Expand** button only when the host's
`hostContext.availableDisplayModes` includes `fullscreen` — a host that does not
offer it is never asked — and it follows the host if the host changes the mode
itself (`ui/notifications/host-context-changed` with a `displayMode`). Expanded,
the shapes whose nodes are free to move (flow, tree, mind map, concept, cycle,
swimlanes, layers, canvas and the UML class, activity, state and use case
diagrams) can be dragged, and their edges follow; sequence, timeline, 2×2,
chart, table and groups expand but do not move. A rearranged layout is view
state: nothing about it is sent to the host or the region, and it does not
change what a `create_visual` call returns. While expanded, clicking a node
does not send `ui/message`, because the message would land in a conversation
the frame is covering. See
[expand a visual, and move things around](how-to/conversations-and-visuals.md#expand-a-visual-and-move-things-around).

A host implementing this needs three things: list `fullscreen` in
`availableDisplayModes` at `ui/initialize`, answer `ui/request-display-mode`
with the mode it actually granted, and send `host-context-changed` when the mode
changes. Restyle the existing frame rather than moving it in the page — a moved
iframe reloads and loses the layout — and ignore `size-changed` while
fullscreen. `ui/src/chat/McpAppFrame.tsx` is a working reference.

## Resources

```text
pheasant://knowledge-bases
pheasant://knowledge-bases/{kb_id}/sources
pheasant://knowledge-bases/{kb_id}/graph
pheasant://knowledge-bases/{kb_id}/sources/{source_id}/manifest
pheasant://knowledge-bases/{kb_id}/sources/{source_id}/repo-map
pheasant://knowledge-bases/{kb_id}/sources/{source_id}/history
pheasant://knowledge-bases/{kb_id}/sync-history
pheasant://knowledge-bases/{kb_id}/graph-slices/{node_id}
pheasant://knowledge-bases/{kb_id}/nodes/{node_id}
pheasant://evaluation/taxonomy
```

## Prompts

### `use_pheasant_for_coding_task`

1. Call `get_relevant_files` with the user's task.
2. Inspect returned files/chunks.
3. Make the smallest safe change.
4. Run checks.
5. Commit or record the write action.
6. Call `sync_source` with `mode=incremental`.
7. Check `get_sync_status` before the next task.

### `use_pheasant_for_document_research`

Use `search_context` first, prefer chunks with explicit provenance, avoid claims beyond retrieved evidence, and call `get_graph_neighbors` for related material.

### `use_pheasant_for_raw_retrieval`

For a harness that judges evidence itself: `describe_retrieval` once, then `search_context` (hybrid) with `expand=true` or `search_context_batch` for several facets — never `ask_knowledge_base`. Judge each hit from its text, provenance and graph block; follow neighbours with `get_graph_neighbors` or `get_graph_slice`; read whole files with `get_file_summary`; pin repeated runs with `snapshot_id`.
