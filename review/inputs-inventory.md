# pheasant inputs: keep/cut review

Every way content can get **into** a pheasant knowledge base, as of
`claude/ecstatic-cori-umjkvl`. Mark each row `[x]` to keep or leave it `[ ]`
to cut. The **Suggest** column is only a starting point for an initial product:

- **Core**: the product does not work without it.
- **Keep**: cheap, stable, and broadly useful.
- **Defer**: works, but adds surface, dependencies or support load. Hide or remove it for v1.
- **Cut**: niche or experimental, with a large blast radius.

Code pointers are where removal would start.

---

## 1. Built-in source types (`sources[].type`)

Source: `SourceType` in `src/pheasant/config/schema.py:21`, dispatched in
`connector_for_source` (`src/pheasant/sync/connectors.py:579`).

| Keep | Type | What it ingests | Connector | Status | Suggest |
|---|---|---|---|---|---|
| [ ] | `repository` | A local git repo: branch-aware IDs, commit trigger, uncommitted files, optional clone (`repo.clone_url`) | `FilesystemConnector` | stable | **Core** |
| [ ] | `markdown_folder` | A folder of Markdown/text | `FilesystemConnector` | stable | **Core** |
| [ ] | `document_folder` | A folder of PDFs/Office/etc. (§3) | `FilesystemConnector` | stable | **Core** |
| [ ] | `single_file` | One file | `FilesystemConnector` | stable | Keep |
| [ ] | `obsidian_vault` | An Obsidian vault (`![[…]]` embeds, wiki links) | `FilesystemConnector` | stable | Keep (it is a folder with link semantics) |
| [ ] | `memory` | Agent memory records, one frontmatter `.md` per record | `FilesystemConnector` | stable | Decide alongside the memory feature set |
| [ ] | `web_collection` | An explicit list of URLs, with per-page revalidation and backoff | `WebCollectionConnector` (`sync/web_connector.py`) | **experimental** (`allow_experimental`) | Defer |
| [ ] | `api` | A generic JSON endpoint (`api_endpoint`, `api_items_field`, `api_content_field`) | `APIConnector` | **experimental** | Cut |
| [ ] | `s3` | An S3 bucket/prefix (needs `boto3`) | `S3Connector` | **experimental** | Defer |

## 2. Connector plugins (entry point `pheasant.connectors`)

Source: `pyproject.toml:120`, `src/pheasant/connectors/`. Plugins resolve
through `sync/connector_registry.py`, so dropping one only touches
`pyproject.toml` and its module.

| Keep | Plugin | What it ingests | Auth | Suggest |
|---|---|---|---|---|
| [ ] | `notion` | Notion pages/databases | API token env | Defer |
| [ ] | `gdrive` | Google Drive files | OAuth token env | Defer |
| [ ] | `slack` | Slack channel messages (chunked by message) | Bot token env | Defer |
| [ ] | `confluence` | Confluence pages (needs `bs4`) | API token env | Defer |
| [ ] | `imap` | Email over IMAP | Password env | Cut |
| [ ] | **Third-party plugin mechanism** (entry points + `pheasant.testing.ConnectorConformance`) | Any external connector | n/a | Keep the mechanism even if you cut all five |
| [ ] | **Sandboxed runtime** (`connector.runtime: sandboxed`, wasmtime guest, `allowed_hosts`, `wasm_module_path`) | Runs a plugin inside WASM | n/a | Defer (the `wasm` extra plus `src/pheasant/sandbox/`) |

## 3. File formats (what a source will actually parse)

Source: `src/pheasant/ingestion/content_types.py`. Note that the default
`include` globs (`schema.py:54`) admit only `.py .md .txt .yaml .yml .toml .json`.
Everything else has to be opted into per source.

| Keep | Family | Extensions | Parser | Suggest |
|---|---|---|---|---|
| [ ] | Markdown/prose | `.md .mdx .txt .rst` | pipeline (heading-aware) | **Core** |
| [ ] | Config/data | `.yaml .yml .toml .json .xml` | pipeline | **Core** |
| [ ] | Code | `.py .js .jsx .ts .tsx .sh .css` | pipeline (symbols, imports, calls) | **Core** |
| [ ] | HTML | `.html` | `extractor.py` | Keep |
| [ ] | PDF | `.pdf` | `extractor.py` (+ `pdf_pages.py`, fleet `pdf_split.py`) | **Core** |
| [ ] | Word | `.docx` | `extractor.py` | Keep |
| [ ] | PowerPoint / Excel | `.pptx .xlsx` | `office.py` | Keep |
| [ ] | EPUB / RTF | `.epub .rtf` | `office.py` | Defer |
| [ ] | Legacy Word | `.doc` | `msdoc.py` (484 lines, binary OLE parser) | Cut |
| [ ] | Images | `.png .jpg .jpeg .webp .gif` | `captioner.py` (stub, or OpenAI-spec network call) + media store | Defer |
| [ ] | Audio | `.wav .mp3 .m4a .flac .ogg` | `transcriber.py` (stub, or OpenAI-spec network call) | Cut |
| [ ] | ZIP archives | members of any type above, read in place | `sync/zip_archive.py` | Defer |

Extraction providers (`auto`, `native`, `builtin`, `sandboxed`):

| Keep | Provider | Suggest |
|---|---|---|
| [ ] | `auto` (default: native, then builtin) | **Core** |
| [ ] | `native` / `builtin` | Keep (they are what `auto` uses) |
| [ ] | `sandboxed` (PDF tokenizer in WASM, `extractor_sandbox.py`) | Cut |

## 4. Sidecars and in-file conventions

| Keep | Input | Effect | Suggest |
|---|---|---|---|
| [ ] | `<file>.extract.txt` | Overrides document extraction | Keep (cheap, and good for tests) |
| [ ] | `<file>.caption.txt` | Overrides the image caption | Goes with images |
| [ ] | `<file>.transcript.txt` | Overrides the audio transcript | Goes with audio |
| [ ] | OKF frontmatter and bundles (`ingestion/okf.py`, `graph/okf.py`) | Types, tags, links, provenance, listings | Defer |
| [ ] | Structural taxonomy (`ingestion/taxonomy.py`, per source, opt-in) | Heading detection across conventions; section-aligned chunks | Defer |
| [ ] | Image embeds in Markdown/HTML (`graph/media_links.py`) | `embeds` edges, figures in answers | Goes with images |

## 5. Ingestion entry points (how content is submitted)

| Keep | Entry point | Surface | Suggest |
|---|---|---|---|
| [ ] | YAML `sources:` in `pheasant.yaml` | config | **Core** |
| [ ] | `pheasant up [PATH...]` (detect → config → index → serve) | CLI | **Core** |
| [ ] | `pheasant setup` wizard | CLI | Keep |
| [ ] | `pheasant host <path>` / `pheasant mount <path>` | CLI | Keep, or pick one |
| [ ] | `pheasant sync --source … --mode …` | CLI | **Core** |
| [ ] | `POST /sources`, `POST /sources/quick-add` | HTTP/UI | Keep |
| [ ] | `POST /sources/upload` (UI drop zone, lands bytes in `/state/uploads`) | HTTP/UI | Keep |
| [ ] | `POST /sync`, `POST /sync/{source_id}`, `/sources/{id}/scan` | HTTP | **Core** |
| [ ] | MCP `register_source`, `sync_source`, `start_sync_source`, `sync_all`, `scan_source` | MCP | Keep (`register_source` with URLs depends on `web_collection`) |
| [ ] | MCP/HTTP `submit_documents` / `POST /ingest/submit`, plus acknowledge/reconcile receipts | MCP+HTTP | Defer (readiness plane) |
| [ ] | Landing service forwarding (`ingestion/landing_service.py`, `/internal/ingestion/land`) | fleet only | Defer (needed only with the role split) |
| [ ] | Memory writes: MCP `memory_write`, `POST /memory`, candidates promote/reject, consolidate/synthesize | MCP+HTTP | Decide alongside the memory feature set |
| [ ] | Memory **formation** from the observation plane (`memory.formation`, off by default) | automatic | Cut |

## 6. Triggers (when an input is re-read)

| Keep | Trigger | Config | Suggest |
|---|---|---|---|
| [ ] | Sync on startup | `sync.on_startup` | **Core** |
| [ ] | File watcher, debounced (`sync/watcher.py`) | `sync.on_file_change` | Keep |
| [ ] | Git commit trigger | `sync.on_git_commit`, `repo.commit_trigger` | Keep |
| [ ] | Scheduler beat (default 900s) | `sync.scheduler.interval_seconds` | Keep |
| [ ] | Per-source interval / web backoff | `sources[].sync.interval_seconds`, `connector.max_refresh_seconds` | Goes with `web_collection` |
| [ ] | Durable index queue (`local`/`nats`) + worker fleet | `sync.queue.*`, `serve --role indexer/worker` | Defer (scale) |

## 7. Content processed at sync time that can call the network

Each has an offline stub. All are optional.

| Keep | Input | Providers | Suggest |
|---|---|---|---|
| [ ] | Embeddings (vector arm) | `openai-spec`, `stub` | Keep (needed for hybrid search) |
| [ ] | Image captioner | `openai-spec`, `stub` | Goes with images |
| [ ] | Audio transcriber | `openai-spec`, `stub` | Goes with audio |

---

### Notes for deciding

- **Coupled cuts.** Cutting images also lets you drop the captioner, the media
  store (`ingestion/media.py`), `graph/media_links.py`, `get_image`, and
  figures in answers. Cutting `web_collection` also drops
  `security/url_policy.py` and the URL path of MCP `register_source`.
- **The MCP tool surface is public API (CLAUDE.md rule 8).** Nothing has
  shipped yet, so removing tools now is fine. After v1, removal needs a
  deprecation.
- **The experimental flag already marks the riskiest inputs.** `api`, `s3` and
  `web_collection` refuse to sync without `connector.allow_experimental: true`.
  Hiding them from the UI catalog and the wizard is the lightest cut.
- **Outside this list:** the planes that consume content rather than ingest it
  (evaluation, tuning, readiness, observation, assistant, Synapse contract).
  Tell me if you want the same keep/cut list for those.

---

## Decisions (recorded from review)

**Keep**
- Source types: `repository`, `markdown_folder`, `document_folder`, `single_file`, `memory`, `web_collection`, `api`
- Plugins: `gdrive` (untested, so verify before release), third-party plugin mechanism, WASM sandboxed connector runtime
- Formats: all of section 3, including images, audio, ZIP, legacy `.doc`, EPUB/RTF and the sandboxed PDF extractor (open question: the "Something else" box in the text/code/web question was ticked with no detail, so a missing format may be wanted)
- Sidecars and conventions: `.extract.txt`, `.caption.txt`, `.transcript.txt`, OKF, structural taxonomy
- Entry points: YAML + `pheasant sync`, `pheasant up`, `pheasant setup`, `pheasant host` and `pheasant mount`, HTTP source routes, UI drop zone, MCP source/sync tools, receipt-tracked submission, memory write routes
- Triggers: startup/watcher/git commit, scheduler and per-source intervals, durable queue + worker fleet
- Network-capable steps: embeddings, captioner, transcriber

**Cut**
- Source types: `obsidian_vault`, `s3`
- Plugins: `notion`, `slack`, `confluence`, `imap`

**Defer**
- Memory formation from the observation plane (`memory.formation`)

**Open design item**
- The WASM runtime today runs only the per-item transform inside the guest. Listing and reading stay on the host, and the only shipped guest is a reference `.wat`. A decoupled "check before it lands" gate would need the guest to cover reads.

## Implemented

- **File types:** every common software-project text format is read by default (see `content_types.TEXT_EXTENSIONS` and `TEXT_FILENAMES`). `.html`, `.xml`, `.patch` and `.diff` are opt-in. Minified bundles, source maps and lockfiles are excluded as noise.
- **Cut:** the `notion`, `slack`, `confluence` and `imap` plugins, the `s3` built-in and the `obsidian_vault` built-in. Old configs and `/state` rows keep loading through `config/retired.py`: `obsidian_vault` loads as `markdown_folder`, and the removed types are refused at sync with the reason.
- **Deferred, no code change:** memory formation was already off by default (`memory.formation`).
- **Not developed:** the WASM "check before it lands" gate.
- **Follow-up:** `.csv`, `.tsv`, `.ipynb` (read as cells, outputs dropped) and `.log` are added and read by default. Import, call and symbol analysis now covers 22 languages beyond Python, and each one has tests (`tests/test_code_analysis.py`).
