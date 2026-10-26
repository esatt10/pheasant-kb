# Configure pheasant-kb

pheasant-kb keeps its settings in three places, split by who changes them and
how often:

| Where | What it holds | Changed by |
|---|---|---|
| `.env` | Your keys, tokens, port, image, and the size of every service | Editing the file, then `docker compose up -d`; a changed token needs `docker compose up -d --force-recreate` ([Rotate a token](operate.md#rotate-a-token)) |
| `pheasant.yaml` (shipped as [`deploy/compose/pheasant-kb.yaml`](https://github.com/esatt10/pheasant-kb/blob/main/deploy/compose/pheasant-kb.yaml)) | What pheasant does: models, retrieval, ingestion, memory, security | A copy you own, then a restart |
| The knowledge base itself | Sources you added, applied tuning bundles, memory records | The UI, the HTTP API and MCP. Stored in Postgres and kept across restarts and upgrades. |

Sizing is covered in [Scale up or down](scale.md). This page covers the other
two.

## Change the configuration

Every pheasant service mounts the same `pheasant.yaml`, read-only. To change it,
make your own copy and point the stack at it:

```bash
cp deploy/compose/pheasant-kb.yaml pheasant.yaml        # gitignored
echo "PHEASANT_KB_CONFIG=./pheasant.yaml" >> .env
# edit pheasant.yaml, then:
docker compose up -d --force-recreate api graph indexer logger
```

`--force-recreate` is needed because Compose only restarts a container when
its own definition changed, not when a file it mounts did. Check an edit before
restarting:

```bash
docker compose run --rm --no-deps api validate /config/pheasant.yaml
docker compose run --rm --no-deps api config show -c /config/pheasant.yaml
```

Keep your copy's `pheasant.name`. It is the knowledge base's id, and the
data already indexed is stored under it.

!!! note "The UI's Config page cannot save here"
    The config is mounted read-only into every service, so a persistent edit
    from the Config page fails. Edit the file instead. Tuning bundles are
    different: they live in the database, and the Tuning page applies them.

Your copy only needs the keys you change. `pheasant-kb.yaml` is pheasant's
default configuration written out in full, so a copy holding just
`assistant: {model: gpt-6-sol}` is pheasant-kb with a different model. Every
other key is documented in the [configuration reference](../configuration.md),
and [Defaults](../configuration.md#defaults-pheasant-kb-or-standalone) lists
what pheasant-kb sets.
The sections below describe what pheasant-kb sets and the changes people
usually make.

## Models

| Setting in pheasant-kb | Value | Notes |
|---|---|---|
| `assistant.model` | `gpt-6-luna` | Writes answers. `assistant.retrieval.grader_model` judges evidence. |
| `assistant.decisions.mode` | `beta` | Evidence and acceptance judgements through OpenAI's Decisions API. `off` restores model-written judgements. |
| `search.embeddings.model` | `text-embedding-3-small`, 1,536 dimensions | Changing the model or dimensions re-embeds every chunk on the next sync. |
| `ingestion.captioner.model` | `gpt-6-luna` | Image captions. |
| `ingestion.transcriber.model` | `whisper-1` | Audio transcripts. |

To use a different provider for answers, set `assistant.provider` (`anthropic`,
`gemini` or any OpenAI-compatible `base_url`) and pass its key through. The
stack only forwards `OPENAI_API_KEY` by default, so add the key to the services
that call the model, in a `compose.override.yaml` beside `compose.yaml`
(Compose merges it automatically; it is gitignored):

```yaml
services:
  api:
    environment:
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
```

Embeddings, captions and transcripts run on the indexer, so a key for those
goes on `indexer` too.

## Sources

pheasant-kb ships with two sources: `agent-memory` (`/memory`) and `uploads`
(the UI drop zone). Everything else is added at runtime from the UI, HTTP or
MCP, and stored in the database, so it survives restarts, upgrades and config
edits. You can also list sources under `sources:` in your `pheasant.yaml`; see
[Configure sources](../how-to/sources.md) for every type and option.

### Index a folder on the host

A source can only read a path the containers can see. Mount the folder under
`/data` (an allowed source root in pheasant-kb) on the indexer, which reads it,
and on the api tier, which shows the original file in the document viewer:

```yaml
# compose.override.yaml
services:
  indexer:
    volumes:
      - ~/notes:/data/notes:ro
  api:
    volumes:
      - ~/notes:/data/notes:ro
```

Run `docker compose up -d`, then add `/data/notes` as a source from
**Sources → Add**. Use `/data`, not a folder inside `/workspace`: the api tier
mounts `/workspace` read-only, and Docker cannot create a mount point inside a
read-only volume.

### Repositories added by URL

The indexer clones them into the `pheasant-workspace` volume and fetches before
each sync. Set `PHEASANT_FLEET_WORKSPACE_PATH` in `.env` to keep the clones in
a host directory instead, and `GITHUB_TOKEN` for private repositories.

## Retrieval

| Setting | pheasant-kb | What it does |
|---|---|---|
| `search.default_mode` | `hybrid` | Full-text, vector and graph arms, fused by reciprocal rank. |
| `storage.lexical_index` | `pg_textsearch` (default) | BM25 inside Postgres. `tsvector` works on a Postgres without the extension. |
| `search.vector_store.resident` | `true` | Keeps every vector in the api tier's memory for an exact, fast scan. See [the vector store](scale.md#the-vector-store). |
| `search.vector_store.ann_index` | `ivf_hnsw_sq` | An approximate index for when vectors do not fit in memory. It trades recall for speed. |
| `assistant.retrieval.max_rounds` | `2` | Up to one follow-up search when the evidence is judged insufficient. |
| `assistant.latency.max_concurrent_answers` | `4` | Answers in flight at once; beyond it the API answers `429 ASSISTANT_BUSY`. |

Ranking parameters are better changed by the tuning plane than by hand: it
measures, gates and records each change. See
[Retrieval tuning](../retrieval-tuning.md).

## Ingestion

| Setting | pheasant-kb | What it does |
|---|---|---|
| `sync.source_processing.chunk_strategy` | `auto` | Chunks each file by its structure: code by block, Markdown by heading, spreadsheets by row. See [Dynamic chunking](../how-to/dynamic-chunking.md). |
| `sync.source_processing.taxonomy_enabled` | `true` | Detects document headings and cuts chunks at section boundaries. |
| `sync.concurrency.file_executor` | `remote` | Files are parsed on the worker tier. |
| `sync.limits.max_file_size_mb` | `1024` | Larger files are skipped. |
| `ingestion.extractor.math` | `off` (default) | `latex` reads equations in Word and PDF documents as TeX. |
| `readiness.corpus_denylist` | `benchmark/*`, `*.answers.json` | Paths that may never be indexed, enforced on every way in. |

A change to a processing setting causes a full pass over each affected source
on its next sync.

## Features you can turn off

pheasant-kb runs everything. These are the parts that cost something when idle
and can be switched off without losing the core product:

| Turn off | Set | Then |
|---|---|---|
| The interaction ledger | `observability.interactions.enabled: false` | The `logger` service has nothing to do: `docker compose up -d --scale logger=0`. |
| Evaluation and tuning | `evaluation.enabled: false`, `tuning.enabled: false` | Their pages and endpoints go away; nothing else changes. |
| Readiness probes | `readiness.enabled: false` | `POST /readiness/check` is refused; the capability contract stays readable. |
| Image captions, transcripts | `ingestion.captioner.provider: stub`, `ingestion.transcriber.provider: stub` | Offline placeholders instead of model calls at sync time. |
| The agentic answer loop | `assistant.workflow: simple` | One search and one model call per answer. |

## Security

| Setting | pheasant-kb | Notes |
|---|---|---|
| `security.api_auth.token_env` | `PHEASANT_API_TOKEN` | Every request except `/health`, `/ready` and `/metrics` needs the token. If an ingress already authenticates, set `security.api_auth.behind_authenticating_proxy: true`. |
| `security.acl_enforced` | `true` | Results are filtered by the caller's principal. |
| `security.allow_workspace_roots` | `/workspace`, `/memory`, `/state`, `/exports`, `/data` | The only paths a source may read. |
| `server.api.cors_origins` | localhost origins, `http://api:8765` | The MCP endpoint's host check is derived from this list. Reaching the stack under another hostname needs that origin added, or MCP answers `421`. |
| `PHEASANT_BIND` (`.env`) | `127.0.0.1` | Widen only behind a reverse proxy that terminates TLS. |

The four internal tokens and the boundaries they protect are explained in
[Security](../security.md).

## Change the shipped defaults

To change what pheasant-kb ships with for everyone (in a pull request, not for
your own deployment), edit the answer file and regenerate the YAML from it, so
the live schema validates every key:

```bash
python -m pheasant setup --answers deploy/compose/answers/pheasant-kb.json \
  --accept-defaults --plain --target compose \
  --output deploy/compose/pheasant-kb.yaml --force
python -m pheasant doctor -c deploy/compose/pheasant-kb.yaml --no-require-paths
```

Commit both files together.
