from __future__ import annotations
import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 1


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
        conn.executescript("BEGIN;" + sql + f"PRAGMA user_version={SCHEMA_VERSION};COMMIT;")
