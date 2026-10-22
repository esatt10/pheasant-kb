# How to back up and restore region state

!!! tip "pheasant-kb is the default deployment"
    This page covers the single-container, SQLite-backed deployment. pheasant-kb
    keeps its index in Postgres; back it up as described in
    [Operate pheasant-kb](../pheasant-kb/operate.md#back-up-and-restore).
    Start with [Run pheasant-kb](../pheasant-kb/index.md).

`/state` is pheasant's operational source of truth — SQLite, the graph and its
snapshots, manifests, the published contract, the event stream, and the vector
index. Treat it as **user data** and back it up.

## What's in a backup

`pheasant backup` writes a single `.tar.zst` archive containing:

| Item | Notes |
|---|---|
| `pheasant.db` | A **consistent** SQLite snapshot via `VACUUM INTO` (never a raw copy; survives WAL). |
| `graphs/` | Graph snapshots (`graph.<ts>.json.zst`), plus `graph.latest.json.zst` on `storage.graph_format: node_link_json`. On the default `rows` backend the published graph is in the database, so it is covered by the row above. |
| `contract.latest.json` | The published Synapse contract, when present. |
| `events/` | The append-only NDJSON sync-event stream, when present. |
| `vectors/` | The per-region vector index, when present. |

The archive is written durably (temp file + fsync + atomic rename).

## Create a backup

```bash
pheasant backup ./backups/pheasant-$(date -u +%Y%m%dT%H%M%SZ).tar.zst \
  --config pheasant.yaml
```

The first positional argument is the output path; `--config` / `-c` points at
your config so the correct `/state` is located.

## Restore a backup

```bash
pheasant restore ./backups/pheasant-20260621T120000Z.tar.zst \
  --config pheasant.yaml
```

Restore is **safe by default**:

- It refuses a non-empty target unless you pass `--force`.
- Extraction is protected against path traversal.
- The DB is validated (`PRAGMA integrity_check`) before being swapped in.
- The swap is atomic; the previous state is preserved as
  `<name>.replaced-<ts>` — never deleted.

To overwrite an existing populated state:

```bash
pheasant restore ./backups/pheasant-20260621T120000Z.tar.zst \
  --config pheasant.yaml --force
```

## Validate after restore

After a restore, confirm the region is healthy and consistent:

```bash
# config + environment
pheasant validate pheasant.yaml
pheasant doctor --config pheasant.yaml

# rebuild any missing/invalid derived state from manifests + DB
pheasant repair --config pheasant.yaml

# runtime health
pheasant start --config pheasant.yaml &
curl http://localhost:8765/health
curl http://localhost:8765/ready
```

If a source looks stale or partially indexed, run a targeted repair or a full
sync:

```bash
pheasant sync --config pheasant.yaml --source <name> --mode repair
pheasant sync --config pheasant.yaml --source <name> --mode full
```

## Graph snapshots and retention

Independently of explicit backups, pheasant writes zstd-compressed, timestamped
graph snapshots after a successful sync
(`graphs/<kb_id>/graph.<utc-ts>.json.zst`). Snapshots are files on both
`storage.graph_format` backends — on `rows` the snapshot is materialized from
the rows, so a history file is the same document whichever backend produced it.
They are:

- **Throttled** by `storage.graph_snapshot_interval_seconds` (default 900s).
- **Bounded** by `storage.max_state_size_gb` — when the cap is exceeded, the
  oldest snapshots are evicted first; the published graph, the SQLite DB, and
  the contract are **never** evicted.

```yaml
storage:
  graph_snapshots: true
  graph_snapshot_interval_seconds: 900
  compression: zstd
  max_state_size_gb: 10
```

Snapshots are point-in-time history; `pheasant backup` is the portable archive
you copy off-box.

## Operational notes

- Never point two independent pheasant instances at the same writable `/state`.
  Use one volume per instance.
- Back up before upgrades or migrations. State migrations are one-shot and
  idempotent and preserve originals, but a fresh archive is cheap insurance.
