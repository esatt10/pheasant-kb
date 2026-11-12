Fixes 1–3 are implemented and deployed to local Docker and the Azure development
fleet. Option 4 (keeping services running) was not implemented. Interactive
save/delete responses and artifact retrieval exclusion meet the one-second
target in these warm samples. Searchability and physical cleanup do not yet
consistently meet that target; performance parity is therefore partial.

The original Azure problem was more than a container wake delay: loading the
236,449-node, 1,554,848-edge graph took roughly eight minutes, and individual
source removals repeatedly loaded it. Observed removals took 274–397 seconds
each and accumulated behind one another.

The changes are:

1. Source and document removals use bounded graph-row reads and deltas, update
   the node index, and publish a new generation without loading the corpus.
   Artifact-owned node lookup has an index. Source-id indexes on chunks,
   symbols, artifact terms and memory records prevent whole-table scans on
   source deletion and live-chunk lookup. Removed sources and document
   tombstones exclude pending deletions from retrieval. File-backed standalone
   deployments retain their existing path.
2. Fleet tasks share a supervised writer subprocess. Its graph survives between
   tasks. Config changes, an externally published generation, process death,
   exceptions and watchdog deadlines invalidate the cached writer as appropriate.
   An ambiguous failed write is not automatically replayed by the supervisor;
   durable delivery handles recovery against committed state.
3. Fleet artifact saves commit their payload and a coalesced publication intent
   in one database transaction. The response does not wait for landing-service
   traffic or indexing. The indexer projects pending intents and acknowledges
   them after successful indexing. Deletion atomically records removal intent
   and tombstones; a pending save followed by deletion cannot revive the artifact.
   Notifications wake the landing service without also queuing a redundant full
   source sync.

The schema adds an `artifact_outbox` table and advances the schema marker from
4 to 5. The migration and derived indexes preserve existing data. The four
source-id indexes were installed concurrently on the live PostgreSQL databases;
Azure query plans changed from sequential scans to index scans. No volumes,
corpus sources, scaling rules, resource sizes or replica limits were reset.

Measured on 2026-11-12 UTC with `scripts/benchmark_mutations.py`, using five
unique artifact samples and one unique uploaded source per deployment. Each
artifact contains an index page and a small Markdown file. The probe verifies
the saved artifact's indexed status, absence after deletion, absence from text
search, and physical database cleanup. Its database connection is read-only;
its HTTP mutations affect only its own uniquely named probe data.

All numbers below are seconds, shown as median (minimum–maximum). Source
measurements are single samples. Timing runs inside each API container against
its loopback HTTP endpoint, so they compare application behavior separately
from client network overhead. The deployed corpora and infrastructure differ;
this is not an equal-sized corpus throughput comparison.

| Milestone | Local Docker | Azure |
| --- | ---: | ---: |
| Artifact save acknowledgment | 0.019 (0.014–0.098) | 0.035 (0.027–0.074) |
| Artifact indexed status | 0.428 (0.238–3.537) | 7.573 (0.977–7.872) |
| Artifact delete acknowledgment | 0.015 (0.011–0.036) | 0.028 (0.025–0.034) |
| Artifact absence and text-search exclusion verified | 0.052 (0.046–0.183) | 0.121 (0.089–0.130) |
| Artifact physical row cleanup | 1.501 (0.113–1.796) | 0.135 (0.091–7.119) |
| Source delete acknowledgment | 0.022 | 0.095 |
| Source physical cleanup | 0.735 | 6.865 |

The Azure five-sample batch ran after installing the final indexes on v3; v4
persists that same index migration for future deployments. A further v4 sample
through public HTTPS measured save acknowledgment 0.159 s, delete acknowledgment
0.071 s, retrieval exclusion 0.299 s, indexed status 1.468 s and artifact physical
cleanup 0.314 s. Source delete acknowledgment was 0.125 s and physical cleanup
7.698 s. Before the source indexes, the source cleanup samples were 13.034 s and
18.742 s. This improvement does not yet meet the one-second physical-cleanup goal.

These are small diagnostic samples, not p95 or p99 service-level guarantees.
Earlier intermediate runs, a local batch overlapping builds/tests, deployment
startup failures and a cold probe whose read-only database connection expired
are excluded from this final table. Earlier clean Azure warm batches had indexed
status ranging from 1.7–17.6 seconds; the latest batch is not a tail-latency bound.

Azure's first graph materialization still takes minutes after the writer has
been stopped. Saving and reading the artifact remain available during that
work, but indexing waits. Reusing the writer removes this cost from subsequent
tasks; it does not eliminate the first load. Azure sync traces also show about
3.1–3.25 seconds in graph enrichment on changed content. Job-snapshot listing
over the shared filesystem measured 0.19–0.27 seconds for 15 files; progress
publication and queue contention need separate timing before assigning the
remaining long outliers to either cause.

Further work compatible with keeping option 4 excluded is to make changed-file
enrichment independent of the full resident graph, and to decouple/coalesce
shared-filesystem progress publication from task completion. The current
implementation should not be described as one-second end-to-end indexing.

Validation completed:

- Linux Docker against a disposable real PostgreSQL database: 123 passed,
  covering backend parity, graph backends, targeted mutations, archive and
  idempotency. The disposable database was dropped afterward.
- Latest Windows mutation/lifecycle/archive/writer/queue checks: 63 passed,
  1 skipped. Latest mutation, module budget, service layering and configuration
  freshness checks: 56 passed. These sets overlap and should not be summed.
- A broader affected Windows suite earlier in the work: 191 passed, 6 skipped.
- After adding the source lookup indexes: 42 mutation, module-budget and
  service-layering tests passed on Windows; the 123-test PostgreSQL run also
  includes the final migration and its regression test.
- Ruff lint and formatting checks passed; `git diff --check` passed.
- Real writer reuse/death recovery, cold removal without whole-graph loading,
  provenance retirement, failed projection retry, pending-save deletion and
  retrieval exclusion have regression coverage.

Deployment state:

| Tier | Local image | Azure revision / image |
| --- | --- | --- |
| API | `pheasant:mutation-latency-v4` | `pheasant-kb--0000056` / `pheasant:mutation-latency-20261112-v4` |
| Indexer | `pheasant:mutation-latency-v2` | `pheasant-indexer--0000051` / `pheasant:mutation-latency-20261112-v2` |
| Graph | `pheasant:mutation-latency-v2` | `pheasant-graph--0000049` / `pheasant:mutation-latency-20261112-v2` |
| Logger | `pheasant:mutation-latency-v2` | `pheasant-logger--0000049` / `pheasant:mutation-latency-20261112-v2` |

The v3 refinement affects API notification dispatch; v4 persists the additional
source lookup indexes in the migration path. The warm writing tier was
retained; it uses the new database indexes without a restart. Azure images are
in the deployment's existing registry,
`acrpheasantkbdevwus3001.azurecr.io`, and preserve its `azure/msd1` customizations.
The local `.env` selects v4 for subsequent Compose recreations. Preparation
workers and infrastructure images were not changed.

All local services were healthy; Azure resource provisioning succeeded and the
API's latest revision became ready. Scale-to-zero remains enabled. Indexer
cooldown is 1800 seconds, other infrastructure/API cooldowns remain 900 seconds,
and worker cooldown remains 300 seconds. Polling remains 30 seconds; existing
connection/request thresholds and maximum replicas are unchanged.

The Azure post-run check found zero saved probe artifacts, zero probe sources,
and zero pending outbox records. Benchmark output and deployment diagnostics
are retained locally under `.tmp/` for reproducibility; they contain no tokens.
This is the baseline deployment snapshot before the row-writer follow-up.
See `row-writer-performance-2026-11-12.md` for the subsequent measurements and
deployment state.
