from __future__ import annotations
import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 3


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
