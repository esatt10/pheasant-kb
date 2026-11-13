# Replacing memory with artifacts

Assessment date: 2026-11-12. Baseline revision: `ad39a96`.

The replacement is feasible, and local measurements support building it. It
is a cross-cutting migration, not a source rename or a configuration switch.
The existing artifact path already provides durable content, ordinary indexing,
graph references, folder browsing, rendering, and reversible archival. The
remaining work is to move memory's semantics onto artifacts, separate job
lifetimes from published outputs, and unify evidence identity and rendering.

This assessment adds a reproducible benchmark and a regression test; it does
**not** migrate existing data or change the running stack's configuration.
Measurements cover existing implementations, not the proposed replacement.

## What exists and what must change

| Requirement | Existing implementation | Migration work |
| --- | --- | --- |
| Durable artifacts in search and graph | Saved rows plus an asynchronously indexed `artifacts` document source; evidence links become reference edges. | Add immutable versions, current/deprecated state, replacement links, and explicit publication. |
| Logging and retention | Interaction ledger, trace propagation, NATS logger, hot/cold retention; artifact trace snapshots; artifact age-based archival. | Preserve the observation plane and change formation/admission to produce typed artifacts. Apply lifecycle-specific retention instead of one age limit to all artifacts. |
| Memory behavior | Validity and historical queries, scope and identity, steering rules, reinforcement, compaction, synthesis, candidate review. | Move these contracts onto artifact metadata and projections; remove the independent memory store after a verified migration. |
| Concise, consistent citations | Thread-wide dynamic numbering and a shared HTML renderer already exist. | One evidence registry/resolver for every format, content form, visual, and transport; stable targets independent of displayed numbers. |
| Taxonomy and viewpoints | Artifact folders participate in normal browsing. Heading extraction is a separate source setting. Saved visuals retain viewpoint data. | Enable/preserve heading taxonomy for artifacts, section scoping, and evidence bindings through all views and redraws. |
| Evaluation and retrieval tuning | Proof, snapshots, ablations, independent holdouts, RRF re-fusion, bundles, base rollback, named rollback. | Replace memory-specific policies, IDs, snapshots and ablations with artifact equivalents without contaminating evaluation baselines. |
| Durable, deletable jobs | General jobs use shared JSON snapshots with count eviction; evaluation/tuning use durable run tables and checkpoints. | A common durable job contract, scheduled terminal-job retention, cancellation/recovery, and deletion that cannot cascade into published outputs. |

The mounted local configuration already enables interactions, evaluation, and
tuning. Its artifact retention default is `0` (keep indefinitely). These are
configuration observations, not proof that every optional workflow has run.
The registered artifact source has heading taxonomy **disabled**. Folder
browsing alone therefore does not establish full taxonomy functionality.

The code paths behind these findings are [artifact services](https://github.com/esatt10/pheasant-kb/blob/main/src/pheasant/services/artifacts.py),
[artifact lifecycle](https://github.com/esatt10/pheasant-kb/blob/main/src/pheasant/services/artifact_lifecycle.py),
[memory](https://github.com/esatt10/pheasant-kb/blob/main/src/pheasant/memory/), [observations](memory-formation.md),
[job snapshots](https://github.com/esatt10/pheasant-kb/blob/main/src/pheasant/job_snapshots.py), and
[tuning persistence](https://github.com/esatt10/pheasant-kb/blob/main/src/pheasant/tuning/store.py).

## Artifact semantics and lifetime

Use one artifact identity with immutable revisions and typed metadata. Keep
presentation kinds (`files`, `report`, `thread`) separate from semantic types
such as knowledge, session digest, alias, preference, exclusion, evaluation
report, and retrieval configuration. The semantics must survive even when the
product no longer exposes a separate Memory feature.

Preserve scope, subject, writer, ACL, validity interval, origin observations,
rejection history, deduplication/reinforcement and replacement relationships.
Retain explicit historical lookup. Filter current validity in every retrieval
arm before ranking/truncation, and make tree, graph, detail, export and direct
content reads enforce the same visibility. This is essential: current saved
artifacts are shared, and their owner folders are **not access controls**.
Copying private/session memories into that source today would change visibility.

Keep the observation plane separate. Ordinary logged chat/search calls remain
retention-bound observations; they do not automatically become searchable
knowledge. Formation produces private, expiring session artifacts or reviewed
candidates. Only admitted/published knowledge participates in the appropriate
search policy. Preserve the existing deterministic indexing boundary.

Separate three lifetimes:

| Object | Lifetime | Effect of deleting its originating job |
| --- | --- | --- |
| Observations and diagnostic payloads | Existing configurable hot/cold retention. | Retention continues independently. |
| Jobs, trials, replay checkpoints and unpromoted candidates | Configurable retention measured from terminal completion, with explicit deletion and resumability while active. | Removes operational history and its owned payloads only. |
| Published artifacts and promoted retrieval configurations | No time-based expiry; current until explicitly replaced/deprecated. | No content, version, citation, ranking or active-pointer change. |

Use `draft → published → superseded/deprecated` as explicit lifecycle states.
Deprecation removes an output from default current search while leaving its
version resolvable for citations and historical review. Preserve pinned evidence
or a source snapshot when the original source changes or disappears; show
unavailable evidence explicitly when it cannot be retained. A short provenance
receipt belongs to the published output itself, so auditing it does not depend
on a retained job row. Do not use cascading deletes from jobs to outputs.

Existing `artifacts.retention_days` archives by creation age and kind; it cannot
express these distinctions. Published artifacts need exemption from generic
age-based archival, while session/draft artifacts need their applicable TTLs.
Here "persistent search outputs" means published knowledge and serving retrieval
configurations; ordinary query responses remain part of the observation policy.

## Evidence identity and compact presentation

Keep stable evidence IDs bound to source/document identity, revision or content
digest, and passage/section locator. Display numbers are a rendering concern:
assign `[1]`, `[2]`, ... in first-use order for each document or assembled thread,
deduplicate the same evidence, and derive every marker and source entry from
one map. Never identify evidence by a display number or a bare relative path.

Show compact inline markers and one short Sources list. In the connected viewer,
each marker opens the source passage/section and can traverse its graph relation;
put optional provenance in expandable details. In HTML/Markdown/ZIP exports,
emit real resolvable links or bundled evidence pages with stable anchors and
backlinks. Escape/encode destinations and retain the renderer's isolation policy.
Do not relax the document sandbox to make navigation work; use a controlled
host bridge or an export-appropriate link strategy.

Apply the same registry to report prose, thread transcripts, Markdown files,
visual elements, tables, and downloaded bundles. Parse Markdown tokens so inline
code, fenced/indented code, existing links and literal array indices stay intact.
Keep generated code files byte-identical; when their original numeric citations
cannot safely be rewritten, expose a compact per-file mapping to global evidence.
Visual redraws/viewpoints must preserve evidence IDs, supported/inferred status,
and their local-to-display mapping.

A local probe reproduced current gaps:

- HTML citation markers link to source entries, but those entries have no link
  to the document itself.
- `items[7]` inside inline code is rewritten by the renderer when evidence 7 exists.
- Two citations to `guide.md` in different sources generate identical Markdown
  link destinations. This makes source identity ambiguous.
- A standalone report retains sparse citation numbers; thread renumbering is
  already implemented and should be generalized.

These findings come from [the renderer](https://github.com/esatt10/pheasant-kb/blob/main/src/pheasant/services/artifact_render.py),
[source-file generation](https://github.com/esatt10/pheasant-kb/blob/main/src/pheasant/services/artifact_files.py), and
[thread numbering](https://github.com/esatt10/pheasant-kb/blob/main/src/pheasant/services/artifact_trace.py).

## Evaluation, tuning and recoverable configurations

"Fine tuning" here means the repository's retrieval/RRF parameter tuning.
Training model weights would require a separate scope and validation.

Rework the existing memory-specific snapshots, B0–B6 policies, shadow candidates,
leave-one-out attribution and tier comparisons around artifact semantics.
Preserve denominators, proof strength, negative evidence, ACL/current/as-of gates,
independent holdouts, control cohorts and replay without usage reinforcement.
Exclude learned artifacts from the corpus-only baseline explicitly: its current
`memory=off` policy does not exclude ordinary saved artifacts. Keep measurement
reports out of default retrieval so evaluation cannot reward its own reports.
Reports may still be durable artifacts browsable in the appropriate views.

Preserve the shared fusion implementation and re-fusion equivalence checks.
An experiment proposes an immutable configuration artifact; promotion updates
the serving pointer separately. Freeze the full effective parameter set, code/
schema compatibility, corpus snapshot and a compact gate/holdout receipt into
the published version before allowing its job to be deleted.

Current operations already support:

```bash
pheasant tune show
pheasant tune bundles
pheasant tune apply <bundle-id>
pheasant tune rollback
pheasant tune rollback --to <bundle-id>
```

Current base rollback means the **currently mounted** `search.ranking`, not an
immutable original baseline. Add named/versioned base presets so a later config
edit cannot erase the intended recovery point. Keep base/overlay/effective values
and promotion history visible, and verify serving replicas converge after a
switch (the current resolver caches for 30 seconds).

The new [regression test](https://github.com/esatt10/pheasant-kb/blob/main/tests/test_tuning_batch.py) actually deletes a
completed experiment and its trials, verifies the promoted bundle row, active
overlay and effective parameters are unchanged, then rolls back and reapplies
the retained bundle. It passes locally. This confirms the current tuning
foundation; it does not yet establish the contract for every job/output type.
Also fix pruning's ordering before extending it: `prune_experiment` currently
deletes child trials/decisions before its parent DELETE tests `status != running`.
Reject active jobs first and delete owned history transactionally.

## Delivery sequence and acceptance

1. **Artifact contract and jobs.** Add semantic metadata, immutable revisions,
   ACL/validity, publication and replacement, independent output ownership,
   durable jobs and separate retention policies. Update schema documentation,
   wizard surfaces and both HTTP/MCP adapters.
2. **Migrate memory behaviors.** Redirect write/formation/review, steering,
   reinforcement, compaction and synthesis onto artifacts. Run an idempotent,
   resumable migration preserving originals and legacy-ID mappings, then
   switch readers. Keep temporary compatibility adapters for deployed clients;
   remove the separate store and deprecate the old public interfaces deliberately.
3. **Unify evidence and views.** Implement one resolver/numbering map, fix source
   navigation and export links, enable heading taxonomy, and exercise section
   scope, folder/document browsing, graph layouts and saved visual viewpoints.
4. **Rewire evaluation and tuning.** Move snapshots/ablations/promotion to artifact
   identities, preserve independent evidence gates, publish durable configuration
   versions, and provide named baseline restore and alternative promotion.
5. **Verify the replacement locally.** Compare migrated and legacy corpora on
   SQLite and a dedicated Postgres/NATS fleet; exercise restart/recovery,
   retention under load, concurrent publication, and actual deletion of each
   terminal job kind. Confirm byte-identical published outputs, stable citation
   resolution, unchanged active weights and surviving rollback targets afterward.

Before cutover, require zero ACL/currentness leaks, idempotent migration, exact
re-fusion equivalence, resolvable evidence across all supported exports, and no
quality/control regression on independent evaluation cases. Compare latency and
storage at realistic artifact sizes, taxonomy depth, publication volume and
concurrency against the baselines below. Agree a regression budget before making
the switch; these single-machine baseline runs are not a production SLA.

This is a multi-week change because the semantics span indexing, retrieval,
graph, UI, both APIs, telemetry, evaluation and tuning. A planning allowance of
roughly **3–5 engineer-weeks** for implementation, migration and validation is
reasonable, with substantial uncertainty until phase 1 and a migrated-corpus
prototype establish the data model. No prerequisite requires retraining a model.

## Local measurements

Reproduction and machine-readable results are in
[`benchmarks/artifact-transition-2026-11-12/results.json`](https://github.com/esatt10/pheasant-kb/blob/main/benchmarks/artifact-transition-2026-11-12/results.json).
The benchmark uses real memory writes and artifact saves with indexing batched
afterward, then real hybrid BM25/graph retrieval with diagnostic capture. Each
case performs 100 timed searches after five warmups. No vector/embedding or model
provider is used. A synthetic exact-token hit rate tests basic retrieval, not
semantic quality or equivalent memory policies. Re-fusion timing uses one
captured query for 1,000 identical-parameter trials, not an optimizer quality run.

Run from the repository root (activate the project virtual environment first):

```bash
python scripts/benchmark_artifact_transition.py --counts 100 1000 --repeats 100 --output comparison.json
python scripts/benchmark_artifact_transition.py --counts 1000 --kinds artifact --taxonomy --output taxonomy.json
python -m pheasant.evaluation.benchmark --queries 20 --files 120 --output evaluation.json
pytest tests/test_tuning_batch.py::test_pruning_an_experiment_keeps_its_promoted_bundle_and_rollback -q
```

The running local fleet held 990 indexed documents, 10,627 chunks, 27,301 nodes
and 48,286 links. Thirty timed requests per shape, after five warmups, measured:

| Existing live path | p50 | p95 |
| --- | ---: | ---: |
| Text search | 21.74 ms | 30.28 ms |
| Graph search | 29.89 ms | 57.08 ms |
| Text search plus graph expansion | 206.87 ms | 465.17 ms |
| Text search restricted to artifacts | 47.53 ms | 60.58 ms |

The artifact-only live case returned 0–2 hits; it is not an artifact-scale load
test. Graph expansion's cost supports lazy evidence traversal rather than
expanding every citation eagerly. The requests made no embedding/model calls;
the existing enabled observation logger may record these benchmark requests.

The existing offline evaluation benchmark completed 246 query/variant replays
over 120 synthetic files in 3.52 seconds (14.299 ms per replay), with 564 metric
rows and 622,259 bytes of peak replay checkpoint payload. It reported no warnings.
This demonstrates baseline evaluation operation, not a migrated-artifact eval.

The 1,000-artifact heading-taxonomy run measured 66.221/111.988 ms search
p50/p95, 11.348 seconds for batched indexing, and a 0.206 ms p95 for rendering
the small report. Every exact-token query found its target in the top five and
the captured ranking re-fused exactly. Run order, warm caches and concurrent
local activity affect comparisons; timing is not an isolated taxonomy overhead
estimate. The JSON file includes the separate memory/artifact comparison runs.

The final comparison on Windows/Python 3.14.4 measured:

| 1,000 short records | Search p50 / p95 | Batched index | Region bytes |
| --- | ---: | ---: | ---: |
| Existing memories | 88.836 / 161.701 ms | 10.732 s | 18,852,654 |
| Existing artifacts | 30.802 / 52.481 ms | 8.841 s | 27,249,864 |
| Artifacts with heading taxonomy | 66.221 / 111.988 ms | 11.348 s | 32,251,152 |

All cases found every sampled exact-token target in the top five. Artifact
storage includes a viewer row and indexed files; memories have different
policy/projection work, so their faster/slower paths do not establish semantic
parity. Region bytes include original source files, metadata and SQLite/WAL at
measurement time; they are not a compacted database or a steady-state disk
projection. An earlier comparison measured 127.717 versus 61.203 ms p95 and
11.589 versus 13.528 s indexing for memories versus artifacts. The variation is
why neither one-run indexing speed nor the size ratio should become a guarantee.

Focused verification completed **409 passed, 13 skipped** across four test
invocations, including the new job/output independence regression. Ruff lint
and format checks passed for the changed Python files. Skipped infrastructure/
optional paths remain unverified; the live Postgres checks above exercised
read-only retrieval, not migration writes or backend-parity tests.

Full three-arm/vector performance, provider latency, migration throughput,
privacy/validity parity on artifacts, large exported bundles, concurrency and
the proposed common job-retention system remain cutover validation work.


Implementation is now covered by [Artifact migration](how-to/artifact-migration.md).
The measurements above are the original pre-change baseline; implementation
validation and measurements are recorded separately.
