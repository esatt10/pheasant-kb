# pheasant

pheasant is a **Docker-first, local-first knowledge harness** for you and your
agents. It turns your sources — git repositories, folders, single
files, PDFs and Office documents, images and audio, web collections, SaaS
connectors (Notion, Google Drive, Slack, Confluence, IMAP) and API/S3 — into a
queryable **knowledge graph** with hybrid self-search, served over MCP, HTTP and
a web UI.

!!! tip "Part of the Synapse Suite"
    pheasant is the **region** component of **Synapse**, a federated
    knowledge-base platform. New here, or want the whole-system view? Start at
    the suite front door:
    **[The Synapse Suite →](https://esatt10.github.io/pheasant-flock/)**
    (the [pheasant-flock](https://github.com/esatt10/pheasant-flock)
    router site). This site is the region/KB half.

It runs perfectly well **standalone**: point it at some sources, sync, and query
over MCP or HTTP. It also has a **second role**: each pheasant container can act
as a federated **"brain region"** inside a **Synapse** fleet.

## Two roles, one container

<div class="grid cards" markdown>

-   :material-database-search: **Standalone knowledge base / MCP context server**

    Index your code and docs, search them by text, graph, or vector
    similarity, and expose the result to agents over MCP. Multi-modal: images
    are captioned and audio is transcribed into the same searchable space.

    [:octicons-arrow-right-24: Start with the quickstart](tutorials/quickstart.md)

-   :material-lan-connect: **Federated region in a Synapse fleet**

    Publish a bounded **semantic contract** describing what this region knows.
    A Synapse router scores contracts, routes global queries to the right
    regions, and fans out to each region's self-search.

    [:octicons-arrow-right-24: Attach to a Synapse fleet](how-to/attach-to-synapse.md)

</div>

!!! tip "Standalone behavior is sacred"
    Every Synapse feature is **off by default**. With no `synapse:` block set,
    pheasant behaves exactly like a router-less knowledge base. You never have
    to join a fleet to get full value.

## Pick your path

| You want to… | Go here |
|---|---|
| Stand up a knowledge base in 10 minutes | [Quickstart tutorial](tutorials/quickstart.md) |
| See it in the browser (Docker Compose or CLI) | [Run the web UI](how-to/run-the-ui.md) |
| Index images and audio (offline) | [Multi-modal tutorial](tutorials/multimodal.md) |
| Configure source types and sync modes | [Configure sources](how-to/sources.md) |
| Turn on semantic / vector search | [Vector self-search](how-to/vector-search.md) |
| Tune local workers or add indexing nodes | [Speed up indexing](how-to/indexing-performance.md) |
| Join a federated Synapse fleet | [Attach to a Synapse fleet](how-to/attach-to-synapse.md) |
| Connect an agent over MCP | [MCP for agents](mcp_client.md) |
| Back up and restore region state | [Backup & restore](how-to/backup-restore.md) |
| See every command, route, and tool | [Interface matrix](reference/interfaces.md) |

## The persistence split

pheasant keeps three clearly separated directories. Understanding the split
makes operations (backups, restores, mounts) predictable:

| Directory | Contents | Treat as |
|---|---|---|
| `/state` | SQLite DB, graph JSON + zstd snapshots, manifests, the published contract, the event stream, the vector index | **Operational source of truth — user data.** Back this up. |
| `/exports` | Graph JSON and visualization payloads | Regenerable |

See [Architecture](architecture.md) for the full component map and runtime flow.

## How the two repos relate

Synapse is a two-repo system:

- **pheasant** (this repo) is a **region** — a self-contained knowledge base
  with its own sync engine, graph, and self-search. It publishes a semantic
  contract and answers fan-out queries.
- **[pheasant-flock](https://github.com/esatt10/pheasant-flock)** is
  the **router** — the "nervous system" that scores contracts, routes global
  queries across regions, merges and re-ranks the answers, and discovers
  cross-region relationships ("white matter").

The boundary between them is **contract JSON over HTTP** — there is no Python
dependency between the repos, and a router-less pheasant keeps working unchanged.
For the global, cross-region search experience, see the
[pheasant-flock documentation site](https://github.com/esatt10/pheasant-flock).

## What's shipped

- **Sync engine** with `incremental` / `full` / `validate_only` / `repair` modes,
  crash-safe WAL state, a single-writer lease, and connector checkpoints.
- **Hybrid search** over `text` (SQLite FTS5 + BM25), `graph`, and `vector`
  (optional embeddings) — see [Vector self-search](how-to/vector-search.md).
- **Multi-modal ingest**: image captioning and audio transcription, both with a
  deterministic offline stub default — see the
  [multi-modal tutorial](tutorials/multimodal.md).
- **Backup & restore** of region state (`pheasant backup` / `pheasant restore`)
  plus zstd graph snapshots with retention.
- **Synapse region wiring**: contract publisher, NDJSON event stream + router
  webhook, and optional Ed25519 contract signing.
- **MCP and HTTP** interfaces, plus a React web UI sidecar.
