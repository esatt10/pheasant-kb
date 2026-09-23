# How to configure sources

Sources are the inputs pheasant indexes. They live under `sources:` in your
`pheasant.yaml` and can also be registered at runtime over MCP
(`register_source`) and later promoted to durable config
(`promote_runtime_source_to_config`).

Three routes reach the same place, so pick whichever fits:

- **One command / one field.** `pheasant up <target>` — or **+ Add source**
  in the UI, or `POST /sources/quick-add` — takes a path, a git URL, a glob
  or a connector name, detects what it is, names it, registers it and syncs
  it. Nothing else to fill in. In the UI, registration is immediate and the
  first sync runs in the background (`wait: false` — see
  [HTTP API reference](../reference/http-api.md#sync)) — the form closes
  right away instead of holding the connection open for however long a
  large source's first clone + index takes; watch it land on the Sources
  page, which shows a live "syncing" state per source until it finishes.
  `pheasant up`/CLI calls still block by default (`wait: true`), matching
  the shell's own expectation of a command that returns when it's done.
  In a role-split fleet, the API only records a repository URL and publishes
  the sync task; the writable indexer clones or fast-forwards it under
  `/workspace` before indexing. The API does not need write access to
  `/state` or `/workspace`.
- **The form.** **Sources → Advanced…** in the UI exposes every field on this
  page, with the type list read from `GET /sources/types` so installed
  connector plugins appear automatically.
- **The file.** Everything below.

## Source types

| Type | Indexes |
|---|---|
| `repository` | A git repository (branch/commit-aware, dependency graph) |
| `markdown_folder` | A folder of Markdown notes |
| `obsidian_vault` | An existing Obsidian vault (`.md` + `.canvas`) |
| `document_folder` | PDFs, DOCX, TXT, HTML, XML |
| `single_file` | One file |
| `web_collection` | A set of web URLs |
| `memory` | Agent-memory records (see [Agent memory](agent-memory.md)) |
| `notion` | A Notion workspace, via an integration token (below) |
| `gdrive` | Google Drive docs + text files (`connector.api_key_env`, default `GDRIVE_TOKEN`) |
| `slack` | Slack channel transcripts (`SLACK_TOKEN`; ids rendered as-is) |
| `confluence` | Confluence pages (`CONFLUENCE_TOKEN` + `connector.api_endpoint` site URL) |
| `imap` | An email mailbox (`IMAP_CREDENTIALS` as `user:password`; `path` = mailbox) |
| `api` | Experimental — an HTTP API source |
| `s3` | Experimental — an S3-style object store |

Third-party connector plugins add further types by name — see the
[Connector SDK](../reference/connector-sdk.md).

## Notion

Create an internal integration at `notion.so/my-integrations`, share the
pages with it, and export the token in the environment — it never lands in
config:

```yaml
sources:
  - name: team-notion
    type: notion
    path: /unused            # required by the schema; Notion ignores it.
                             # Registering through the API or the UI form
                             # fills this in for you — service-backed types
                             # have `path_role: unused` in /sources/types.
    include: []
    connector:
      api_key_env: NOTION_TOKEN   # default; name of the env var
```

Pages are listed through Notion's search API and rendered to deterministic
Markdown (headings, lists, to-dos, quotes, code, nested blocks). Sync is
incremental: unchanged pages (by `last_edited_time`) are skipped before
any block is fetched, so a large workspace re-syncs in seconds. Page
`created_by` / `last_edited_by` ids are captured for the upcoming
permission-aware retrieval work.

## Web pages

List the URLs; nothing else is required. `path` may be omitted — the
connector never opens it. In a config file the connector needs the explicit
experimental opt-in (the UI, `pheasant up` and the MCP tool imply it):

```yaml
ingestion:
  extractor:
    html_text: true            # index page text, not markup (see Document ingest)
sources:
  - name: fde-web
    type: web_collection
    urls:
      - https://example.com/blog/forward-deployed-engineering
      - https://example.com/about.html
      - https://example.com/reports/2025-annual-report.pdf
    connector:
      allow_experimental: true
```

Every listed URL is fetched. The stock `include` globs (code, Markdown,
config) are written for walking a folder and are **not** applied to a URL
list; an `include` you set yourself still is, and a URL it filters out is
logged rather than dropped silently. Excludes (including the credential
patterns) always apply.

A page served as `text/html` is extracted as HTML even when its URL has no
extension. With `html_text: true` that means its text, without tags,
`<script>` or `<style>` bodies. A listed `.pdf` (or other document
format) is extracted like a local one.

### How web pages stay fresh

The scheduler beat (`sync.scheduler.interval_seconds`, 15 minutes) is shared
by every source, but each page has its **own** revalidation schedule, kept in
the source's checkpoint:

- a page is first re-checked after `sync.interval_seconds` (default one hour);
- each check that finds it unchanged **doubles** the wait, up to
  `connector.max_refresh_seconds` (default three days);
- a check that finds it changed **resets** it to the minimum, so a page being
  edited is watched closely;
- a `Cache-Control: max-age` the server sends is honoured as a floor, and
  `no-cache` as "use the minimum";
- a page that is not due costs **no request**, and a due check is conditional
  (`ETag` / `Last-Modified`), so an unchanged page usually costs a `304`.

So a stable page is fetched about twice a week and a page under active
editing about hourly, instead of 96 times a day. `sync_source` with
`mode: full` ignores the schedule and re-checks everything now; setting
`sync.interval_seconds: 0` checks every page on every beat. Each sync's
checkpoint reports `revalidated` (requests made) and `deferred` (pages left
alone because they were not due).

A web source indexed by an earlier release is re-read **once**, automatically,
on its first sync after upgrading. Its fingerprint carries the web text
pipeline, so pages stored as raw markup, or skipped by the old include
globs, are re-derived. Turning `html_text` on or off re-reads web sources the
same way; folder sources are not affected.

### Other ways to add web pages

- `pheasant up <url>` writes the opt-in for you;
- **Sources → + Add source** in the UI takes a pasted URL, and
  **Sources → Advanced… → Web pages** takes a list;
- `POST /sources` with `"type": "web_collection"` and `"urls"` (the opt-in is
  implied, and `path` may be `"/unused"`);
- the MCP `register_source` tool with `source_type: "web_collection"` and
  `urls`. Over MCP only public addresses are accepted unless
  `security.allow_agent_private_urls: true`, because an agent can be steered
  by what it reads into asking the region to fetch an internal endpoint.

## A minimal source

```yaml
sources:
  - name: my-repo            # stable id; appears in stable node IDs
    type: repository
    path: /workspace         # must resolve under an allowlisted root
    enabled: true
    include:
      - "**/*.py"
      - "**/*.md"
    exclude:
      - "**/.git/**"
      - "**/__pycache__/**"
    chunking:
      enabled: true
      strategy: semantic     # or heading_or_page for documents
      max_chars: 4000
      overlap_chars: 400
    sync:
      on_startup: true
      on_file_change: debounce
      interval_seconds: 900
```

## Include / exclude

- `include` is a list of glob patterns. A file must match at least one to be
  indexed.
- `exclude` removes matches. Secrets (`.env*`, private keys, `.pem`/`.key`) are
  excluded by default via `security.default_exclude_secrets`; keep those
  patterns in `exclude` too for defense in depth.
- **Extension globs control more than filtering.** Admitting an image extension
  (`**/*.png`) builds the image captioner; admitting an audio extension
  (`**/*.wav`) builds the transcriber. See
  [Multi-modal ingest](multimodal-ingest.md).

The reference `pheasant.example.yaml` ships a thorough `exclude` list (`.git`,
`node_modules`, `dist`, `build`, virtualenvs, state/exports) — copy it as
a baseline.

## Sync modes

Run a sync with `pheasant sync`:

```bash
pheasant sync --config pheasant.yaml --source my-repo --mode incremental
pheasant sync --config pheasant.yaml --all --mode full
```

| Mode | Behavior |
|---|---|
| `incremental` | Uses connector checkpoints + content hashes to skip unchanged artifacts. The default. |
| `full` | Rebuilds artifact, chunk, graph, manifest, and checkpoint state for a source. |
| `validate_only` | Checks connector health and readability without writing index artifacts or manifests. |
| `repair` | Rebuilds missing or invalid state from manifests and database rows. (Also available as `pheasant repair`.) |

Indexing is **idempotent**: re-syncing unchanged content produces the same state
(content `sha256` + stable IDs), so a no-op sync skips everything.

## When syncs run automatically

The `sync:` block on each source, plus the global `sync:` block, control
automatic syncing:

- **On startup** — `sync.on_startup: true` (and `sync.startup.full_validation`).
- **On file change** — the watcher (`sync.watcher`) debounces filesystem events.
  Watcher reliability varies across Docker mount types; keep the scheduler on as
  a fallback.
- **On git commit / branch switch** — `sync.git` re-indexes or validates.
- **On a schedule** — `sync.scheduler.interval_seconds` (default 900s).

## Validate before you run

```bash
pheasant validate pheasant.yaml      # config shape + allowlist + paths
pheasant doctor --config pheasant.yaml   # runtime environment checks
pheasant config show --effective --config pheasant.yaml   # resolved config
```

See the full key-by-key reference in [Configuration](../configuration.md).
