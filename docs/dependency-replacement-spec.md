# Dependency replacement specification

Status: proposed implementation specification. Assessment date: 2026-11-16.
Repository baseline: `042e10d` (Pheasant 1.6.2).

This specification removes PyMuPDF/MuPDF, Psycopg and its pool, and the
TimescaleDB deployment image from Pheasant's supported application stack.
**`pg_textsearch` stays, including its BM25 indexes and current retrieval
behavior.** The preferred replacements are PDFium through `pypdfium2`,
`pg8000` with SQLAlchemy pooling, and a maintained PostgreSQL 17 image that
installs only `pg_textsearch` beyond the required PostgreSQL components.

These are candidate selections, not completed migrations or measured parity
claims. Implementation cannot be accepted until the correctness, performance,
packaging, and migration gates below pass. No production deployment or data
migration is authorized by this document.

## 1. Scope and decisions

### 1.1 Why replace these components

| Component | Current terms and motivation | Required disposition |
| --- | --- | --- |
| PyMuPDF and its MuPDF engine | AGPL or Artifex commercial terms. A separate upstream licensing decision is needed for proprietary distribution that cannot meet AGPL obligations. | Remove the Python dependency, imports, native binaries, and test/benchmark generation dependency from supported builds. |
| `psycopg`, `psycopg-binary`, `psycopg-pool` | LGPL permits commercial use. Replacement is a policy choice to remove these copyleft application dependencies, not evidence that commercial use is prohibited. | Replace the driver, pooling, connection checks, error translation, and direct maintenance/test connections. |
| `timescale/timescaledb-ha:pg17` | Bundles components beyond PostgreSQL and `pg_textsearch`, including software governed by Timescale-specific terms. Their applicability depends on the components and use. | Stop shipping or requiring this image in supported deployment and CI paths. |
| `pg_textsearch` | PostgreSQL License; independent of TimescaleDB. | Retain as the default PostgreSQL lexical backend and a required integration test target. |

Sources: [PyMuPDF licensing](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright),
[Psycopg license](https://github.com/psycopg/psycopg/blob/master/LICENSE.txt),
[Timescale terms](https://github.com/timescale/timescaledb/blob/main/tsl/LICENSE-TIMESCALE),
and [`pg_textsearch` license](https://github.com/timescale/pg_textsearch/blob/main/LICENSE).

This is a targeted replacement of the named upstream components. It does not
change [Pheasant's own license](https://github.com/esatt10/pheasant-kb/blob/main/COMMERCIAL-LICENSE.md),
promise that every operating-system package in a container is permissive, or eliminate ordinary
copyright, attribution, patent, or redistribution obligations. For example,
the application image also installs Git; a claim that the entire image is
copyleft-free would require a separate inventory and policy decision.

### 1.2 Preferred replacements and alternatives

| Workstream | Most likely replacement | Why it fits | Selection gate / alternative |
| --- | --- | --- | --- |
| PDF extraction | `pypdfium2` plus its packaged PDFium build | Native engine; page text and character geometry; fits the existing page-reader boundary. | Prove font/code, baseline, path geometry, reading-order, and equation parity. Evaluate `pdfminer.six` for missing extraction capabilities if necessary; do not silently remove mathematics. |
| EPUB extraction | Existing `extract_builtin("epub", ...)` | Already used as a fallback; avoids another package and a native EPUB layout engine. | Test spine order and the malformed EPUBs where the current native path recovers text the builtin path misses. Improve the existing reader if needed. |
| PostgreSQL driver | `pg8000` DB-API interface | Synchronous, pure Python, BSD-3-Clause; matches the application's existing synchronous storage interface. | Prove transaction, parameter, type, timeout, TLS, and batching behavior. If performance fails, reassess the driver rather than accepting a regression by default. |
| PostgreSQL pool | SQLAlchemy `QueuePool` or a deliberately selected SQLAlchemy pooling integration | MIT; established bounded pooling; usable independently of an ORM. | Prove checkout deadlines, idle checks, invalidation, reset, and lifecycle behavior. A raw `QueuePool` does not automatically inherit all dialect-aware engine behavior. |
| Database image | A project-maintained image based on a pinned `postgres:17` Debian variant with a pinned `pg_textsearch` release | Retains PostgreSQL and BM25 without TimescaleDB. | Build and test every published architecture; verify extensions and effective server settings in the resulting image. |

Upstream references: [`pypdfium2` licensing](https://github.com/pypdfium2-team/pypdfium2#licensing),
[`pypdfium2` API](https://pypdfium2.readthedocs.io/en/stable/python_api.html),
[`pdfminer.six` license](https://github.com/pdfminer/pdfminer.six/blob/master/LICENSE),
[`pg8000` documentation](https://pypi.org/project/pg8000/),
[SQLAlchemy license](https://github.com/sqlalchemy/sqlalchemy/blob/main/LICENSE),
[standalone pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html#using-a-pool-instance-directly),
and [`pg_textsearch` installation](https://github.com/timescale/pg_textsearch#installation).
Exact supported versions and binary hashes must be chosen and recorded during
the feasibility stage; this document does not imply compatibility with every
future release.

### 1.3 Product invariants

| ID | Requirement |
| --- | --- |
| INV-01 | Keep `storage.backend: postgres`, `storage.lexical_index: pg_textsearch`, the existing database URL configuration, and existing public HTTP/MCP operations working. |
| INV-02 | Keep standalone SQLite, FTS5, builtin extraction, sandboxed extraction, and offline test operation functional. |
| INV-03 | Indexing stays deterministic. Add no LLM, OCR service, captioner, remote conversion service, or network dependency to replace native document parsing. |
| INV-04 | Preserve artifact ID grammar, ownership and access controls, provenance, citations, receipts, and durable artifacts. Changed extraction text can change derived chunk IDs only through the existing replacement/retraction lifecycle. |
| INV-05 | Preserve the single commit authority, atomic artifact replacement, queue/lease behavior, independent heartbeats, and graph/vector consistency. |
| INV-06 | Keep local whole-document extraction and distributed page-range extraction on the same implementation and parser fingerprint. |
| INV-07 | Preserve state and original volumes. Database image migration must use a rehearsed backup/restore and explicit cutover with a rollback record. |
| INV-08 | Retain one operation implementation under `services/`, with equivalent HTTP and MCP behavior. |

Changing the project's license, PostgreSQL major version, BM25 algorithm,
embedding model, graph model, UI framework, or source definitions is outside
this migration. Do not combine an extension upgrade with the image swap unless
the old version cannot run on the target and the additional change is assessed
and tested explicitly.

## 2. Inventory: what changes where

Paths below are relative to the repository root. Existing paths are confirmed
at the baseline revision. Proposed new paths are identified as such. Incidental
comments mentioning old libraries must be reviewed, but they are distinct from
executable dependencies.

### 2.1 Packaging and deployment

| Existing surface | Required change |
| --- | --- |
| `pyproject.toml` | Replace core `pymupdf`; replace the `postgres` extra's `psycopg[binary,pool]`; keep the extra name and optional-install behavior. Declare every replacement used directly. |
| `Dockerfile` | Install replacements through `pyproject.toml`; retain universal extras and standalone support; include required native dependency licenses; ensure cached layers cannot leave removed packages in the final image. |
| `compose.yaml` | Change database, backup, and restore images together; update data-directory mounts, ownership, initialization and any image-specific expectations. Keep service names, network boundaries, and resource knobs compatible. |
| `deploy/compose/docker-compose.scale.yml` | Replace its PostgreSQL image and validate the fleet's schema-init, readiness, secret and volume behavior. |
| `deploy/compose/ci/docker-compose.evaluation.yml`, `deploy/compose/ci/docker-compose.log-tier.yml` | Use the same new database image contract in executable evaluation and logger checks. |
| `.github/workflows/ci.yml`, `evaluation.yml`, `memory.yml`, `tuning.yml` | Replace database service images and retain real PostgreSQL execution; the main backend parity job remains unfiltered and requires the extension. |
| Other workflows that build/publish application images | Discover through `.github/workflows/`; add database-image build/publication, provenance, architecture checks, and a reliable CI bootstrap path. |
| `deploy/kubernetes/scaled/README.md`, other deployment docs and Helm examples | Replace Timescale-image guidance; state the required PostgreSQL/extension/preload contract for an external database. Do not invent a bundled Kubernetes database where none exists. |
| `deploy/compose/backup.sh`, `restore.sh` | Remove assumptions about the Timescale image's binary directories and tools; test every invoked utility in the new image. Preserve checksum verification, refusal while services run, and backups of replaced state. |
| `.env.example`, `deploy/compose/.env.example`, relevant values/manifests | Document a database image override if introduced, pinned defaults, new-volume cutover, and rollback. Never embed credentials. |

Proposed image definition: `deploy/postgres/Dockerfile`, with adjacent build
documentation and license/provenance records. The exact registry repository
and tag format are implementation decisions recorded before publication.

### 2.2 Document extraction

| Existing surface | Current reliance / required work |
| --- | --- |
| `src/pheasant/ingestion/pdf_pages.py` | Primary replacement boundary: imports, lock, page counts, text, math, glyphs, drawing rules and reader version. |
| `src/pheasant/ingestion/extractor.py` | Replace native PDF availability checks and EPUB native implementation. Preserve `auto`/`native`/`builtin`/`sandboxed` configuration meanings and sidecar precedence. |
| `src/pheasant/ingestion/pdf_math.py`, `pdf_regions.py`, `math_layout.py`, `tex_symbols.py` | Keep the existing deterministic equation engine; adapt reader output to `Glyph` and `Rule`. Change recognition rules only when fixtures demonstrate a necessary correction. |
| `src/pheasant/ingestion/pdf_glyphs.py`, `pdf_objects.py` | Retain the builtin interpretation path; assess reuse for raw character codes or rules the native candidate cannot expose accurately. |
| `src/pheasant/sync/pdf_split.py` | Retain page counting, range partitioning, ordering, task rejection, fallback and bounded parallel work. |
| `src/pheasant/sync/worker_transport.py`, `grpc_worker.py` | Replace PyMuPDF-specific diagnostics and reader identity; keep HTTP/gRPC range-result validation. |
| `src/pheasant/sync/fingerprint.py` | Add a scoped extraction implementation identity to invalidate affected stored text once; currently reader replacement is not represented in the source fingerprint. |
| `src/pheasant/config/schema.py`, `setup_wizard.py` | Update descriptions of native extraction and any capability reporting. If schema changes are necessary, update `docs/configuration.md` and the wizard/live-config surfaces required by repository rules. |
| `scripts/fetch_benchmark_corpus.py` | Replace any PyMuPDF-based PDF creation or manipulation; benchmark setup must not reinstall the removed engine. |
| `tests/test_pdf_split.py`, `test_document_math.py` | Remove PyMuPDF as a fixture-generation dependency; retain committed PDF fixtures or use a separately reviewed permissive fixture generator. |
| `tests/test_document_extraction.py`, `test_fresh_ui_compose.py` | Update import-failure expectations and source/documentation assertions without weakening extraction coverage. |

`ingestion/msdoc.py` and other explanatory references need wording review;
replacing the PDF reader must not accidentally change legacy DOC, DOCX, PPTX,
XLSX or RTF behavior. The browser's `pdfjs-dist` viewer stays unchanged.

### 2.3 PostgreSQL application integration

| Existing surface | Required work |
| --- | --- |
| `src/pheasant/persistence/backends.py` | Driver/pool creation, row metadata, connection lifecycle, DB-API execution, batching, deadlines, schema lock, transactions and release/discard behavior. |
| `src/pheasant/persistence/sql.py` | Verify the selected parameter style, literal-percent handling, named parameters, arrays, and translation cache against the new driver. |
| `src/pheasant/persistence/pg_liveness.py` | Replace libpq-only keepalive options, Psycopg-specific SQLSTATE/type checks and pool checkout checks. |
| `src/pheasant/persistence/heartbeat.py` | Preserve the dedicated non-pooled autocommit connection, bounded statements, reset/reconnect and warnings. |
| `src/pheasant/deployment/wake_gate.py` | Replace its direct `psycopg.connect` probe while preserving a small, bounded database wake check. |
| `src/pheasant/services/errors.py` | Replace concrete driver/pool exception handling with normalized database error classification. |
| `src/pheasant/api/app.py`, `src/pheasant/mcp_server/server.py` | Validate registrations and translation of normalized transient database failures through both transports. |
| `src/pheasant/search/pg_textsearch.py`, `persistence/lexical_index.py`, `search/sqlite_store.py` | Preserve BM25 query expression, ordering, index selection, candidate expansion and existing deliberate fallback conditions. |
| `src/pheasant/persistence/state_store.py`, `graph_rows.py`, `sync/log_queue.py`, other backend callers | Validate batching and transaction semantics through the existing facade; avoid a broad domain-layer SQL rewrite. |
| `scripts/benchmark_mutations.py` | Replace its direct Psycopg connection and error handling, retaining before/after comparability. |
| `src/pheasant/analytics.py`, `cli.py`, `services/inventory.py` | Review old-driver-specific comments/diagnostics and any assumptions they document. |

Proposed integration module: `src/pheasant/persistence/pg_driver.py` for DSN
normalization, connection setup, result metadata, SQLSTATE extraction and
driver/pool error normalization. Keep it below services and independent of
HTTP/MCP transports. Do not create a compatibility package named `psycopg`.

Direct Psycopg use in tests also occurs in `test_backend_parity.py`,
`test_backend_differential.py`, `test_graph_backends.py`,
`test_assistant_inventory.py`, `test_inventory_relations.py`,
`test_graph_query_service.py`, `test_heartbeats.py`, `test_index_queue.py`,
`test_log_queue.py`, `test_pg_textsearch.py`, `test_postgres_idle_connections.py`,
`test_search_scope.py`, `test_source_browser.py`, `test_source_health.py`,
`test_state_store_batching.py` and `test_sync_wait.py`. Replace setup and admin
connections as well as exception assertions. Tests must run with Psycopg
absent, rather than disappearing through `importorskip("psycopg")`.

## 3. PDF and EPUB parity contract

### 3.1 Current features that must survive

| ID | Current behavior | Acceptance evidence |
| --- | --- | --- |
| PDF-01 | A PDF can be counted and read from in-memory bytes; ranges use `[first, stop)` and stop at the document end. | Empty/boundary ranges, a whole document and concatenated ranges give the expected ordered pages. |
| PDF-02 | Native extraction handles CID/Type0 fonts, Unicode mappings and complex encodings better than the builtin fallback. | Golden prose fixtures and a held-out corpus cover CJK, ligatures, rotated text, multi-column pages and missing/broken mappings. Report failures by category. |
| PDF-03 | Every page has stable text; local extraction and both worker transports use the same reader. | Whole-document text equals concatenated range text byte-for-byte for the same new reader, settings and build. |
| PDF-04 | `auto` tries native text and retains builtin recovery when useful; a corrupt or unreadable file does not terminate a source sync. | Native failure, empty output, encrypted/corrupt files and fallback paths remain observable and tested. Do not turn every parser error into a successful zero-chunk file. |
| PDF-05 | Authored `.extract.txt` sidecars override extraction. | Existing sidecar fixtures and incremental sync checks pass. A reader change must not alter the sidecar's authored text. |
| PDF-06 | `math: latex` recognizes inline/display equations and equation numbers. | Existing exact TeX assertions pass for all supported providers and committed typesetter fixtures. |
| PDF-07 | Mathematics reads font identity, size, character boxes, origin/baseline, order, line association, italic flags and thin horizontal strokes/fills. | A native adapter produces correctly normalized `Glyph` and `Rule` inputs, including crop/rotation transforms and nested page objects where relevant. |
| PDF-08 | Legacy TeX encodings, CMEX extension glyphs, OpenSymbol and BaKoMa codes recover mathematical symbols. | `tex_article.pdf`, `office_equations.pdf`, `bakoma_equations.pdf` and low-level symbol tests pass unchanged in meaning. Mapped Unicode alone is insufficient evidence. |
| PDF-09 | Math-on does not change prose-only documents; math-off remains a normal text reader. | Within the new implementation, prose text with math on/off is byte-identical; equation insertion changes only the intended spans. |
| PDF-10 | Fractions, radicals and indices, scripts, limits, matrices/cases, accents, big operators and delimiters are reconstructed deterministically. | `test_math_reading.py`, `test_document_math.py`, `test_document_extraction.py`, `test_answer_math.py` retain their assertions. |
| PDF-11 | Native operations are serialized within a process; multiple worker processes provide parallelism. | Stress concurrent calls with a process-wide PDFium lock and deterministic cleanup. No simultaneous unprotected PDFium call is permitted. |
| PDF-12 | Builtin and WASM-sandboxed providers stay independent of a host native reader. | Their fixture output and guest/reference token parity remain intact; sandboxed requests never route through PDFium. |
| PDF-13 | Text and decompression safeguards exist in the builtin path. | Preserve existing bounds. Review native allocations separately; builtin limits are not a claim that all native-engine memory is bounded. |
| EPUB-01 | EPUB produces text with book/spine ordering and has a recovery path when normal metadata is broken. | Existing `primer.epub` plus multi-spine, nested-path, missing-manifest, missing-spine, empty and malformed archive fixtures; no silent loss of previously readable chapters. |

### 3.2 Native adapter requirements

Retain the callable interfaces `pdf_page_count`, `pdf_page_texts`,
`pdf_page_math_texts` and `pdf_reader_version`, or update all internal callers
atomically while preserving their behavior. Keep external configuration values
stable; `native` describes the selected native reader rather than a vendor.

The new adapter must explicitly handle:

1. Bytes lifetime, document/page/text-page ownership, and explicit closure of
   native resources on success, exceptions, early return and cancellation.
2. Newline and Unicode normalization, generated spaces, inserted characters,
   reading order, and page-coordinate conversion. PDFium's character sequence
   is not automatically the old reader's block/line sequence.
3. Font metadata and raw character-code recovery. Confirm which stable public
   PDFium APIs expose the needed information; document any use of lower-level
   bindings. Do not assume a Unicode code point is the original font code.
4. Rule extraction from filled rectangles and stroked paths, including page
   transforms; preserve the equation engine's existing width/height thresholds.
5. An honest capability failure when the candidate cannot recover a required
   feature. Use the established builtin recovery path or a reviewed additional
   reader; no hidden PyMuPDF fallback.

PDFium requires synchronization even across different documents in one
process. Preserve the process-level lock instead of claiming thread parallelism.
See [upstream threading and resource guidance](https://pypdfium2.readthedocs.io/en/stable/python_api.html).

Do not add page rendering or OCR to solve text extraction unless it becomes a
separately approved product requirement. Existing scanned-PDF sidecar behavior
is part of parity; turning scans into automatically recognized text is not.

### 3.3 Reader identity and stored extraction migration

There are two different identities to maintain:

| Identity | Required content and effect |
| --- | --- |
| Worker compatibility identity | Engine name, wrapper version, native PDFium build identity, adapter revision, math revision when enabled, and any secondary-reader identity. Indexer and worker must match before accepting page results. |
| Stored-text implementation identity | A deliberate extraction-pipeline revision scoped to sources that can carry affected documents. Change it when an implementation change can alter text; persist only after a complete successful pass. |

`sync/fingerprint.py` currently records the math revision for document-capable
sources, but not a native reader replacement. Installing a new parser without
changing configuration would otherwise leave old text indexed indefinitely.

Implementation requirements:

- Make PDF and EPUB pipeline identity explicit even with mathematics off.
  Unrelated code-only sources must keep their fingerprint.
- Scope eligibility conservatively using the source's admitted content,
  connector behavior and content types. Include web URLs without `.pdf`
  suffixes and archive members; do not rely only on filename globs.
- Use a deliberate adapter/pipeline revision rather than causing every harmless
  package patch to reprocess all sources. An unreviewed engine upgrade that
  changes output must nevertheless trigger a revision change.
- Initially use the existing full-source-pass mechanism for affected mixed
  sources. Document its cost; a later per-document optimization needs its own
  correctness proof. Do not claim only PDFs are reread if the mechanism walks
  an entire mixed source.
- On changed text, replace chunks and retract old graph edges, lexical rows,
  embeddings and obsolete vector entries through existing code. Retain artifact
  IDs and historical provenance; handle saved citations to older chunks using
  the established history/absence behavior.
- Interrupted or cancelled passes must resume/retry without recording migration
  success prematurely. The next unchanged sync after success does no work.
- Never reinterpret a worker mismatch as an empty successful extraction. Keep
  the existing explicit rejection/local recovery semantics and test both
  HTTP and gRPC paths.

Exact incidental whitespace equality between PyMuPDF and PDFium is not assumed.
Every output change must be classified as equivalent formatting, an approved
correction, or a regression. Retain the current exact equation goldens and
provenance obligations; do not normalize away meaningful math differences.

## 4. PostgreSQL driver and pooling contract

### 4.1 Connection and query semantics

| ID | Required parity | Implementation / evidence |
| --- | --- | --- |
| PG-01 | Existing PostgreSQL URLs and credentials continue to work. | A centralized connection factory parses escaped credentials, IPv6, ports, database names and supported query options. Inventory libpq-specific DSNs/options before removal; unsupported forms must be explicitly migrated or rejected with actionable diagnostics. |
| PG-02 | TLS verification and supported authentication stay intact. | Validate local password/SCRAM and deployed hosted-server TLS/CA/hostname settings. Do not silently convert `verify-full` to unverified encryption. Redact DSNs and passwords in errors. |
| PG-03 | Positional and named bindings, nulls and literal SQL characters retain meaning. | Test `?` translation, named parameters, `::` casts, apostrophes, percent signs, LIKE patterns and empty-parameter statements against the chosen `pg8000` parameter style. |
| PG-04 | Result rows support `row["column"]` and `row[0]`, with the current duplicate-name behavior. | Adapt DB-API description tuples instead of relying on Psycopg's `column.name`; exercise zero-row results and statements with no result set. |
| PG-05 | `rowcount` and `RETURNING` drive the same mutation decisions. | Compare update/delete/upsert, lease claims, queue claims and duplicate-key handling. Preserve affected-row counts even when there are no returned columns. |
| PG-06 | Multiple writes for an artifact remain one transaction. | Pin one connection through the unit of work, retain explicit commit/rollback and nested facade scope behavior. Failure between delete and reinsert leaves the previous committed state intact. |
| PG-07 | Reads inside pending work see their own writes; ordinary serving reads release promptly. | Preserve `_pending`, `reads_from_any_thread`, the per-thread facade and autocommit for standalone reads. Do not spread one transaction across pooled connections. |
| PG-08 | Ordinary standalone reads avoid unnecessary `BEGIN`/`ROLLBACK` trips. | Measure server statements and network trips; retain the present optimization and restore transactional mode before reuse. |
| PG-09 | Schema DDL is protected by a session advisory lock. | The same checked-out session must hold `pg_advisory_lock` across internal commits until unlock. A transaction lock is not equivalent. |
| PG-10 | Array/type adaptation and database values preserve meaning. | Exercise `ANY` array bindings, empty/nonempty ID sets, Unicode, binary content, JSON serialized values, booleans, timestamps and large integers wherever actually used. |
| PG-11 | Multi-statement migration scripts keep their transaction and error behavior. | Prove execution through the selected DB-API interface; do not split scripts naively on semicolons. |
| PG-12 | `executemany` still provides efficient batched persistence. | Benchmark artifact chunks, enrichment, graph rows and log batches at local and hosted latencies. If necessary use bounded multi-row parameterized statements or staging/COPY where existing semantics permit. |

Keep `StateBackend`, `_PgConnection` and `StateStore` as the domain integration
boundaries. SQLAlchemy is introduced for pooling, not as a reason to convert
domain queries or schema definitions to an ORM. Existing `postgres` extras and
SQLite installations must not require a PostgreSQL server at import time.

Audit SQL placeholder assumptions in both translated domain SQL and direct
driver calls such as timeout setup and advisory locking. A module-global
`pg8000` paramstyle must be selected once and remain consistent across threads.

### 4.2 Pooling, deadlines and failure behavior

| ID | Current leveraged feature | Required parity |
| --- | --- | --- |
| POOL-01 | Pool minimum 1, bounded maximum from configuration; the limit is per process. | Preserve the maximum server-connection budget. For `QueuePool`, configure overflow explicitly; its defaults must not increase the connection cap. Lazy warmup may replace minimum-one behavior only if documented and measured. |
| POOL-02 | Checkout timeout respects the caller's remaining request budget. | Enforce the remaining deadline for each checkout, including contention and new connection creation. Do not mutate a shared timeout per request. Select a pooling integration that can meet this or reject the candidate. |
| POOL-03 | Idle connections are checked after 30 seconds; active reuse avoids a ping each time. | Preserve conditional checkout validation, or benchmark a justified alternative against the same round-trip budget. |
| POOL-04 | TCP keepalives currently use idle 60 s / interval 10 s / count 3 where supported. | Translate into supported socket settings rather than passing libpq kwargs to `pg8000`. Document OS differences and verify the deployed hosted path. |
| POOL-05 | A failed statement rolls back and returns/discards the connection instead of poisoning the thread. | Test expected FTS failure followed by fallback, constraint failures followed by later work, reset failure, and pool recovery. |
| POOL-06 | A disconnected standalone read is retried once when nothing is pending. | Normalize driver network failures and SQLSTATE classes `08`/`57P` carefully. Do not retry constraints, syntax errors, cancellation `57014`, writes, or a transaction with pending work. |
| POOL-07 | A deadline applies a per-query `statement_timeout`, then clears it. | Preserve cleanup on success and failure. Discard a connection whose timeout/session state cannot be restored; prevent timeout leakage to the next borrower. |
| POOL-08 | Pool wait, reads and SQL operations have timings/spans. | Preserve existing telemetry names/meaning, especially `postgres_pool_wait` and `postgres_read`, and application names visible in `pg_stat_activity`. |
| POOL-09 | Connections are released at read/commit boundaries and shutdown. | Concurrent requests beyond the pool size complete or time out predictably; no idle-in-transaction growth, connection leaks, or inherited live pools after process creation. |

A server-side statement timeout alone does not bound a network blackhole.
Connect, socket I/O, pool checkout, and statement execution must each have
explicit deadline behavior. Demonstrate cancellation/timeout cleanup with real
sockets and a real server, not only exception mocks.

### 4.3 Heartbeats, wake checks and transport errors

- Keep `HeartbeatWriter` outside the ordinary pool, in autocommit, with its
  own connection. Preserve the 10-second statement bound, reconnect after
  failure, first/every-sixth-failure warnings, and recovery indication.
  Saturating the query pool must not stop proof of life.
- Preserve the wake gate's bounded database connection probe and readiness
  behavior. Its direct connection must not instantiate the full query pool.
- Centralize SQLSTATE and connectivity extraction. `pg8000` exceptions may
  carry server fields differently from Psycopg; broad exception-name matching
  is insufficient.
- Normalize pool exhaustion and retryable database disconnection into the
  same service error codes and retryability as today. Preserve HTTP status and
  MCP error semantics. Constraint violations and programmer errors remain
  permanent failures rather than automatic retries.
- Replace all direct test/admin/script connections. Verify operations with
  `psycopg`, `psycopg-binary` and `psycopg-pool` entirely absent.

### 4.4 Batching performance is a selection gate

The current persistence path deliberately batches rows under the single
writer's commit mutex. `test_state_store_batching.py` documents a historical
30-chunk/40-term/8-symbol artifact improvement from 57 ms to 25 ms on loopback.
Those comments are historical context, not a newly measured baseline.

A DB-API method with the same name does not imply equivalent pipelining.
Measure network round trips and wall time for the new driver. If one
`executemany` becomes one network exchange per row, implement an equivalent
batch strategy or reject the choice. Any optimization must preserve parameter
binding, order-dependent IDs, duplicate handling, error atomicity, row counts,
and PostgreSQL's parameter limits. Do not expand values into SQL strings.

## 5. Retained `pg_textsearch` contract

| ID | Requirement |
| --- | --- |
| BM25-01 | Install and preload the pinned extension on PostgreSQL 17; enable it per database. Preserve startup refusal when the configured extension cannot be provided. |
| BM25-02 | Preserve the weighted document expression and repetition-count validation in `persistence/lexical_index.py`; index and query must spell the same expression. |
| BM25-03 | Preserve `text_config='simple'`, score sign/mapping, structural priors, configured weights, and whole-region corpus statistics for source-scoped queries. |
| BM25-04 | Preserve the fenced ordered index scan, candidate floor/factor, selective-filter widening, ceiling and existing fallback to the unfenced filtered plan. |
| BM25-05 | Preserve the separate SELECT/ORDER BY expression spellings that allow planner score reuse. Validate the actual plan and returned scores under `pg8000`; driver changes must not accidentally force generic-plan behavior that loses the optimization. |
| BM25-06 | Preserve source/folder/section scope, access/artifact policy, exclusions, and archived/deleted document filtering. |
| BM25-07 | Edits and deletions update the index incrementally; no corpus-wide lexical rebuild on each sync. |
| BM25-08 | Keep deliberate `tsvector` behavior for a changed weight bundle or runtime query failure, with existing diagnostics. Such fallback is not evidence that the required BM25 deployment passed. |

Use `tests/test_pg_textsearch.py` to verify index selection, score reuse,
fenced/unfenced result equivalence, weights, deletion/update behavior and
missing-extension refusal. Record `EXPLAIN (ANALYZE, BUFFERS)` on realistic
corpus sizes; tiny-table sequential scans alone are not a planner regression.
Record fallback counts alongside every performance result so degraded search
cannot masquerade as a successful benchmark.

## 6. Database image and release integration

### 6.1 Image contract

The new image must:

1. Use a pinned PostgreSQL 17 distribution and a recorded image digest;
   compile/install `pg_textsearch` against that distribution's headers and
   libraries, or use a matching verified upstream binary.
2. Pin the extension release/commit and source checksum. Separate build tools
   from the runtime where practical. Record build commands, compiler/base
   versions and supported CPU architectures.
3. Set effective `shared_preload_libraries` to include `pg_textsearch`, preserving
   explicitly configured compatible libraries rather than overwriting them
   accidentally. Prove the setting on a newly initialized cluster and restart.
4. Supply `pg_dump`, `pg_restore`, `pg_isready`, and the shell/archive/checksum/
   networking utilities actually used by `backup.sh` and `restore.sh`, or move
   those scripts to a separately specified compatible tooling image.
5. Define the data directory, UID/GID, healthcheck, initialization, locale and
   encoding behavior. Keep local database ports private as today.
6. Contain no TimescaleDB extension/toolkit payload as an accidental dependency.
   Inspect installed packages/files and `pg_available_extensions`; merely not
   issuing `CREATE EXTENSION timescaledb` is insufficient packaging evidence.
7. Preserve the default and fleet resource budgets, shutdown semantics,
   password/secret handling, and explicit no-reset behavior.

### 6.2 Build and CI bootstrap

GitHub Actions service containers are created before ordinary job steps. A
service image cannot be built in an earlier step of that same job and then
used retrospectively. Choose and test one bootstrap strategy:

- A prerequisite image-build job publishes an immutable candidate digest,
  which downstream integration jobs consume through a supported workflow
  mechanism; or
- Integration jobs build/start the candidate database through Docker/Compose
  steps after checkout, replacing service-container setup while keeping the
  same readiness, DSN and test requirements.

The final workflow must work for forks and pull requests without requiring
publication credentials. Keep the main backend parity job unfiltered and set
`PHEASANT_REQUIRE_PG_TEXTSEARCH=1`. Preserve the agent-installed and
agent-absent Python test legs described in `CLAUDE.md`; do not trade away
unrelated coverage to add this image.

Publication gates include `linux/amd64` and `linux/arm64` where the application
claims support, clean-volume startup, restart, restored-volume startup, backup
round trip, extension presence, and provenance/license artifacts. Test the
actual default Compose topology and the universal application image, not just
an isolated SQL container.

## 7. Performance and quality acceptance

### 7.1 Measurement protocol

All numerical limits in this section are proposed release gates, not current
measurements. Record the old implementation's values before changing it.
Threshold changes require a written product/engineering decision and updated
specification; a failed candidate does not lower the gate automatically.

Define four separately reproducible comparisons:

| Comparison | Hold constant | Change |
| --- | --- | --- |
| Image only | Application, driver, parser, PostgreSQL major, extension version, corpus and server settings | Timescale image versus new PostgreSQL image |
| Driver only | Database image, database snapshot, query set, extension and parser | Psycopg/pool versus `pg8000`/new pool |
| Parser only | Input bytes, extraction/math settings, chunking and host | PyMuPDF versus new PDF/EPUB readers |
| Complete stack | Corpus, models/stubs, topology, resource limits and workload | Old released stack versus all replacements |

Use isolated test copies and retain the old environment only as a benchmark
reference; it must not remain in final runtime or test dependency resolution.
Record repository SHA, image digests, package/native versions, hardware, kernel,
filesystem/volume type, memory/CPU limits, connection limits, locale, source
hashes, concurrency, network RTT and cold/warm state. Do not compare runs that
also change the extension, model, ranking parameters or corpus.

After warmup, collect at least five independent measured runs, alternate
baseline/candidate order, and report median, p95, sample counts and variation.
For request percentiles, collect at least 1,000 operations per workload cell;
long-document jobs may use fewer completed jobs with that limitation stated.
Retain raw machine-readable results, slow-query plans and errors. If variance
prevents a conclusion at a gate, repeat under controlled conditions rather
than marking it passed.

### 7.2 Required workload matrix

| Workload | Coverage |
| --- | --- |
| Lexical search | Unfiltered, source/folder scoped, exclusions/artifact policy, selective filters, top-k depths, zero-hit, warm/cold cache; at least 50k and 250k chunks. |
| Persistence | 1/30/300-chunk artifacts, graph/enrichment and log batches; small/large updates, deletion and rollback; loopback plus controlled 10 ms and 40 ms RTT. |
| Concurrency | 1 caller, configured pool limit, and at least twice that limit; concurrent API, graph, writer and logger activity. |
| PDFs | Committed fixtures plus a frozen held-out corpus of at least 100 documents across prose/layout/font/error categories; include a representative PDF exceeding the configured page-split threshold. |
| Mathematics | All committed typesetter fixtures, dense equations and prose-only documents; math on/off; local, HTTP and gRPC workers. |
| EPUB | Ordered and broken metadata cases, Unicode, large books and empty/malformed archives. |
| End-to-end sync | Fresh ingest, changed files, unchanged second sync, cancelled/resumed sync, and parser-change reprocessing using deterministic model stubs. |
| Failure/soak | Database restart, stale connections, pool exhaustion, network blackhole, worker mismatch, native failures, repeated resource cleanup and backup/restore. |

### 7.3 Gates

Let `B` be the baseline and `N` the replacement value under the same workload.
A higher-is-better throughput gate uses `N/B`; a latency/memory gate uses
`N <= ...`. Apply the gates per significant workload category, not only as a
single average that hides a bad category.

| ID | Metric | Required result |
| --- | --- | --- |
| PERF-01 | Warm BM25 p95 response latency | `N <= max(1.10 * B, B + 2 ms)`; actual expected BM25 plan and scores retained. |
| PERF-02 | Full-stack sustained retrieval throughput | `N/B >= 0.95`, with no increase in errors, deadline failures or unintended fallback. |
| PERF-03 | Batched artifact/enrichment commit p95 | `N <= max(1.10 * B, B + 2 ms)` at each measured RTT and batch size. No per-row round-trip regression hidden by loopback. |
| PERF-04 | PDF extraction throughput, math off and on | `N/B >= 0.90` separately for each; end-to-end ingest `N/B >= 0.95`. Report fallback frequency and long-document range scaling. |
| PERF-05 | EPUB extraction throughput | `N/B >= 0.90`; previously readable chapters must survive. |
| PERF-06 | Peak RSS for like-for-like PDF workers and query services | `N <= max(1.10 * B, B + 32 MiB)` and always within existing configured limits. |
| PERF-07 | Repeated extraction and connection lifecycle | No monotonic retained-memory/handle/connection growth over a documented soak of at least 1,000 extractions and 10,000 DB operations after warmup. Explain allocator plateaus separately from leaks. |
| PERF-08 | Idle query connections | Count stays bounded by per-process pool settings; heartbeats/probes are accounted for separately; no idle-in-transaction accumulation. |
| PERF-09 | Unchanged second sync after migration | Zero unnecessary content re-extraction/re-embedding and no graph/count drift. |
| PERF-10 | Cold startup, restore, BM25 build and image footprint | Measure all. Startup/build/restore duration must fit the existing deployment health/readiness bounds and the declared migration window; record an approved quantitative budget before cutover. |
| QUALITY-01 | Committed exact math goldens | All pass; no new skip, xfail, assertion weakening or automatic conversion of failed math to prose. |
| QUALITY-02 | Held-out prose transcription | Character error rate against reviewed reference text no worse than baseline + 0.5 percentage points in each script/layout category; zero missing required passages or pages. Record normalization rules before scoring. |
| QUALITY-03 | Retrieval over identical indexed text | Expected result IDs/order/scores match within a fixed numerical tolerance, preserving established tie handling; existing gold-set assertions pass. |
| QUALITY-04 | Retrieval after changed extraction | All committed grounding/citation tests pass; held-out Recall@10 and MRR@10 fall by no more than 1 percentage point absolute. Every lost gold passage is investigated and cannot be dismissed by the aggregate. |

These gates deliberately make the native parser and pure-Python driver prove
their suitability. No unmeasured assertion that they are faster, smaller or
equally faithful is sufficient.

## 8. Migration and rollback criteria

### 8.1 Preflight and compatibility record

Create a migration record containing old/new application and database image
digests, PostgreSQL versions, `pg_extension` names/versions, effective preload
libraries, database encoding/collation, ownership/roles/grants, row counts and
logical checksums, graph/vector locations, parser identities, volume names,
source checkpoints, expected reprocessing work, and rollback boundary.
Keep secret values outside this record.

Inspect actual deployments; do not infer that an unused-by-Pheasant extension
is absent from the database. Inventory Timescale extensions, hypertables and
other nonstandard objects. A database with user-created Timescale-dependent
objects requires a separate conversion plan. It must not be migrated by
filtering out extension declarations while leaving dependent objects behind.

Before cutover, rehearse on a restored copy. Preserve the same `kb_id`, project
identity, source IDs, filesystem mounts and stable paths. An existing
`pheasant-lab` stack must not silently become a new project with empty volumes.

### 8.2 Database image migration

1. Build and validate the new image with the exact intended extension version.
   Verify required collation/locale compatibility; decide explicitly whether
   affected derived indexes require rebuilds.
2. Take a verified backup and prove that it restores. Include PostgreSQL and
   operational state: `/state`, `/memory`, `/exports` as required by the
   deployment, connection-vault key material, worker/ingestion/graph/API
   secrets, and source workspace/checkpoints where required. The current
   backup scripts do not imply all of these are automatically covered; list
   every volume and its recovery treatment explicitly.
3. Quiesce all writers, not only the indexer: API operations that mutate
   configuration/accounts/artifacts, logger/drainers, scheduled jobs,
   administrative clients and sync workers must not create post-snapshot
   state. Drain or freeze NATS queues and record consumer/ack state. Disable
   submissions during the maintenance window.
4. Take the final consistent database and volume snapshot after quiescence.
   Use a compatible `pg_dump`/`pg_restore`; do not copy live PostgreSQL data
   files into the new image or reuse an unverified data directory in place.
5. Restore into a separate new target volume/database. Enable `pg_textsearch`
   in an order compatible with restore, restore roles/grants as needed, and
   recreate derived indexes deliberately when necessary. Do not grant the
   ordinary application role permanent superuser merely to simplify setup.
6. Validate schema version, row counts/checksums, artifacts and historical
   records, source/queue state, graph rows, vectors, sealed snapshots,
   credential decryption and mounted paths. Run `ANALYZE` and verify BM25
   index plans and gold queries on the restored corpus.
7. Start exactly one intended writer and the remaining roles against the new
   target. Check service readiness, bounded pools, independent heartbeats,
   retrieval, sync progress, cancellation, upload receipts and observation
   drain. Queue recovery must not lose acknowledged work or replay
   non-idempotent operations without reconciliation.
8. Admit traffic only after the acceptance record is complete. Preserve the
   old database and all original volumes for the declared rollback period;
   never run `down -v` or delete them as part of migration.

Keep image migration, driver replacement and extraction reprocessing as
separate rollout stages where possible. A database image or driver change
alone must not trigger source re-extraction or embedding-model invalidation.

### 8.3 Parser migration

Deploy matched indexer/worker parser builds together. An old/new mixed fleet
must reject mismatched range results explicitly. Stage the reprocessing
backlog and estimate CPU, disk, embedding requests/cost and availability from
the frozen corpus before enabling automatic sync.

Use the scoped implementation fingerprint from section 3.3 to trigger the
existing full-source-pass mechanism. Capture before/after chunk and graph
counts, missing/empty documents, extraction warnings, passage quality and
citations. Regenerate embeddings only where changed text requires it; retain
the selected embedding space. If the existing pipeline re-embeds a wider set,
measure and document that cost before scheduling the pass.

Do not mark a source migrated until its complete successful pass records the
new fingerprint. Verify cancellation, interruption and retry, then prove an
unchanged second sync is a no-op. Never treat old chunk references as current
without checking whether retraction replaced them.

### 8.4 Rollback boundaries

| Point | Safe rollback behavior |
| --- | --- |
| Before new target admits writes | Stop the candidate stack and switch back to the preserved old application/database/state snapshot. Verify original project/volume identity. |
| Driver-only deployment, schema/data unchanged | Revert application image/config after draining work; confirm no introduced serialization/schema difference. |
| After new target accepts writes | Do not point clients at the stale original database. Quiesce again and preserve/reconcile accepted writes through a tested reverse restore or recovery plan. Declare the recovery point and downtime explicitly. |
| After parser reprocessing starts | Preserve the pre-migration full state snapshot. Reverting binaries alone does not undo changed chunks/vectors or source fingerprints. Restore a consistent snapshot or run a deliberate reverse extraction migration and reconcile writes. |

For each production cutover, declare a maintenance window, rollback retention,
RPO and RTO based on rehearsal. Target RPO is zero for acknowledged writes at
the quiesced cutover. Do not promise an RTO without measured restore and index
build times. Health failures, data/count mismatches, failed math goldens,
unexpected fallback, pool leakage, missing secrets or performance-gate failure
block traffic admission or trigger the documented rollback decision.

## 9. Verification and release evidence

### 9.1 Existing coverage to retain

| Area | Existing test modules / evidence |
| --- | --- |
| Extraction, format coverage and math | `test_document_extraction.py`, `test_document_math.py`, `test_math_reading.py`, `test_answer_math.py`, `test_office_extraction.py` |
| Page ranges and worker compatibility | `test_pdf_split.py`, `test_pdf_ranges_and_agent_connect.py`, `test_fleet.py` |
| Idempotency, source behavior and citations | `test_sync_idempotency.py`, `test_source_browser.py`, `test_source_health.py`, `test_answer_grounding.py`; existing fingerprint coverage plus new reader-change cases |
| Database/graph semantics | `test_backend_parity.py`, `test_backend_differential.py`, `test_graph_backends.py`, `test_state_store_batching.py` |
| Pooling and long-running processes | `test_postgres_idle_connections.py`, `test_heartbeats.py`, `test_graph_query_service.py`, `test_sync_wait.py` |
| Queues and lexical retrieval | `test_index_queue.py`, `test_log_queue.py`, `test_pg_textsearch.py`, `test_search_scope.py` |
| Transport, packaging and deployment | `test_surface_conformance.py`, `test_service_layering.py`, `test_fleet_manifests.py`, `test_workflow_coverage.py`, `test_flagship_defaults.py`, `test_fresh_ui_compose.py`, `test_config_surface_freshness.py` |

Use the canonical commands from `CLAUDE.md`: the full offline pytest suite,
Ruff check/format verification, and `mkdocs build --strict`. Run the real
PostgreSQL suite with `PHEASANT_TEST_POSTGRES_DSN` pointing only at an isolated
throwaway database and `PHEASANT_REQUIRE_PG_TEXTSEARCH=1`. Existing tests reset
schemas; they must never target a production region. Build the actual images
and run actual NATS/gRPC/worker topology checks for changed integration paths.

New tests should cover behavior that the replacement risks: parser identity
invalidation, raw math encodings, pool deadlines, TLS/DSN mapping, normalized
errors, blackholed sockets, batch round trips, absence of removed packages,
new-image restoration, and rollback state consistency. Do not replace real
backend testing with method-call mocks.

### 9.2 Dependency and license evidence

For wheel, application image, database image and build/test environments:

- Resolve and record direct, transitive, native and operating-system
  dependencies, exact versions, hashes, licenses and provenance in an SBOM.
- Check absence of `pymupdf`/MuPDF, `psycopg`/binary/pool and TimescaleDB/toolkit
  runtime payloads. Historical source text and explicitly retained baseline
  benchmark artifacts are not runtime payloads.
- Include PDFium's bundled third-party license notices, not only the Python
  wrapper's license. Inspect any secondary PDF reader and fixture generator.
- Review optional extras and platform markers; a permissive direct dependency
  is not proof of every resolved transitive license. Record any unexpected
  reintroduction of a removed dependency and resolve it before release.
- Add a focused manifest/package check preventing the removed application
  dependencies and Timescale image from returning through supported paths.
  Do not confuse a blanket text grep with package-level license verification.

### 9.3 Required delivery artifacts

1. Final dependency decisions with pinned versions, supported platforms and
   rejected alternatives with evidence.
2. File-change inventory mapped to requirement IDs in this specification.
3. Correctness results, real-backend runs, planner evidence and raw benchmark
   output, including quality/fallback/error breakdowns.
4. New application/database image digests, build provenance, SBOMs and notices.
5. A documented operator migration and rollback runbook with rehearsal results,
   measured downtime/RPO/RTO, exact volume mapping and data verification.
6. Parser-change and worker-compatibility migration evidence, plus a verified
   no-op second sync.
7. Updated setup/configuration/help/deployment/ingestion documentation and
   release notes explaining affected installations and reprocessing cost.

## 10. Implementation sequence and completion gates

| Stage | Deliverable | Exit criteria |
| --- | --- | --- |
| A. Baseline and feasibility | Freeze inputs and baselines; prototype PDF glyph/rule adapter, driver batching/pool deadlines and clean PostgreSQL image. | Candidate can meet exact math, transaction, extension and security contracts; initial performance data supports proceeding. Resolve gaps before broad refactoring. |
| B. Database image | Build/publish candidate image; update deployment/CI tooling; rehearse a real restored database. | BM25 plans/scores retained; image-only gates pass; backup/restore and safe rollback proven; fork/PR CI works. |
| C. Driver and pool | Introduce adapter and replace all driver consumers/tests/scripts. | Psycopg absent; full real PostgreSQL semantics, failure behavior, connection budgets and driver-only performance gates pass. |
| D. PDF/EPUB | Replace native extraction and fixture generation; implement parser identity and scoped invalidation. | PyMuPDF absent; exact math, EPUB recovery, local/range equality, worker mismatch and parser performance/quality gates pass. |
| E. Integrated release | Validate universal image, standard stack and supported alternatives; complete notices and operator documentation. | Full-stack gates pass; restored production-like rehearsal and parser reprocessing complete; no required check skipped. |
| F. Deployment | Apply the approved operational runbook to explicitly identified targets. | Traffic admission, observation window and rollback-retention milestones satisfied with recorded evidence. |

Stages B, C and D should remain independently reviewable. Temporary comparison
environments may retain old dependencies, but the accepted final release must
not install them. Do not retain the old implementation as an undocumented
production fallback to make acceptance tests pass.

### Open decisions that must close before implementation acceptance

- Can PDFium expose sufficient original font-code and baseline information for
  all existing TeX goldens? If not, which deterministic supplemental path
  supplies it, and what are its throughput and licensing costs?
- Does `pg8000` meet hosted-latency batching targets, and which bounded batch
  mechanism is used when simple `executemany` does not?
- Which SQLAlchemy pooling integration satisfies per-request checkout
  deadlines, conditional idle validation and reliable invalidation without
  private API dependencies?
- Which currently deployed DSN/authentication/TLS forms must be supported?
- What exact PostgreSQL/base/extension versions and registry digests are
  selected, and how does CI test unpublished database-image changes?
- Which deployed databases have extra Timescale-dependent objects, collation
  differences, external writers or source volumes outside the standard backup?
- What are the measured reprocessing backlog, embedding impact, migration
  window, rollback retention and recovery targets for each rollout?

The migration is complete only when these decisions have evidence, every
required gate passes, all supported packaging/deployment paths use the new
stack, and `pg_textsearch` remains the verified PostgreSQL lexical backend.
