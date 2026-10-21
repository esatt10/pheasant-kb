# Other ways to run pheasant

[pheasant-kb](pheasant-kb/index.md) is the standard deployment and the one the
documentation uses as its reference. The same code also runs in smaller and
larger shapes. They differ in what they turn on, not in what they are: each
one is pheasant-kb with some services folded together or spread further
apart.

| Shape | Use it when | Start here |
|---|---|---|
| **Single container** | You want the smallest footprint: no database, no broker, no API key. A laptop, a demo, a CI job, or a corpus under ~25,000 files. | [Below](#single-container) |
| **No Docker** (`pip`) | You want pheasant on the host, for development or a stdio MCP server. | [Below](#no-docker) |
| **Local profiles** | One container with more turned on (LanceDB, OpenAI, agentic answers), still SQLite. | [Below](#local-profiles) |
| **Kubernetes** | Each service on its own nodes, several api replicas behind a load balancer, or managed Postgres. | [Below](#kubernetes) |
| **Sharded** | One indexer cannot keep up with the corpus. Several knowledge bases, each a pheasant-kb. | [Capacity planning](how-to/capacity-planning.md#when-you-cross-the-line-shard) |
| **Synapse region** | Several knowledge bases answering one federated query. | [Attach to a Synapse fleet](how-to/attach-to-synapse.md) |

## How they relate to pheasant-kb

| pheasant-kb service | Single container | Kubernetes (`deploy/kubernetes/scaled/`) |
|---|---|---|
| `api`, `graph`, `indexer`, `logger` | One process, `serve --role all` | One Deployment or StatefulSet each |
| `worker` | Threads inside that process | A Deployment, scaled on queue depth |
| `postgres` | SQLite in `/state` | A Postgres 17 you provide, with `pg_textsearch` |
| `nats` | An in-process queue, or none | NATS JetStream |
| Vectors, graph files | `/state` | A shared volume (RWX) |

The single-container defaults are the code's own defaults. Every setting
pheasant-kb changes is something you can switch on in a smaller shape one at a
time, through `pheasant setup` or the [configuration reference](configuration.md).

## Single container

```bash
docker run -p 127.0.0.1:8765:8765 \
  -v "$PWD:/workspace:ro" \
  -v pheasant-state:/state \
  ghcr.io/esatt10/pheasant
```

It writes its own configuration, indexes `/workspace`, and serves the UI, API
and MCP at <http://127.0.0.1:8765>. With no key, search is full-text and graph,
and answers are extractive. Pass `-e OPENAI_API_KEY` for written answers, and
turn on `search.embeddings` for vector search. The Compose equivalent is `deploy/compose/docker-compose.yml`.

- [Quickstart tutorial](tutorials/quickstart.md), step by step
- [Run the web UI](how-to/run-the-ui.md), including `pheasant host` and the
  fresh-reset manifest
- [Deployment](deployment.md): mounts, paths and image options

## No Docker

```bash
pip install pheasant-kb
pheasant up .          # detect, configure, index and serve the current folder
pheasant setup         # or the interactive wizard, reading the live schema
```

[Set pheasant up](how-to/setup.md) covers the wizard, and
[Attach to a coding agent](how-to/attach-to-coding-agent.md) the stdio MCP
server.

## Local profiles

Generated configs under [`deploy/compose/`](https://github.com/esatt10/pheasant-kb/tree/main/deploy/compose),
each from an answer file in `deploy/compose/answers/`:

| Profile | What it is |
|---|---|
| `local-small.yaml` | SQLite, full-text search and extractive answers, MCP and memory. Offline. Run with `pheasant start -c`. |
| `local-advanced.yaml` | One container with LanceDB, OpenAI embeddings, WASM acceleration and the agentic workflow. `docker-compose.advanced.yml` runs it. |
| `fleet.yaml` | The generic version of pheasant-kb's topology, run by `docker-compose.scale.yml`. The CI fleet tests use it; pheasant-kb is the tuned version of the same shape. |

Commands for each are in the
[`deploy/compose` README](https://github.com/esatt10/pheasant-kb/blob/main/deploy/compose/README.md).

## Kubernetes

`deploy/kubernetes/` holds a single-pod manifest and, under `scaled/`, the
pheasant-kb services as separate workloads; `deploy/helm/` is a chart for the
single pod. See [Deployment](deployment.md),
[Running a worker fleet](how-to/worker-fleet.md) and
[Separate graph-query service](how-to/graph-query-service.md).
