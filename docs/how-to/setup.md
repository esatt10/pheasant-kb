# Set pheasant up

There are three ways in, and none of them require you to write YAML by hand.

## 1. One line, no config at all

```bash
docker run -p 8765:8765 \
  -v "$PWD:/workspace:ro" \
  -v pheasant-state:/state \
  ghcr.io/esatt10/pheasant
```

The image is **universal**: every optional feature is installed (semantic
search, the agentic answer loop, sandboxed connectors, signed contracts), and
it serves the HTTP API, the MCP server and the web UI from the same container
on the same port. Open <http://localhost:8765>.

With no config file mounted, the entrypoint generates one on first boot — from
the same defaults `pheasant setup --accept-defaults` uses — and indexes
whatever you mounted at `/workspace`. Mount your own config at
`/config/pheasant.yaml` and it is used untouched; the entrypoint only ever
writes when the file is absent.

Outside Docker:

```bash
pip install -e ".[mcp]"
pheasant up ~/notes            # detect, configure, index, serve
```

## 2. The interactive wizard

```bash
pheasant setup
```

A sectioned interview that explains each area before asking about it. Every
question has a working default, so pressing Enter the whole way through is a
supported path — you get a valid config and can change your mind later from
the UI.

| Flag | What it does |
|---|---|
| `--advanced` | Ask about every option, not just the ones that usually matter |
| `--accept-defaults` | Ask nothing; write a config of defaults (scriptable) |
| `--plain` | Disable color and terminal control sequences (useful for logs and narrow terminals) |
| `--answers FILE` | Answer from a JSON file of `{"dotted.config.key": value}` |
| `--target local\|docker\|compose` | Which startup commands to print at the end |
| `-o PATH` | Where to write the config (default `pheasant.yaml`) |

It covers: knowledge-base identity, storage paths, sources, search,
embeddings, the assistant, retrieval tuning, sync and scheduling, the graph,
image/audio ingestion, server and MCP, security, agent memory, and
Synapse federation.

### Secrets

The wizard never puts a secret in `pheasant.yaml`. It writes the **name** of an
environment variable into the config and the **value** into `.env` with mode
`0600`:

```yaml
search:
  embeddings:
    api_key_env: OPENAI_API_KEY   # the name — this is all the config knows
```

An existing `.env` is merged, not overwritten: comments, ordering and unrelated
keys survive. Inside a git working tree the wizard also checks `.gitignore`
covers the file and adds it if not. A variable already set in your environment
is not asked about at all.

Interrupt it (`Ctrl-C`) and answers so far are checkpointed to
`.pheasant-setup.json`; re-run to resume. Secrets are deliberately **not**
checkpointed — the whole point of the `0600` file is that the key exists in
exactly one place.

### Responsive terminal flow

The guided interview uses the available terminal width (up to 100 columns),
never clears the screen, and is grouped into six chapters: Foundation; Sources
& content; Search & graph; Assistant & memory; Operations & security; and
Federation. On an interactive terminal a redacted review screen lets you write
files, revisit a chapter, or save progress and exit.

Use `--plain` for log-friendly output without color or terminal control
sequences. Non-TTY and `--accept-defaults` runs keep the original scriptable
behavior and skip the review.

The wizard labels `search.embeddings.model` **Search embedding model** and
`assistant.model` **Chat and agent workflow model**. An explicit provider offers
its tested runtime default as **Recommended** plus **Custom model ID**. For
`provider: auto`, setup displays the currently resolved provider and exact
default from available environment keys, while keeping `assistant.model: null`.
When no key is available it reports that extractive answers only are available.

Document extraction is enabled by source include globs; PDF and DOCX files are
handled by the existing extractor. OCR is intentionally not a setup option:
image-only scanned PDFs need an authored `<file>.extract.txt` sidecar.

## 3. Point-and-click

Start the server with no sources and the UI shows an empty state with three
ways forward: paste a path or URL, drop files in, or copy a one-line command.

- **Drop files or ZIP archives in.** Files land in a directory under
  `/state/uploads`, which is registered as an ordinary `document_folder`
  source — the same connector → chunk → graph pipeline as everything else,
  removable by deleting the source. Supported files inside ZIP folders are
  indexed separately. No path to type, no directory to mount.
- **Paste a path.** pheasant detects what it is (folder, Obsidian vault, git
  checkout or clone URL, web page, S3 bucket, connector).
- **Change your mind.** Settings has purpose-built panels for the
  knowledge base, the answering workflow, retrieval tuning and embeddings, on
  top of the full form/YAML editor.

## Indexing a directory that is not in the container

This is the most common first-run problem: you type a path you can see, and the
sync reports `path_missing`. The path is real — it is just not *in* the
container.

Paste it into "Add a source" and pheasant checks its own mount table, then
either tells you the container path it resolved to or hands you the fix. From
the terminal:

```bash
pheasant mount ~/clients/acme
```

That writes the bind mount into `docker-compose.override.yml` **and** adds the
container path to `security.allow_workspace_roots` — a mount without the
allow-list entry is a half-fix, because the path becomes visible and the API
still refuses to register a source under it. Then:

```bash
docker compose up -d
pheasant up /data/acme
```

Use `--at /mnt/somewhere` to choose the container path, or `--print-only` to
see what would be written without writing it.

## Watching a long index

A first index of a large repository takes minutes. The jobs tray at the bottom
of every page shows each running job with its phase (`listing` → `indexing` →
`enriching` → `saving`), a counter and the file it is on. Over HTTP:

```bash
curl localhost:8765/jobs          # everything, newest first
curl localhost:8765/jobs/stream   # server-sent events as it happens
```

Registering a source never blocks on its first sync (`wait: false`), so the
form returns immediately and indexing continues in the background.

## Tuning retrieval

`assistant.retrieval` controls how hard the assistant looks before it answers —
rounds, breadth, graph expansion, evidence grading. Edit it in Settings →
Retrieval tuning, or over HTTP:

```bash
curl -X PUT localhost:8765/assistant/retrieval \
  -H 'content-type: application/json' \
  -d '{"max_rounds": 3, "max_context_passages": 16}'
```

Retrieval is query-time only, so changes apply to the next question — nothing
to restart, nothing to re-index.

An agent can test a setting before you commit it. Over MCP, `describe_retrieval`
reports what this region is configured to do and what is tunable per call, and
`preview_retrieval` runs criteria against real content and reports the delta
against the standing configuration. Nothing is persisted by a preview.

## Related

- [Configuration reference](../configuration.md)
- [Run the web UI](run-the-ui.md)
- [Configure sources](sources.md)
- [Attach to a coding agent (MCP)](attach-to-coding-agent.md)
