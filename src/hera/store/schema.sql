-- HERA state store, schema version 1. SQLite only at baseline.
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, repo_id TEXT, objective TEXT, status TEXT NOT NULL DEFAULT 'new',
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS model_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, task_id TEXT, purpose TEXT,
  destination TEXT, tier TEXT NOT NULL, model_id TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
  usd REAL NOT NULL);
CREATE INDEX IF NOT EXISTS model_calls_task ON model_calls(task_id);
CREATE INDEX IF NOT EXISTS model_calls_at ON model_calls(at);
CREATE TABLE IF NOT EXISTS repo_policy (repo_id TEXT PRIMARY KEY, class TEXT NOT NULL, set_at REAL);
CREATE TABLE IF NOT EXISTS labels (
  artefact_id TEXT PRIMARY KEY, repo_class TEXT, content_hash TEXT,
  findings_json TEXT, derived_from_json TEXT);
CREATE TABLE IF NOT EXISTS grants (
  id TEXT PRIMARY KEY, content_hashes_json TEXT, destinations_json TEXT, purpose TEXT,
  expires_at REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS transmissions (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, destination TEXT, purpose TEXT,
  task_id TEXT, artefacts_json TEXT, bytes INTEGER, grant_ids_json TEXT, scanner_json TEXT,
  decision TEXT NOT NULL, reason TEXT, payload_hash TEXT, prev_hash TEXT NOT NULL, this_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requirements (id TEXT PRIMARY KEY, task_id TEXT, text TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS checksets (
  hash TEXT PRIMARY KEY, task_id TEXT, runner_cmd TEXT, manifest_json TEXT, frozen_at REAL, supersedes TEXT);
CREATE TABLE IF NOT EXISTS checks (
  id TEXT, checkset_hash TEXT, requirement_id TEXT, kind TEXT, visibility TEXT, sha256 TEXT,
  author_model TEXT, author_role TEXT, health TEXT DEFAULT 'valid', health_evidence_json TEXT,
  PRIMARY KEY (id, checkset_hash));
CREATE TABLE IF NOT EXISTS receipts (
  id TEXT PRIMARY KEY, task_id TEXT, candidate_commit TEXT, checkset_hash TEXT, env_hash TEXT,
  receipt_json TEXT NOT NULL, created_at REAL);
CREATE TABLE IF NOT EXISTS verdicts (receipt_id TEXT PRIMARY KEY, verdict TEXT NOT NULL, reasons_json TEXT);
CREATE TABLE IF NOT EXISTS attributions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT, label TEXT NOT NULL, confidence TEXT,
  rule TEXT, evidence_json TEXT, superseded_by INTEGER);
