# Back up and restore a region

A region's user data is in two places that have to be copied together: the
**PostgreSQL database** (state, chunks, the full-text index, the graph's rows,
usage, evaluation and tuning) and **`/state`** (vectors, graph history
snapshots, media, uploads, artifact records). pheasant-kb also has the
`/memory` volume (a 1.x region's `agent-memory` records) and its generated
tokens.

## Back up

One command copies all of it as one checksummed folder:

```bash
docker compose stop indexer && docker compose run --rm backup && docker compose start indexer
```

The backup runs `pg_dump` in the same Postgres image the stack serves from, so
the dump always matches the server. Stopping the indexer makes the database and
`/state` an exact pair; without it the backup still runs, warns, and is
usable after a repair sync. Each backup is a folder in `./backups`
(`PHEASANT_BACKUP_PATH`) holding `db.pgdump`, `state.tar.gz`, `memory.tar.gz`,
`secrets.tar.gz` and `SHA256SUMS`. A backup that fails half way leaves no
folder, so one that exists is complete.

## Restore

```bash
docker compose stop api graph indexer logger worker
docker compose run --rm restore pheasant-kb-20261021T203120Z --force
docker compose up -d
```

Restore checks every file against `SHA256SUMS` first, refuses while a pheasant
service is running, refuses a populated stack without `--force`, and with it
keeps what it replaces (the current database dumped to
`backups/pre-restore-<time>/`, `/state` and `/memory` moved into
`.replaced-<time>` folders). The database is restored in one transaction.

The full procedure, including the repair sync after a live backup and moving a
backup between hosts, is in
[Operate pheasant-kb](../pheasant-kb/operate.md#back-up-and-restore).

## Graph history snapshots

Independently of backups, the indexer writes zstd-compressed node-link
snapshots of the published graph (`graphs/<kb_id>/graph.<utc-ts>.json.zst`
under `/state`), materialized from the rows on a separate publisher so a
snapshot can never block a commit or be half written. They are:

- **Throttled** by `storage.graph_snapshot_interval_seconds` (900 s by default).
- **Bounded** by `storage.max_state_size_gb`: past the cap the oldest snapshots
  are evicted first. Nothing else under `/state` is ever evicted.

```yaml
storage:
  graph_snapshots: true
  graph_snapshot_interval_seconds: 900
  compression: zstd
  max_state_size_gb: 10
```

Snapshots are point-in-time history; a backup is the portable copy you take off
the host.

## Notes

- One region, one database and one `/state`. Never point two regions at the
  same writable volume or the same database.
- `/exports` is regenerable (`pheasant export parquet`) and is not backed up.
- `storage.max_state_size_gb` bounds snapshot history; the knowledge base's own
  size limit is `storage.knowledge_base_limit_gb`, measured as indexed content.
