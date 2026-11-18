"""The task-event log is safe for two writers, and a task has one runner at a time."""
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from hera.agent import orchestrator as orch
from hera.agent.orchestrator import TaskBusy, _append, _task_lock, load_events
from hera.store import db


@pytest.fixture
def db_file(tmp_path):
    path = tmp_path / "state.sqlite"
    db.connect(path).close()
    return path


def test_append_returns_the_seq_of_the_row_it_inserted(conn):
    seqs = [_append(conn, "t", "k", {"i": i})["seq"] for i in range(3)]
    assert seqs == [1, 2, 3] and [e["seq"] for e in load_events(conn, "t")] == [1, 2, 3]
    assert _append(conn, "other", "k", {})["seq"] == 1                 # numbering is per task
    assert not conn.in_transaction


def test_append_inside_a_callers_transaction_uses_that_transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    ev = _append(conn, "t", "k", {})
    assert conn.in_transaction                                         # not committed behind the caller's back
    conn.execute("ROLLBACK")
    assert ev["seq"] == 1 and load_events(conn, "t") == []


def test_a_duplicate_seq_is_refused_by_the_primary_key(conn):
    _append(conn, "t", "k", {})
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO task_events(task_id, seq, at, kind) VALUES ('t', 1, 0, 'x')")


def test_two_connections_appending_from_two_threads_get_unique_gap_free_seqs(db_file):
    n, got, errors = 40, [[], []], []
    start = threading.Barrier(2)

    def writer(i):
        try:
            c = db.connect(db_file)                                    # one connection per thread, same file
            try:
                start.wait(timeout=10)
                for j in range(n):
                    got[i].append(_append(c, "t", "k", {"w": i, "j": j})["seq"])
            finally:
                c.close()
        except BaseException as e:                                     # surfaced below, not swallowed by the thread
            errors.append(e)
    threads = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads) and errors == []
    assert got[0] == sorted(got[0]) and got[1] == sorted(got[1])      # each writer saw increasing numbers
    assert sorted(got[0] + got[1]) == list(range(1, 2 * n + 1))       # no duplicate, no gap, none lost
    c = db.connect(db_file)
    try:
        rows = load_events(c, "t")
    finally:
        c.close()
    assert [e["seq"] for e in rows] == list(range(1, 2 * n + 1))
    for e in rows:                                                     # each returned seq belongs to its own row
        w, j = e["detail"]["w"], e["detail"]["j"]
        assert got[w][j] == e["seq"]


def test_a_second_writer_waits_for_the_first_and_reads_back_its_own_seq(db_file):
    a = db.connect(db_file)
    out = {}

    def second():
        b = db.connect(db_file)
        try:
            out["ev"] = _append(b, "t", "second", {})
        finally:
            b.close()
    try:
        a.execute("BEGIN IMMEDIATE")                                   # the first writer holds the write lock
        t = threading.Thread(target=second)
        t.start()
        t.join(timeout=0.3)
        assert t.is_alive() and "ev" not in out                        # the second one is waiting, not interleaving
        first = _append(a, "t", "first", {})
        a.execute("COMMIT")
        t.join(timeout=30)
        assert not t.is_alive()
    finally:
        a.close()
    assert first["seq"] == 1 and out["ev"]["seq"] == 2


# ---- one runner per task ---------------------------------------------------------------------
def test_a_second_run_of_the_same_task_is_refused_and_other_tasks_are_not(db_file):
    a, b = db.connect(db_file), db.connect(db_file)
    try:
        with _task_lock(a, "t1"):
            with pytest.raises(TaskBusy, match="already being run"):
                with _task_lock(b, "t1"):
                    pytest.fail("the lock was granted twice")
            with _task_lock(b, "t2"):                                  # a different task is not blocked
                pass
        with _task_lock(b, "t1"):                                      # released: the task can be run again
            pass
    finally:
        a.close()
        b.close()
    assert issubclass(TaskBusy, orch.TaskConfigError)                  # the CLI prints it and exits 2


def test_the_lock_is_not_taken_for_an_in_memory_database(conn):
    with _task_lock(conn, "t"), _task_lock(conn, "t"):
        pass


def test_the_lock_file_name_is_a_hash_of_the_task_id(db_file):
    c = db.connect(db_file)
    try:
        with _task_lock(c, "Weird/Name"), _task_lock(c, "weird/name"):   # ids differing only in case do not collide
            pass
    finally:
        c.close()
    names = [p.name for p in (db_file.parent / "locks").iterdir()]
    assert len(names) == 2 and all(len(n) == len("0" * 32 + ".lock") and "/" not in n for n in names)


def test_run_and_continue_refuse_a_busy_task_before_logging_or_sending_anything(db_file):
    a, b = db.connect(db_file), db.connect(db_file)
    spec = SimpleNamespace(task_id="busy")
    try:
        with _task_lock(a, "busy"):
            with pytest.raises(TaskBusy):
                orch.run_task(b, spec, providers={}, policy=None)
            with pytest.raises(TaskBusy):
                orch.continue_task(b, "busy", spec=spec, providers={}, policy=None)
        assert b.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0
    finally:
        a.close()
        b.close()


def test_a_lock_held_by_a_killed_process_is_released(db_file):
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time\n"
         "from hera.agent.orchestrator import _task_lock\n"
         "from hera.store import db\n"
         "c = db.connect(sys.argv[1])\n"
         "with _task_lock(c, 'crashy'):\n"
         "    print('locked', flush=True)\n"
         "    time.sleep(120)\n", str(db_file)],
        stdout=subprocess.PIPE, text=True)
    c = db.connect(db_file)
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(TaskBusy):
            with _task_lock(c, "crashy"):
                pass
        child.kill()
        child.wait(timeout=30)
        with _task_lock(c, "crashy"):                                  # a crash leaves no stale lock: resume works
            pass
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=30)
        child.stdout.close()
        c.close()
