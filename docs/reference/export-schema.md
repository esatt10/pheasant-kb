# Parquet export schema

The reference for a system that is **not** pheasant reading a pheasant export:
every table, every column, what joins to what, and which semantics live in
pheasant's code rather than in the files.

The export is a complete extract. A reader with the Parquet directory and this
page can reconstruct the corpus text, the knowledge graph and the agent-memory
record set without calling pheasant — see
[Export a corpus as Parquet](../how-to/parquet-exports.md) for producing one.

!!! warning "An export carries no access control"

    Inside pheasant, `memory_records.scope` is enforced: `org` records are
    shared, while `user` and `session` records are readable only by the
    principal that wrote them, and `artifacts.acl` gates retrieval per
    principal. **A Parquet file enforces nothing.** Both columns are exported
    as ordinary data, so whoever can read the directory can read every record
    of every scope and every ACL-restricted artifact.

    Treat an export as having the union of every principal's access, and put
    the access control on the directory. If that is not acceptable, filter at
    export time by exporting a subset of tables, or post-process the files
    before handing them on.

## Layout

```
<exports_path>/parquet/<kb_id>/
├── export.json          ← manifest: format version, kb, backend, per-file row counts
├── sources.parquet
├── artifacts.parquet
├── chunks.parquet
├── symbols.parquet
├── memory_records.parquet
├── sync_events.parquet
├── graph_nodes.parquet
├── graph_edges.parquet
└── artifact_terms.parquet   ← only when asked for with --table
```

`export.json` carries `format_version` (currently **1**). It is bumped when a
table or column is removed, renamed or retyped, or when an identifier grammar
changes — **not** when a table or column is added, so a reader written against
version 1 keeps working as the export grows. Check it before parsing; a
version you do not recognise is a schema you have not been written for.

## Getting the schema from the tool

This page is generated from the same declarations the exporter uses, and
`tests/test_parquet_export.py` fails CI if the two drift. You can also ask
directly, which is the better habit for an integration:

```bash
pheasant export tables --schema                     # declared schema
pheasant export tables --schema -c pheasant.yaml    # live schema, incl. migration-added columns
pheasant export tables --schema --json -c pheasant.yaml | jq .
```

## The entity model

```
sources ──┬─< artifacts ──┬─< chunks              (artifact_id)
          │               ├─< symbols             (artifact_id)
          │               ├─< memory_records ─┐   (artifact_id)
          │               └─< graph_nodes ──┐ │   (artifact_id)
          └─< sync_events                   │ │
                                            │ └── supersedes ─> memory_records.record_id
                                            └── graph_edges.from_node / .to_node
```

Every join, as a table:

| From | To |
|---|---|
| `artifacts.source_id` | `sources.id` |
| `chunks.artifact_id` | `artifacts.id` |
| `chunks.source_id` | `sources.id` |
| `symbols.artifact_id` | `artifacts.id` |
| `symbols.source_id` | `sources.id` |
| `memory_records.artifact_id` | `artifacts.id` |
| `memory_records.source_id` | `sources.id` |
| `memory_records.supersedes` | `memory_records.record_id` |
| `sync_events.source_id` | `sources.id` |
| `graph_nodes.source_id` | `sources.id` |
| `graph_nodes.artifact_id` | `artifacts.id` |
| `graph_edges.from_node` | `graph_nodes.node_id` |
| `graph_edges.to_node` | `graph_nodes.node_id` |
| `artifact_terms.artifact_id` | `artifacts.id` |
| `artifact_terms.node_id` | `graph_nodes.node_id` |


Every one of those joins is referentially clean in a real export — verified on
a 2,467-node / 4,220-edge index with zero dangling endpoints on either side of
`graph_edges`, zero orphan chunks, and zero orphan `memory_records`.

`sources.id` and `sources.name` currently hold the same value (the configured
source name), and it is that value which appears in every `source_id` column.
Join on `sources.id` — it is the declared key.

## Tables

### `sources.parquet`

One row per configured source. Primary key `id`.

| Column | Type | Notes |
|---|---|---|
| `id` | VARCHAR | Source name; the value every `source_id` column carries. |
| `knowledge_base_id` | VARCHAR | The `kb_id`, matching `export.json`. |
| `name` | VARCHAR | Same as `id` today. |
| `type` | VARCHAR | `repository`, `markdown_folder`, `document_folder`, `web_collection`, `single_file`, `api`, `memory`, or a connector-plugin type. Rows written before those types were retired may still read `obsidian_vault` or `s3`. |
| `path` | VARCHAR | Where the source was read from. |
| `enabled` | BIGINT | 0/1, not a boolean — SQLite has no boolean type. |
| `config_json` | VARCHAR | The source's full resolved config, as JSON text. |
| `last_indexed_at` | VARCHAR | ISO-8601 UTC. |
| `last_status` | VARCHAR | Outcome of the last sync. |

### `artifacts.parquet`

One row per indexed file. Primary key `id`.

| Column | Type | Notes |
|---|---|---|
| `id` | VARCHAR | Stable ID — see [ID grammar](#id-grammar). |
| `source_id` | VARCHAR | → `sources.id` |
| `type` | VARCHAR | Artifact class (`file`, `markdown_note`, …). |
| `path` | VARCHAR | Absolute path at index time. |
| `relative_path` | VARCHAR | Path within the source. **This is the one to group and display on.** |
| `mime_type` | VARCHAR | Detected content type. |
| `size_bytes` | BIGINT | Bytes on disk. |
| `sha256` | VARCHAR | Content hash. Equal hashes mean identical bytes — this is what makes re-syncs free, and it doubles as a duplicate detector. |
| `mtime` | VARCHAR | ISO-8601 UTC. |
| `git_branch` | VARCHAR | Branch at index time, for repository sources. |
| `git_commit` | VARCHAR | Commit at index time. |
| `last_indexed_at` | VARCHAR | ISO-8601 UTC. |
| `status` | VARCHAR | Indexing outcome for this artifact. |
| `acl` | VARCHAR | Access-control expression, or NULL for "the source expressed no ACL". **Data, not enforcement** — see the warning above. Added by migration, so it is present in a live export and absent from the declared schema. |

### `chunks.parquet`

The indexed text itself — the largest file, and the one that makes the export a
true corpus extract rather than a metadata dump. Primary key `id`.

| Column | Type | Notes |
|---|---|---|
| `id` | VARCHAR | Content-addressed: it embeds `text_hash`, so identical text yields an identical chunk ID. |
| `artifact_id` | VARCHAR | → `artifacts.id` |
| `source_id` | VARCHAR | → `sources.id` |
| `chunk_index` | BIGINT | Position within the artifact, from 0. Order chunks by this to rebuild a document. |
| `heading_path` | VARCHAR | Breadcrumb through the document's structure (`MSA > Article IV > § 12.3`), when the source extracts a taxonomy. NULL otherwise. |
| `start_line` | BIGINT | First line of the chunk in the source file. |
| `end_line` | BIGINT | Last line. |
| `text` | VARCHAR | **The chunk's full text.** |
| `text_hash` | VARCHAR | Hash of `text`. |
| `summary` | VARCHAR | Usually NULL — no LLM runs in the indexing path. |
| `token_estimate` | BIGINT | Rough token count, for budgeting a context window. |

### `symbols.parquet`

Code symbols extracted from the corpus. Primary key `id`.

| Column | Type | Notes |
|---|---|---|
| `id` | VARCHAR | Stable ID. |
| `artifact_id` | VARCHAR | → `artifacts.id` |
| `source_id` | VARCHAR | → `sources.id` |
| `language` | VARCHAR | `python`, `typescript`, … |
| `symbol_type` | VARCHAR | `class`, `function`, `method`, `constant`, … |
| `name` | VARCHAR | Bare name. |
| `qualified_name` | VARCHAR | Dotted/qualified name where the language has one. |
| `start_line` / `end_line` | BIGINT | Line span in the file. |
| `signature` | VARCHAR | Declaration text. |
| `docstring_summary` | VARCHAR | First line of the docstring, when present. |

### `memory_records.parquet`

The structured face of agent memory. Primary key `record_id`.

**The record's text is not in this table** — memory records are ordinary source
content (one frontmatter Markdown file each, indexed by the normal pipeline),
so the body lives in `chunks` and joins through `artifact_id`:

```sql
SELECT m.scope, m.kind, m.asserted_at, c.text
FROM memory_records m
JOIN chunks c ON c.artifact_id = m.artifact_id
WHERE m.valid_until IS NULL;
```

| Column | Type | Notes |
|---|---|---|
| `record_id` | VARCHAR | Stable record identifier. |
| `artifact_id` | VARCHAR | → `artifacts.id`; join to `chunks` for the text. |
| `source_id` | VARCHAR | → `sources.id` (the `type: memory` source). |
| `scope` | VARCHAR | `org`, `user` or `session`. Isolation is enforced in pheasant, **not** in the file. |
| `subject` | VARCHAR | What the record is about, when given. |
| `kind` | VARCHAR | `fact` (default), or the steering kinds `alias`, `preference`, `exclusion`. |
| `asserted_at` | VARCHAR | ISO-8601 UTC — when the record was written. |
| `valid_from` | VARCHAR | Explicit start of validity, when set. |
| `valid_until` | VARCHAR | End of validity. **Derived**: when B supersedes A, A's `valid_until` is B's `asserted_at`. NULL means currently valid. |
| `supersedes` | VARCHAR | → `memory_records.record_id` — the record this one corrects. |
| `tags` | VARCHAR | Free-form tags. |
| `written_by` | VARCHAR | Principal that wrote it. |
| `salience` | DOUBLE | Ranking weight. |
| `uses` | BIGINT | Times recalled (only counted with `memory.usage_tracking` on). |
| `last_used_at` | VARCHAR | ISO-8601 UTC — last time `uses` was bumped. |
| `canon_key` | VARCHAR | Normalized-content dedup key (scope/subject/kind/ACL/text — see `pheasant.memory.normalize`); NULL for a record from before compaction. Derived, not earned: recomputed on every re-sync. |
| `observations` | BIGINT | Times a write re-asserted this record — exactly or as a paraphrase — instead of creating a new file. |
| `last_seen` | VARCHAR | ISO-8601 UTC — last time `observations` was bumped. |
| `variants` | VARCHAR | JSON array of distinct surface forms (up to 8) that reinforced this record, when any differed from the stored text. |
| `tier` | VARCHAR | `hot` (default) or `cold` (demoted by compaction — see `memory_compactions.parquet`). Earned, carried over on re-sync. |
| `subsumed_by` | VARCHAR | → `memory_records.record_id` — the canonical record a near-duplicate cluster promoted, when this one was demoted. **Distinct from `supersedes`**: a subsumed record is redundant but still true, so this never narrows `valid_until`. |
| `schema_version` | BIGINT | Record-format version. |

Two semantics an outside reader must apply itself, because pheasant applies
them at query time rather than at write time:

- **Validity.** A correction supersedes rather than overwrites, so the export
  contains *both* records. `WHERE valid_until IS NULL` is "what is true now";
  omitting it gives you the full history, which is the point — and
  `WHERE valid_until > <instant> OR valid_until IS NULL` reconstructs what was
  believed at that instant.
- **Steering records are not knowledge.** `kind IN ('alias','preference','exclusion')`
  are rules that change ranking; pheasant excludes them from result lists by
  default. A reader that joins `memory_records` to `chunks` without filtering
  will surface rule syntax dressed as retrieved content.
- **Compaction is a tier, not a deletion.** `WHERE tier = 'hot'` is "what a
  default query would return"; a `cold` row is redundant, still valid, and
  still fully present — join `memory_compactions` (below) to see which
  canonical record absorbed it and why.

### `memory_compactions.parquet`

The audit trail for every near-duplicate cluster compaction has resolved:
one row per demoted (`tier='cold'`) record. Primary key `id`.

```sql
SELECT c.member_id, c.canonical_id, c.rule_id, c.at, m_text.text AS member_text, c_text.text AS canonical_text
FROM memory_compactions c
JOIN memory_records mr ON mr.record_id = c.member_id
JOIN chunks m_text ON m_text.artifact_id = mr.artifact_id
JOIN memory_records cr ON cr.record_id = c.canonical_id
JOIN chunks c_text ON c_text.artifact_id = cr.artifact_id;
```

| Column | Type | Notes |
|---|---|---|
| `id` | VARCHAR | Deterministic hash of `(op, member_id, canonical_id, params_hash)` — a re-run over unchanged content with unchanged parameters produces the same id, which is what makes the pass idempotent. |
| `op` | VARCHAR | `subsume` (deterministic medoid promotion) or `synthesize` (Phase 4's LLM merge — `rule_id='llm-synthesis-v1'` and `params_hash` encodes the model id). |
| `member_id` | VARCHAR | → `memory_records.record_id` — the demoted record. |
| `canonical_id` | VARCHAR | → `memory_records.record_id` — the promoted medoid. |
| `rule_id` | VARCHAR | The algorithm version, e.g. `jaccard-medoid-v1`. |
| `params_hash` | VARCHAR | Hash of the similarity threshold, minimum cluster size, and normalizer version this decision was made under. |
| `at` | VARCHAR | ISO-8601 UTC — when the pass made this decision. |

### `sync_events.parquet`

Sync run history. Primary key `id`.

| Column | Type | Notes |
|---|---|---|
| `id` | VARCHAR | Event ID. |
| `source_id` | VARCHAR | → `sources.id`; NULL for whole-KB events. |
| `event_type` | VARCHAR | e.g. `sync.completed`. |
| `status` | VARCHAR | e.g. `healthy`. |
| `started_at` / `finished_at` | VARCHAR | ISO-8601 UTC. |
| `details_json` | VARCHAR | Run detail, as JSON text. |
| `error_json` | VARCHAR | Failure detail, as JSON text. |

### `graph_nodes.parquet`

One row per knowledge-graph node. No primary-key column is declared, but
`node_id` is unique.

| Column | Type | Notes |
|---|---|---|
| `node_id` | VARCHAR | Stable ID — see [ID grammar](#id-grammar). |
| `type` | VARCHAR | Node class; see the vocabulary below. |
| `label` | VARCHAR | Display label. |
| `source_id` | VARCHAR | → `sources.id`; NULL for KB-level nodes. |
| `artifact_id` | VARCHAR | → `artifacts.id` when the node belongs to a file. |
| `created_at` / `updated_at` | VARCHAR | ISO-8601 UTC. |
| `attributes` | VARCHAR | **JSON text** holding every attribute not promoted to a column. Node attributes vary by type — a symbol has a signature, a heading has a level — and Parquet wants one schema per file. Reach in with `json_extract_string(attributes, '$.language')`. |

Node `type` vocabulary, with counts from this repository's own index for scale:
`chunk` (1,077), `symbol` (583), `file` (265), `entity` (230),
`external_reference` (214), `directory` (51), `markdown_note` (44),
`knowledge_base`, `source`, `source_type` (1 each). `memory_record` and
`heading` appear when the corpus has agent memory or an extracted taxonomy.
The authoritative taxonomy is [Graph model](../graph_model.md).

### `graph_edges.parquet`

One row per edge — **one row per parallel edge**, not per node pair.

| Column | Type | Notes |
|---|---|---|
| `from_node` | VARCHAR | → `graph_nodes.node_id` |
| `to_node` | VARCHAR | → `graph_nodes.node_id` |
| `type` | VARCHAR | Relation: `contains`, `has_chunk`, `has_heading`, `mentions`, `references`, `imports`, `calls`, `similar_to`, `supersedes`, `about`, `indexes`, `derived_from`. |
| `confidence` | DOUBLE | 1.0 unless the enrichment pass said otherwise. |
| `created_at` | VARCHAR | ISO-8601 UTC. |
| `attributes` | VARCHAR | JSON text, as for nodes. |

Named `from_node`/`to_node` rather than node-link JSON's `source`/`target`
because in pheasant "source" already means a *configured source*, and a column
called `source` next to `sources.parquet` gets misread once by everyone.

### `artifact_terms.parquet`

Opt-in with `--table artifact_terms`. Legacy enrichment terms, largely
historical concept rows; retired as a retrieval path. Primary key `id`.
Columns: `id`, `artifact_id` → `artifacts.id`, `source_id`, `node_id` →
`graph_nodes.node_id`, `node_type`, `term`, `normalized_term`, `weight`
(DOUBLE), `metadata_json`.

## ID grammar

Stable IDs are a contract in pheasant — they are what make re-indexing
idempotent — so they are safe to store and join on downstream. The grammar is
documented in [Graph model](../graph_model.md); the shapes you will meet in an
export are:

```
file:{source}:{relpath}:branch={branch}
chunk:{source}:{relpath}:sha256={text_hash}
symbol:{kb}:{source}:{relpath}:{name}-{line}
entity:{kb}:{source}:{slug}
directory:{source}:{relpath}:branch={branch}
```

They encode their source and path, so an ID is parseable — but parse it only
for diagnostics. Join on the columns.

## Type conventions

- **Timestamps are VARCHAR**, ISO-8601 UTC, exactly as stored. Cast when you
  need date maths: `CAST(last_indexed_at AS TIMESTAMP)`.
- **Booleans are BIGINT** 0/1 (`sources.enabled`), because SQLite has no
  boolean type and the export does not invent one.
- **`*_json` and `attributes` are VARCHAR** holding JSON text.
- **Row order is the primary key's**, because the export streams by keyset
  pagination on it. Do not rely on it meaning anything else.
- Both state backends export identically — same columns, same types, same
  stable IDs, same values; only run timestamps differ, because two indexing
  runs are two moments. Row *order* follows the database's collation
  (SQLite compares bytes, Postgres uses its configured collation), which is
  why order is not a contract. `export.json`'s `state_backend` records which
  backend produced the file. See
  [On Postgres](../how-to/parquet-exports.md#on-postgres).

## What is not in an export

| Missing | Why, and what to do |
|---|---|
| **Embedding vectors** | They live in the LanceDB store under `<state>/vectors/`. An export supports lexical and structural analysis, not semantic search. Copy the vector directory separately if you need it. |
| **The full-text index** | `chunks_fts` is a derived cache and dialect-specific. Rebuild your own from `chunks.text`. |
| **Identity and audit** | `idp_groups`, `idp_sync_meta`, `source_audit_events` are deliberately excluded. |
| **Operational state** | `source_checkpoints`, `source_leases`, `index_tasks`, `sync_fingerprints`, `manifests` — transient and meaningless outside the region that wrote them. |
| **Access-control enforcement** | See the warning at the top. |

An export is therefore not a backup: it cannot restore a region. Use
[`pheasant backup`](../how-to/backup-restore.md) for that.

## Loading the graph elsewhere

The two graph files are an edge list with attributes, which is the input format
most graph tooling wants.

=== "networkx"

    ```python
    import json
    import duckdb
    import networkx as nx

    d = "/exports/parquet/my-kb"
    g = nx.MultiDiGraph()
    for node_id, kind, label, attrs in duckdb.sql(
        f"SELECT node_id, type, label, attributes FROM '{d}/graph_nodes.parquet'"
    ).fetchall():
        g.add_node(node_id, type=kind, label=label, **json.loads(attrs))
    for a, b, kind, confidence in duckdb.sql(
        f"SELECT from_node, to_node, type, confidence FROM '{d}/graph_edges.parquet'"
    ).fetchall():
        g.add_edge(a, b, type=kind, confidence=confidence)
    ```

=== "Neo4j / openCypher"

    ```cypher
    // after exporting the two files to CSV, or via the Parquet loader
    LOAD CSV WITH HEADERS FROM 'file:///graph_nodes.csv' AS row
    CREATE (:Node {node_id: row.node_id, type: row.type, label: row.label});
    CREATE INDEX FOR (n:Node) ON (n.node_id);
    LOAD CSV WITH HEADERS FROM 'file:///graph_edges.csv' AS row
    MATCH (a:Node {node_id: row.from_node}), (b:Node {node_id: row.to_node})
    CREATE (a)-[:REL {type: row.type, confidence: toFloat(row.confidence)}]->(b);
    ```

    Produce the CSVs with
    `pheasant export query --format csv --limit 0 "SELECT * FROM graph_nodes"`.

=== "A warehouse"

    Parquet loads natively into BigQuery, Snowflake, Redshift Spectrum, Athena
    and Spark. Partition by `source_id` if you land several regions in one
    table, and carry `export.json`'s `kb_id` and `generated_at` as columns so
    snapshots stay distinguishable.

## Pulling exports from another system

The export is a directory of files, so the integration is a file copy — no API,
no client library, no pheasant process:

```bash
# on the region
pheasant export parquet -c /config/pheasant.yaml

# from anywhere that can read the volume
rsync -a pheasant-host:/exports/parquet/my-kb/ ./my-kb/
python -c "import duckdb; print(duckdb.sql(\"SELECT count(*) FROM './my-kb/chunks.parquet'\"))"
```

Read `export.json` first: `format_version` tells you whether your reader still
applies, `generated_at` tells you how stale the snapshot is, and `tables[]`
gives per-file row counts to validate the copy against. An export taken during
a sync is a snapshot of a moving corpus — schedule it after the sync if you
need the two to agree.
