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
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION   # now 4; see the later tests
    assert c.execute("SELECT declassified FROM grants WHERE id='g'").fetchone()[0] == 0


def test_v2_store_migrates_to_v3_and_gets_disputes_table(tmp_path):
    import sqlite3
    from importlib import resources
    p = tmp_path / "v2.sqlite"
    raw = sqlite3.connect(p)
    raw.executescript(resources.files("hera.store").joinpath("schema.sql").read_text(encoding="utf-8"))
    raw.execute("ALTER TABLE grants ADD COLUMN declassified INTEGER NOT NULL DEFAULT 0")
    raw.execute("INSERT INTO requirements(id,task_id,text,created_at) VALUES('t:R-1','t','x',1)")
    raw.execute("PRAGMA user_version=2")
    raw.commit()
    assert not raw.execute("SELECT name FROM sqlite_master WHERE name='disputes'").fetchall()
    raw.close()
    c = db.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION       # connect() migrates all the way up (v4 since S8a)
    cols = [r[1] for r in c.execute("PRAGMA table_info(disputes)")]
    assert cols == ["id", "task_id", "checkset_hash", "check_id", "requirement_id", "argument", "status",
                    "outcome", "resolver", "new_checkset_hash", "opened_at", "resolved_at"]
    assert c.execute("SELECT text FROM requirements WHERE id='t:R-1'").fetchone()[0] == "x"   # data kept
    c.close()
    db.connect(p).close()                      # reopening does nothing further


def test_v3_store_migrates_to_v4_and_gets_attempts_and_turns(tmp_path):
    import sqlite3
    p = tmp_path / "v3.sqlite"
    c = db.connect(p)
    c.execute("INSERT INTO requirements(id,task_id,text,created_at) VALUES('t:R-1','t','x',1)")
    # step back to a genuine v3 store: drop the v4 tables and reset the version
    c.executescript("DROP TABLE attempts; DROP TABLE turns; PRAGMA user_version=3;")
    c.close()
    raw = sqlite3.connect(p)
    assert not raw.execute("SELECT name FROM sqlite_master WHERE name IN ('attempts','turns')").fetchall()
    raw.close()
    c = db.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 4 == db.SCHEMA_VERSION
    assert [r[1] for r in c.execute("PRAGMA table_info(attempts)")] == [
        "id", "task_id", "seq", "tier", "provider", "model_id", "candidate", "status", "base_commit",
        "final_commit", "summary", "declared_scope_json", "twin_pattern", "started_at", "ended_at"]
    assert [r[1] for r in c.execute("PRAGMA table_info(turns)")] == [
        "attempt_id", "seq", "request_sha256", "reply_json", "tool_results_json", "checkpoint_commit",
        "usd", "created_at"]
    assert c.execute("SELECT text FROM requirements WHERE id='t:R-1'").fetchone()[0] == "x"
    c.execute("INSERT INTO attempts(id,task_id,seq,status) VALUES('a','t',1,'running')")
    import pytest
    with pytest.raises(sqlite3.IntegrityError):          # UNIQUE(task_id, seq)
        c.execute("INSERT INTO attempts(id,task_id,seq,status) VALUES('b','t',1,'running')")
    c.close()
    db.connect(p).close()                                 # reopening does nothing further
