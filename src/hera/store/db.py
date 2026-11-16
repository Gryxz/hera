from __future__ import annotations
import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 4


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
