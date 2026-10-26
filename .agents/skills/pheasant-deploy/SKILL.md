---
name: pheasant-deploy
description: Configure, deploy, validate, troubleshoot, or scale Pheasant, starting from pheasant-kb (the default stack, `compose.yaml` at the repository root) or, when asked for something smaller, a blank canvas or the small, advanced and fleet presets. Use when an IDE agent is asked to set up Pheasant, select or modify a deployment profile, configure Docker Compose, enable MCP/vector/agentic/memory/PostgreSQL/NATS/gRPC/WASM features, connect an agent, verify repository URL synchronization, or diagnose startup and indexing health.
---

# Deploy Pheasant

Treat configuration as generated data and deployment state as user data.

## Establish context

1. Read `AGENTS.md`, then `CLAUDE.md`.
2. Read `docs/pheasant-kb/index.md`: pheasant-kb is the default deployment
   and the reference every other guide is written against.
3. Read `docs/how-to/setup.md` for configuration work and
   `deploy/compose/README.md` for the other Docker profiles.
4. Read only the task-specific guide afterward:
   - sources or repository URLs: `docs/how-to/sources.md`
   - MCP attachment: `docs/how-to/attach-to-coding-agent.md`
   - indexing progress: `docs/how-to/monitor-indexing.md`
   - configuring pheasant-kb: `docs/pheasant-kb/configure.md`
   - scaling: `docs/pheasant-kb/scale.md`, then `docs/how-to/worker-fleet.md`
     and `docs/how-to/capacity-planning.md`
   - upgrades, backups, resets: `docs/pheasant-kb/operate.md`
   - failures: `docs/troubleshooting.md`

Inspect the current worktree and running containers before changing either.
Preserve unrelated local changes and existing volumes.

## Choose one route

### pheasant-kb (default)

Use it unless the user asks for something smaller, has no OpenAI key, or
cannot run several containers:

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
stopping the pheasant services; never `pheasant backup` on this stack, which
refuses a Postgres region.

An existing stack named `pheasant-lab` holds its data in `pheasant-lab_*`
volumes under knowledge base `pheasant-lab`. Keep it with
`COMPOSE_PROJECT_NAME=pheasant-lab` and its own config in `.env`; never start
it under the new names without saying that means a full re-index.

### Blank canvas

Prefer the one-container Compose manifest when the user wants Docker:

```bash
cp deploy/compose/.env.example .env
docker compose --env-file .env -f deploy/compose/docker-compose.yml config --quiet
docker compose --env-file .env -f deploy/compose/docker-compose.yml up -d --build
```

Prefer the CLI outside Docker when the user wants a purely local install:

```bash
pheasant setup
pheasant doctor -c pheasant.yaml
pheasant start -c pheasant.yaml
```

Never hand-write `pheasant.yaml`. Use `pheasant setup`, `--answers`, or a
documented API/config surface so the live schema validates every key.

pheasant's defaults are pheasant-kb's (Postgres, NATS, workers, OpenAI). Any
config for a smaller shape must say `pheasant.defaults: standalone` (or run
with `PHEASANT_DEFAULTS=standalone`); one that names no table becomes a
pheasant-kb config and refuses to start without Postgres. `pheasant up` and
the image's first-boot config already write standalone.

### Preset

Select the smallest adequate preset:

- `deploy/compose/local-small.yaml`: SQLite, offline text search, MCP and
  durable memory; run directly with `pheasant start -c`.
- `deploy/compose/docker-compose.advanced.yml`: one Docker node using
  `local-advanced.yaml`, SQLite, LanceDB, WASM, OpenAI embeddings and agentic
  retrieval.
- `deploy/compose/docker-compose.scale.yml`: PostgreSQL, NATS JetStream,
  LanceDB and gRPC preparation workers using `fleet.yaml` and `worker.yaml`.

For advanced or fleet deployments, require `OPENAI_API_KEY` in `.env`. The
generic fleet additionally requires a real `POSTGRES_PASSWORD` and four
*distinct* long random values — `PHEASANT_API_TOKEN` (callers to the API),
`PHEASANT_GRAPH_SERVICE_TOKEN` (API replicas to the internal graph API),
`PHEASANT_INDEX_WORKER_TOKEN` (the indexer to the preparation workers) and
`PHEASANT_INGESTION_SERVICE_TOKEN` (API replicas to the indexer's landing
service). Never
reuse one value across two of them: workers are the least-trusted tier and
hold the third by necessity, and a serving process refuses to start when the
graph and worker tokens match. Never print or place secret values in YAML,
logs, commits or responses.

Start the advanced preset:

```bash
docker compose --env-file .env \
  -f deploy/compose/docker-compose.advanced.yml config --quiet
docker compose --env-file .env \
  -f deploy/compose/docker-compose.advanced.yml up -d --build
```

Start the generic fleet and scale the CPU preparation path:

```bash
docker compose --env-file .env \
  -f deploy/compose/docker-compose.scale.yml config --quiet
docker compose --env-file .env \
  -f deploy/compose/docker-compose.scale.yml up -d --build \
  --scale indexer=1 --scale worker=8
```

Scale gRPC workers for parsing/chunking. Extra indexers are hot standbys, not
throughput: one indexer is the commit authority per knowledge base, and past
its ceiling the answer is a shard. Do not claim that workers accelerate OpenAI embeddings; the
provider's RPM/TPM quota bounds that phase.

## Configure a custom preset

1. Copy the nearest JSON answer file under `deploy/compose/answers/`.
2. Change dotted schema answers and source definitions; keep only environment
   variable names for secrets.
3. Generate YAML with `pheasant setup --answers ... --accept-defaults --force`.
4. Run `pheasant doctor -c <generated-yaml> --no-require-paths` before Compose.
5. Keep the answer JSON and generated YAML together in the change.

For managed repository URLs, retain `repo.clone_url`, `clone_path` and
`clone_ref`. Give the coordinator/indexer a writable `/workspace`; workers do
not need that mount. Confirm remote, checkout and indexed commit evidence from
the source status after synchronization.

For durable memory, configure exactly one writable `type: memory` source and a
persistent `/memory` mount shared by the API and indexer. Memory is written
explicitly through MCP `memory_write` or `POST /memory`; ordinary chat is not
silently recorded.

## Validate and hand off

After starting a profile:

```bash
docker compose --env-file .env -f <manifest> ps
curl -fsS http://127.0.0.1:8765/health
curl -fsS http://127.0.0.1:8765/ready
curl -fsS http://127.0.0.1:8765/jobs
```

Inspect service logs when readiness, queue depth or indexing progress stalls.
Report the selected profile (pheasant-kb unless another was chosen), config path, Compose path, UI URL
`http://127.0.0.1:8765`, MCP URL `http://127.0.0.1:8765/mcp`, required unset
environment variable names, health state and any remaining validation failure.

Never run `deploy/compose/docker-compose.fresh.yml` or `docker compose down -v`, remove a named volume, or replace an
existing state directory unless the user explicitly requests a reset and the
exact targets have been inspected first.
