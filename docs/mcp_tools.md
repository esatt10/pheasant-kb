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

    Indexing an Obsidian vault as a **source** (`type: obsidian_vault`) is
    unaffected and remains fully supported.

| Tool | Purpose |
|---|---|
| `list_knowledge_bases` | Return registered knowledge bases and status. |
| `register_source` | Add a source at runtime after path/include/exclude validation. Optional `sync_now`; `wait=false` returns a followable background job. |
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
| `search_context` | Search graph/search state in `text` (SQLite full-text over chunk content and paths), `graph` (node/relationship labels, types and attribute values), `vector` (embedding similarity; requires `search.embeddings.enabled`, otherwise contributes nothing), or `hybrid` (merged and re-ranked) mode. Also accepts **retrieval criteria** an agent can set per call instead of relying on how the region was configured: `source_name`, `source_types`, `exclude_source_types`, `exclude_sources`, `node_types`, `min_score`. `source_types` scopes by the *kind* of source (`repository`, `notion`, `slack`, …) rather than by name, which is what you want when you do not already know every source in the region; every hit reports its own as `provenance.source_type`, and `describe_retrieval` lists the types present. All optional and additive — an existing caller is unaffected. |
| `describe_retrieval` | Report how this knowledge base retrieves and what an agent may override per call: default mode and result count, which modes actually work here (`vector` is only offered when a vector index exists), the sources present, the `assistant.retrieval` settings, and one line of help per knob. Call this before guessing at parameters for an unfamiliar region. |
| `preview_retrieval` | Run retrieval criteria and report how they differ from the standing configuration — both result sets plus the delta (added / dropped / kept). Lets an agent test a setting against real content before anyone writes it into `pheasant.yaml`. Read-only: nothing is persisted. |
| `get_relevant_files` | Return files likely needed for a coding task. |
| `get_graph_neighbors` | Traverse graph neighbors with true depth-aware BFS and optional edge-type filters. |
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
