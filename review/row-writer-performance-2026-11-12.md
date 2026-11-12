# Changed-file writer performance — 2026-11-12

The 1.1.0 candidate includes the previously authorized mutation fixes 1–3,
changed-file enrichment, coalesced progress publication and asynchronous fleet
history exports. Option 4 remains excluded: scale-to-zero, resources, replica
limits, polling and cooldowns are unchanged. Existing volumes and corpus data
are preserved.

## Changes and correctness

- Incremental fleet writers hydrate touched graph nodes and complete parallel
  edge pairs from row storage. They retain compact path/reference metadata,
  invalidate references affected by changed paths, and use the existing resolver
  for late targets, imports, ambiguity and media. They release clean graph
  caches between tasks. Standalone and file-backed paths retain their behavior.
- A reusable supervised child owns graph commits. Failed results and dead
  processes discard the child; durable tasks recover against committed state.
- Source and artifact removal use indexed predicates and immediate retrieval
  tombstones. Durable artifact projection intents commit with the saved record
  or deletion and cannot resurrect a pending artifact after its deletion.
- Shared job snapshots publish on a separate writer, coalescing progress to the
  latest payload. Terminal states take priority; clearing an in-flight job
  cannot recreate its file. Failed share writes retry the latest payload.
- Full-completion probing exposed a periodic inline history export taking
  **321.6 seconds** on an intermediate candidate. Interval-based fleet history
  now streams one consistent read-only database transaction on a separate
  connection and thread. Counts are checked before atomic publication, and
  interruption discards the partial file. Explicit interval-zero and standalone
  snapshots remain synchronous.
- A lightweight artifact-projection queue notification interrupts idle claims;
  it never queues a redundant full source sync. Durable outbox polling remains
  the fallback if notification fails.

## Measurement boundaries

`scripts/benchmark_mutations.py --samples 5 --wait-for-projection` creates
uniquely named artifacts, waits for the folder's publication intent to finish,
verifies positive text retrieval, then deletes and verifies retrieval exclusion
and physical cleanup. It also removes its own uploaded source. Credentials stay
inside the API process. All probe data is removed afterward.

`searchable_s` is the legacy indexed-status proxy. It can precede enrichment,
embedding and history, and must not be advertised as end-to-end indexing.
`projection_complete_s` waits for durable work acknowledgement;
`verified_searchable_s` additionally includes positive retrieval. These probes
use the same code and real PostgreSQL/NATS deployments, with API calls from
inside the API container unless explicitly marked as public HTTPS.

The corpora differ substantially: the original local baseline had 27,301 nodes
and 48,286 edges; Azure had 236,449 nodes and 1,554,848 edges. Five samples are
an operational comparison, not a percentile SLA. First-use results are reported
separately from the remaining warm samples. Scale-to-zero wakeups remain outside
any one-second guarantee.

## Progress publication

The same actual Azure shared filesystem was tested with 15 disposable existing
job files and 50 updates followed by completion:

| Metric | Before | After |
| --- | ---: | ---: |
| Task-side publication overhead | 9.450 s | 0.0131 s |
| Terminal file available | 9.450 s | 0.0518 s |
| Shared file writes | 52 | 1 |

Task-side overhead fell **99.86% (about 722×)**. Terminal publication was about
182× faster in this burst test. In-memory progress remains immediate; shared
files are eventually published and can lag if the share is unavailable.

## Baselines and failed intermediate experiments

The controlled warm Azure baseline before the row-writer change completed
artifact projection in **6.585–7.835 s**, median **7.023 s** (three samples).
This baseline measured outbox completion but did not include positive retrieval.
An earlier cold writer sample spent **356.3 s** reaching indexed status.

An early row-writer experiment still reloaded reference metadata and retained
inline history. Full projection took 35.3 s, 8.8 s and 321.6 s. It was rejected.
The compact invalidation cache and independent history publisher address those
observed regressions; the final measurements below supersede that candidate.

## Validation

- 173 tests passed against disposable real PostgreSQL, including resident/row
  differential enrichment, concurrent snapshot consistency, deletion,
  idempotency, writer recovery, jobs and schema migration.
- Windows graph/history/storage/architecture suite: 106 passed, 5 skipped.
- Architecture/config/version suite: 66 passed.
- Projection notification and focused architecture checks: 43 passed on Windows.
- Ruff lint and formatting passed across `src`, `tests` and `scripts` (613 files);
  version synchronization and whitespace checks passed.

These suites overlap; their counts should not be added. Cloud and local timing
results below are integration probes against the actual deployed image.
