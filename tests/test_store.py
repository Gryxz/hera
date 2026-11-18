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
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION      # connect() migrates all the way up
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


def test_v4_store_migrates_to_v5_and_gets_task_events(tmp_path):
    import sqlite3
    import pytest
    p = tmp_path / "v4.sqlite"
    c = db.connect(p)
    c.execute("INSERT INTO requirements(id,task_id,text,created_at) VALUES('t:R-1','t','x',1)")
    c.execute("INSERT INTO attempts(id,task_id,seq,status) VALUES('t-a1','t',1,'submitted')")
    c.executescript("DROP TABLE task_events; PRAGMA user_version=4;")      # step back to a genuine v4 store
    c.close()
    raw = sqlite3.connect(p)
    assert not raw.execute("SELECT name FROM sqlite_master WHERE name='task_events'").fetchall()
    raw.close()
    c = db.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6 == db.SCHEMA_VERSION    # connect() migrates all the way up
    assert [r[1] for r in c.execute("PRAGMA table_info(task_events)")] == [
        "task_id", "seq", "at", "kind", "detail_json"]
    assert c.execute("SELECT text FROM requirements WHERE id='t:R-1'").fetchone()[0] == "x"     # data kept
    assert c.execute("SELECT status FROM attempts WHERE id='t-a1'").fetchone()[0] == "submitted"
    c.execute("INSERT INTO task_events(task_id,seq,at,kind,detail_json) VALUES('t',1,1.0,'routing','{}')")
    with pytest.raises(sqlite3.IntegrityError):                  # PRIMARY KEY(task_id, seq)
        c.execute("INSERT INTO task_events(task_id,seq,at,kind,detail_json) VALUES('t',1,2.0,'stopped','{}')")
    with pytest.raises(sqlite3.IntegrityError):                  # kind is required
        c.execute("INSERT INTO task_events(task_id,seq,at,kind,detail_json) VALUES('t',2,2.0,NULL,'{}')")
    c.close()
    db.connect(p).close()                                         # reopening does nothing further


def test_v5_store_migrates_to_v6_and_gets_summaries_and_the_checkset_column(tmp_path):
    import sqlite3
    import pytest
    p = tmp_path / "v5.sqlite"
    c = db.connect(p)
    c.execute("INSERT INTO checksets(hash,task_id,runner_cmd,manifest_json,frozen_at) VALUES('h1','t','pytest','{}',1)")
    # step back to a genuine v5 store: no summary tables (their triggers go with them), no checksets.summary_hash
    c.executescript("DROP TABLE summary_decisions; DROP TABLE summaries; "
                    "ALTER TABLE checksets DROP COLUMN summary_hash; PRAGMA user_version=5;")
    c.close()
    raw = sqlite3.connect(p)
    assert not raw.execute("SELECT name FROM sqlite_master WHERE name LIKE 'summar%'").fetchall()
    assert "summary_hash" not in [r[1] for r in raw.execute("PRAGMA table_info(checksets)")]
    raw.close()
    c = db.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6 == db.SCHEMA_VERSION
    assert [r[1] for r in c.execute("PRAGMA table_info(summaries)")] == ["hash", "task_id", "summary_json", "created_at"]
    assert [r[1] for r in c.execute("PRAGMA table_info(summary_decisions)")] == [
        "id", "summary_hash", "decision", "decider", "note", "may_leave_machine", "decided_at"]
    assert c.execute("SELECT hash, summary_hash FROM checksets").fetchone()[:] == ("h1", None)   # data kept, no summary
    c.execute("INSERT INTO summaries(hash,task_id,summary_json,created_at) VALUES('s','t','{}',1)")
    c.execute("INSERT INTO summary_decisions(summary_hash,decision,decider,may_leave_machine,decided_at) "
              "VALUES('s','approved','human:a',0,1)")
    with pytest.raises(sqlite3.IntegrityError):                  # an approval carries the flag, a rejection does not
        c.execute("INSERT INTO summary_decisions(summary_hash,decision,decider,decided_at) VALUES('s','approved','human:a',1)")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO summary_decisions(summary_hash,decision,decider,may_leave_machine,decided_at) "
                  "VALUES('s','rejected','human:a',1,1)")
    with pytest.raises(sqlite3.IntegrityError):                  # only approved or rejected
        c.execute("INSERT INTO summary_decisions(summary_hash,decision,decider,decided_at) VALUES('s','maybe','h',1)")
    with pytest.raises(sqlite3.IntegrityError):                  # a decision needs its summary
        c.execute("INSERT INTO summary_decisions(summary_hash,decision,decider,may_leave_machine,decided_at) "
                  "VALUES('nope','approved','human:a',0,1)")
    with pytest.raises(sqlite3.IntegrityError):                  # immutable and append-only
        c.execute("UPDATE summaries SET task_id='x'")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM summary_decisions")
    c.close()
    db.connect(p).close()                                        # reopening does nothing further
