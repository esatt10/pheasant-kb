"""Additive GTM storage. Existing tables retain their names and rows."""

MIGRATIONS = (
    tuple(
        statement.strip()
        for statement in """
CREATE TABLE IF NOT EXISTS gtm_runs (
  kb_id TEXT NOT NULL,
  id TEXT NOT NULL,
  owner TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  lease_token TEXT,
  lease_until TEXT,
  PRIMARY KEY (kb_id, id)
);
CREATE TABLE IF NOT EXISTS gtm_collection_state (
  source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gtm_catalog_companies (
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  company_id TEXT NOT NULL,
  settings_json TEXT NOT NULL,
  state_json TEXT NOT NULL DEFAULT '{}',
  refresh_seconds INTEGER NOT NULL,
  checked_at DOUBLE PRECISION,
  requested_at DOUBLE PRECISION,
  revision TEXT,
  PRIMARY KEY (source_id, company_id)
);
CREATE TABLE IF NOT EXISTS gtm_catalog_publications (
  source_id TEXT NOT NULL,
  company_id TEXT NOT NULL,
  revision TEXT NOT NULL,
  profile_json TEXT NOT NULL,
  markdown TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  PRIMARY KEY (source_id, company_id, revision),
  FOREIGN KEY (source_id, company_id) REFERENCES gtm_catalog_companies(source_id, company_id)
    ON DELETE CASCADE
);
""".split(";")
        if statement.strip()
    ),
)
