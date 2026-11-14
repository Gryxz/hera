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
