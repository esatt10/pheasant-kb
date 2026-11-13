# Azure artifact transition validation

Validated on the existing Azure Container Apps deployment on 2026-11-12/13 UTC.
The public endpoint is
<https://pheasant-kb.redcoast-aa021187.westus3.azurecontainerapps.io>.

## Deployment and data

The API, indexer, graph service, logger and worker use one immutable image:
`sha256:93008a2c81ff1a7d98daee359bfb5286dcfad3d6af8d6c2e004293d3d90da05a`.
Application images were updated while preserving deployment configuration,
secrets, persistent mounts, resource limits and scaling rules. The existing
local Docker fleet was not changed by this Azure rollout.

A custom-format PostgreSQL backup was made before migration and checked with
`pg_restore --list`. It is retained on the existing persistent storage at
`/home/postgres/pgdata/artifact-transition-backup-20261112-194422.dump`.
Its SHA-256 is
`a396453415e2ea2a351b2a73c529c79836d202b4b036b59d0eb4b7bed742b4db`.
A read-only comparison against that backup found all 29,294 original indexed
artifact IDs preserved. During the checks, normal source synchronization
refreshed 50 repository files and 15 web pages and discovered 22 new repository
files. All 87 new/refreshed hashes were verified against repository bytes or
the captured HTML snapshots; the remaining original hashes were unchanged.

## Fixes found by testing Azure

- Existing PostgreSQL regions could skip the new tables under an old schema
  marker. Schema version 6 and explicit table/column guards now force the
  additive upgrade before source adoption.
- Automatic chunk planning suppressed typed-record headings. Records now
  expose their taxonomy outline while retaining their chunk content/IDs.
  Qualified taxonomy requests filter and limit rows before decoding them.
- MCP retrieval metadata scanned the entire graph to count artifact links.
  Coverage now reads the records' own outgoing edges, shared by both transports.
- A 2.3 MB production configuration froze the settings editor's quadratic diff.
  Common context is trimmed, large changes use bounded memory, and the preview
  displays nearby context. Tests cover 40,000 lines within a 128 MB heap.
- Citation clicks now open the qualified file in the taxonomy view, including
  on phones. Fragment links stay within sandboxed documents in WebKit.
- Published artifact responses expose their immutable revision and status.
  Deleting operational history leaves those versions inspectable.
- A read-only NFS mount could report an error after successful durable job
  deletion. Redundant snapshot cleanup now defers to the writable indexer.
  Saving a configured base also refreshes the UI's available bundles.
- Retiring a typed record now installs the archive visibility barrier and
  closes its current validity before asynchronous reclamation. Each cleanup
  task has a fresh broker ID, so a preceding sync cannot suppress it.
  Directory groups preserve each artifact's actual source and scope path.

## Functional coverage

The live checks exercise canonical artifact records, deprecated HTTP/MCP
adapters, indexing and search, taxonomy, dynamically numbered citations,
pinned source evidence, HTML/Markdown/ZIP exports, real MCP initialization,
tool calls and App resources. The interaction ledger contains retrievals of
the test artifact; source write auditing remains enabled.

Evaluation reports are retained as Artifacts independently of operational
history. The live evaluation reached its configured budget limit and produced
a **truncated** report; this is not a completed relevance-quality assessment.
Tuning validation uses diagnosis without promotion. The configured Azure base
is retained as `baseline:pre-artifact-azure-base`; active ranking weights are
compared against their pre-upgrade values throughout the checks.

Desktop and phone testing uses real Chromium and WebKit with emulated
1440×1000, 390×844, 412×915 and 844×390 viewports. It covers the eight primary
routes, artifact/source navigation, retention settings, Form/Raw YAML and
diff controls, the legacy redirect, phone menus and persistent light/dark
themes. This does not substitute for physical-device testing.

## Measurements and evidence

Warm client-observed HTTPS measurements include text, graph, expanded,
artifact-only, hybrid and vector retrieval, four concurrent text clients,
MCP text retrieval and operational reads. Scale-to-zero startup is separate:
liveness can respond before readiness, and startup can temporarily return 503.

Machine-readable results, final revisions and latency summaries are recorded in
`benchmarks/artifact-transition-2026-11-12/azure-validation.json`.
Private screenshots and detailed operational logs remain in `.tmp/`.
The broader local regression results are in
[Artifact transition validation](artifact-transition-validation.md).

## Final results

All 46 live functional checks passed. The 64 primary route/layout cases across
Chromium and WebKit completed with no runtime errors, unexpected HTTP failures
or horizontal page overflow. Focused phone/desktop checks also verified saving
a baseline refreshes the bundles list and typed-record folders open their actual
source and scope. Retirement immediately excludes a record from search while
its immutable version remains readable.

All five application revisions are ready; running containers had zero restarts.
Original mounts and scaling rules are preserved. All 29,294 original artifact
IDs are preserved. Normal synchronization refreshed 50 existing repository files
and 15 web pages and discovered 22 new repository files. All 87 new/refreshed
hashes match the repository files or captured page snapshots. Temporary validation items were retired;
the retained evaluation report and named base remain available.

A real configured-model response returned a cited answer in 3.827 seconds.
Saving that answer as an artifact preserved pinned evidence and resolvable links.
This is one functional model-call measurement.

| Warm operation | Samples | Median | p95 |
| --- | ---: | ---: | ---: |
| API text search | 20 | 100.6 ms | 154.4 ms |
| API graph search | 20 | 132.3 ms | 185.8 ms |
| API expanded text | 20 | 516.4 ms | 583.7 ms |
| API artifact-only | 20 | 99.0 ms | 128.5 ms |
| API hybrid search | 10 | 446.1 ms | 502.4 ms |
| API vector search | 10 | 438.0 ms | 504.6 ms |
| API text, four concurrent clients | 40 | 102.7 ms | 257.6 ms |
| MCP text search | 20 | 100.0 ms | 108.8 ms |

There were no failures in 210 measured warm requests, including operational reads.
