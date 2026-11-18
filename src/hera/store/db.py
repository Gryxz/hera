from __future__ import annotations
import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 6


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (and create or migrate) the state store. WAL so a crash loses no committed work."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"state store is schema {version}; this build understands {SCHEMA_VERSION}")
    if version < 1:
        sql = resources.files("hera.store").joinpath("schema.sql").read_text(encoding="utf-8")
        conn.executescript("BEGIN;" + sql + "PRAGMA user_version=1;COMMIT;")
        version = 1
    if version < 2:      # grants.declassified: only such grants can release sensitive-origin text
        conn.executescript("BEGIN;ALTER TABLE grants ADD COLUMN declassified INTEGER NOT NULL DEFAULT 0;"
                           "PRAGMA user_version=2;COMMIT;")
    if version < 3:      # disputes: an implementer or reviewer contests a visible check
        conn.executescript(
            "BEGIN;CREATE TABLE IF NOT EXISTS disputes ("
            "id TEXT PRIMARY KEY, task_id TEXT, checkset_hash TEXT, check_id TEXT, requirement_id TEXT, "
            "argument TEXT, status TEXT NOT NULL DEFAULT 'open', outcome TEXT, resolver TEXT, "
            "new_checkset_hash TEXT, opened_at REAL, resolved_at REAL);"
            "PRAGMA user_version=3;COMMIT;")
    if version < 4:      # agent loop: one row per implementer attempt, one per model turn (resume)
        conn.executescript(
            "BEGIN;CREATE TABLE IF NOT EXISTS attempts ("
            "id TEXT PRIMARY KEY, task_id TEXT NOT NULL, seq INTEGER NOT NULL, tier TEXT, provider TEXT, "
            "model_id TEXT, candidate TEXT, status TEXT NOT NULL, base_commit TEXT, final_commit TEXT, "
            "summary TEXT, declared_scope_json TEXT, twin_pattern TEXT, started_at REAL, ended_at REAL, "
            "UNIQUE(task_id, seq));"
            "CREATE TABLE IF NOT EXISTS turns ("
            "attempt_id TEXT NOT NULL, seq INTEGER NOT NULL, request_sha256 TEXT NOT NULL, reply_json TEXT, "
            "tool_results_json TEXT, checkpoint_commit TEXT, usd REAL, created_at REAL, "
            "PRIMARY KEY(attempt_id, seq));"
            "PRAGMA user_version=4;COMMIT;")
    if version < 5:      # task orchestrator: one row per decision of the recovery ladder (resume)
        conn.executescript(
            "BEGIN;CREATE TABLE IF NOT EXISTS task_events ("
            "task_id TEXT NOT NULL, seq INTEGER NOT NULL, at REAL, kind TEXT NOT NULL, detail_json TEXT, "
            "PRIMARY KEY(task_id, seq));"
            "PRAGMA user_version=5;COMMIT;")
    if version < 6:      # requirements summaries (immutable), their decisions (append-only), checksets.summary_hash
        conn.executescript(
            "BEGIN;CREATE TABLE IF NOT EXISTS summaries ("
            "hash TEXT PRIMARY KEY, task_id TEXT NOT NULL, summary_json TEXT NOT NULL, created_at REAL NOT NULL);"
            "CREATE TABLE IF NOT EXISTS summary_decisions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, summary_hash TEXT NOT NULL REFERENCES summaries(hash), "
            "decision TEXT NOT NULL CHECK (decision IN ('approved','rejected')), decider TEXT NOT NULL, note TEXT, "
            "may_leave_machine INTEGER CHECK (may_leave_machine IN (0,1)), decided_at REAL NOT NULL, "
            "CHECK ((decision='approved') = (may_leave_machine IS NOT NULL)));"
            "CREATE INDEX IF NOT EXISTS summary_decisions_hash ON summary_decisions(summary_hash, id);"
            "CREATE TRIGGER IF NOT EXISTS summaries_no_update BEFORE UPDATE ON summaries "
            "BEGIN SELECT RAISE(ABORT, 'summaries are immutable'); END;"
            "CREATE TRIGGER IF NOT EXISTS summaries_no_delete BEFORE DELETE ON summaries "
            "BEGIN SELECT RAISE(ABORT, 'summaries are immutable'); END;"
            "CREATE TRIGGER IF NOT EXISTS summary_decisions_no_update BEFORE UPDATE ON summary_decisions "
            "BEGIN SELECT RAISE(ABORT, 'summary decisions are append-only'); END;"
            "CREATE TRIGGER IF NOT EXISTS summary_decisions_no_delete BEFORE DELETE ON summary_decisions "
            "BEGIN SELECT RAISE(ABORT, 'summary decisions are append-only'); END;"
            + ("" if any(r[1] == "summary_hash" for r in conn.execute("PRAGMA table_info(checksets)"))
               else "ALTER TABLE checksets ADD COLUMN summary_hash TEXT;")
            + "PRAGMA user_version=6;COMMIT;")
