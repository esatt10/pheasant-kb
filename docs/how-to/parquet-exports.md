# Export a corpus as Parquet (and query it)

`pheasant export parquet` writes your indexed state to `/exports` as one
[Parquet](https://parquet.apache.org/) file per table — artifacts, chunks,
symbols, memory records, sync history, and the knowledge graph as nodes and
edges. `pheasant export query` runs SQL over the result.

This is the analytics surface. Search answers *"what is relevant to this
question"*; an export answers *"what is in this corpus"* — how big, how
duplicated, how connected, how it changed — with a `GROUP BY` instead of a
retrieval call.

Parquet, specifically, because the export outlives the region that wrote it: it
is columnar, compressed, self-describing, and readable by DuckDB, pandas,
polars, Spark, Athena, BigQuery and DuckDB-WASM in a browser tab — with no
pheasant process running and no Python dependency on this package.

!!! info "It reads state, it never writes it"

    An export takes no lease and issues nothing but `SELECT`. Running one
    during a sync is a WAL reader alongside the writer, not a second writer, so
    it is safe to put on a schedule. `/state` stays the source of truth;
    `/exports` stays regenerable.

## Install

DuckDB is an optional extra — it is the Parquet writer and the query engine,
and a region that never exports should not carry the wheel:

```bash
pip install 'pheasant-kb[analytics]'
```

The published Docker image installs it as part of `PHEASANT_EXTRAS`.

## Export

```bash
pheasant export parquet -c pheasant.yaml
```

```
Exported pheasant-repo -> /exports/parquet/pheasant-repo
  artifacts.parquet: 309 row(s), 24637 bytes
  chunks.parquet: 1077 row(s), 1142271 bytes
  graph_edges.parquet: 4220 row(s), 89944 bytes
  graph_nodes.parquet: 2467 row(s), 260281 bytes
  memory_records.parquet: 0 row(s), 416 bytes
  sources.parquet: 1 row(s), 3330 bytes
  symbols.parquet: 211 row(s), 12399 bytes
  sync_events.parquet: 1 row(s), 2062 bytes
  export.json: manifest
```

Those are real numbers, from indexing this repository into itself: 309 files
become 1,077 chunks and a 2,467-node graph, and the whole corpus — text
included — exports to about 1.5 MB.

| Flag | Default | What it does |
|---|---|---|
| `--out DIR` | `<exports_path>/parquet/<kb_id>` | Where the files land. |
| `--table NAME` | every table but `artifact_terms` | Export only this table. Repeatable. |
| `--compression` | `zstd` | `zstd`, `snappy`, `gzip` or `uncompressed`. |
| `--json` | off | Print the manifest instead of the summary. |

Each file is written to `<table>.parquet.tmp` and renamed into place, so a
reader polling the directory sees either the previous export or the new one —
never a half-written file.

### The manifest

`export.json` describes **the directory**, not just the last run:

```json
{
  "format_version": 1,
  "kb_id": "pheasant-repo",
  "generated_at": "2026-08-19T01:22:12+00:00",
  "pheasant_version": "0.10.0",
  "state_backend": "sqlite",
  "compression": "zstd",
  "exported": ["chunks"],
  "tables": [
    {"table": "artifacts", "file": "artifacts.parquet", "rows": 309,
     "bytes": 24637, "modified_at": "2026-08-19T01:04:55+00:00", "refreshed": false},
    {"table": "chunks", "file": "chunks.parquet", "rows": 1077,
     "bytes": 1142271, "modified_at": "2026-08-19T01:22:12+00:00", "refreshed": true}
  ]
}
```

`exported` is what this run refreshed; `refreshed: false` with an older
`modified_at` is how a stale file from a previous export shows up as stale
instead of passing for current. `format_version` is the layout contract for
readers that are not pheasant — see
[Parquet export schema](../reference/export-schema.md).

## What gets exported

Run `pheasant export tables` for the live list. Every column of every state
table is exported, including columns added by migration (`artifacts.acl`).

This is the summary. The column-by-column reference — types, join keys, ID
grammar, and the semantics an outside reader has to apply itself — is
[Parquet export schema](../reference/export-schema.md).

| File | One row per | Useful columns |
|---|---|---|
| `sources.parquet` | configured source | `name`, `type`, `path`, `enabled`, `last_indexed_at`, `last_status` |
| `artifacts.parquet` | indexed file | `relative_path`, `sha256`, `size_bytes`, `mime_type`, `git_branch`, `git_commit`, `last_indexed_at`, `acl` |
| `chunks.parquet` | indexed chunk | `artifact_id`, `chunk_index`, `heading_path`, `start_line`, `end_line`, `text`, `token_estimate` |
| `symbols.parquet` | code symbol | `language`, `symbol_type`, `name`, `qualified_name`, `signature`, `start_line` |
| `memory_records.parquet` | agent-memory record | `scope`, `subject`, `kind`, `asserted_at`, `valid_until`, `supersedes`, `salience` |
| `sync_events.parquet` | sync run | `event_type`, `status`, `started_at`, `finished_at`, `details_json` |
| `graph_nodes.parquet` | graph node | `node_id`, `type`, `label`, `source_id`, `artifact_id`, `attributes` |
| `graph_edges.parquet` | graph edge | `from_node`, `to_node`, `type`, `confidence`, `attributes` |
| `artifact_terms.parquet` | enrichment term (opt in with `--table`) | `node_id`, `node_type`, `term`, `weight` |

Three conventions are worth knowing before you write a query:

- **Timestamps are ISO-8601 strings**, not Parquet timestamps — they are stored
  that way in state and are exported verbatim. Cast when you need date maths:
  `CAST(last_indexed_at AS TIMESTAMP)`.
- **`graph_nodes.attributes` and `graph_edges.attributes` are JSON text.** Node
  attributes are heterogeneous by node type (a symbol has a signature, a
  heading has a level) and Parquet wants one schema per file, so everything
  beyond the promoted columns lives in the bag. Reach into it with
  `json_extract_string(attributes, '$.language')`.
- **Edge endpoints are `from_node` / `to_node`**, not `source` / `target`. In
  pheasant "source" already means a *configured source*, and a column named
  `source` sitting next to `sources.parquet` would be misread by everyone once.

### What is deliberately not exported

| Not exported | Why |
|---|---|
| `chunks_fts`, `chunks_vocab` | Derived caches, and dialect-specific (FTS5 virtual table vs. a `tsvector` column). Rebuildable from `chunks`. |
| `idp_groups`, `idp_sync_meta`, `source_audit_events` | Who a principal is, which groups they are in, and what they did. An export is a file people pass around; identity and audit data is not. |
| `source_checkpoints`, `source_leases`, `index_tasks`, `sync_fingerprints`, `manifests` | Operational state — transient, opaque, and meaningless once detached from the region that wrote it. |
| Vectors | They already have a columnar home in the LanceDB store under `<state>/vectors/`. |

`artifact_terms` *is* exportable but is not in the default set: it reached 1.27M
rows on a 2,132-file corpus and is retired as a retrieval path, so paying for it
by default would be paying for history. Ask for it explicitly:

```bash
pheasant export parquet -c pheasant.yaml --table artifact_terms
```

## Query it with pheasant

```bash
pheasant export query -c pheasant.yaml "SELECT count(*) FROM artifacts"
```

Every Parquet file in the export directory is registered as a view named after
the file, so tables are referred to by name and joins work across them:

```bash
pheasant export query -c pheasant.yaml "
  SELECT a.relative_path, count(*) AS chunks, sum(c.token_estimate) AS tokens
  FROM chunks c JOIN artifacts a ON a.id = c.artifact_id
  GROUP BY 1 ORDER BY chunks DESC"
```

| Flag | Default | What it does |
|---|---|---|
| `--dir DIR` | `<exports_path>/parquet/<kb_id>` | Which export to query. |
| `--format` | `table` | `table`, `json` or `csv`. |
| `--limit N` | `50` | Caps the result set. `--limit 0` removes the cap. |

`--limit` wraps your statement rather than trusting you to have added one,
because the first thing anyone types is `SELECT * FROM chunks` and the honest
answer to that on a real corpus is a page, not a million rows through a
terminal. Long values are elided in `table` format; use `--format json` or
`--format csv` when you need them whole.

Pipe `csv` or `json` straight into whatever comes next:

```bash
pheasant export query -c pheasant.yaml --format csv --limit 0 \
  "SELECT relative_path, size_bytes FROM artifacts" > files.csv
```

## Query it without pheasant

The whole point of Parquet is that the files outlive the tool. From the export
directory, DuckDB resolves a bare filename in `FROM`:

```bash
cd /exports/parquet/my-kb
duckdb -c "SELECT relative_path, size_bytes FROM 'artifacts.parquet' ORDER BY 2 DESC LIMIT 10"
```

=== "DuckDB (Python)"

    ```python
    import duckdb

    duckdb.sql("""
        SELECT type, count(*) AS edges
        FROM read_parquet('/exports/parquet/my-kb/graph_edges.parquet')
        GROUP BY 1 ORDER BY edges DESC
    """).show()
    ```

=== "pandas"

    ```python
    import pandas as pd

    artifacts = pd.read_parquet("/exports/parquet/my-kb/artifacts.parquet")
    artifacts.groupby("source_id")["size_bytes"].sum().sort_values(ascending=False)
    ```

=== "polars"

    ```python
    import polars as pl

    # Lazy: predicate and projection are pushed into the Parquet reader, so a
    # multi-GB chunks file never lands in memory whole.
    (
        pl.scan_parquet("/exports/parquet/my-kb/chunks.parquet")
        .filter(pl.col("token_estimate") > 400)
        .select("artifact_id", "heading_path", "token_estimate")
        .collect()
    )
    ```

## Recipes

Every query below runs as-is through `pheasant export query`.

**Where the corpus's weight is**

```sql
SELECT source_id, count(*) AS files, sum(size_bytes) AS bytes
FROM artifacts GROUP BY 1 ORDER BY bytes DESC;
```

**Top-level directories by file count**

```sql
SELECT coalesce(nullif(regexp_extract(relative_path, '^([^/]+)/', 1), ''), '(root)') AS dir,
       count(*) AS files
FROM artifacts GROUP BY 1 ORDER BY files DESC;
```

**Duplicated content** — same bytes indexed under several paths

```sql
SELECT sha256, count(*) AS copies, string_agg(relative_path, ', ') AS paths
FROM artifacts GROUP BY 1 HAVING count(*) > 1 ORDER BY copies DESC;
```

Run against this repository it returns exactly one row: the nine empty
`__init__.py` files, which share the SHA-256 of zero bytes.

**Chunking sanity** — which files split into the most pieces

```sql
SELECT a.relative_path, count(*) AS chunks, sum(c.token_estimate) AS tokens
FROM chunks c JOIN artifacts a ON a.id = c.artifact_id
GROUP BY 1 ORDER BY chunks DESC;
```

**The shape of the graph**

```sql
SELECT type, count(*) AS edges FROM graph_edges GROUP BY 1 ORDER BY edges DESC;
```

**The most-referenced things in the corpus**

```sql
SELECT n.label, n.type, count(*) AS incoming
FROM graph_edges e JOIN graph_nodes n ON n.node_id = e.to_node
GROUP BY 1, 2 ORDER BY incoming DESC;
```

**Edges that cross sources** — what a shard split would sever
(see [capacity planning](capacity-planning.md))

```sql
SELECT f.source_id AS from_source, t.source_id AS to_source, count(*) AS edges
FROM graph_edges e
JOIN graph_nodes f ON f.node_id = e.from_node
JOIN graph_nodes t ON t.node_id = e.to_node
WHERE f.source_id IS DISTINCT FROM t.source_id
GROUP BY 1, 2 ORDER BY edges DESC;
```

**Symbols by language and kind**

```sql
SELECT language, symbol_type, count(*) AS n
FROM symbols WHERE language IS NOT NULL GROUP BY 1, 2 ORDER BY n DESC;
```

**Reaching into the graph attribute bag**

```sql
SELECT json_extract_string(attributes, '$.language') AS language, count(*) AS nodes
FROM graph_nodes WHERE type = 'symbol' GROUP BY 1 ORDER BY nodes DESC;
```

**Live agent memory by scope** (see [agent memory](agent-memory.md))

```sql
SELECT scope, kind, count(*) AS n
FROM memory_records WHERE valid_until IS NULL GROUP BY 1, 2;
```

**Sync history**

```sql
SELECT event_type, status, count(*) AS n,
       max(CAST(finished_at AS TIMESTAMP)) AS latest
FROM sync_events GROUP BY 1, 2 ORDER BY n DESC;
```

## Reaching an export from outside

The integration is a **file copy**. pheasant writes `/exports`; anything that
can read that directory is a consumer — no API, no client library, no pheasant
process on the reader's side. Which means the whole question is "how do I get
at that volume", and it has a different answer per runtime.

One rule spans all of them: **the producer needs state access, the reader does
not.** Running the export requires a writable `/state` (SQLite) or the DSN
(Postgres). Reading the result requires `/exports` and nothing else. Keeping
those separate is why `/exports` is its own volume rather than a directory
inside `/state`.

!!! warning "The volume's access *is* the access control"

    An export enforces nothing — memory scope isolation and `artifacts.acl` are
    exported as data. Mounting `/exports` into a service grants it the union of
    every principal's access to the corpus. Mount it only where you would hand
    over the whole knowledge base. See
    [Parquet export schema](../reference/export-schema.md).

### Docker Compose

`/exports` is a named volume (`pheasant-exports`). Three ways out, easiest
first:

=== "Bind-mount a host path"

    Swap the named volume for a directory your other service already reads —
    the compose files carry this as a commented line:

    ```yaml
    volumes:
      - ${PHEASANT_EXPORTS_PATH:-./exports}:/exports
    ```

    ```bash
    PHEASANT_EXPORTS_PATH=/srv/analytics/pheasant docker compose up -d
    ```

=== "Mount the volume from another container"

    Any container on the same host can attach it read-only:

    ```bash
    docker run --rm -v pheasant-exports:/in:ro python:3.12-slim \
      sh -c "pip install -q duckdb && python -c \"
    import duckdb
    print(duckdb.sql(\\\"SELECT count(*) FROM '/in/parquet/my-kb/chunks.parquet'\\\")) \""
    ```

    As a long-lived sibling service, in the same compose file:

    ```yaml
    services:
      analytics:
        image: your/loader
        volumes:
          - pheasant-exports:/exports:ro
    ```

=== "Copy it out"

    ```bash
    docker compose cp pheasant:/exports/parquet/my-kb ./my-kb
    ```

Produce the export on a schedule from the host's cron:

```bash
0 4 * * *  docker compose exec -T pheasant pheasant export parquet -c /config/pheasant.yaml
```

### Kubernetes

`/exports` is a `PersistentVolumeClaim` named `pheasant-exports`, and
`deploy/kubernetes/scaled/exports-cronjob.yaml` fills it nightly. A consumer
mounts the same claim read-only:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: warehouse-load
  namespace: pheasant
spec:
  template:
    spec:
      restartPolicy: OnFailure
      containers:
        - name: load
          image: your/loader
          volumeMounts:
            - name: exports
              mountPath: /exports
              readOnly: true
      volumes:
        - name: exports
          persistentVolumeClaim:
            claimName: pheasant-exports
```

The claim is **ReadWriteMany** in the scaled fleet for the same reason `/state`
is: with ReadWriteOnce the volume attaches to one node, so a reader scheduled
elsewhere cannot mount it at all. If your cluster has no RWX StorageClass, give
the claim RWO and run the reader as a sidecar or a same-node job — or push the
files to object storage instead (below). Losing the volume costs a re-export,
never data.

For a one-off, `kubectl cp` works:

```bash
kubectl -n pheasant cp "$(kubectl -n pheasant get pod -l app.kubernetes.io/component=indexer \
  -o jsonpath='{.items[0].metadata.name}')":/exports/parquet/my-kb ./my-kb
```

With Helm, point the chart at a claim your consumer already mounts rather than
letting it create one:

```yaml
persistence:
  exports:
    enabled: true
    existingClaim: analytics-shared
```

### Object storage, for a consumer outside the cluster

The usual answer when the reader is a warehouse, a notebook, or another team.
Add a sync step after the export — same CronJob, or a sidecar:

```bash
pheasant export parquet -c /config/pheasant.yaml \
  && aws s3 sync /exports/parquet/my-kb "s3://my-bucket/pheasant/my-kb/" --delete
```

Parquet in object storage is directly queryable — DuckDB, Athena, BigQuery,
Snowflake and Spark all read it in place, so the consumer never mounts
anything:

```sql
SELECT count(*) FROM 's3://my-bucket/pheasant/my-kb/chunks.parquet';
```

### Not over HTTP

pheasant does not serve `/exports` from its API, deliberately. An export
flattens the ACL model, so a route would hand every principal's memory records
and every ACL-restricted artifact to any caller who can reach it. If you want
HTTP delivery, put your own authenticated static file server or object-store
presign in front of the volume — that keeps the authorization decision
somewhere it can actually be made.

That is also why **exporting one source** is a different operation. The
taxonomy view's and Sources page's **Export source**, `GET
/sources/{name}/export` and MCP `export_source` hand back one source's
documents as a ZIP, JSONL or Parquet file, built per request from what the
*caller* may read (the listing's scope and ACL, `knowledge.use`). It is the
documents a person or agent wants to keep; this page's export is the whole
corpus, every table, for analysis, and it stays on the volume.

### One constraint worth knowing before you design around it

On **SQLite**, the export must run where `/state` is writable. SQLite creates
its `-wal` and `-shm` sidecars even to read, so a read-only `/state` — which is
what `deploy/compose/docker-compose.scale.yml` gives the api replicas — fails outright:

```
ERROR: could not open the SQLite state at /state/pheasant.db: unable to open
database file. If /state is mounted read-only, that is the cause…
```

Run the export from the container that owns `/state` (the indexer, or the
single-container install), or copy the state directory somewhere writable
first. On **Postgres** it does not arise: the tables come from the database, so
`/state` is read-only-mountable and the shipped CronJob does exactly that.

## Keep exports fresh

An export is a snapshot. Re-run it after a sync — the command is idempotent, so
re-running over an existing directory just replaces the files:

```bash
pheasant sync --all -c pheasant.yaml && pheasant export parquet -c pheasant.yaml
```

## Limits worth knowing

- **The export is derived data.** It is a snapshot of `/state`, not a backup of
  it — it deliberately omits the operational tables a region needs to resume.
  Use [`pheasant backup`](backup-restore.md) for anything you intend to restore.
- **Row order is the primary key's**, because the export streams by keyset
  pagination on it. Do not rely on it meaning anything else.
- **`chunks.parquet` carries every chunk's full text**, so it is by far the
  largest file — roughly the size of the indexed corpus before compression.
  Deselect it with `--table` when you only want structure.
- **Both state backends export identically.** SQLite and Postgres produce the
  same columns and the same stable IDs, and the manifest records which one the
  export came from.

## What it costs

Measured on a 4-core machine, exporting synthetic corpora of increasing size.

### When you are not exporting: nothing

DuckDB is imported lazily, inside the functions that need it. Nothing else
pulls it in — verified against `sys.modules`, not assumed:

| Process | DuckDB loaded? |
|---|---|
| `pheasant serve` / the API app | no |
| `pheasant sync` / the indexer | no |
| `pheasant --help`, `validate`, `config show` | no |
| `pheasant export tables` | no |
| `pheasant export parquet` / `query` | yes |

So a running region pays **zero** — no memory, no latency, no import. The CLI
pays about 7 ms and 1.7 MB to build its argument parser (the `analytics`
module itself, stdlib only); on any command that loads the sync engine that is
lost in the noise.

### Exporting

| Corpus | Export | Peak RSS | Parquet out |
|---|---|---|---|
| 250 files (0.8 MB) | 0.2 s | 76 MB | 0.1 MB |
| 1,000 files (3.2 MB) | 0.3 s | 103 MB | 0.3 MB |
| 4,000 files (12.6 MB) | 0.8 s | 178 MB | 1.3 MB |
| 16,000 files (50.6 MB) | 1.8 s | 461 MB | 5.1 MB |

For scale: indexing that 16,000-file corpus took **203 s at 254 MB peak**. The
export is under 1% of the sync's time, and it runs on a container already
sized for the sync.

The fixed floor is ~23 MB for Python and pheasant plus ~33 MB for DuckDB
itself. Past that, two things drive the numbers:

- **The graph tables dominate at scale**, and that cost is pheasant's existing
  in-memory graph, not DuckDB. On the 16,000-file corpus: `chunks` alone is
  0.7 s / 246 MB, all six SQL tables are 0.9 s / 273 MB, and adding
  `graph_nodes` + `graph_edges` takes it to 1.8 s / 461 MB — the graph has to
  be loaded whole to be walked. Deselect it when you only want the tables:

    ```bash
    pheasant export parquet --table artifacts --table chunks --table symbols
    ```

- **Memory is flat, not proportional.** DuckDB is given a 512 MB buffer-pool
  limit and spills to `.pheasant-export-tmp/` inside the export directory
  rather than growing. On a fixture of unique, incompressible text the peak is
  585 MB at 100k rows, 707 MB at 400k and 646 MB at 1M — no trend across a 10×
  range. Without the limit the same three sizes climb 696 → 1,238 → 1,805 MB
  and keep going.

### Querying

Query latency is set by the Parquet files, not by the corpus behind them, and
it did not move across the sweep:

| Operation | Latency |
|---|---|
| Cold start (open DuckDB, register every view) | ~50 ms |
| `SELECT count(*) FROM chunks` | ~16 ms |
| `artifacts ⨝ chunks`, group and sort | ~22 ms |

Add ~60 ms of process start if you are measuring `pheasant export query` from a
shell rather than the library.

### Disk

The Parquet output is small — on this repository's own index, 309 files and
1,077 chunks come to 1.5 MB total, against 195 MB of `/state` for the larger
sweep corpus. Two transient costs during a run, both deleted before the command
returns:

- a per-table NDJSON staging file, roughly the size of that table's raw
  content — bounded by the **largest single table**, not by all of them, since
  it is removed as soon as that table's Parquet lands;
- DuckDB's spill directory, bounded by how far the working set exceeds the
  512 MB limit.

### Concurrency

The export issues only `SELECT` and takes no lease, so on SQLite it is a WAL
reader beside the indexer's writer and on Postgres an ordinary read-only
session. It does not block a sync, and a sync does not block it — though an
export taken mid-sync is a snapshot of a moving corpus, which is a consistency
question rather than a performance one.

## Why DuckDB is here and not in `/state`

DuckDB is a Parquet writer and a query engine in pheasant — never a state
backend, and never on the sync path. That boundary is deliberate:

- The indexing write path is per-artifact `DELETE` + re-`INSERT`,
  conditional-`UPDATE` lease claims and `UPDATE … RETURNING` queue claims. That
  is single-row OLTP, which is a bulk-columnar engine's worst case.
- DuckDB's full-text index cannot be maintained incrementally — it is rebuilt
  wholesale — which would turn "a re-sync of an untouched corpus does no work"
  into "every sync rebuilds the index". Its `match_bm25` also has no per-column
  weighting, so the measured `8/3/2/1` title/path/heading/text weights could not
  be expressed at all.
- DuckDB takes an exclusive **file lock**: while one process holds the database
  read-write, no other process can open it, even read-only. SQLite's WAL is what
  lets `deploy/compose/docker-compose.scale.yml` mount `/state:ro` on the API replicas while the
  indexer writes.

So it earns its place on the read side, owning nothing. For state, the choice
stays SQLite (default) or [Postgres](../configuration.md).
