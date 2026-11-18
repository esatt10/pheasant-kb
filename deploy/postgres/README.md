# pheasant's PostgreSQL image

`ghcr.io/esatt10/pheasant-postgres:17.11-pgts1.5.1-r1` is the database every
shipped pheasant stack runs: the official PostgreSQL 17 image with the
[`pg_textsearch`](https://github.com/timescale/pg_textsearch) extension built
in and preloaded, and nothing else. `storage.lexical_index: pg_textsearch`
(the BM25 lexical arm, the Postgres default) needs exactly that.

It replaces `timescale/timescaledb-ha:pg17`, which pheasant used only because
it happened to preload `pg_textsearch`. That image also carries TimescaleDB,
the Timescale Toolkit and other components under Timescale-specific terms;
pheasant never used any of them.

## What is in it

| Component | Version | Licence | Source |
| --- | --- | --- | --- |
| PostgreSQL (official Docker image, Debian bookworm) | 17.11 | PostgreSQL License | `postgres:17.11-bookworm@sha256:665dc7b37ac52a17e298511414c21d23ffa4e6a6ef6225b3e5d59c7afb35e994` |
| pg_textsearch | 1.5.1 | PostgreSQL License | `https://github.com/timescale/pg_textsearch/archive/refs/tags/v1.5.1.tar.gz`, sha256 `2e7fb76ed96176afc6d16cc5d095ced2cb233ff27fbeaa8c7bfffc82e02f7498` |

The extension's `LICENSE` and `NOTICE` are installed at
`/usr/share/doc/pg-textsearch/`. The Debian packages in the base image carry
their own licences under `/usr/share/doc/*/copyright`. `pg_prewarm` and
`pg_stat_statements` are PostgreSQL contrib modules already in the base.

The extension is compiled in a separate build stage against the base's own
`postgresql-server-dev-17` package (the exact `$PG_VERSION` the server was
installed from), and only its installed files are copied into the final image.
The compiler is not shipped.

Behaviour the image sets:

- **Preload.** `shared_preload_libraries = 'pg_textsearch'` is written into
  the `postgresql.conf.sample` that `initdb` copies, so every new cluster
  preloads it. A `-c shared_preload_libraries=...` on the command line still
  wins: Container Apps passes `pg_textsearch,pg_prewarm`.
- **Locale.** New clusters are `--locale-provider=libc --locale=C.UTF-8
  --encoding=UTF8`, what every pheasant database has had, so a restored dump
  sorts and compares text as it did.
- **Data.** The official layout: `PGDATA=/var/lib/postgresql/data`, owned by
  `postgres` (uid/gid 999). The TimescaleDB image used `/home/postgres/pgdata`
  as uid 1000, so a stack that ran it moves its data to a new volume (below).
- **Upgrade guard.** `pheasant-entrypoint.sh` runs before the official
  entrypoint and refuses to initialize an empty cluster while the previous
  data directory (mounted read-only at `/var/lib/pheasant-previous-postgres`)
  holds one. `PHEASANT_POSTGRES_ALLOW_FRESH=1` lets it initialize.

## Build

```sh
docker build -t pheasant-postgres:17 deploy/postgres
docker buildx build --platform linux/amd64,linux/arm64 deploy/postgres
```

`compose.yaml` names the published tag and `build: ./deploy/postgres`, so a
checkout builds the image itself when the tag is not available. CI builds it
in every Postgres job (`.github/actions/pheasant-postgres`), and
`container.yml` publishes it for `linux/amd64` and `linux/arm64`.

The tag is the image's contents: PostgreSQL minor, extension version, and this
directory's revision (`-r1`). Changing any of them is a new tag, and an
extension upgrade is never folded into an image swap.

## Upgrading an existing stack

A stack that ran `timescale/timescaledb-ha:pg17` keeps its database in the
`pheasant-pg17` volume. This image uses `pheasant-postgres17`, and the old
volume is mounted read-only only so the guard can see it. The data moves by a
logical dump and restore. Nothing is copied at the file level and nothing is
deleted.

1. **Quiesce and back up on the old image.** With the stack still on its
   current `compose.yaml`, stop the writers and take a backup:

   ```sh
   docker compose stop api indexer graph logger worker
   docker compose run --rm backup
   ```

   The backup holds the database, `/state`, `/memory` and the tokens, with
   checksums (`deploy/compose/backup.sh`).

2. **Upgrade and start only the new database:**

   ```sh
   git pull            # or take the release that ships this image
   PHEASANT_POSTGRES_ALLOW_FRESH=1 docker compose up -d postgres
   ```

3. **Restore** the backup from step 1 (`--force`, because `/state` and
   `/memory` already hold the same data):

   ```sh
   docker compose run --rm restore <backup-name> --force
   ```

   `restore.sh` leaves out the TimescaleDB image's own extension entries (it
   says how many). Every pheasant table, index and row is restored, and it runs
   `ANALYZE` afterwards.

4. **Start the stack:** `docker compose up -d`. The next restart needs no
   flag, because the new volume now holds the cluster.

Verify it with `GET /ready` and a search, and compare row counts if you want
certainty (`SELECT count(*)` per table on both).

**Rollback.** Until the new stack accepts writes, switching back is the
previous `compose.yaml` (or `PHEASANT_POSTGRES_IMAGE=timescale/timescaledb-ha:pg17`
with the old volume mapping): `pheasant-pg17` was never written. After the new
stack has accepted writes, the old volume is stale. Take a backup of the new
stack and restore that instead of pointing clients at old data.

A database with objects that depend on TimescaleDB (hypertables, continuous
aggregates, Toolkit functions used by your own objects) needs its own
conversion plan first. pheasant creates none. Check with:

```sql
SELECT count(*) FROM timescaledb_information.hypertables;
SELECT count(*) FROM timescaledb_information.continuous_aggregates;
```
