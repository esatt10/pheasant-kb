# Deployment

pheasant runs in one shape: a **region** of separate tiers over PostgreSQL and
NATS JetStream, built from one image. Locally that region is **pheasant-kb**,
`compose.yaml` at the repository root. In production it is the same tiers on
Azure Container Apps.

| Tier | Command | Job |
|---|---|---|
| `api` | `serve --role api` | The UI, HTTP API and MCP endpoint on `:8765`. Publishes syncs to the queue; never indexes. Scales out. |
| `graph` | `serve --role graph` | Answers graph walks and slices for the api tier from the graph's rows. |
| `indexer` | `serve --role indexer` | The one writer per region: drains the index queue, commits chunks, graph rows and vectors. A second indexer is a hot standby. |
| `worker` | `worker --transport grpc` | Parses and chunks files for the indexer. Holds no database credential, no model key and no source list. Scales on queue depth. |
| `logger` | `serve --role logger` | Drains the observation queue into the interaction ledger, rolls it to Parquet. |
| `postgres` | `deploy/postgres` | PostgreSQL 17 with pg_textsearch preloaded: all state, the full-text index and the graph's rows. |
| `nats` | `nats:2.10-alpine -js` | JetStream: the index and log work queues, and graph-commit announcements. |

The roles' invariants are startup refusals, not conventions: an `api` without a
queue, a non-`all` role on a routable bind with no token, a worker holding a
DSN or a model key, and the HTTP worker app (`serve --role worker`) all refuse
to start. See [Security](security.md).

## Locally: pheasant-kb

```bash
echo "OPENAI_API_KEY=sk-..." > .env && docker compose up -d
```

[Run pheasant-kb](pheasant-kb/index.md) is the guide: what each service does,
where data lives, [configuring it](pheasant-kb/configure.md),
[scaling it](pheasant-kb/scale.md) from `.env`, and
[operating it](pheasant-kb/operate.md) (upgrades, backups, token rotation).

## Production: Azure Container Apps

A production region runs the same image and the same tiers as Container Apps
in one environment:

- each tier is its own app, scaled independently (the api tier on requests,
  the worker tier on queue depth, the indexer and graph tiers at one replica,
  everything able to scale to zero);
- PostgreSQL and NATS run as internal apps, with Postgres data and `/state` on
  durable file storage;
- tokens, the database password and the credential key live in Key Vault and
  reach each tier as secrets, the worker tier receiving only its own token;
- model calls go to Azure AI Foundry with the region's managed identity
  ([Use Azure AI Foundry](pheasant-kb/foundry.md));
- a region that scales to zero is woken by its first request, and
  `python -m pheasant.deployment.wake_gate` holds the api's port while the
  database wakes, answering `503 REGION_WAKING` with `Retry-After` rather than
  leaving a caller to time out.

Startup commands that need the database call
`python -m pheasant.deployment.database wait` (and `ensure` for a new slot's
database), so a deployment never depends on which driver the image ships.
Hosting several knowledge bases for other people — delegated access, the model
catalog, usage and spend limits, the fleet directory of slots — is in
[Host a region for someone else](how-to/hosted-regions.md).

## The generic fleet file

`deploy/compose/docker-compose.scale.yml` with `deploy/compose/fleet.yaml` is
the same topology with generic sizing. CI's fleet smoke tests run it:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.scale.yml up -d \
  --scale indexer=1 --scale worker=4
```

`deploy/compose/ci/` holds two smaller topologies CI also runs: the evaluation
plane (`docker-compose.evaluation.yml`) and the log tier
(`docker-compose.log-tier.yml`). Do not grow a third copy of the full topology.

## When one indexer is the ceiling: shard

One indexer is the sole commit authority for a region, so more workers stop
helping once `pheasant_commit_authority_saturation` stays above 0.8. Past that
point the corpus is split across regions, each a whole pheasant-kb:

```bash
pheasant scan                     # projected RAM, disk and time, and the warning
pheasant shard plan --emit out/   # which sources go where, plus each region's config
```

A region is a separate knowledge base: a query is answered within one region,
never across them. See [Capacity planning](how-to/capacity-planning.md).

## MCP server inside a container

Agents reach MCP over HTTP at `/mcp` with the api token
([Attach a coding agent](how-to/attach-to-coding-agent.md)). For a stdio
client, run the server inside the api container:

```bash
docker compose exec -T api python -m pheasant mcp --transport stdio
```

`pheasant client-config vscode|cursor|claude-code` writes the client
configuration for either transport.

## Probes and ports

| | |
|---|---|
| API, UI and MCP | `8765` |
| Worker (gRPC) | `8766`, reachable only from the indexer |
| Liveness | `GET /health` (no I/O) |
| Readiness | `GET /ready` (publishes the loaded and published graph generations) |
| Metrics | `GET /metrics` (Prometheus) |
| Wake state | `GET /region/status` |

A `SIGTERM` fails readiness first and keeps serving on a timer thread while
in-flight requests drain.

## Storage

| Where | What | Notes |
|---|---|---|
| PostgreSQL | Artifacts, chunks, the full-text index, graph rows, leases, receipts, usage, evaluation and tuning rows | User data. Back it up with the rest. |
| `/state` | Vectors (LanceDB), graph history snapshots, manifests, media, uploads, artifact records | User data. Every serving role mounts it read-only; the indexer writes it. |
| `/exports` | Parquet exports, cold interaction partitions, tuning payloads | Regenerable. Every api replica mounts it read-write for export jobs. |
| NATS (`/data`) | Work queues | Tasks are removed when acknowledged; dead letters are kept for `pheasant queue requeue-dead`. |

`docker compose run --rm backup` copies the database, `/state`, the artifact
records and the tokens as one checksummed folder; `restore <name>` brings it
back ([Backup and restore](how-to/backup-restore.md)). Schema changes ship a
one-shot idempotent migration that preserves originals.

## Images and releases

- `.github/workflows/ci.yml` runs lint, formatting, version alignment, the
  test suite against `deploy/postgres` and a JetStream broker on Python 3.11
  and 3.12, the browser tests, the package build, Compose validation and the
  image build with smoke tests.
- `.github/workflows/container.yml` publishes after CI passes on `main`:
  `ghcr.io/esatt10/pheasant:<version>` and `:latest`, and the database image
  `ghcr.io/esatt10/pheasant-postgres:<tag>`. It resolves the release increment
  from every commit since the last `chore: release`, pushes the images first,
  and only then commits the new version into `compose.yaml` and the other
  files that pin it. An image published without its commit is skipped by the
  next release, which increments from the higher of `pyproject.toml` and the
  highest published tag.
- `python scripts/sync_version.py --check` fails when a file that pins the
  image has drifted; `--list-paths` prints every file the release rewrites.
  A new file that pins `ghcr.io/esatt10/pheasant` is added to `replacements()`
  in the same change, or `tests/test_version_alignment.py` fails.
