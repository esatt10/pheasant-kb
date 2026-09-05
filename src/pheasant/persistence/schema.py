"""The state schema, per dialect (Phase 35.2).

The table definitions are **shared verbatim** between SQLite and Postgres —
same names, same columns, same constraints — so every one of the ~57 raw SQL
call sites and, more importantly, every stable ID built from these rows is
identical whichever backend is running. Only the type spellings differ, and
only where SQLite's affinities have no Postgres equivalent.

Full-text search is the one genuine divergence. SQLite uses FTS5 virtual
tables; Postgres has no such thing. The port keeps ``chunks_fts`` as a real
table with the *same columns*, so every write in
:mod:`pheasant.persistence.state_store` — the per-artifact ``INSERT``, the
``DELETE … WHERE artifact_id=?``, the source-wide delete — runs unchanged, and
adds a generated ``search_vector`` column plus a GIN index. Only the *query*
side differs, and that lives in :mod:`pheasant.search.sqlite_store`.

The column weights are the load-bearing detail. SQLite ranks with
``bm25(chunks_fts, 8, 3, 2, 1)`` over title/path/heading_path/text — weights
measured in the 2026-08-03 retrieval overhaul that took MRR from 0.230 to
0.594. Postgres's ``setweight`` has exactly four classes, A-D, which is a
lucky fit: title→A, path→B, heading_path→C, text→D preserves the *ordering* of
the four fields' importance. It does not reproduce BM25's arithmetic, and
nothing here pretends it does — ``tests/test_backend_parity.py`` gates on
measured retrieval quality rather than on scores matching.
"""

from __future__ import annotations

import re

from pheasant.persistence.sql import Dialect

#: Tables and indexes, shared by every backend.
CORE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS pheasant_schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_bases (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  config_hash TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  knowledge_base_id TEXT NOT NULL,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  path TEXT NOT NULL,
  enabled INTEGER NOT NULL,
  config_json TEXT NOT NULL,
  last_indexed_at TEXT,
  last_status TEXT,
  FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id)
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  type TEXT NOT NULL,
  path TEXT NOT NULL,
  relative_path TEXT,
  mime_type TEXT,
  size_bytes INTEGER,
  sha256 TEXT,
  mtime TEXT,
  git_branch TEXT,
  git_commit TEXT,
  last_indexed_at TEXT,
  status TEXT,
  FOREIGN KEY (source_id) REFERENCES sources(id)
);
CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  heading_path TEXT,
  start_line INTEGER,
  end_line INTEGER,
  text TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  summary TEXT,
  token_estimate INTEGER,
  FOREIGN KEY (artifact_id) REFERENCES artifacts(id),
  FOREIGN KEY (source_id) REFERENCES sources(id)
);
-- Same per-artifact DELETE pattern as artifact_terms; see that index's
-- comment.
CREATE INDEX IF NOT EXISTS idx_chunks_artifact_id ON chunks(artifact_id);
CREATE TABLE IF NOT EXISTS symbols (
  id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  language TEXT,
  symbol_type TEXT,
  name TEXT,
  qualified_name TEXT,
  start_line INTEGER,
  end_line INTEGER,
  signature TEXT,
  docstring_summary TEXT,
  FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
);
-- Same per-artifact DELETE pattern as artifact_terms; see that index's
-- comment.
CREATE INDEX IF NOT EXISTS idx_symbols_artifact_id ON symbols(artifact_id);
CREATE TABLE IF NOT EXISTS artifact_terms (
  id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  node_type TEXT NOT NULL,
  term TEXT NOT NULL,
  normalized_term TEXT NOT NULL,
  weight REAL NOT NULL,
  metadata_json TEXT NOT NULL,
  FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
);
-- Without this, `DELETE FROM artifact_terms WHERE artifact_id=?` (run once
-- per artifact on every sync, in replace_artifact_enrichment) is a full
-- table scan. On a table that grows past a million rows over a real sync,
-- that turns a full-corpus sync into O(n^2): each artifact's delete gets
-- slower as the table grows. Measured cause of a 2,132-file sync taking
-- 1.5+ hours.
CREATE INDEX IF NOT EXISTS idx_artifact_terms_artifact_id
  ON artifact_terms(artifact_id);
-- Retained for the historical concept rows: `WHERE node_type='concept'
-- GROUP BY node_id, ... COUNT(DISTINCT artifact_id)`. Without it, that
-- query is an unindexed scan + sort over the whole table — measured at
-- 10+ minutes and still not finished on a 1.27M-row table.
CREATE INDEX IF NOT EXISTS idx_artifact_terms_node_lookup
  ON artifact_terms(node_type, node_id, artifact_id);
CREATE TABLE IF NOT EXISTS sync_events (
  id TEXT PRIMARY KEY,
  source_id TEXT,
  event_type TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  details_json TEXT,
  error_json TEXT
);
CREATE TABLE IF NOT EXISTS source_checkpoints (
  source_id TEXT PRIMARY KEY,
  connector_type TEXT NOT NULL,
  cursor_json TEXT NOT NULL,
  high_watermark_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL,
  FOREIGN KEY (source_id) REFERENCES sources(id)
);
CREATE TABLE IF NOT EXISTS source_audit_events (
  id TEXT PRIMARY KEY,
  source_id TEXT,
  action TEXT NOT NULL,
  actor TEXT,
  transport TEXT,
  client_id TEXT,
  created_at TEXT NOT NULL,
  details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifests (
  source_name TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idp_groups (
  principal TEXT NOT NULL,
  group_name TEXT NOT NULL,
  PRIMARY KEY (principal, group_name)
);
CREATE TABLE IF NOT EXISTS idp_sync_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- Step 33.5 — the structured face of the agent-memory records that already
-- live as Markdown files under the `type: memory` source.
--
-- This is a **projection**, not a second source of truth: every column is
-- derivable from the record files, and `replace_memory_records` rebuilds a
-- source's rows wholesale on each sync, exactly as `chunks_fts` is a derived
-- cache over `chunks` + `artifacts`. Losing this table costs a re-sync, never
-- data. The records themselves stay append-only files on disk.
--
-- It exists because scope/subject/asserted_at/supersedes were reachable only
-- as *prose inside the indexed chunk text* — so nothing could filter on them,
-- and a superseded record stayed retrievable until a batch job archived it.
--
-- `valid_until` is derived, never double-stored: when B supersedes A, A's
-- validity ends at B's `asserted_at`. An explicit `valid_until` in the record
-- wins when it is earlier.
CREATE TABLE IF NOT EXISTS memory_records (
  record_id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  subject TEXT,
  kind TEXT NOT NULL DEFAULT 'fact',
  asserted_at TEXT NOT NULL,
  valid_from TEXT,
  valid_until TEXT,
  supersedes TEXT,
  tags TEXT,
  written_by TEXT,
  salience REAL NOT NULL DEFAULT 1.0,
  uses INTEGER NOT NULL DEFAULT 0,
  last_used_at TEXT,
  -- Phase 1 (agent-speed memory compaction): `canon_key` is a pure function
  -- of the record's own fields (see pheasant.memory.normalize), so it is
  -- recomputed on every projection rebuild like every other column above
  -- this line, never carried over. `observations`/`last_seen`/`variants`
  -- are earned by reinforcement (a near-duplicate write folding into this
  -- record instead of creating its own file) and are carried over on
  -- rebuild exactly like `salience`/`uses`/`last_used_at` below.
  canon_key TEXT,
  observations INTEGER NOT NULL DEFAULT 0,
  last_seen TEXT,
  variants TEXT,
  -- Phase 3: `tier` and `subsumed_by` are earned by a compaction pass (a
  -- near-duplicate *cluster*, as opposed to Phase 1's exact canonical-key
  -- match) choosing a medoid and demoting the rest — carried over on
  -- rebuild exactly like the Phase 1 columns above. Deliberately DISTINCT
  -- from `supersedes`/`valid_until`: a subsumed record is redundant but
  -- still TRUE, so `subsumed_by` must never feed `effective_valid_until`
  -- (memory/projection.py) — conflating the two would silently expire
  -- facts that are still valid. `tier` is `hot` (default, in every result
  -- set a policy would normally return) or `cold` (demoted; excluded from
  -- default results, reachable via an explicit tier filter or
  -- `current_only=False`/`as_of`, same as a retained superseded record).
  tier TEXT NOT NULL DEFAULT 'hot',
  subsumed_by TEXT,
  schema_version INTEGER NOT NULL DEFAULT 1
  -- Deliberately NO `FOREIGN KEY (artifact_id) REFERENCES artifacts(id)`
  -- here (there was one before this comment; removing it fixed a real,
  -- reproduced-against-a-real-Postgres bug — CLAUDE.md rule 10). SQLite
  -- never enforced it (no `PRAGMA foreign_keys=ON` exists anywhere in this
  -- codebase), but a real Postgres connection enforces every declared FK by
  -- default, and `delete_source_artifacts`/`delete_artifacts` *deliberately*
  -- delete an `artifacts` row while leaving its `memory_records` row alone
  -- (see those methods' own docstrings: wiping earned `uses`/`salience`/
  -- `observations`/`tier` on every consolidation pass would reset a
  -- memory's track record for no benefit, since `replace_memory_records`
  -- rebuilds the row moments later regardless). Under Postgres with the FK
  -- declared, that DELETE raises `foreign key constraint ... still
  -- referenced from table "memory_records"` and aborts the whole
  -- transaction — every full sync of any source once a single memory
  -- record existed, and after Phase 0 (agent-speed memory compaction) also
  -- `_drop_archived`'s targeted `delete_artifacts` on every consolidation
  -- pass that archived anything. Same reasoning `memory_compactions`
  -- already documents for its own `member_id`/`canonical_id` columns —
  -- applied here to the column that predates this plan.
);
-- The `idx_memory_records_canon_key` (Phase 1) and `idx_memory_records_tier`
-- (Phase 3) indexes are NOT declared here: on a fresh database this CREATE
-- TABLE already carries both columns, so they could be, but on an upgraded
-- one the columns are added later by guarded ALTER TABLE in
-- StateStore.migrate() — and `executescript` runs this whole file as one
-- script, before those ALTERs ever run. Declaring an index on a column that
-- does not exist yet would fail against a pre-Phase-1/3 table. See migrate().
-- Retrieval joins chunks -> artifacts -> memory_records on every memory-aware
-- query, and the validity predicate is `scope` + `valid_until`.
CREATE INDEX IF NOT EXISTS idx_memory_records_artifact_id
  ON memory_records(artifact_id);
CREATE INDEX IF NOT EXISTS idx_memory_records_scope
  ON memory_records(scope, valid_until);
-- Append-only audit trail for every compaction decision (Phase 3): a
-- near-duplicate cluster's medoid promotion, one row per subsumed member.
-- `op` is currently always `subsume`, kept as a column rather than a fixed
-- value so a later op (e.g. an LLM-synthesized merge, Phase 4) needs no
-- schema change. `id` is a deterministic hash of
-- (op, member_id, canonical_id, params_hash), so re-running a pass over
-- unchanged content with unchanged parameters writes the SAME row id and
-- `INSERT OR IGNORE` makes the pass idempotent — the property
-- `MemoryStore.consolidate` already has for archiving.
-- `member_id`/`canonical_id` are `memory_records.record_id`, not enforced as
-- a live FK (SQLite never enforces FKs here regardless — see CLAUDE.md's
-- note on `delete_source_artifacts`, and a member's canonical record could,
-- in principle, itself later be superseded or subsumed by something else,
-- at which point the ledger row is history rather than a pointer that must
-- still resolve).
CREATE TABLE IF NOT EXISTS memory_compactions (
  id TEXT PRIMARY KEY,
  op TEXT NOT NULL,
  member_id TEXT NOT NULL,
  canonical_id TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  params_hash TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_compactions_canonical
  ON memory_compactions(canonical_id);
CREATE INDEX IF NOT EXISTS idx_memory_compactions_member
  ON memory_compactions(member_id);
-- What the indexed state was built with, per scope (a source, or the vector
-- space). A restart compares the live config against these: same fingerprint
-- means the stored artifacts/chunks/vectors are still valid and there is
-- nothing to redo. See pheasant.sync.fingerprint.
-- Phase 35.4: per-source write leases. EngineLease permits one writer per
-- /state dir, which is the right model for SQLite and is the ceiling Phase 35
-- lifts: two different sources have no reason to wait for each other. The row
-- is claimed by a single conditional UPDATE, so the database arbitrates races
-- rather than a read-then-write in Python.
CREATE TABLE IF NOT EXISTS source_leases (
  source_id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_fingerprints (
  scope TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- Phase 35.5: the durable index work queue. Without it, `sync_all` holds its
-- remaining sources in a Python list: a process killed nine sources into ten
-- has lost the tenth, and nothing outside that process can see the backlog
-- or act on it. A row survives the process, so a restart resumes and a
-- scheduler has a queue depth to scale on.
--
-- `visible_at` is the visibility timeout: a claimed task is invisible until
-- it expires, so a worker that dies mid-task releases it by simply not
-- heartbeating. At-least-once redelivery is safe here because indexing is
-- already idempotent by design (content sha256 + stable IDs) — the existing
-- pillar is what makes the queue cheap.
CREATE TABLE IF NOT EXISTS index_tasks (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  payload TEXT,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  owner TEXT,
  visible_at TEXT NOT NULL,
  enqueued_at TEXT NOT NULL,
  updated_at TEXT,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_index_tasks_claim
  ON index_tasks(status, visible_at, enqueued_at);
-- Memory candidates: what formation proposes, before anything admits it.
--
-- Evidence, never memory. A row here is a *suggestion* derived from the
-- observation plane; it becomes knowledge only when something admits it, and
-- admission goes through MemoryStore.append like every other write. That is
-- what keeps memory's first invariant intact while still letting the region
-- learn from how it is used.
--
-- `id` is content-addressed over (rule, scope, subject, kind, normalized text,
-- params_hash), so a rule re-deriving the same proposal on the next beat
-- updates the counters on the row it already wrote instead of piling up
-- duplicates -- the same property `memory_compactions` gets from hashing its
-- own fields.
--
-- **A rejected candidate is never re-proposed.** The upsert below only ever
-- refreshes a row that is still `pending`, so `rejected` stays rejected and
-- `admitted` stays admitted, exactly as `index_tasks` keeps a dead task dead.
-- Without that, a rule would re-suggest on every beat something a person has
-- already said no to.
CREATE TABLE IF NOT EXISTS memory_candidates (
  id TEXT PRIMARY KEY,
  rule_id TEXT NOT NULL,
  params_hash TEXT NOT NULL,
  scope TEXT NOT NULL,
  subject TEXT,
  kind TEXT NOT NULL DEFAULT 'fact',
  text TEXT NOT NULL,
  written_by TEXT,
  evidence_json TEXT,
  observations INTEGER NOT NULL DEFAULT 1,
  sessions INTEGER NOT NULL DEFAULT 1,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  admitted_by TEXT,
  record_id TEXT,
  decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_candidates_status
  ON memory_candidates(status, last_seen);
-- The log tier's own queue, deliberately NOT a `kind` column on index_tasks.
-- Observations arrive at request rate against a corpus that changes hourly at
-- most, so sharing a table would mean request-rate churn on the very index
-- (idx_index_tasks_claim) the indexer claims from, plus the vacuum pressure
-- that comes with it under Postgres — exactly the burden the separate tier
-- exists to avoid. The cost of separating is small because the abstraction was
-- already right: `drain()` is task-agnostic and is reused verbatim, and the
-- race-free conditional-UPDATE claim stays one implementation parameterized by
-- table name.
--
-- No `source_id`/`mode`: those are indexing vocabulary. A batch is opaque JSON
-- and the payload is the whole task.
CREATE TABLE IF NOT EXISTS log_tasks (
  id TEXT PRIMARY KEY,
  payload TEXT,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 2,
  owner TEXT,
  visible_at TEXT NOT NULL,
  enqueued_at TEXT NOT NULL,
  updated_at TEXT,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_tasks_claim
  ON log_tasks(status, visible_at, enqueued_at);
-- The observation plane: one row per API/MCP call, when
-- `observability.interactions.enabled`.
--
-- **These are not memory records and must never become them.** A row here is
-- never a file, never chunked, never indexed, and never returned by a search;
-- a UI session's chat does not become knowledge because it was observed. The
-- only path from here into memory is a *candidate* that something admits,
-- and admission goes through MemoryStore.append like every other write. See
-- docs/memory-formation.md.
--
-- Unlike every other table in this file, this one is high-churn and
-- retention-bounded: rows are deleted once past
-- `interactions.hot_retention_days`, after being rolled to Parquet under
-- /exports when `cold_enabled`. That is the one sanctioned exception to
-- "nothing is ever deleted" (CLAUDE.md rule 2) and it is why the retention is
-- a declared, documented policy rather than an implementation detail.
--
-- `id` is blake2b(trace_id|span_id), so at-least-once redelivery of a batch
-- is a no-op rather than a duplicate — the same argument index_tasks makes
-- from content sha256, reached a different way.
CREATE TABLE IF NOT EXISTS interaction_events (
  id TEXT PRIMARY KEY,
  kb_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  span_id TEXT NOT NULL,
  parent_span_id TEXT,
  modality TEXT NOT NULL,
  operation TEXT NOT NULL,
  principal TEXT,
  session_id TEXT,
  client_id TEXT,
  started_at TEXT NOT NULL,
  duration_ms REAL,
  status TEXT NOT NULL,
  query_text TEXT,
  answer_text TEXT,
  criteria_json TEXT,
  -- Stable node ids and source-relative paths, kept in two homogeneous lists
  -- rather than one heterogeneous one. `result_ids` joins to graph_nodes and
  -- chunks; `result_paths` is in the grammar steering rules already match
  -- against, so a `preference` rule minted from these can actually fire.
  result_ids_json TEXT,
  result_paths_json TEXT,
  result_count INTEGER,
  top_score REAL,
  attributes_json TEXT,
  schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_interaction_events_time
  ON interaction_events(started_at);
CREATE INDEX IF NOT EXISTS idx_interaction_events_session
  ON interaction_events(session_id, started_at);

-- ---------------------------------------------------------------------------
-- The evaluation plane. Records *about* the knowledge base, never part of it:
-- nothing here is a file, is chunked, is indexed, or is returned by a search.
-- Same boundary `interaction_events` draws, and drawn again here because the
-- temptation is stronger: an evaluation report reads like knowledge.
--
-- Ids are content digests, not sequences. Two API replicas computing a
-- manifest for one knowledge-base state must agree on its id without
-- coordinating, which is also what makes an INSERT idempotent under
-- at-least-once retry -- the argument `index_tasks` makes from content
-- sha256, reached the same way.
CREATE TABLE IF NOT EXISTS evaluation_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  kb_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  effective_as_of TEXT NOT NULL,
  complete INTEGER NOT NULL DEFAULT 1,
  manifest_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evaluation_snapshots_time
  ON evaluation_snapshots(kb_id, created_at);
-- Typed interaction evidence. `polarity` is one of positive/negative/unknown
-- and unknown is stored rather than dropped: an artifact that was served and
-- neither selected nor rejected is *unjudged*, and a table that only held
-- judgments could not tell that apart from one nobody ever saw.
--
-- `event_type` is preserved verbatim even when several types map to one
-- polarity and weight, because re-weighting the taxonomy has to be
-- recomputable over evidence already collected.
CREATE TABLE IF NOT EXISTS evaluation_proofs (
  proof_id TEXT PRIMARY KEY,
  kb_id TEXT NOT NULL,
  query_id TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  polarity TEXT NOT NULL,
  strength TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 0.0,
  observed_at TEXT NOT NULL,
  interaction_id TEXT,
  snapshot_id TEXT,
  principal_partition TEXT,
  position INTEGER,
  exposed INTEGER NOT NULL DEFAULT 1,
  outcome_reference TEXT,
  supersedes_proof_id TEXT,
  reason_code TEXT,
  multipliers_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_evaluation_proofs_query
  ON evaluation_proofs(kb_id, query_id, polarity);
CREATE INDEX IF NOT EXISTS idx_evaluation_proofs_target
  ON evaluation_proofs(kb_id, target_id);
CREATE INDEX IF NOT EXISTS idx_evaluation_proofs_time
  ON evaluation_proofs(kb_id, observed_at);
-- A frozen anchor cohort is the whole reason longitudinal comparison works:
-- the same questions, asked of every later snapshot. `frozen` is enforced in
-- `pheasant.evaluation.cohorts`, not by a constraint, because re-materializing
-- a rolling cohort is the normal case and only the anchor is immutable.
CREATE TABLE IF NOT EXISTS evaluation_cohorts (
  cohort_id TEXT PRIMARY KEY,
  kb_id TEXT NOT NULL,
  name TEXT NOT NULL,
  purpose TEXT NOT NULL,
  created_at TEXT NOT NULL,
  frozen INTEGER NOT NULL DEFAULT 0,
  window_start TEXT,
  window_end TEXT,
  eligibility_digest TEXT,
  query_count INTEGER NOT NULL DEFAULT 0,
  queries_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evaluation_cohorts_purpose
  ON evaluation_cohorts(kb_id, purpose, created_at);
-- One row per batch. `report_json` is the whole evidence-bearing report; the
-- per-metric rows below exist so an aggregate can be resolved to the per-query
-- calculations that produced it without parsing a large document.
--
-- **Progress lives here, not in a process.** A batch is minutes of work, and
-- the UI, the CLI and an agent all need to watch it -- from other processes,
-- and across a restart. An in-memory job registry answers none of those: a
-- container that stops mid-run leaves a watcher with no record at all, and
-- `status='running'` forever. So the phase, the unit counters and a heartbeat
-- are columns, written as the run moves. A row whose heartbeat has gone stale
-- is reclaimable (see `reclaim_stale_runs`), which is the same recovery an
-- indexer already gets from `source_leases`.
CREATE TABLE IF NOT EXISTS evaluation_runs (
  run_id TEXT PRIMARY KEY,
  kb_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'current_state',
  config_digest TEXT NOT NULL,
  gates_passed INTEGER NOT NULL DEFAULT 1,
  report_json TEXT,
  phase TEXT,
  phase_detail TEXT,
  completed_units INTEGER NOT NULL DEFAULT 0,
  total_units INTEGER NOT NULL DEFAULT 0,
  heartbeat_at TEXT,
  owner TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_evaluation_runs_time
  ON evaluation_runs(kb_id, started_at);
-- NOTE: the companion index on (kb_id, status, heartbeat_at) is deliberately
-- NOT here. `CREATE TABLE IF NOT EXISTS` no-ops against a /state written
-- before `heartbeat_at` existed, so an index naming that column in this same
-- script runs *before* the guarded ALTER that adds it and fails the whole
-- migration with "no such column". It is created in `StateStore.migrate`,
-- after the ALTER — exactly where `idx_memory_records_canon_key` is, and for
-- exactly the same reason.
-- One replayed (cohort, variant) pair, checkpointed as it completes.
--
-- This is what makes a batch **resumable** rather than merely idempotent. A
-- run is content-addressed, so a restart re-derives the same `run_id` and the
-- metric rows dedup -- but without this, every query would be replayed again
-- from the top, and on a large cohort that is the whole cost of the run. With
-- it, a resumed batch loads the pairs it already finished and replays only
-- what is missing. The same argument `source_checkpoints` makes for a
-- connector: the expensive part is the fetch, so remember what was fetched.
--
-- Rows are keyed by (run, cohort, variant) and are written once. They are
-- pruned with their run rather than kept forever: a completed run's evidence
-- is its metric rows and its report, and this is scaffolding.
CREATE TABLE IF NOT EXISTS evaluation_replays (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  kb_id TEXT NOT NULL,
  cohort_id TEXT NOT NULL,
  cohort_name TEXT NOT NULL,
  variant_id TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  query_count INTEGER NOT NULL DEFAULT 0,
  failure_count INTEGER NOT NULL DEFAULT 0,
  duration_ms REAL,
  results_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evaluation_replays_run
  ON evaluation_replays(run_id, cohort_name, variant_id);
-- Per-query rows and aggregates share this table; an aggregate has a NULL
-- query_id. That is what makes "resolve this 0.89 to the five queries behind
-- it" one query rather than a join across two shapes.
CREATE TABLE IF NOT EXISTS evaluation_metrics (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  kb_id TEXT NOT NULL,
  metric_id TEXT NOT NULL,
  metric_version INTEGER NOT NULL DEFAULT 1,
  classification TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  cohort_id TEXT,
  variant_id TEXT,
  query_id TEXT,
  value REAL,
  numerator REAL,
  denominator REAL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evaluation_metrics_run
  ON evaluation_metrics(run_id, metric_id, variant_id);
CREATE INDEX IF NOT EXISTS idx_evaluation_metrics_trend
  ON evaluation_metrics(kb_id, metric_id, cohort_id, variant_id);
-- The tuning plane (docs/retrieval-tuning.md). Four tables, and the split
-- between what is here and what is not is the point of the design: /state
-- holds the small, queryable index -- an experiment, a trial's scores, a
-- decision, a bundle -- and the bulky per-query, per-trial rankings go to
-- /exports/tuning as compressed JSONL. A trial's ranked lists are
-- regenerable from the corpus and the parameters; an operational database is
-- not where they should accumulate, and putting them here would grow /state
-- proportionally to (queries x trials) on every pass.
--
-- Every table here is additive and none of it is ever indexed, chunked, or
-- returned by a search. A region must not retrieve its own experiments as
-- knowledge, for the same reason it must not retrieve its own measurements.
--
-- One tuning batch. Content-addressed like `evaluation_runs` and for the same
-- reason: an experiment IS its (region, snapshot, space, cohort, budget)
-- tuple, so re-running an unchanged one is the same experiment rather than a
-- second row that looks like a second data point. Progress lives in columns,
-- not in a process, so the UI can watch a batch it did not start and a
-- container that stops mid-pass leaves a reclaimable row rather than a
-- spinner that never ends.
CREATE TABLE IF NOT EXISTS tuning_experiments (
  experiment_id TEXT PRIMARY KEY,
  kb_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  cohort_id TEXT NOT NULL,
  holdout_cohort_id TEXT,
  control_cohort_id TEXT,
  space_digest TEXT NOT NULL,
  baseline_point_id TEXT NOT NULL,
  budget_json TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  phase TEXT,
  phase_detail TEXT,
  completed_units INTEGER NOT NULL DEFAULT 0,
  total_units INTEGER NOT NULL DEFAULT 0,
  heartbeat_at TEXT,
  owner TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  searches INTEGER NOT NULL DEFAULT 0,
  diagnosis_json TEXT,
  report_json TEXT,
  error TEXT,
  -- Cancellation is a column, not a process flag. A batch runs in a thread
  -- inside whichever replica started it, and the person cancelling is talking
  -- to whichever replica their browser reached — usually a different one. The
  -- runner reads this between units, so a cancel from any replica lands.
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  cancel_requested_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_tuning_experiments_time
  ON tuning_experiments(kb_id, started_at);
CREATE INDEX IF NOT EXISTS idx_tuning_experiments_live
  ON tuning_experiments(kb_id, status, heartbeat_at);
-- One point evaluated on one cohort. Written as each trial completes, which
-- is what makes a batch **resumable**: the experiment id is content-addressed,
-- so a restart re-derives it, loads the trials already done, and evaluates
-- only what is missing. Same argument `evaluation_replays` makes, and the same
-- argument `source_checkpoints` makes for a connector.
--
-- `metrics_json` is the scores; the ranked ids behind them are in cold storage
-- under `cold_ref`. A trial row stays a few hundred bytes however large the
-- cohort is.
CREATE TABLE IF NOT EXISTS tuning_trials (
  trial_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  kb_id TEXT NOT NULL,
  point_id TEXT NOT NULL,
  cohort_id TEXT NOT NULL,
  cohort_name TEXT NOT NULL,
  cost_class TEXT NOT NULL,
  motivating_stage TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  evaluated_queries INTEGER NOT NULL DEFAULT 0,
  excluded_queries INTEGER NOT NULL DEFAULT 0,
  searches INTEGER NOT NULL DEFAULT 0,
  duration_ms REAL,
  primary_metric REAL,
  point_json TEXT NOT NULL,
  proposal_json TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  histogram_json TEXT,
  cold_ref TEXT,
  failed TEXT
);
CREATE INDEX IF NOT EXISTS idx_tuning_trials_experiment
  ON tuning_trials(experiment_id, cohort_id, point_id);
CREATE INDEX IF NOT EXISTS idx_tuning_trials_rank
  ON tuning_trials(experiment_id, primary_metric);
-- What an experiment concluded, and every reason behind it. Separate from the
-- experiment row because a decision outlives the batch that produced it: a
-- bundle points at its decision, and reading "why is the region ranked this
-- way" must not depend on an experiment row still being around.
CREATE TABLE IF NOT EXISTS tuning_decisions (
  decision_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  kb_id TEXT NOT NULL,
  outcome TEXT NOT NULL,
  reason TEXT NOT NULL,
  winning_point_id TEXT,
  gates_passed INTEGER NOT NULL DEFAULT 0,
  holdout_confirmed INTEGER NOT NULL DEFAULT 0,
  control_regressed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tuning_decisions_experiment
  ON tuning_decisions(kb_id, experiment_id, created_at);
-- A packaged configuration set, and the fleet's active pointer.
--
-- At most one row per kb_id has `applied_at` set and `superseded_at` NULL;
-- that row IS the region's retrieval configuration overlay, and every replica
-- reading this /state resolves it (see `search.ranking.RankingResolver`).
-- Applying is fleet-scoped by construction: there is no principal column and
-- no request column, so there is nowhere for a per-caller override to live.
--
-- `replaces_json` is what was in force when the bundle was applied. Rollback
-- restores it, which makes reverting a stored fact rather than an operator's
-- recollection of what the config used to say.
CREATE TABLE IF NOT EXISTS tuning_bundles (
  bundle_id TEXT NOT NULL,
  kb_id TEXT NOT NULL,
  experiment_id TEXT,
  decision_id TEXT,
  snapshot_id TEXT,
  created_at TEXT NOT NULL,
  applied_at TEXT,
  applied_by TEXT,
  superseded_at TEXT,
  parameters_json TEXT NOT NULL,
  replaces_json TEXT,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (kb_id, bundle_id)
);
CREATE INDEX IF NOT EXISTS idx_tuning_bundles_active
  ON tuning_bundles(kb_id, applied_at, superseded_at);
-- The knowledge graph, as rows rather than one file (Phase 35.10).
--
-- The graph used to be a single zstd node-link blob that every commit
-- re-serialized whole and every serving replica held resident. Measured on
-- this repo's own benchmark at 100k files (630k nodes / 630k edges): 9.1s to
-- write for a one-file change, 4.6s to load before a replica can serve, and
-- 1.5GB of RSS in every process that answers a graph query. As rows the same
-- commit writes only what changed -- 10ms, and flat in graph size -- and a
-- bounded traversal is an indexed walk that needs no residency at all.
--
-- `attrs` carries every attribute *not* promoted to a column, so the promoted
-- ones are not stored twice. Reconstituting a node is the row's columns
-- merged over its `attrs`, which is the one place that projection may live.
--
-- `digest` is the row's own content hash, and it is what makes the
-- content-addressed generation id survive the move: the published id folds
-- every row's digest with XOR, which is invertible, so a commit updates it in
-- O(changed) by folding the old digest out and the new one in. A counter or a
-- clock would have been cheaper and would have broken pillar 1 -- two replicas
-- must name an unchanged graph identically without coordinating.
CREATE TABLE IF NOT EXISTS graph_nodes (
  kb_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  type TEXT,
  label TEXT,
  source_id TEXT,
  relative_path TEXT,
  artifact_id TEXT,
  attrs TEXT NOT NULL,
  digest TEXT NOT NULL,
  PRIMARY KEY (kb_id, node_id)
);
-- Per-source deletion (`remove_source_content`, `replace_source`) and the
-- source filter every graph query may carry.
CREATE INDEX IF NOT EXISTS idx_graph_nodes_source ON graph_nodes(kb_id, source_id);
-- The per-type tally `/overview`, `/graph/diagnostics` and the graph service's
-- `stats` all publish, and the UI polls. As a *covering* index the GROUP BY
-- never touches the table: measured 386ms -> 61.5ms at 630k nodes, for 10.8MB
-- (1% of that database). The in-memory graph maintains the same tally on write
-- for the same reason -- "re-counting 240k node types per request was costing
-- seconds on endpoints the UI polls" -- so this is that decision, kept.
CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes(kb_id, type);
CREATE TABLE IF NOT EXISTS graph_edges (
  kb_id TEXT NOT NULL,
  source TEXT NOT NULL,
  target TEXT NOT NULL,
  type TEXT NOT NULL,
  seq INTEGER NOT NULL,
  source_id TEXT,
  attrs TEXT NOT NULL,
  digest TEXT NOT NULL,
  PRIMARY KEY (kb_id, source, target, type, seq)
);
-- Out-adjacency is the primary key's own prefix, so traversal needs no index
-- of its own. The reverse index is not for reading: removing a node has to
-- remove the edges that point *at* it, and without this that delete is a full
-- scan of the edge table -- the O(edges) cost this whole change exists to take
-- off the sync path.
CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(kb_id, target);
-- Two more looked obviously useful and are deliberately absent: one on
-- `graph_edges(source_id)` and one on `graph_nodes(artifact_id)`. Nothing
-- reads either -- per-source deletion goes through the node table and
-- cascades by endpoint -- and at 630k edges they measured 190MB of the
-- database between them. An index with no reader is the storage version of a
-- config flag with no reader: it looks like the mechanism, and it is not.
-- One row per knowledge base: the publication record the graph file used to
-- keep in a JSON sidecar. `node_fold`/`edge_fold` are the XOR aggregates the
-- generation id is derived from, persisted rather than recomputed so a restart
-- does not owe a full scan.
CREATE TABLE IF NOT EXISTS graph_generations (
  kb_id TEXT PRIMARY KEY,
  generation_id TEXT NOT NULL,
  published_at TEXT NOT NULL,
  nodes INTEGER NOT NULL,
  edges INTEGER NOT NULL,
  node_fold TEXT NOT NULL,
  edge_fold TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- The readiness plane: receipts and seals.
--
-- A receipt answers a question the rest of this schema cannot: *what happened
-- to the thing I submitted*. `artifacts` says what the region holds, which is
-- the same answer for "you never sent it" and "you sent it and it was
-- rejected" -- and a harness reconciling submissions against stored counts
-- needs those to be different answers, because the first is its own bug and
-- the second is the region's.
--
-- One row per submitted item, keyed by the caller's idempotency key so a
-- retried submission folds onto the row it already wrote rather than
-- producing a second one. `receipt_id` is a digest over (kb_id,
-- idempotency_key) for exactly the reason `evaluation_snapshots` ids are
-- digests: two replicas handed the same retry must write the same row without
-- coordinating.
CREATE TABLE IF NOT EXISTS ingest_receipts (
  receipt_id TEXT PRIMARY KEY,
  kb_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  submission_id TEXT NOT NULL,
  source_name TEXT,
  submitted_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  -- accepted | indexed | rejected | failed. Deliberately four rather than a
  -- boolean: `accepted` is the transport's answer and `indexed` is the index
  -- barrier, and collapsing them is how a harness starts searching for
  -- content the region has persisted and not yet indexed.
  disposition TEXT NOT NULL,
  indexed_at TEXT,
  artifact_id TEXT,
  content_sha256 TEXT,
  chunk_count INTEGER,
  -- Every write of the *same* key increments this. A count above one with an
  -- unchanged artifact_id is the idempotency proof; a count above one with a
  -- changed one is the bug it exists to catch.
  submissions INTEGER NOT NULL DEFAULT 1,
  error_code TEXT,
  retryable INTEGER NOT NULL DEFAULT 0,
  detail_json TEXT,
  UNIQUE (kb_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_ingest_receipts_submission
  ON ingest_receipts(kb_id, submission_id);
CREATE INDEX IF NOT EXISTS idx_ingest_receipts_disposition
  ON ingest_receipts(kb_id, disposition);

-- A sealed snapshot. The manifest itself lives in `evaluation_snapshots`,
-- which already computes every digest capable of changing retrieval and is
-- already content-addressed; sealing is a separate fact about one of those
-- manifests -- that somebody has declared it the reference state for a run --
-- and it is separate because an evaluation batch seals nothing and a sealed
-- snapshot may outlive every run that used it.
CREATE TABLE IF NOT EXISTS snapshot_seals (
  snapshot_id TEXT PRIMARY KEY,
  kb_id TEXT NOT NULL,
  label TEXT,
  sealed_at TEXT NOT NULL,
  sealed_by TEXT,
  -- The corpus digest at sealing time, lifted out of the manifest so drift can
  -- be tested with one comparison rather than by re-reading a JSON blob.
  corpus_digest TEXT NOT NULL,
  manifest_digest TEXT NOT NULL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshot_seals_kb
  ON snapshot_seals(kb_id, sealed_at);
"""

#: SQLite-only: WAL, plus the FTS5 virtual tables.
SQLITE_EXTRAS = """\
PRAGMA journal_mode=WAL;
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED,
  source_id UNINDEXED,
  artifact_id UNINDEXED,
  title,
  path,
  heading_path,
  text
);
-- The corpus's own vocabulary, with document frequencies, read straight off
-- the FTS index. `fts5vocab` is a view over chunks_fts's internal term table:
-- it stores nothing of its own and stays exact as the index changes.
--
-- This replaces the concept layer as the source of "what is this corpus
-- about" (the Synapse contract's vocabulary.top_concepts + minhash, and the
-- planner's structural grounding). Concept extraction had been materializing
-- 141k nodes and 1.27M artifact_terms rows to answer that question; SQLite
-- was already maintaining the same information for free.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vocab USING fts5vocab(chunks_fts, 'row');
"""

#: Postgres-only: ``chunks_fts`` as a real table with the same columns, so the
#: write path is untouched, plus the generated search vector and its index.
#:
#: ``search_vector`` is a STORED generated column rather than a trigger: it
#: cannot drift from its row, and there is no ordering hazard between the
#: INSERT and a trigger that a concurrent reader could observe.
#:
#: **Punctuation is flattened to spaces before tokenizing**, which is not
#: cosmetic. SQLite indexes these columns with FTS5's ``unicode61`` tokenizer,
#: which splits on every non-alphanumeric: ``deploy-gateway.md`` becomes
#: ``deploy``, ``gateway``, ``md``. Postgres's ``simple`` dictionary keeps it
#: as the single lexeme ``deploy-gateway.md``, so a search for "deploy" did
#: not match the file *named* for it at all — silently, with no error and a
#: perfectly plausible result list. Measured: the file named for the query
#: ranked below a decoy that merely repeats it in prose. The regexp restores
#: unicode61's splitting so both backends see the same terms.
POSTGRES_EXTRAS = """\
CREATE TABLE IF NOT EXISTS chunks_fts (
  chunk_id TEXT PRIMARY KEY,
  source_id TEXT,
  artifact_id TEXT,
  title TEXT,
  path TEXT,
  heading_path TEXT,
  text TEXT,
  search_vector tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('simple',
      regexp_replace(coalesce(title, ''), '[^a-zA-Z0-9]+', ' ', 'g')), 'A') ||
    setweight(to_tsvector('simple',
      regexp_replace(coalesce(path, ''), '[^a-zA-Z0-9]+', ' ', 'g')), 'B') ||
    setweight(to_tsvector('simple',
      regexp_replace(coalesce(heading_path, ''), '[^a-zA-Z0-9]+', ' ', 'g')), 'C') ||
    setweight(to_tsvector('simple',
      regexp_replace(coalesce(text, ''), '[^a-zA-Z0-9]+', ' ', 'g')), 'D')
  ) STORED
);
CREATE INDEX IF NOT EXISTS idx_chunks_fts_vector ON chunks_fts USING GIN (search_vector);
-- Mirrors the per-artifact and per-source DELETEs the write path issues.
CREATE INDEX IF NOT EXISTS idx_chunks_fts_artifact ON chunks_fts(artifact_id);
CREATE INDEX IF NOT EXISTS idx_chunks_fts_source ON chunks_fts(source_id);
"""


def schema_for(dialect: Dialect) -> str:
    """The full DDL script for one dialect.

    Type substitution is applied longest-key-first so ``INTEGER PRIMARY KEY``
    is never half-rewritten into ``BIGINT PRIMARY KEY PRIMARY KEY``.
    """

    body = CORE_SCHEMA
    for source, target in sorted(dialect.types.items(), key=lambda kv: -len(kv[0])):
        body = body.replace(source, target)
    extras = POSTGRES_EXTRAS if dialect.is_postgres else SQLITE_EXTRAS
    return body + extras


#: One ``CREATE TABLE IF NOT EXISTS <name> ( … );`` block in :data:`CORE_SCHEMA`.
#: Every block in that string ends on its own ``);`` line, which is what makes
#: the non-greedy body match unambiguous.
_CREATE_TABLE = re.compile(
    r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);", re.DOTALL | re.IGNORECASE
)

#: A ``--`` comment, stripped before a table body is split on commas so a
#: comma inside prose cannot look like a column boundary.
_SQL_COMMENT = re.compile(r"--[^\n]*")

#: Table-level constraints, which are entries in the comma-separated body but
#: are not columns.
_CONSTRAINT_KEYWORDS = frozenset({"FOREIGN", "PRIMARY", "UNIQUE", "CHECK", "CONSTRAINT"})


def _split_top_level(body: str) -> list[str]:
    """Split a table body on commas that are not inside parentheses.

    ``PRIMARY KEY (principal, group_name)`` is one entry, not two — and a
    naive ``body.split(",")`` would silently invent a ``group_name)`` column.
    """

    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for character in body:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(character)
    parts.append("".join(current))
    return parts


def columns_of(table: str) -> list[tuple[str, str]]:
    """``[(column, portable type)]`` for a core table, in declaration order.

    Read off :data:`CORE_SCHEMA` rather than from a live database, for two
    reasons. Order: ``StateBackend.table_columns`` answers with a *set*,
    which is the right shape for the additive migrations that ask it "does
    this column exist yet" and the wrong shape for anything that has to
    produce a stable column layout. Availability: a caller can ask what a
    table looks like without opening a connection at all.

    The type is the portable spelling (``TEXT`` / ``INTEGER`` / ``REAL``),
    before :func:`schema_for` substitutes the dialect's own. Returns an empty
    list for a table that is not in the shared schema — ``chunks_fts`` and
    ``chunks_vocab`` live in the per-dialect extras and genuinely have no one
    portable definition.
    """

    for name, body in _CREATE_TABLE.findall(CORE_SCHEMA):
        if name.lower() != table.lower():
            continue
        columns: list[tuple[str, str]] = []
        for entry in _split_top_level(_SQL_COMMENT.sub("", body)):
            tokens = entry.split()
            if not tokens or tokens[0].upper() in _CONSTRAINT_KEYWORDS:
                continue
            columns.append((tokens[0], tokens[1].upper() if len(tokens) > 1 else "TEXT"))
        return columns
    return []


def primary_key_of(table: str) -> str | None:
    """The single-column primary key of a core table, or ``None``.

    ``None`` covers both "no primary key" and a *composite* one
    (``idp_groups``), because the caller this exists for — keyset pagination
    in :mod:`pheasant.analytics` — needs one orderable column and a composite
    key is not that.
    """

    for name, body in _CREATE_TABLE.findall(CORE_SCHEMA):
        if name.lower() != table.lower():
            continue
        for entry in _split_top_level(_SQL_COMMENT.sub("", body)):
            tokens = entry.split()
            if not tokens or tokens[0].upper() in _CONSTRAINT_KEYWORDS:
                continue
            if "PRIMARY KEY" in " ".join(tokens[1:]).upper():
                return tokens[0]
        return None
    return None
