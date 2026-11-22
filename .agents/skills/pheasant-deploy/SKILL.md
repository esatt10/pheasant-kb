---
name: pheasant-deploy
description: Configure, deploy, validate, troubleshoot, or scale Pheasant, starting from pheasant-kb (the default stack, `compose.yaml` at the repository root), or the generic fleet preset and a one-process development region when asked for something else. Use when an IDE agent is asked to set up Pheasant, modify a deployment profile, configure Docker Compose, enable MCP/vector/agentic/records/gRPC/WASM features, connect an agent, verify repository URL synchronization, or diagnose startup and indexing health.
---

# Deploy Pheasant

Treat configuration as generated data and deployment state as user data.

## Establish context

1. Read `AGENTS.md`, then `CLAUDE.md`.
2. Read `docs/pheasant-kb/index.md`: pheasant-kb is the default deployment
   and the reference every other guide is written against.
3. Read `docs/how-to/setup.md` for configuration work and
   `deploy/compose/README.md` for what each file under `deploy/compose/` is.
4. Read only the task-specific guide afterward:
   - sources or repository URLs: `docs/how-to/sources.md`
   - MCP attachment: `docs/mcp_client.md`
   - indexing progress: `docs/how-to/monitor-indexing.md`
   - configuring pheasant-kb: `docs/pheasant-kb/configure.md`
   - scaling: `docs/pheasant-kb/scale.md`, then `docs/how-to/worker-fleet.md`
     and `docs/how-to/capacity-planning.md`
   - upgrades, backups, resets: `docs/pheasant-kb/operate.md`
   - failures: `docs/troubleshooting.md`

Inspect the current worktree and running containers before changing either.
Preserve unrelated local changes and existing volumes.

Every route runs PostgreSQL (the only state store, the `deploy/postgres`
image with pg_textsearch preloaded). Every route with a queue runs NATS
JetStream (the only queue).

## Choose one route

### pheasant-kb (default)

Use it unless the user asks for something else:

```bash
test -f .env || cp .env.example .env    # then set OPENAI_API_KEY
docker compose config --quiet
docker compose up -d
```

It needs only `OPENAI_API_KEY`: `secrets-init` generates the four distinct
tokens into volumes on first boot and keeps them. The API token for the UI and
MCP clients is read with
`docker compose exec api sh -c 'cat /run/pheasant/secrets/api-token'`
(the `sh -c` keeps Git Bash on Windows from rewriting the path); never print
it into a commit or a shared log. Size every service through the variables in
the root `.env.example` (`PHEASANT_WORKERS`, `PHEASANT_INDEXER_MEMORY`, ...)
rather than by editing `compose.yaml`. To change behaviour, copy
`deploy/compose/pheasant-kb.yaml`, set `PHEASANT_KB_CONFIG` to the copy and
recreate the pheasant services. Host folders mount under `/data` through a
gitignored `compose.override.yaml`. Back up with
`docker compose run --rm backup` (stop the indexer first for an exact copy)
and restore with `docker compose run --rm restore <name> --force` after
stopping the pheasant services.

An existing stack named `pheasant-lab` holds its data in `pheasant-lab_*`
volumes under knowledge base `pheasant-lab`. Keep it with
`COMPOSE_PROJECT_NAME=pheasant-lab` and its own config in `.env`; never start
it under the new names without saying that means a full re-index.

### Preset

The generic fleet is the same topology as pheasant-kb, with its own config
(`deploy/compose/fleet.yaml`, workers on `worker.yaml`), and is what CI's
smoke tests run: `deploy/compose/docker-compose.scale.yml`. Use it when a task
asks for that file specifically; otherwise pheasant-kb is the better start.

It requires `OPENAI_API_KEY`, a real `POSTGRES_PASSWORD` and four *distinct*
long random values in `.env` — `PHEASANT_API_TOKEN` (callers to the API),
`PHEASANT_GRAPH_SERVICE_TOKEN` (API replicas to the internal graph API),
`PHEASANT_INDEX_WORKER_TOKEN` (the indexer to the preparation workers) and
`PHEASANT_INGESTION_SERVICE_TOKEN` (API replicas to the indexer's landing
service). Never reuse one value across two of them: workers are the
least-trusted tier and hold the third by necessity, and a serving process
refuses to start when the graph and worker tokens match. Never print or place
secret values in YAML, logs, commits or responses.

```bash
cp deploy/compose/.env.example .env
docker compose --env-file .env \
  -f deploy/compose/docker-compose.scale.yml config --quiet
docker compose --env-file .env \
  -f deploy/compose/docker-compose.scale.yml up -d --build \
  --scale indexer=1 --scale worker=8
```

Scale gRPC workers for parsing/chunking. Extra indexers are hot standbys, not
throughput: one indexer is the commit authority per knowledge base, and past
its ceiling the answer is a shard (`pheasant shard plan --emit`). Do not claim
that workers accelerate OpenAI embeddings; the provider's RPM/TPM quota bounds
that phase.

### Blank canvas

A one-process region (`--role all`, no queue, offline stub models) for
development outside Docker. It still needs PostgreSQL:

```bash
docker run -d --name pheasant-pg -p 127.0.0.1:5432:5432 \
  -e POSTGRES_USER=pheasant -e POSTGRES_PASSWORD=<choose one> \
  ghcr.io/esatt10/pheasant-postgres:$(cat deploy/postgres/TAG)
export PHEASANT_DATABASE_URL=postgresql://pheasant:<password>@127.0.0.1:5432/pheasant
pheasant setup                    # answer on the standalone table
pheasant doctor -c pheasant.yaml
pheasant start -c pheasant.yaml
```

Never hand-write `pheasant.yaml`. Use `pheasant setup`, `--answers`, or a
documented API/config surface so the live schema validates every key.

pheasant's defaults are pheasant-kb's (the queue, workers, graph service,
OpenAI). A config for a one-process region must say
`pheasant.defaults: standalone` (or run with `PHEASANT_DEFAULTS=standalone`);
one that names no table becomes a pheasant-kb config and refuses to start
without NATS and the other tiers.

## Configure a custom preset

1. Copy the nearest JSON answer file under `deploy/compose/answers/`.
2. Change dotted schema answers and source definitions; keep only environment
   variable names for secrets.
3. Generate YAML with `pheasant setup --answers ... --accept-defaults --force`.
4. Run `pheasant doctor -c <generated-yaml> --no-require-paths` before Compose.
5. Keep the answer JSON and generated YAML together in the change.

For managed repository URLs, retain `repo.clone_url`, `clone_path` and
`clone_ref`. Give the indexer a writable `/workspace`; workers do not need
that mount. Confirm remote, checkout and indexed commit evidence from the
source status after synchronization.

Durable typed records (facts, aliases, preferences, exclusions) are written
explicitly through MCP `record_artifact` or `POST /artifacts/records`; the first
write creates the `artifact-records` source under `/state`. Ordinary chat is not
silently recorded.

## Validate and hand off

After starting a profile (send the API token on every call but `/health`):

```bash
docker compose --env-file .env -f <manifest> ps
curl -fsS http://127.0.0.1:8765/health
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/ready
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/jobs
```

Inspect service logs when readiness, queue depth (`GET /queue`) or indexing
progress stalls. Report the selected profile (pheasant-kb unless another was
chosen), config path, Compose path, UI URL `http://127.0.0.1:8765`, MCP URL
`http://127.0.0.1:8765/mcp`, required unset environment variable names,
health state and any remaining validation failure.

Never run `docker compose down -v`, remove a named volume, or replace an
existing state directory unless the user explicitly requests a reset and the
exact targets have been inspected first.
