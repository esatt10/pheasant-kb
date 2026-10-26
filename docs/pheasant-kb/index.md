# Run pheasant-kb

**pheasant-kb** is the standard way to run pheasant: one command starts a
small set of containers that index your sources, answer questions with
citations, and serve the web UI, the HTTP API and an MCP endpoint for agents.
Every other page in this section uses it as the reference: what to change,
what to scale, and how to operate it.

## The one line

You need Docker with Compose v2 (Docker Desktop on macOS and Windows, or Docker
Engine with the compose plugin on Linux) and an OpenAI API key.

=== "macOS / Linux"

    ```bash
    git clone https://github.com/esatt10/pheasant-kb && cd pheasant-kb && echo "OPENAI_API_KEY=sk-..." > .env && docker compose up -d
    ```

=== "Windows (PowerShell)"

    ```powershell
    git clone https://github.com/esatt10/pheasant-kb; cd pheasant-kb; Set-Content .env "OPENAI_API_KEY=sk-..."; docker compose up -d
    ```

That is the whole install. `compose.yaml` at the root of the repository is
pheasant-kb; `.env` beside it holds your key and any setting you change later
([`.env.example`](https://github.com/esatt10/pheasant-kb/blob/main/.env.example)
lists them all, commented, with their defaults).

The first start pulls the images and initializes the database, which takes a
few minutes. `docker compose ps` shows each service becoming `healthy`.

## Open it

1. Go to <http://127.0.0.1:8765>.
2. Print the API token and paste it into **Connect API** at the top right:

    ```bash
    docker compose exec api sh -c 'cat /run/pheasant/secrets/api-token'
    ```

The token was generated on first boot and is kept across restarts. The browser
keeps it for the session only. The `sh -c` is there for Git Bash on Windows,
which rewrites a bare `/run/...` argument into a Windows path.

## Give it something to know

| Content | How |
|---|---|
| Files and documents | Drag them onto the UI's drop zone. They land in the `uploads` source and are indexed by the indexer. |
| A Git repository | **Sources → Add** with its URL. The indexer clones it into the workspace volume and keeps it fetched. Set `GITHUB_TOKEN` in `.env` for private repositories. |
| A web page or site section | **Sources → Add** with its URL. Each page is re-checked on its own schedule. |
| A folder on this machine | Mount it into the stack, then add its container path as a source. See [Index a folder on the host](configure.md#index-a-folder-on-the-host). |
| Notes an agent should remember | MCP `memory_write` or `POST /memory`, into the `agent-memory` source. |

Everything above is also available to agents over MCP (`register_source`,
`submit_documents`, `sync_source`).

## Connect an agent

The MCP endpoint is `http://127.0.0.1:8765/mcp` (streamable HTTP), and every
call carries the API token as a bearer header. For Claude Code:

```bash
claude mcp add --transport http pheasant http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $(docker compose exec -T api sh -c 'cat /run/pheasant/secrets/api-token')"
```

Any other MCP client takes the same URL and `Authorization: Bearer <token>`
header. The tools are listed in [MCP tools and resources](../mcp_tools.md).

## What is running

```text
          browser · agents (MCP) · your apps (HTTP)
                         │  :8765 + API token
                         ▼
  ┌───────┐  graph   ┌───────┐  sync requests (via nats),  ┌─────────┐  files  ┌────────────┐
  │ graph │◄─────────│  api  │────────────────────────────►│ indexer │────────►│ worker  ×4 │
  └───┬───┘  queries └───┬───┘  uploads                    └────┬────┘  chunks └────────────┘
      │ reads            │ reads                                │ writes: the only writer
      ▼                  ▼                                      ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  postgres: state, chunks, graph rows, BM25 index  ·  /state: vectors  │
  └──────────────────────────────────────────────────────────────────────┘

  logger: drains observations from nats into postgres and /exports
```

| Service | Its job | What it holds | Scales on |
|---|---|---|---|
| `api` | Serves the UI, HTTP API and MCP. Publishes syncs and forwards uploads instead of writing. | Result sets and the in-memory vector matrix. No graph. | Request traffic |
| `graph` | Answers graph walks and slices for the api tier over an internal API. | Nothing it cannot re-read. | Graph-query latency |
| `indexer` | The single writer. Claims syncs from the queue, commits chunks, graph rows and vectors, runs enrichment. | The graph working set. | Corpus size, commit rate |
| `worker` ×4 | Parses, extracts and chunks files for the indexer. Long PDFs are split across all of them by page range. | One secret and nothing else. | Ingest backlog |
| `logger` | Drains the observation queue into Postgres and Parquet. | One batch at a time. | Request volume |
| `postgres` | Operational state, chunks, the graph's rows and the BM25 full-text index. | The index. | Corpus size, query load |
| `nats` | The durable index queue and the observation queue. | Queued tasks. | Rarely needs to |
| `secrets-init`, `db-init`, `workspace-init` | One-shot setup on each start: tokens, schema, workspace permissions. They exit when done. | | |

Each part, its signals and its knobs are covered in
[Scale up or down](scale.md).

### Where your data lives

Named volumes, prefixed with the project name (`pheasant-kb_`):

| Volume | Contents | Treat as |
|---|---|---|
| `pheasant-pg17` | The Postgres database: state, chunks, graph rows, full-text index | **User data.** Back it up. |
| `pheasant-state` | LanceDB vectors, graph snapshots, image bytes, the upload landing zone | **User data.** Back it up. |
| `pheasant-memory` | Agent memory records, one Markdown file each | **User data.** |
| `pheasant-workspace` | Clones of repositories added by URL | Re-cloneable |
| `pheasant-exports` | Parquet exports and cold observation partitions | Regenerable |
| `pheasant-nats` | Queue storage | Transient |
| `pheasant-secrets`, `pheasant-worker-secret` | The four generated tokens | Keep; deleting them rotates every token |

## What it is configured to do

The shipped configuration, [`deploy/compose/pheasant-kb.yaml`](https://github.com/esatt10/pheasant-kb/blob/main/deploy/compose/pheasant-kb.yaml),
is pheasant's default configuration: a key any config leaves unset takes its
value. It turns on the full product:

- **Hybrid retrieval**: BM25 full-text (Postgres `pg_textsearch`), vectors
  (`text-embedding-3-small` in LanceDB, held in memory, with an HNSW index) and
  the graph, fused by reciprocal rank.
- **Grounded answers** from `gpt-6-luna` with an agentic loop that judges
  evidence before writing, bounded to four concurrent answers.
- **Ingestion** of code, documents (PDF, Word, PowerPoint, Excel and more),
  images (captioned) and audio (transcribed), chunked per file by its structure,
  with a heading taxonomy.
- **Durable agent memory**, the interaction ledger (seven days), and the
  evaluation, tuning and readiness planes, available for manual runs.
- **Access control**: every request needs the API token, results respect ACLs,
  and the port is bound to loopback.

[Configure pheasant-kb](configure.md) explains each part and how to change it.

## Next

- [Configure pheasant-kb](configure.md): models, sources, features, host folders.
- [Scale up or down](scale.md): memory, CPU, storage, the vector store, workers,
  the indexer.
- [Operate it](operate.md): status, upgrades, backups, resets, troubleshooting.
- Using it: [the web UI](../how-to/chat-and-ui.md),
  [answers and visuals](../how-to/conversations-and-visuals.md),
  [agent memory](../how-to/agent-memory.md).

Want something smaller, such as one container with no key at all, or no
Docker? See [Other ways to run pheasant](../other-deployments.md).
