---
name: pheasant-retrieval
description: Query a Pheasant knowledge base with raw hybrid search and walk its knowledge graph from the results, using your own judgement instead of the region's answering workflow. Use when an agent or evaluation harness connected to Pheasant over MCP (or HTTP) needs passages with provenance, wants to follow imports, calls, references, headings, symbols or memory supersession from a hit, wants reproducible retrieval for scoring, or is asked to "search pheasant", "find related files", or "traverse the graph" without calling ask_knowledge_base.
---

# Retrieve from Pheasant, judge it yourself

Pheasant exposes two ways to get knowledge out of a region:

- **`ask_knowledge_base`**: the region plans, retrieves, grades and writes a
  cited answer. Its opinions are built in.
- **Raw retrieval** (this skill): hybrid search and graph traversal, with no
  model, planner or grader anywhere in the path. You get ranked evidence and
  the structure around it. Choosing what matters is your job.

Use raw retrieval when your harness has its own evaluation, reranking or
answer synthesis, or when you need to see the evidence rather than a summary
of it. Do not mix the two in one measurement. `ask_knowledge_base` retrieves
again, by its own rules.

## Workflow

1. **Learn the region once.** Call `describe_retrieval(knowledge_base)`. It
   lists the working modes (`vector` appears only when an embedding index
   exists), the sources and source types, and the criteria each tool takes.
   Call `list_knowledge_bases` first if you don't know the name.
2. **Search.** Call `search_context(knowledge_base, query, mode="hybrid",
   max_results=10, expand=true)`. Hybrid fuses three arms (BM25 text, vector,
   graph) by reciprocal rank fusion. For several facets of one question, use
   `search_context_batch(queries=[...])`. It takes up to 25 queries and
   returns one merged, deduplicated list ordered by rank across queries.
3. **Judge each hit** from `summary`, `chunks[].text_preview`, `provenance`
   (`source_id`, `relative_path`, `source_type`, `heading_path`) and its
   `graph` block. `rank` is the order. `score` is a fused RRF value with **no
   absolute scale**, so compare hits by rank, never against a fixed `min_score`.
4. **Follow structure** from the hits that matter. See *Walking the graph*.
5. **Read in full** when a preview is not enough: call
   `get_file_summary(knowledge_base, path=relative_path, source_name=source_id)`,
   which returns the whole indexed text.
6. **Report back (optional).** If your harness decided a hit was or was not
   useful, `record_evidence` gives the region typed proof (`cited`,
   `selected`, `explicit_accept`, `explicit_reject`, ...). Its evaluation plane
   counts only what a caller asserts. Being served is never evidence.

## `expand`: graph neighbourhoods inline

`expand` attaches each hit's graph neighbourhood under `hit.graph`:

```json
{"seed": "file:code:app/main.py:branch=none",
 "neighbors": [
   {"node_id": "file:code:app/util.py:branch=none", "type": "file",
    "label": "app/util.py", "relative_path": "app/util.py",
    "depth": 1, "edge_types": ["imports"],
    "via": "file:code:app/main.py:branch=none"}],
 "truncated": false}
```

| Value | Meaning |
|---|---|
| omitted / `false` / `0` | No expansion. The payload is unchanged (the default). |
| `true` | 1 hop, up to 8 neighbours per hit. |
| `2` | 2 hops (1 to 3 allowed). |
| `{"depth": 2, "max_neighbors": 12}` | Set either bound (`max_neighbors` 1 to 50). |
| `{"edge_types": ["imports", "calls"]}` | Follow only these edge types. |
| `{"exclude_edge_types": []}` | Follow everything, including a file's own `has_chunk` passages. |

- `has_chunk` and `indexes` are skipped by default: a file's own passages, and
  the source-to-every-file shortcut. Naming `edge_types` drops those default
  exclusions. Naming `exclude_edge_types` replaces them.
- `via` is the node each neighbour was reached from. With `depth`, it lets you
  rebuild the tree.
- The response's `expansion` block reports the settings in force, how many
  distinct hits were walked (`seeds`, at most 25), how many were skipped
  (`seeds_skipped`), and the total `nodes` returned.
- Expansion never changes retrieval: the same hits in the same order, with the
  same `lineage.query_id`.
- With `security.acl_enforced`, pass `principal` (and `principal_groups`).
  Neighbours pass the same artifact check the hits do, and anything reached
  *through* a withheld node is withheld too. A node that belongs to no
  artifact (a directory, a source, a shared stub) is withheld under
  enforcement.
- `include_graph_neighbors` and `include_chunks` do nothing and are kept only
  for compatibility. Use `expand`.

## Walking the graph

Every hit's `node_id`, every passage's `chunk_id` and every neighbour's
`node_id` is a graph node ID. Any of the tools below accepts one.

| Tool | Use it for |
|---|---|
| `get_graph_neighbors(node_id, depth=2, edge_types, max_nodes, exclude_edge_types, exclude_node_types)` | A breadth-first list of nodes reached, each with `depth`, `edge_types`, `path` and full attributes. Set `max_nodes` whenever you start at a `directory` or `source` node: those are hubs. |
| `get_graph_slice(node_id, depth=1, limit=100, ...)` | The induced sub-graph: `nodes`, every `links` entry between any two of them, `depths`, and `truncated`. Use it to reason about how several results relate. |
| `explain_node(node_id)` | Everything the graph holds about one node. |
| `get_repo_map(source_name)` | Every indexed path in one source. |

Walks follow **outgoing** edges, structural `contains` edges first. Edges that
point the way you usually want:

| From | Edge | To |
|---|---|---|
| file | `imports` | the resolved file, and an `external_reference` stub per import |
| file | `calls` | call-target `symbol` |
| file | `mentions` | its own `symbol` / `entity` nodes |
| file | `references`, `embeds` | linked documents, URLs and images |
| file | `has_heading` | its section outline (sources with taxonomy enabled) |
| symbol, chunk | `derived_from` | the file it came from |
| directory | `contains` | its children |
| memory record | `supersedes`, `about` | the record it corrected, and what it is about |

"Who imports this file?" is an *incoming* question. A walk from that file
cannot answer it. Search for the module name, or walk from the importers you
already have. Over HTTP, `GET /graph/path?source=&target=` finds the shortest
path in either direction between two nodes.

## Reproducible runs

- `snapshot_id` (from `seal_snapshot`) pins a search to a sealed state. If the
  corpus has moved since, the region refuses with `SNAPSHOT_DRIFTED` and names
  what moved. It never silently answers from a different corpus.
- `lineage` on every search names the ranking parameters, graph generation,
  criteria and memory policy that produced it. Store it with your scores.
- `memory="off"` keeps agent-memory records out of a corpus-only arm.
  `memory="only"` retrieves just those records.
- Scope with `source_name`, `source_types`, `exclude_sources`, `node_types` and
  `section`. These criteria behave identically on MCP and HTTP.
- Call `use_pheasant_for_raw_retrieval` (an MCP prompt) to get this workflow in
  one message.

## HTTP equivalents

Raw retrieval is the same operation on both surfaces, with the same results
and the same refusal text:

| MCP | HTTP |
|---|---|
| `search_context` | `POST /search` (JSON body, same field names, `expand` included) |
| `search_context_batch` | `POST /search/batch` |
| `get_graph_neighbors` | `GET /graph/neighbors?node_id=&depth=&max_nodes=&edge_types=&exclude_edge_types=&exclude_types=` (lists comma-separated) |
| `get_graph_slice` | `GET /graph/slice?node_id=&depth=&limit=&...` |
| `explain_node` | `GET /nodes/explain?node_id=` |
| `get_file_summary` | `GET /files/summary?path=&source_name=` |

A malformed `expand` is refused before anything is retrieved: `ToolError` with
the reason over MCP, and `422` with `code: INVALID_REQUEST` over HTTP.
