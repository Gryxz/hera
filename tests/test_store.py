from hera.store import db


def test_schema_creates_and_is_idempotent(tmp_path):
    p = tmp_path / "state.sqlite"
    c = db.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tasks", "model_calls", "transmissions", "grants", "receipts", "verdicts"} <= names
    c.close()
    db.connect(p).close()                       # reopening must not fail or re-create


def test_newer_schema_is_refused(tmp_path):
    import pytest
    p = tmp_path / "s.sqlite"
    c = db.connect(p)
    c.execute("PRAGMA user_version=99")
    c.close()
    with pytest.raises(RuntimeError):
        db.connect(p)


def test_v1_store_migrates_to_v2(tmp_path):
    import sqlite3
    from importlib import resources
    p = tmp_path / "old.sqlite"
    raw = sqlite3.connect(p)
    raw.executescript(resources.files("hera.store").joinpath("schema.sql").read_text(encoding="utf-8"))
    raw.execute("INSERT INTO grants(id,content_hashes_json,destinations_json,purpose,expires_at,created_at)"
                " VALUES('g','[]','[]','x',1,1)")
    raw.execute("PRAGMA user_version=1")
    raw.commit(); raw.close()
    c = db.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 2
    assert c.execute("SELECT declassified FROM grants WHERE id='g'").fetchone()[0] == 0
