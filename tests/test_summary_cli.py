"""`hera summary ...`, the summary requirement of `hera checks freeze` and `hera run`, and the task-level
check-set refusal (the same world as test_cli_run)."""
import re

import pytest

from hera import cli, paths
from hera.oracle import checkstore, summary as sm
from hera.store import db
from scripted_llm import step
from test_cli_run import OBJECTIVE, SPEC, attempt, freeze, hash_of, world  # noqa: F401
from toyrepo import CHECKS, CHECK_FILES, CORRECT_CALC, REQUIREMENTS, approve_toy_summary, toy_summary_toml

H64 = re.compile(r"[0-9a-f]{64}")


def rows(sql, *args):
    conn = db.connect(paths.db_path())
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def write_summary(world, name, text):
    p = world.specdir / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def import_summary(world, capsys, task="s1", **kw):
    path = write_summary(world, f"SUMMARY-{task}.toml", toy_summary_toml(task, **kw))
    assert cli.main(["summary", "import", path]) == 0
    return H64.search(capsys.readouterr().out).group(0)


def approve(h, may="no", by="human:ana"):
    return cli.main(["summary", "approve", h, "--by", by, "--may-leave-machine", may])


def freeze_with(world, task, summary, spec="spec.toml", *extra):
    return cli.main(["checks", "freeze", "--task", task, "--repo", str(world.toy.source), "--spec",
                     str(world.specdir / spec), "--allow-unsandboxed", "--summary", summary, *extra])


def nothing_frozen(task):
    return not rows("SELECT 1 FROM checksets WHERE task_id=?", task) and not (paths.checks_dir() / task).exists()


# ---- hera summary ------------------------------------------------------------------------------------------
def test_import_prints_the_hash_and_the_screen_and_is_idempotent(world, capsys):
    path = write_summary(world, "S.toml", toy_summary_toml("s1"))
    assert cli.main(["summary", "import", path]) == 0
    out = capsys.readouterr().out
    h = H64.search(out).group(0)
    assert out.startswith(f"summary hash: {h}\n") and "Requirements summary: s1" in out
    assert f"hash {h[:12]} | NOT APPROVED" in out and "R-1    high" in out
    assert cli.main(["summary", "import", path]) == 0 and h in capsys.readouterr().out
    assert len(rows("SELECT hash FROM summaries")) == 1
    assert approve(h) == 0
    capsys.readouterr()
    assert cli.main(["summary", "import", path]) == 0                  # the screen shows the state it is in now
    assert "APPROVED | text may leave the machine: no | by human:ana" in capsys.readouterr().out


def test_import_refuses_a_bad_file_without_echoing_it(world, capsys):
    bad = write_summary(world, "bad.toml", toy_summary_toml("s1").replace('confidence = "high"', 'confidence = "SECRET-CANARY"', 1))
    assert cli.main(["summary", "import", bad]) == 2
    err = capsys.readouterr().err
    assert "requirements[1]: confidence" in err and "SECRET-CANARY" not in err
    assert rows("SELECT 1 FROM summaries") == []
    assert cli.main(["summary", "import", str(world.specdir / "missing.toml")]) == 2


def test_show_by_hash_and_by_task_and_list(world, capsys):
    h1 = import_summary(world, capsys, "s1")
    assert cli.main(["summary", "show", h1]) == 0
    assert capsys.readouterr().out.startswith("Requirements summary: s1")
    edited = toy_summary_toml("s1").replace("12.50 can be read", "12.50 can be parsed", 1)
    path = write_summary(world, "S2.toml", edited)
    assert cli.main(["summary", "import", path]) == 0
    h2 = H64.search(capsys.readouterr().out).group(0)
    assert h2 != h1
    assert cli.main(["summary", "show", "--task", "s1"]) == 0
    assert f"hash {h2[:12]}" in capsys.readouterr().out                 # the latest imported
    assert cli.main(["summary", "show", "--task", "s1", "--full"]) == 0
    assert cli.main(["summary", "show"]) == 2 and cli.main(["summary", "show", h1, "--task", "s1"]) == 2
    assert cli.main(["summary", "show", "--task", "nope"]) == 2
    assert cli.main(["summary", "show", "0" * 64]) == 2
    capsys.readouterr()
    assert approve(h1) == 0
    capsys.readouterr()
    assert cli.main(["summary", "list"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"{h1}  task=s1  state=approved  by=human:ana")
    assert out[1].startswith(f"{h2}  task=s1  state=not approved")
    assert cli.main(["summary", "list", "--task", "other"]) == 0 and "no summaries" in capsys.readouterr().out


def test_approve_needs_may_leave_machine_and_a_human_and_records_both(world, capsys):
    h = import_summary(world, capsys)
    with pytest.raises(SystemExit) as e:                               # no default for the declassification control
        cli.main(["summary", "approve", h, "--by", "human:ana"])
    assert e.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["summary", "approve", h, "--by", "human:ana", "--may-leave-machine", "maybe"])
    with pytest.raises(SystemExit):
        cli.main(["summary", "approve", h, "--may-leave-machine", "yes"])
    capsys.readouterr()
    assert approve(h, by="ana") == 2 and approve(h, by="model:x") == 2 and approve(h, by="human:") == 2
    assert "decider: must be a person" in capsys.readouterr().err
    assert rows("SELECT 1 FROM summary_decisions") == []
    assert approve(h, may="yes") == 0
    assert "approved" in capsys.readouterr().out
    (r,) = rows("SELECT decision, decider, may_leave_machine FROM summary_decisions")
    assert tuple(r) == ("approved", "human:ana", 1)


def test_approving_with_an_unanswered_question_is_refused_and_an_answered_edit_is_approved(world, capsys):
    text = toy_summary_toml("s1") + '\n[[questions]]\nq = "Should commas be accepted?"\n'
    h = H64.search((cli.main(["summary", "import", write_summary(world, "Q.toml", text)]),
                    capsys.readouterr().out)[1]).group(0)
    assert approve(h) == 2
    assert "1 unanswered" in capsys.readouterr().err and rows("SELECT 1 FROM summary_decisions") == []
    assert cli.main(["summary", "show", h]) == 0
    assert "[UNANSWERED - blocks approval]" in capsys.readouterr().out
    h2 = H64.search((cli.main(["summary", "import", write_summary(world, "Q2.toml", text + 'answer = "no"\n')]),
                     capsys.readouterr().out)[1]).group(0)
    assert h2 != h and approve(h2) == 0


def test_reject_needs_a_note_and_a_rejected_summary_can_be_approved_later(world, capsys):
    h = import_summary(world, capsys)
    with pytest.raises(SystemExit):
        cli.main(["summary", "reject", h, "--by", "human:ana"])
    capsys.readouterr()
    assert cli.main(["summary", "reject", h, "--by", "human:ana", "--note", "  "]) == 2
    assert cli.main(["summary", "reject", h, "--by", "bot", "--note", "no"]) == 2
    assert cli.main(["summary", "reject", h, "--by", "human:ana", "--note", "outcomes are vague"]) == 0
    capsys.readouterr()
    assert cli.main(["summary", "show", h]) == 0
    assert "REJECTED by human:ana: outcomes are vague" in capsys.readouterr().out
    assert approve(h, may="yes", by="human:ben") == 0                  # allowed: the latest decision wins
    assert [tuple(r) for r in rows("SELECT decision, decider FROM summary_decisions ORDER BY id")] == [
        ("rejected", "human:ana"), ("approved", "human:ben")]
    capsys.readouterr()
    assert cli.main(["summary", "show", h]) == 0
    assert "APPROVED | text may leave the machine: yes | by human:ben" in capsys.readouterr().out


def test_summary_commands_fail_cleanly_without_a_store(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERA_HOME", str(tmp_path / "empty"))
    assert cli.main(["summary", "list"]) == 2 and "hera init" in capsys.readouterr().err


# ---- hera checks freeze ------------------------------------------------------------------------------------
def test_freeze_needs_a_summary(world, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "freeze", "--task", "f1", "--repo", str(world.toy.source), "--spec",
                  str(world.specdir / "spec.toml"), "--allow-unsandboxed"])
    assert e.value.code == 2 and nothing_frozen("f1")


def test_freeze_refuses_a_summary_that_is_unknown_unapproved_rejected_or_for_another_task(world, capsys):
    h = import_summary(world, capsys, "f2")
    assert freeze_with(world, "f2", "0" * 64) == 2 and "unknown summary" in capsys.readouterr().err
    assert freeze_with(world, "f2", "nonsense") == 2
    assert freeze_with(world, "f2", h) == 2 and "no decision yet" in capsys.readouterr().err
    assert cli.main(["summary", "reject", h, "--by", "human:ana", "--note", "no"]) == 0
    capsys.readouterr()
    assert freeze_with(world, "f2", h, "spec.toml", "--skip-validation") == 2
    assert "state: rejected" in capsys.readouterr().err
    other = import_summary(world, capsys, "elsewhere")
    assert approve(other) == 0
    capsys.readouterr()
    assert freeze_with(world, "f2", other) == 2 and "belongs to a different task" in capsys.readouterr().err
    assert nothing_frozen("f2")


def test_freeze_refuses_when_the_summary_and_the_spec_disagree_and_names_the_requirements(world, capsys):
    reqs = list(REQUIREMENTS)
    changed = [reqs[0], type(reqs[1])("R-2", "parse_amount('') raises KeyError"), reqs[2]]
    h = approve_toy_summary("f3", world.specdir, requirements=changed)
    assert freeze_with(world, "f3", h) == 2
    err = capsys.readouterr().err
    assert "requirement(s) R-2 " in err and "R-1" not in err and "KeyError" not in err
    fewer = approve_toy_summary("f3", world.specdir, requirements=reqs[:2])          # the spec has R-3 as well
    assert freeze_with(world, "f3", fewer) == 2 and "requirement(s) R-3 " in capsys.readouterr().err
    more = approve_toy_summary("f3", world.specdir, requirements=reqs + [type(reqs[0])("R-4", "new")])
    assert freeze_with(world, "f3", more) == 2 and "requirement(s) R-4 " in capsys.readouterr().err
    assert nothing_frozen("f3")


def test_freeze_with_an_approved_summary_freezes_and_stores_its_hash(world, capsys):
    h = approve_toy_summary("f4", world.specdir, may_leave=True)
    assert freeze_with(world, "f4", h) == 0
    out = capsys.readouterr().out
    ch = hash_of(out)
    assert f"summary: {h[:12]} (approved)" in out
    (r,) = rows("SELECT summary_hash FROM checksets WHERE hash=?", ch)
    assert r["summary_hash"] == h
    conn = db.connect(paths.db_path())
    try:
        assert checkstore.summary_hash_of(conn, ch) == h and checkstore.summary_hash_of(conn, "0" * 64) is None
        cs = checkstore.load(conn, ch)
        checkstore.verify_integrity(cs)                                              # the summary text is in the manifest
        import json
        stored = json.loads((cs.root / "manifest.json").read_text())["texts"]["summary"]
        assert stored == sm.canonical_json(sm.get_summary(conn, h))
    finally:
        conn.close()
    assert freeze_with(world, "f4", h) == 0 and hash_of(capsys.readouterr().out) == ch      # idempotent
    other = approve_toy_summary("f4", world.specdir, confidence="low")               # another approved summary
    assert freeze_with(world, "f4", other) == 0
    assert hash_of(capsys.readouterr().out) != ch                                    # the check set is bound to it


# ---- hera run ----------------------------------------------------------------------------------------------
def library_checkset(world, task="toy"):
    """A check set frozen the old way (no summary) for the toy repository's own task."""
    conn = db.connect(paths.db_path())
    try:
        cs = checkstore.freeze(conn, task_id=task, requirements=REQUIREMENTS, checks=CHECKS, files=CHECK_FILES)
        conn.execute("INSERT OR IGNORE INTO tasks(id, repo_id, objective, status, created_at, updated_at) "
                     "VALUES(?,'toy-src',NULL,'new',0,0)", (task,))
        return cs.hash
    finally:
        conn.close()


def run(task, h, *extra):
    return cli.main(["run", "--task", task, "--checkset", h, "--objective", OBJECTIVE, "--allow-unsandboxed", *extra])


def routing_of(task):
    from hera.agent.orchestrator import load_events
    conn = db.connect(paths.db_path())
    try:
        return next((e["detail"] for e in load_events(conn, task) if e["kind"] == "routing"), None)
    finally:
        conn.close()


def test_run_is_refused_for_a_check_set_frozen_without_a_summary(world, capsys):
    servers = world.configure(default=attempt(CORRECT_CALC))
    h = library_checkset(world)
    assert run("toy", h) == 2
    err = capsys.readouterr().err
    assert "no approved requirements summary" in err and "hera summary import" in err
    assert "hera summary approve" in err and "--summary HASH" in err and "--without-approved-summary" in err
    assert servers[0].requests == [] and routing_of("toy") is None
    assert rows("SELECT 1 FROM task_events") == [] and rows("SELECT 1 FROM attempts") == []


def test_the_override_runs_and_records_that_the_summary_was_not_approved(world, capsys):
    servers = world.configure(default=attempt(CORRECT_CALC))
    h = library_checkset(world)
    assert run("toy", h, "--without-approved-summary") == 0
    out = capsys.readouterr().out
    assert "summary not approved (override)" in out and "status: PASSED" in out
    assert routing_of("toy")["summary"] == "not approved (override)"
    assert len(servers[0].requests) == 2
    assert cli.main(["task", "status", "toy"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("summary: none") and out[-1] == "status: PASSED"


def test_run_with_an_approved_summary_records_its_hash_and_task_status_shows_it(world, capsys):
    servers = world.configure(default=attempt(CORRECT_CALC))
    h = approve_toy_summary("r1", world.specdir, may_leave=True)
    assert freeze_with(world, "r1", h) == 0
    ch = hash_of(capsys.readouterr().out)
    assert run("r1", ch) == 0
    out = capsys.readouterr().out
    assert f"summary {h[:12]}" in out and "override" not in out
    assert routing_of("r1")["summary"] == h
    assert cli.main(["task", "status", "r1"]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first == f"summary: {h} approved by human:tester (text may leave the machine: yes)"
    assert run("r1", ch, "--without-approved-summary") == 0                          # the override changes nothing here
    assert routing_of("r1")["summary"] == h and len(servers[0].requests) == 2


def test_run_is_refused_once_the_latest_decision_is_not_an_approval(world, capsys):
    servers = world.configure(default=attempt(CORRECT_CALC))
    h = approve_toy_summary("r2", world.specdir)
    assert freeze_with(world, "r2", h) == 0
    ch = hash_of(capsys.readouterr().out)
    assert cli.main(["summary", "reject", h, "--by", "human:ben", "--note", "changed my mind"]) == 0
    capsys.readouterr()
    assert run("r2", ch) == 2
    err = capsys.readouterr().err
    assert "state: rejected" in err and f"hera summary approve {h}" in err
    assert servers[0].requests == [] and routing_of("r2") is None
    assert cli.main(["task", "status", "r2"]) == 0                                   # frozen, never run: no events
    assert capsys.readouterr().out.splitlines() == [f"summary: {h} NOT APPROVED (rejected)", "status: new"]
    assert run("r2", ch, "--without-approved-summary") == 0
    assert routing_of("r2")["summary"] == "not approved (override)"
    assert cli.main(["task", "status", "r2"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "status: PASSED"


def test_a_task_that_ran_against_one_check_set_is_not_run_against_another(world, capsys):
    servers = world.configure(default=attempt(CORRECT_CALC))
    a = approve_toy_summary("r3", world.specdir)
    assert freeze_with(world, "r3", a) == 0
    ca = hash_of(capsys.readouterr().out)
    b = approve_toy_summary("r3", world.specdir, confidence="low")
    assert freeze_with(world, "r3", b) == 0
    cb = hash_of(capsys.readouterr().out)
    assert ca != cb
    assert run("r3", ca) == 0
    capsys.readouterr()
    before = rows("SELECT seq, kind FROM task_events WHERE task_id='r3'")
    requests = len(servers[0].requests)
    assert run("r3", cb) == 2
    err = capsys.readouterr().err
    assert ("this task ran against another check set; after an amended check use `hera task continue`; "
            "otherwise start a new task id") in err
    assert rows("SELECT seq, kind FROM task_events WHERE task_id='r3'") == before and len(servers[0].requests) == requests
    assert run("r3", ca) == 0                                                         # the same check set still resumes


def test_the_library_is_unchanged_run_task_does_not_look_at_summaries(world):
    from hera.agent import orchestrator
    assert "summary" in orchestrator.TaskSpec.__dataclass_fields__
    assert orchestrator.TaskSpec.__dataclass_fields__["summary"].default is None
