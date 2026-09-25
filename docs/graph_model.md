# Graph Model

pheasant uses a directed multi-graph model, compatible with `networkx.MultiDiGraph`, so multiple relationship types can connect the same pair of nodes.

## Node types

| Type | Purpose |
|---|---|
| `knowledge_base` | Root graph node for one pheasant instance/config domain. |
| `source` | Configured source root. |
| `source_type` | A hub grouping every source of one kind — `repository`, `notion`, `slack`, `document_folder`. The type was always an *attribute* of each source node, which meant it could be read but never navigated: nothing connected two Confluence spaces to each other. The hub makes that a structure you can see and walk. Hung off the knowledge base **alongside** sources (`kb contains source_type contains source`), never between them, so a source stays one hop from the root and nothing already visible at the default depth is pushed past the horizon. Attributes: `source_type`. |
| `repository`, `branch`, `commit` | Git-aware repository context. |
| `directory`, `file`, `document`, `markdown_note` | Indexed filesystem artifacts. |
| `memory_record` | One agent-memory record (Step 33.7). Still an ordinary Markdown artifact indexed by the ordinary pipeline — the type exists because the graph previously could not say which of its notes an agent had *remembered*. Attributes: `scope`, `subject`, `asserted_at`, `kind`. Its stable ID is unchanged (`file:{source}:{relpath}:branch=none`); only the type attribute is new, so a graph written before 2026-08-11 types these `markdown_note` until its next sync. |
| `heading`, `chunk` | Retrieval and document structure units. `heading` carries a document's structural outline (chapter/section/§ code) and is emitted when a source sets `taxonomy.enabled` — **note it went unemitted from the initial build until 2026-08-06**, so a graph written before then contains none. Attributes: `level`, `number`, `title`, `kind`, `heading_path`, `start_line`. |
| `symbol` | Code symbols, constants, and call targets extracted from language-aware passes. |
| `entity` | Named systems, products, people, or CamelCase/Title Case mentions. |
| ~~`concept`~~ | **Retired 2026-08-03.** Concept extraction produced 87.2% of a 162k-node graph and 98.6% of its edges while contributing nothing measurable: the retrieval expansion path never fired, the facts panel filled every slot with terms like "limit" and "request info", and the similarity pass it fed emitted zero edges. See `graph.enrichment._add_concept`. |
| `external_reference` | Imported modules, URLs, wiki links, document links, and citations. |
| `dependency`, `tag`, `topic`, `query`, `agent_action` | Optional classification, retrieval audit, and feedback-loop nodes. |

## Edge types

| Type | Purpose |
|---|---|
| `contains`, `indexes`, `has_chunk`, `has_heading` | Hierarchy and indexing relationships. `has_heading` links an artifact to every section it contains; a section `contains` its subsections. Both were documented but unemitted until 2026-08-06. |
| `mentions`, `derived_from` | Artifact-to-entity/symbol links and reverse provenance. |
| `imports`, `calls` | Repository/code relationships. |
| `references` | Markdown links, URLs, wiki links, citations, and other external references. |
| ~~`similar_to`~~ | **Retired 2026-08-03** with the concept layer it keyed off. Not a silent loss: a live 2,132-file graph contained zero of these edges, so the pairwise pass that produced them was doing a full artifact-by-artifact scan every sync and emitting nothing. |
| `links_to`, `tagged_with` | Optional Markdown/document relationships. |
| `about` | What an agent-memory record is *about* (Step 33.7): the record to the corpus artifact, symbol, heading or entity it refers to. Attributes: `record_id`, `match_signal` (`reference` \| `symbol` \| `heading` \| `entity`, strongest first — a record takes the first that fires), `matched` (what actually matched) and `confidence`. Capped per record, so total `about` edges stay bounded by `records x targets`. A lexical/BM25 rung was deliberately **not** materialized: the search index answers that at query time, and materializing it is how the concept layer reached 98.6% of all edges. |
| `belongs_to_branch`, `at_commit`, `supersedes` | Git and version lineage. `supersedes` was documented from the initial build but **unemitted until 2026-08-11**, when Step 33.7 began drawing it between agent-memory records — before that a correction existed only as a frontmatter string resolved in Python, and the graph could not answer "what replaced this". |
| `subsumes` | A near-duplicate cluster's medoid to the records it absorbed (compaction Phase 3): the canonical record to each demoted (`tier=cold`) member. Attributes: `enrichment_pass`. Drawn straight from `memory_records.subsumed_by`, **not** through the `about` ladder — that ladder is corpus-only by design (a memory must not be matched against symbols/headings/entities extracted from *another* memory), and a subsumption is memory-to-memory by definition. Deliberately distinct from `supersedes`: a subsumed record is redundant but still *true*, so `subsumed_by` never feeds `effective_valid_until` — conflating the two would silently expire facts that are still valid. See `docs/memory-system.md` §8 and `pheasant.memory.compaction`. |
| ~~`generated_note`~~ | **Retired 2026-08-16** with the Obsidian projection it described. Like `heading`/`has_heading` before 2026-08-06, it was documented from the initial build and **never emitted** — no graph written by any release contains one. |
| `retrieved_by`, `modified_by` | Agent audit. |

## Stable IDs

Stable IDs must include source identity and enough path/hash/context to support idempotent upserts:

```text
<node_type>:<source_id>:<stable_path_or_hash>:<optional_context>
```

Examples:

```text
source_type:local-pheasant:repository
source:local-pheasant:pheasant-codebase
file:pheasant-codebase:src/pheasant/cli.py:branch=main
chunk:pheasant-codebase:src/pheasant/cli.py:sha256=abc123:chunk=0004
heading:contracts:msa.pdf:sha256=1f4b2c9d0e7a3b58
symbol:pheasant-codebase:src/pheasant/cli.py:PheasantCli.main
commit:pheasant-codebase:6f2a9c1
```

For an unbounded enrichment value such as a generated URL or citation, normal
IDs remain unchanged until they reach the storage-safe limit. Longer values
keep their source and prefix context and end in a deterministic
`:sha256=<digest>` suffix; the original reference remains in node attributes.

## Required provenance

Nodes and search results should record source ID, knowledge base ID, relative path, content hash, indexed timestamp, branch, commit, and parser/rule provenance when available.

## Enrichment passes

pheasant runs deterministic enrichment during sync:

- Code pass: extracts Python imports, classes, functions, constants, and call targets.
- Markdown/document pass: extracts headings, links, wiki links, URLs, citations and named mentions.
- Internal reference resolution: a post-sync pass that turns a file's imports
  and document links into edges pointing at **the file they resolve to**,
  by longest-suffix path match (`agent_framework._workflows._checkpoint` →
  `.../agent_framework/_workflows/_checkpoint.py`). Deterministic and
  rule-based — no LLM. Same-source targets carry
  `enrichment_pass: internal_resolution`; cross-source ones keep
  `cross_source_resolution` (Synapse 21.6B).

  This is the file-to-file connectivity the graph advertised and did not have.
  Resolution used to skip same-source targets on the belief that intra-source
  enrichment already covered them; it did not, so a single-source knowledge
  base — the common case — had **zero** file→file import edges while carrying
  1,871 `imports` edges that all terminated at a bare module name.

Graph-derived terms are also written to SQLite so hybrid search can surface multiple relevant files for a cross-file query, even when no single chunk contains every query term.

Search runs in three modes. `text` queries the SQLite full-text index over chunk content and paths. `graph` matches directly against the live graph — node labels, types and attribute values, plus relationship types and endpoint labels — so enrichment outputs (symbols, entities, external references) and the relationships between them are first-class search targets, not just the chunk text they were derived from. `hybrid` (the default) runs both and merges the results, de-duplicating by node and re-ranking by score. The retrieval mode and the maximum result count are both caller-adjustable.
