"""Harness storage. Every table is ``harness_``-prefixed and created additively.

The journal (``harness_events``) is the record of truth for a run; the other
run tables are projections the coordinator writes in the same transaction as
the journal entry that justifies them, so a reader can always rebuild them.
Bulky content never sits in the journal: it lives in ``harness_blobs`` under
its digest, and a journal entry names the digest only after the bytes landed.
"""

_TABLES = """
CREATE TABLE IF NOT EXISTS harness_blobs (
  digest TEXT PRIMARY KEY,
  size_bytes BIGINT NOT NULL,
  media_type TEXT NOT NULL,
  content_b64 TEXT NOT NULL,
  classification TEXT NOT NULL DEFAULT 'internal',
  created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS harness_registrations (
  kb_id TEXT NOT NULL,
  id TEXT NOT NULL,
  uri TEXT NOT NULL,
  resolver TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  freshness_json TEXT NOT NULL,
  operations_json TEXT NOT NULL,
  classification TEXT NOT NULL,
  label TEXT NOT NULL,
  registered_by TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL,
  revoked_at DOUBLE PRECISION,
  PRIMARY KEY (kb_id, id)
);
CREATE TABLE IF NOT EXISTS harness_grants (
  kb_id TEXT NOT NULL,
  registration_id TEXT NOT NULL,
  principal TEXT NOT NULL,
  granted_by TEXT NOT NULL,
  granted_at DOUBLE PRECISION NOT NULL,
  revoked_at DOUBLE PRECISION,
  PRIMARY KEY (kb_id, registration_id, principal)
);
CREATE TABLE IF NOT EXISTS harness_revisions (
  kb_id TEXT NOT NULL,
  registration_id TEXT NOT NULL,
  revision TEXT NOT NULL,
  kind TEXT NOT NULL,
  manifest_digest TEXT NOT NULL,
  entries INTEGER NOT NULL,
  revision_time DOUBLE PRECISION,
  retrieved_at DOUBLE PRECISION NOT NULL,
  time_bound INTEGER NOT NULL,
  PRIMARY KEY (kb_id, registration_id, revision)
);
CREATE TABLE IF NOT EXISTS harness_runs (
  kb_id TEXT NOT NULL,
  id TEXT NOT NULL,
  tenant TEXT NOT NULL,
  actor TEXT NOT NULL,
  client_key TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  request_json TEXT NOT NULL,
  workload TEXT NOT NULL,
  state TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  last_seq INTEGER NOT NULL DEFAULT 0,
  workflow_version INTEGER NOT NULL,
  lease_owner TEXT,
  lease_token TEXT,
  lease_until DOUBLE PRECISION,
  fence INTEGER NOT NULL DEFAULT 0,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  deadline_at DOUBLE PRECISION NOT NULL,
  budget_json TEXT NOT NULL,
  usage_json TEXT NOT NULL,
  versions_json TEXT NOT NULL,
  wait_json TEXT,
  result_json TEXT,
  parent_run TEXT,
  created_at DOUBLE PRECISION NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (kb_id, id),
  UNIQUE (kb_id, actor, client_key)
);
CREATE TABLE IF NOT EXISTS harness_events (
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  data_json TEXT NOT NULL,
  visibility TEXT NOT NULL DEFAULT 'user',
  created_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS harness_steps (
  run_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  action TEXT NOT NULL,
  identity TEXT NOT NULL,
  input_json TEXT NOT NULL,
  depends_json TEXT NOT NULL,
  capability TEXT NOT NULL,
  effect_class TEXT NOT NULL,
  required INTEGER NOT NULL,
  candidate TEXT,
  status TEXT NOT NULL,
  current_attempt TEXT,
  result_json TEXT,
  policy_json TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (run_id, step_id)
);
CREATE TABLE IF NOT EXISTS harness_attempts (
  attempt_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  fence INTEGER NOT NULL,
  worker TEXT NOT NULL,
  status TEXT NOT NULL,
  reserved_json TEXT NOT NULL,
  spent_json TEXT,
  error_class TEXT,
  started_at DOUBLE PRECISION NOT NULL,
  finished_at DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS harness_outbox (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL,
  done_at DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS harness_evidence (
  run_id TEXT NOT NULL,
  id TEXT NOT NULL,
  registration_id TEXT,
  revision TEXT,
  locator_json TEXT NOT NULL,
  method TEXT NOT NULL,
  status TEXT NOT NULL,
  extract TEXT NOT NULL,
  extract_digest TEXT NOT NULL,
  classification TEXT NOT NULL,
  flags_json TEXT NOT NULL,
  step_id TEXT,
  observed_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (run_id, id)
);
CREATE TABLE IF NOT EXISTS harness_artifacts (
  run_id TEXT NOT NULL,
  digest TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  media_type TEXT NOT NULL,
  parent_digest TEXT,
  step_id TEXT,
  provenance_json TEXT NOT NULL,
  review_status TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (run_id, digest)
);
CREATE TABLE IF NOT EXISTS harness_verifications (
  run_id TEXT NOT NULL,
  id TEXT NOT NULL,
  criterion_id TEXT NOT NULL,
  artifact_digest TEXT NOT NULL,
  input_digests_json TEXT NOT NULL,
  environment_json TEXT NOT NULL,
  grader TEXT NOT NULL,
  grader_version TEXT NOT NULL,
  outcome TEXT NOT NULL,
  observed_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (run_id, id)
);
CREATE TABLE IF NOT EXISTS harness_effects (
  operation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  adapter TEXT NOT NULL,
  target TEXT NOT NULL,
  expected_revision TEXT,
  payload_digest TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  state TEXT NOT NULL,
  downstream_id TEXT,
  downstream_revision TEXT,
  disposition TEXT,
  reconciliation_owner TEXT,
  dispatch_count INTEGER NOT NULL DEFAULT 0,
  created_at DOUBLE PRECISION NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS harness_sandboxes (
  generation TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  tenant TEXT NOT NULL,
  profile TEXT NOT NULL,
  image_digest TEXT NOT NULL,
  root TEXT NOT NULL,
  lease_until DOUBLE PRECISION NOT NULL,
  expires_at DOUBLE PRECISION NOT NULL,
  state TEXT NOT NULL,
  pids_json TEXT NOT NULL DEFAULT '[]',
  created_at DOUBLE PRECISION NOT NULL,
  destroyed_at DOUBLE PRECISION,
  disposal_ms DOUBLE PRECISION,
  disposal_json TEXT
);
CREATE TABLE IF NOT EXISTS harness_test_sink (
  operation_id TEXT PRIMARY KEY,
  payload_digest TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS harness_blind_sink (
  id TEXT PRIMARY KEY,
  payload_digest TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_harness_runs_state ON harness_runs (kb_id, state, updated_at);
CREATE INDEX IF NOT EXISTS idx_harness_attempts_step ON harness_attempts (run_id, step_id);
CREATE INDEX IF NOT EXISTS idx_harness_effects_run ON harness_effects (run_id);
CREATE INDEX IF NOT EXISTS idx_harness_sandboxes_state ON harness_sandboxes (state, lease_until);
"""

#: Revision 2: working through connected accounts. A connection's harness policy
#: (what may be done through it, where), its history, and one audit row per call.
_CONNECTIONS = """
CREATE TABLE IF NOT EXISTS harness_connection_policies (
  kb_id TEXT NOT NULL,
  connection TEXT NOT NULL,
  provider TEXT NOT NULL,
  version INTEGER NOT NULL,
  actions_json TEXT NOT NULL,
  resources_json TEXT NOT NULL,
  write_resources_json TEXT NOT NULL,
  limits_json TEXT NOT NULL,
  approval_json TEXT NOT NULL,
  enabled INTEGER NOT NULL,
  updated_by TEXT NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (kb_id, connection)
);
CREATE TABLE IF NOT EXISTS harness_connection_policy_history (
  kb_id TEXT NOT NULL,
  connection TEXT NOT NULL,
  version INTEGER NOT NULL,
  policy_json TEXT NOT NULL,
  changed_by TEXT NOT NULL,
  changed_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (kb_id, connection, version)
);
CREATE TABLE IF NOT EXISTS harness_connection_calls (
  kb_id TEXT NOT NULL,
  id TEXT PRIMARY KEY,
  run_id TEXT,
  purpose TEXT NOT NULL,
  actor TEXT NOT NULL,
  connection TEXT NOT NULL,
  provider TEXT NOT NULL,
  action TEXT NOT NULL,
  method TEXT NOT NULL,
  host TEXT NOT NULL,
  resource TEXT NOT NULL,
  outcome TEXT NOT NULL,
  status_code INTEGER,
  bytes BIGINT NOT NULL DEFAULT 0,
  reason TEXT,
  policy_version INTEGER,
  flags_json TEXT NOT NULL DEFAULT '{}',
  at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_harness_connection_calls_run
  ON harness_connection_calls (run_id, connection);
CREATE INDEX IF NOT EXISTS idx_harness_connection_calls_at ON harness_connection_calls (kb_id, at)
"""


def _statements(text: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in text.split(";") if s.strip())


MIGRATIONS = (_statements(_TABLES), _statements(_CONNECTIONS))
