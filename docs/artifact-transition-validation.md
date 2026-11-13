# Artifact transition validation

Validated locally on 2026-11-12. Implementation and upgrade behavior are
described in [Replace memory with artifacts](how-to/artifact-migration.md).
The deployed fleet checks are recorded in
[Azure validation](artifact-transition-azure-validation.md).

## Delivered behavior

- Artifacts owns scoped assertions, formation/review, reinforcement, steering,
  consolidation and synthesis. Existing logging, metrics and derived projections
  remain compatible. Legacy source adoption preserves original files and IDs.
- The UI uses Artifacts; `/memory` redirects there. Legacy HTTP routes and MCP
  tools remain deprecated adapters, with canonical artifact operations available.
- Prose, threads and structured diagrams share dynamic evidence numbering.
  Code files retain their bytes and a compact citation map. HTML, Markdown,
  ZIP and graph references have qualified, renderable evidence targets.
- Immutable output versions/publications survive deletion of originating jobs.
  Existing completed reports and promoted bundles are sealed on upgrade.
  Finished job retention defaults to 30 days; published outputs do not age out.
- Evaluation baselines exclude artifact content. Named base ranking bundles
  and other tuned RRF configurations use the same promotion path. Promotion
  and its publication commit atomically; failed publication preserves the
  preceding active configuration.

## Checks

| Check | Result |
| --- | --- |
| Final persistence, evidence, job, lifecycle and module checks, including SQLite and PostgreSQL 17 with real `pg_textsearch` | 143 passed |
| Populated SQLite-to-PostgreSQL transfer of versions, publications, adoption history and durable jobs | Passed; original SQLite preserved |
| Offline built-container smoke: typed retrieval, taxonomy, evidence rendering, job/output independence and named base promotion | Passed |
| Linux platform/core suite | 212 passed after one fixture-path correction; 18 optional cases skipped |
| Final Linux diagram, bounded job-list and adoption follow-up | 4 passed, 2 PostgreSQL cases skipped; those database cases passed in the host parity run |
| UI production build, strict documentation build, Ruff lint/format, whitespace | Passed |

The full native Windows collection ran in 25 bounded subprocess batches:
3,871 passed, 148 skipped and 27 initially failed. Ten change-related failures
were corrected and verified with targeted reruns. The other 17 reflect native
Windows/checkout differences: POSIX signals, UID/GID and mode assertions,
Windows host-path formatting, CRLF/LF file contents, and the sibling repository's
byte-parity check stopping on schema line endings. The corresponding platform
suites passed in Linux. Vendored contract files were not changed.

Raw summaries are in
`benchmarks/artifact-transition-2026-11-12/validation.json`; local detailed test
logs remain under `.tmp/`.

## Performance

The final run used separate temporary regions, 1,000 items per path, taxonomy
enabled, SQLite BM25 plus graph retrieval, 100 measured queries after five
warm-ups, and 1,000 RRF refusion trials. Writes were batched and indexed once;
write latency is not time to searchable. No embeddings or model calls were used.
Foreground builds and tests had finished; the existing local fleet remained up.

| Path | Search median | Search p95 | Batched write median | Index 1,000 items |
| --- | ---: | ---: | ---: | ---: |
| Canonical typed artifact records | 39.7 ms | 87.8 ms | 15.2 ms | 8.28 s |
| Saved report artifacts | 32.5 ms | 77.6 ms | 5.9 ms | 10.83 s |

Both paths retrieved the expected exact-token item in all 100 queries and
passed refusion equivalence. Refusion averaged 0.025 ms per trial for records
and 0.024 ms for reports. A simple report rendered at 0.094 ms median and
0.227 ms p95. These are synthetic local performance checks, not production
latency or natural-language relevance guarantees.

An earlier clean run measured search p95 of 72.3/36.9 ms respectively; the
variation is retained rather than presenting only the faster result.
The final raw data is `benchmarks/artifact-transition-2026-11-12/validated-results.json`;
the preceding run is `pre-final-results.json` in the same directory.
Reproduce with:

```bash
python scripts/benchmark_artifact_transition.py --counts 1000 --repeats 100 \
  --taxonomy --kinds record artifact --output results.json
```

## Deployment state

`pheasant:artifact-transition-local` was built and smoke-tested with disposable
state. The existing running fleet and its volumes were not upgraded or altered.
The migration executes when the updated application starts against that state;
use the normal [backup and upgrade procedure](pheasant-kb/operate.md).

The Azure deployment was subsequently backed up, upgraded and validated;
see [Azure validation](artifact-transition-azure-validation.md).
