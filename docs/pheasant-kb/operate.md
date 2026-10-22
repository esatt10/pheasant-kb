# Operate pheasant-kb

Run these from the directory holding `compose.yaml` and `.env`.

## See what it is doing

```bash
docker compose ps                    # every service and its health
docker compose logs -f indexer       # syncs, commits, embedding errors
docker compose logs -f api           # requests and answers
curl -s http://127.0.0.1:8765/ready  # readiness, and the graph generation served
```

Per-source progress (phase, rate, ETA, stalled) is in the UI's jobs tray and at
`GET /jobs`. Syncs that are queued but not yet claimed by the indexer are at
`GET /queue`. Metrics are at <http://127.0.0.1:8765/metrics>;
[Monitor indexing](../how-to/monitor-indexing.md) explains what to watch, and
[Scale up or down](scale.md) which number to move when one of them is high.

## Where the images come from

pheasant-kb runs three images, from two registries:

| Image | Registry | Built from | Used by |
|---|---|---|---|
| `ghcr.io/esatt10/pheasant:<version>` | GitHub Container Registry | This repository's `Dockerfile` | `api`, `graph`, `indexer`, `worker`, `logger` and the three init services |
| `timescale/timescaledb-ha:pg17` | Docker Hub | Timescale; Postgres 17 with the `pg_textsearch` extension | `postgres`, `backup`, `restore` |
| `nats:2.10-alpine` | Docker Hub | The NATS project | `nats` |

`docker compose up` pulls each from its own registry; nothing needs a login.
Two things to know:

- **Docker Hub limits anonymous pulls** per IP address. On a shared network or
  a CI runner a first start can fail with `429 Too Many Requests`;
  `docker login` with any free Docker Hub account raises the limit.
- **The pheasant image is published for `linux/amd64`.** On Apple Silicon,
  Docker Desktop runs it under emulation, which works but is slower. Building
  locally gives a native image: `docker compose build`.

### When a new pheasant image is published

Every merge to `main` publishes one, automatically, once CI passes on the
merged commit (`.github/workflows/container.yml`):

1. CI runs on the merge commit.
2. If it is green, the workflow builds the `Dockerfile` and pushes
   `ghcr.io/esatt10/pheasant:<next version>` and `:latest` (and the
   `pheasant-ui` image under the same version).
3. It reads the tags back from the registry to confirm they are live.
4. Only then does it commit `chore: release <version>` to `main`, rewriting the
   image tag in `compose.yaml` and every other deployment file
   (`scripts/sync_version.py`).

So pheasant-kb itself is not an image: it is `compose.yaml` and its config in
this repository, pinned to the image of the release it was committed with. A
red CI on a merge publishes nothing, and the workflow run fails and names the
commit. The next green merge publishes `main`'s head, including that commit.

## Upgrade

Each release updates the image tag that `compose.yaml` names, so an upgrade is
a pull and a restart:

```bash
git pull
docker compose pull
docker compose up -d
```

Schema changes migrate on start (`db-init` runs before anything serves) and
keep the originals they replace; release notes say when a release changes how
files are processed, which re-reads the affected sources on their next sync.
Your `.env`, your
`compose.override.yaml` and your own `pheasant.yaml` copy are not touched.

Since pheasant-kb became pheasant's default configuration, a config that names
no defaults table is built on pheasant-kb's. pheasant-kb configs are
unaffected. A config of your own for a *different* shape, such as a laptop
`pheasant up` config written before this release, needs `defaults: standalone`
under `pheasant:` to keep running on SQLite; see
[Defaults](../configuration.md#defaults-pheasant-kb-or-standalone). If
you keep a config copy, compare it with the new
`deploy/compose/pheasant-kb.yaml` for settings you may want to adopt.

## Back up and restore

One command backs up everything a pheasant-kb stack holds: the Postgres
database, `/state` (vectors, graph snapshots, images, uploads), agent memory
and the four tokens.

```bash
docker compose stop indexer && docker compose run --rm backup && docker compose start indexer
```

Each backup is a folder in `./backups` (set `PHEASANT_BACKUP_PATH` in `.env`
to put them elsewhere), named by time, for example
`pheasant-kb-20261021T203120Z`, holding `db.pgdump`, `state.tar.gz`,
`memory.tar.gz`, `secrets.tar.gz` and a `SHA256SUMS` file. A backup that fails
half way leaves no folder, so one that exists is complete.

Stopping the indexer is what makes the database and `/state` an exact pair.
Without it the backup still runs, warns, and is usable: after restoring it,
run a repair sync, which reconciles vectors with the chunks the database
holds:

```bash
docker compose stop indexer && docker compose run --rm --no-deps indexer repair -c /config/pheasant.yaml && docker compose start indexer
```

To restore:

```bash
docker compose stop api graph indexer logger worker
docker compose run --rm restore pheasant-kb-20261021T203120Z --force
docker compose up -d
```

`restore` is safe by default:

- It checks every file against `SHA256SUMS` before touching anything.
- It refuses while any pheasant service is still running.
- It refuses a stack that already holds data unless you pass `--force`. Even
  then nothing is deleted: the current database is dumped to
  `backups/pre-restore-<time>/`, and the current `/state` and `/memory`
  contents are moved into a `.replaced-<time>` folder on their own volumes.
  Remove those once the restored stack checks out.
- The database is restored in one transaction, so a failed restore leaves it
  as it was.

Run `docker compose run --rm restore` with no name to list the backups it can
see. A token set in `.env` still replaces the restored one on the next start.

Both commands run in the Postgres image, so `pg_dump` always matches the
server's version, and neither runs on `docker compose up`. Their scripts are
`deploy/compose/backup.sh` and `deploy/compose/restore.sh`. Copy the
`backups` folder off the machine to make it a backup in any useful sense.

`pheasant backup` and `pheasant restore` are for the single-container,
SQLite-backed deployment ([Backup & restore](../how-to/backup-restore.md)).
On a Postgres region they refuse and point here, rather than writing an
archive without the database.

## Rotate a token

Set the new value in `.env` and recreate the stack:

```bash
# .env
PHEASANT_API_TOKEN=<openssl rand -hex 32>
```

```bash
docker compose up -d --force-recreate
```

`secrets-init` writes it over the stored one, and every service reads it on
start. The four tokens must stay distinct from one another.

## Stop, restart, reset

```bash
docker compose stop          # stop everything, keep all data
docker compose up -d         # start again
docker compose down          # remove containers, keep volumes (data)
docker compose down -v       # remove containers AND every volume: the index,
                             # memory, uploads and tokens are deleted
```

`down -v` cannot be undone. Back up first if anything in it matters.

## Move from pheasant-lab

pheasant-kb is the stack previously shipped as
`deploy/compose/docker-compose.pheasant-lab.yml`, renamed and made the default.
Two names changed, and both decide which data the stack sees:

- the Compose project (`pheasant-lab` → `pheasant-kb`), which prefixes every
  volume name, and
- `pheasant.name` (`pheasant-lab` → `pheasant-kb`), the knowledge base's id
  that indexed data is stored under.

To keep an existing pheasant-lab index exactly as it is, put this in `.env`:

```bash
COMPOSE_PROJECT_NAME=pheasant-lab
PHEASANT_KB_CONFIG=./pheasant.yaml   # the config generated for the lab
```

The lab's four tokens were set in `.env`, and stay there; `secrets-init` writes
them to the new secrets volume on first start, so agents and browsers holding
the old API token keep working. Then `docker compose up -d` from the repository
root. Starting fresh under the new names instead means a full re-index.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `docker compose up` stops with `required variable OPENAI_API_KEY is missing` | `.env` is missing or not beside `compose.yaml`. Run the command from the repository root. |
| The UI shows **Connect API**, or requests return `401` | Paste the API token: `docker compose exec api cat /run/pheasant/secrets/api-token`. |
| MCP returns `421 Misdirected Request` | The client reached the stack under a hostname missing from `server.api.cors_origins`. Add the origin in your config copy. |
| A service restarts during a large sync | Usually out of memory. `docker inspect --format '{{.State.OOMKilled}}' <container>` confirms it; raise that service's `*_MEMORY` ([Scale](scale.md)). |
| A pull fails with `429 Too Many Requests` | Docker Hub's anonymous limit, on the `postgres` or `nats` image. `docker login`, then `docker compose up -d` again. |
| `range of CPUs is from 0.01 to N` on start | A `*_CPUS` value exceeds the host's cores. Lower it in `.env`. |
| The indexer logs `429` from OpenAI | Your rate limit. Lower `sync.concurrency.max_parallel_embeddings`. |
| A sync stays `awaiting_claim` in `GET /queue` | The indexer is not running or not healthy: `docker compose ps indexer`, then its logs. |
| Answers return `429 ASSISTANT_BUSY` | More concurrent answers than `assistant.latency.max_concurrent_answers`. Retry, or raise it with the api's CPUs. |
| A folder you mounted is refused as a source | It must be mounted under `/data` on the indexer and api ([Configure](configure.md#index-a-folder-on-the-host)). |

More in [Troubleshooting](../troubleshooting.md).

## Measure answer latency

`scripts/benchmark_assistant_latency.py` times completed answers over HTTP,
SSE or MCP against a running stack, and marks a report invalid if the corpus
or configuration changed during the run:

```bash
python scripts/benchmark_assistant_latency.py --base-url http://127.0.0.1:8765 \
  --token-env PHEASANT_API_TOKEN --cases <held-out-cases.json> \
  --concurrency 4 --repeats 10 --transport http --output-dir <report-directory>
```

Keep benchmark questions and reports outside indexed sources; the shipped
`readiness.corpus_denylist` refuses `benchmark/*` and `*.answers.json` for that
reason.
