# Quickstart: stand up a knowledge base in 10 minutes

!!! tip "pheasant-kb is the default deployment"
    This tutorial builds the smallest shape, one process with no key, to show
    how pheasant works. To run the full product, use pheasant-kb instead.
    Start with [Run pheasant-kb](../pheasant-kb/index.md).

By the end of this tutorial you will have a running standalone pheasant instance
that has indexed a folder of files and that you can query over HTTP and MCP. No
fleet, no API keys, no model downloads — everything here runs offline.

## Prerequisites

- Python 3.11+ (or Docker, if you prefer the container path below)
- A clone of this repository, or the `pheasant` package installed:

```bash
pip install -e ".[dev,mcp]"
```

This installs the `pheasant` CLI. Confirm it:

```bash
pheasant --help
```

??? note "Prefer Docker?"
    Every command below has a container equivalent. The fastest path is:

    ```bash
    pheasant init --profile quickstart --output pheasant.yaml
    pheasant compose-env pheasant.yaml --output .pheasant/compose.env
    docker compose --env-file .pheasant/compose.env up -d
    ```

    Then jump to [step 5 (health check)](#5-health-check). See
    [Deployment](../deployment.md) for mounts and image options.

## 1. Generate a starter config

```bash
pheasant init --profile quickstart --output pheasant.yaml
```

Expected output:

```text
Wrote pheasant.yaml (profile: quickstart)
```

This writes a `pheasant.yaml` pre-populated from the `quickstart` profile (local
defaults, API + MCP enabled). Open it — you'll see `sources:`, `search:`
and `sync:` blocks.

## 2. Point a source at a folder

Create a small folder to index and drop a couple of files in it:

```bash
mkdir -p ./kb-demo
printf '# Project Atlas\n\nAtlas is the billing service. Owner: payments team.\n' > ./kb-demo/atlas.md
printf 'def charge(amount):\n    """Charge a customer."""\n    return amount\n'   > ./kb-demo/billing.py
```

Edit the `sources:` list in `pheasant.yaml` so it contains a single
`markdown_folder`/`repository`-style source pointing at `./kb-demo`. A minimal
source entry looks like this:

```yaml
sources:
  - name: kb-demo
    type: repository
    path: ./kb-demo
    enabled: true
    include:
      - "**/*.md"
      - "**/*.py"
    chunking:
      enabled: true
      strategy: semantic
      max_chars: 4000
      overlap_chars: 400
    sync:
      on_startup: true
```

!!! note "Paths are validated against an allowlist"
    pheasant only reads paths under its configured workspace roots
    (`security.allow_workspace_roots`). In a local run, point `path` at a folder
    you own. In Docker, mount it under `/workspace`. See
    [Configure sources](../how-to/sources.md).

## 3. Validate the config

```bash
pheasant validate pheasant.yaml
```

Expected output (a passing run ends without errors):

```text
Config valid: pheasant.yaml
```

Then check the runtime environment (paths writable, sources reachable):

```bash
pheasant doctor --config pheasant.yaml
```

`doctor` prints a checklist of environment checks. If a path can't be read, fix
the `path` or your `security.allow_workspace_roots` and re-run.

## 4. Sync (index the source)

```bash
pheasant sync --config pheasant.yaml --source kb-demo --mode incremental
```

Expected output (counts will vary with your files):

```text
Sync kb-demo (incremental): 2 artifacts, 3 chunks indexed, 0 skipped
```

Re-running the **same** command is idempotent — unchanged files are skipped by
content `sha256`:

```text
Sync kb-demo (incremental): 0 artifacts, 0 chunks indexed, 2 skipped
```

??? info "Sync modes"
    | Mode | Behavior |
    |---|---|
    | `incremental` | Skips unchanged artifacts via checkpoints + hashes (default) |
    | `full` | Rebuilds artifact/chunk/graph/manifest/checkpoint state |
    | `validate_only` | Checks readability without writing index artifacts |
    | `repair` | Rebuilds missing or invalid state from manifests + DB |

## 5. Health check

Start the server (HTTP API + MCP on `:8765`):

```bash
pheasant start --config pheasant.yaml
```

In another terminal:

```bash
curl http://localhost:8765/health
curl http://localhost:8765/ready
```

Expected:

```json
{"status": "ok"}
```

```json
{"status": "ready"}
```

## 6. Search it

Search runs over the **HTTP API** (`POST /search`) or **MCP**
(`search_context`). There is no `pheasant search` CLI subcommand — search is a
runtime operation against the running server.

```bash
curl -X POST http://localhost:8765/search \
  -H "content-type: application/json" \
  -d '{"query": "billing owner", "mode": "hybrid", "max_results": 5}'
```

Expected (shape; your IDs and scores will differ):

```json
{
  "query": "billing owner",
  "mode": "hybrid",
  "results": [
    {"node_id": "chunk:kb-demo:atlas.md:0", "score": 0.91, "snippet": "Atlas is the billing service. Owner: payments team."}
  ]
}
```

`mode` accepts `text`, `graph`, `hybrid` (default), and `vector` (once
[vector search](../how-to/vector-search.md) is enabled).

For agents, connect over MCP and call `search_context` instead — see
[MCP for agents](../mcp_client.md).

## Validation checklist

You're done when all of these are true:

- [x] `pheasant validate pheasant.yaml` exits without errors.
- [x] `pheasant doctor` reports a writable `/state` (or local state path) and a
      reachable source.
- [x] `pheasant sync ... --source kb-demo` reports artifacts/chunks indexed.
- [x] Re-running the same sync reports artifacts **skipped** (idempotency).
- [x] `GET /health` returns `{"status": "ok"}` and `GET /ready` returns ready.
- [x] `POST /search` returns at least one result for a query you know is in your
      files.

## Where to next

- **See it in a browser** — the graph, chat and source panes:
  [Run the web UI](../how-to/run-the-ui.md).
- Index **images and audio** (offline): [Multi-modal tutorial](multimodal.md).
- Turn on **semantic search**: [Vector self-search](../how-to/vector-search.md).
- Join a **Synapse fleet**: [Attach to a Synapse fleet](../how-to/attach-to-synapse.md).
- Wire up an **agent**: [MCP for agents](../mcp_client.md).
