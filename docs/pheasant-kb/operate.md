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
`compose.override.yaml` and your own `pheasant.yaml` copy are not touched. If
you keep a config copy, compare it with the new
`deploy/compose/pheasant-kb.yaml` for settings you may want to adopt.

## Back up and restore

Your data is in two volumes that must be copied together: the Postgres database
and `/state`, where the vectors and graph snapshots live. Stop the indexer
first, so neither moves during the copy:

```bash
mkdir -p backups
docker compose stop indexer
docker compose exec -T postgres pg_dump -U pheasant -Fc pheasant > backups/pheasant.pgdump
docker compose run --rm --no-deps --user 0 -v "$PWD/backups:/backups" \
  --entrypoint tar indexer -czf /backups/state.tar.gz -C /state .
docker compose start indexer
```

Add the `pheasant-memory` volume the same way if agents write memory
(`-C /memory`), and `pheasant-secrets` if you want the tokens to survive a
rebuild. To restore into an empty stack:

```bash
docker compose up -d postgres
docker compose exec -T postgres pg_restore -U pheasant -d pheasant --clean --if-exists < backups/pheasant.pgdump
docker compose run --rm --no-deps --user 0 -v "$PWD/backups:/backups" \
  --entrypoint tar indexer -xzf /backups/state.tar.gz -C /state
docker compose up -d
```

`pheasant backup` and `pheasant restore` cover the single-container,
SQLite-backed deployment; see [Backup & restore](../how-to/backup-restore.md).

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
