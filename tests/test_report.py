"""The learning ledger and `hera report`, on synthetic tasks produced by running the orchestrator with
scripted providers. Every number below follows from the scripts:

  toy   GOOD (and a stray file outside its declared scope)           1 attempt:  PASS
  toyb  BAD, BAD, diagnosis, GOOD                                    3 attempts: FAIL, FAIL (retry), PASS (fresh, told the diagnosis)
  toyc  OVERFIT, GOOD                                                2 attempts: OVERFIT, PASS (retry)
  toyd  TAMPERING, GOOD                                              2 attempts: TAMPER, PASS (fresh)
  toye  a check set that leaves R-3 uncovered                        1 attempt:  UNVERIFIABLE (attribution UNKNOWN)
  toyf  contract checks only, GOOD                                   1 attempt:  PASS (nothing rests on in-process checks)

Every verification runs with no sandbox (forced, so the test is the same everywhere); the receipts of
toy, toyb, toyc and toyf are then rewritten to say a sandbox was used, so toyd's pass and toye's verdict
are the only unsandboxed ones."""
import json

import pytest

from hera import cli, paths, report, sandbox
from hera.agent import orchestrator as orch
from hera.oracle.checkstore import freeze
from hera.oracle.verify import run_baseline
from hera.store import db
from scripted_llm import step
from test_orchestrator import (BAD, DIAGNOSIS, GOOD, OVERFIT, TAMPERING, Rig, attempt, hidden_tokens)
from toyrepo import (CHECK_FILES, CHECKS, CONTRACT_CHECKS, CONTRACT_FILES, CORRECT_CALC, REQUIREMENTS,
                     make_toy)

_BASELINE = []


class TaskRig(Rig):
    def __init__(self, conn, toy, cs, task_id):
        super().__init__(conn, toy, cs)
        self.task_id = task_id

    def spec(self, **kw):
        s = super().spec(**kw)
        return orch.dataclasses.replace(s, task_id=self.task_id)

    def kinds(self):
        return [e["kind"] for e in orch.load_events(self.conn, self.task_id)]


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("report")
    mp.setenv("HERA_HOME", str(root / "hera"))
    paths.ensure_layout()
    mp.setattr(sandbox, "detect_mechanism", lambda: "none")

    def cached(repo, adapter, **kw):
        if not _BASELINE:
            _BASELINE.append(run_baseline(repo, adapter, **{**kw, "allow_unsandboxed": True}))
        return _BASELINE[0]
    mp.setattr(orch, "run_baseline", cached)
    conn = db.connect(paths.db_path())
    rigs = []

    def task(task_id, scripts, *, checks=CHECKS, files=CHECK_FILES, reqs=REQUIREMENTS, strong=None):
        toy = make_toy(root / task_id, task_id=task_id)
        cs = freeze(conn, task_id=task_id, requirements=reqs, checks=checks, files=dict(files))
        r = TaskRig(conn, toy, cs, task_id)
        r.add("default", "openai", scripts)
        if strong is not None:
            r.add("strong", "openai", strong)
        rigs.append(r)
        return r.run()

    stray = [step("w", ("write_file", {"path": "calc.py", "content": CORRECT_CALC}),
                  ("write_file", {"path": "notes.txt", "content": "scratch\n"})),
             step("s", ("submit", {"summary": "done", "declared_scope": ["calc.py"]}))]
    outs = {
        "toy": task("toy", stray),
        "toyb": task("toyb", BAD + BAD + GOOD, strong=[step(DIAGNOSIS)]),
        "toyc": task("toyc", OVERFIT + GOOD),
        "toyd": task("toyd", TAMPERING + GOOD),
        "toye": task("toye", GOOD, checks=CHECKS[:3], reqs=REQUIREMENTS,
                     files={k: v for k, v in CHECK_FILES.items() if k != "test_calc_unchanged.py"}),
        "toyf": task("toyf", GOOD, checks=CONTRACT_CHECKS, files=CONTRACT_FILES),
    }
    for tid in ("toy", "toyb", "toyc", "toyf"):          # pretend these were verified in a sandbox
        for r in conn.execute("SELECT id, receipt_json FROM receipts WHERE task_id=?", (tid,)).fetchall():
            doc = json.loads(r["receipt_json"])
            assert doc["evidence"]["mechanism"] == "none"
            doc["evidence"]["mechanism"] = "bwrap"
            conn.execute("UPDATE receipts SET receipt_json=? WHERE id=?", (json.dumps(doc), r["id"]))
    w = type("World", (), {})()
    w.conn, w.outs, w.rigs = conn, outs, rigs
    yield w
    try:
        for r in rigs:                                    # nothing about a hidden check ever left the machine
            tokens = hidden_tokens(r.cs)
            for llm in r.servers:
                for body in llm.requests:
                    assert not [t for t in tokens if t in json.dumps(body, ensure_ascii=False)]
    finally:
        for r in rigs:
            for llm in r.servers:
                llm.close()
        conn.close()
        mp.undo()


@pytest.fixture(scope="module")
def recs(world):
    return report.attempt_records(world.conn)


@pytest.fixture(scope="module")
def summ(recs):
    return report.summary(recs)


def test_the_scripts_ended_the_way_the_docstring_says(world):
    assert {k: (o.status, o.attempts, o.verdict) for k, o in world.outs.items()} == {
        "toy": ("PASSED", 1, "PASS"), "toyb": ("PASSED", 3, "PASS"), "toyc": ("PASSED", 2, "PASS"),
        "toyd": ("PASSED", 2, "PASS"), "toye": ("NEEDS_HUMAN", 1, "UNVERIFIABLE"), "toyf": ("PASSED", 1, "PASS")}


def test_one_record_per_finished_attempt_from_stored_data_only(recs):
    assert [(r.task_id, r.attempt_seq) for r in recs] == [
        ("toy", 1), ("toyb", 1), ("toyb", 2), ("toyb", 3), ("toyc", 1), ("toyc", 2), ("toyd", 1), ("toyd", 2),
        ("toye", 1), ("toyf", 1)]
    by = {(r.task_id, r.attempt_seq): r for r in recs}
    a = by["toy", 1]
    assert (a.tier, a.provider, a.model_id, a.status, a.turns, a.plan, a.rung) == \
        ("default", "default", "mock-default", "submitted", 2, "fresh", 1)
    assert a.usd == pytest.approx(2 * 0.003) and a.verdict == "PASS" and a.attribution is None
    assert a.declared_scope == ("calc.py",) and a.out_of_scope == 1 and a.unsandboxed is False
    assert a.in_process_only == ("R-1", "R-2", "R-3") and a.receipt_id
    b2, b3 = by["toyb", 2], by["toyb", 3]
    assert (b2.plan, b2.rung, b2.feedback_kind, b2.verdict, b2.attribution) == ("retry", 2, "verdict", "FAIL", "IMPL_WRONG")
    assert b2.diagnosis_calls == 1 and b2.diagnosis_usd == pytest.approx(0.012)    # the strong-tier call that followed it
    assert b3.plan == "fresh" and b3.rung == 3 and b3.diagnosis_seq is not None and b3.candidate == "toyb-c2"
    assert by["toyc", 1].attribution == "IMPL_OVERFIT" and by["toyd", 1].attribution == "IMPL_TAMPER"
    e = by["toye", 1]
    assert e.verdict == "UNVERIFIABLE" and e.attribution == "UNKNOWN" and e.unsandboxed is True
    assert by["toyd", 2].unsandboxed is True and by["toyd", 2].passed
    assert by["toyf", 1].in_process_only == ()
    assert [r.later_attempt_passed for r in recs] == [False, True, True, False, True, False, True, False, False, False]
    assert all(r.task_usd > 0 for r in recs) and by["toyb", 1].task_usd == pytest.approx(6 * 0.003 + 0.012)


def test_summary_counts_and_pass_rates(summ):
    assert (summ["attempts"], summ["tasks"], summ["verified_attempts"]) == (10, 6, 10)
    assert summ["pass_rate_by_tier"] == {"default": report.rate(5, 10)}
    assert summ["pass_rate_by_model"] == {"mock-default": report.rate(5, 10)}
    fa = summ["first_attempt_pass_rate"]
    assert fa["overall"] == report.rate(2, 6) and fa["by_tier"]["default"] == report.rate(2, 6)
    assert summ["verdicts"]["counts"] == {"FAIL": 2, "OVERFIT": 1, "PASS": 5, "TAMPER": 1, "UNVERIFIABLE": 1}
    assert summ["verdicts"]["not_verified"] == {}


def test_the_unknown_rate_is_stated(summ):
    a = summ["attribution"]
    assert a["counts"] == {"IMPL_OVERFIT": 1, "IMPL_TAMPER": 1, "IMPL_WRONG": 2, "UNKNOWN": 1}
    assert a["attributed"] == 5 and a["unknown"] == {"num": 1, "den": 5, "rate": 0.2, "small": False}
    assert a["not_attributed"] == {"PASS": 5, "not verified": 0}
    text = report.render_summary(summ)
    assert "UNKNOWN attribution rate: 1/5 (20%)" in text


def test_dollars_per_verified_task_leave_out_the_unsandboxed_pass(summ):
    d = summ["dollars"]
    total = 20 * 0.003 + 0.012                                  # twenty default-tier calls and one diagnosis
    assert d["total"] == pytest.approx(total) and d["tasks"] == 6 and d["per_task"] == pytest.approx(total / 6)
    assert d["verified_tasks"] == 4                              # toy, toyb, toyc, toyf; not toyd (unsandboxed pass), not toye
    assert d["per_verified_task"] == pytest.approx(total / 4)
    assert summ["verdicts_without_sandbox"] == report.rate(3, 10)            # toyd twice, toye once


def test_every_pass_that_rests_on_in_process_checks_only_is_counted(summ):
    assert summ["passes_on_in_process_checks_only"] == report.rate(4, 5)      # all but the contract-checked toyf
    assert summ["attempts_with_out_of_scope_changes"] == report.rate(1, 10)


def test_retries_diagnosis_escalation_and_recovery(summ):
    r = summ["retries"]
    assert (r["succeeded"], r["failed"], r["not_verified"]) == (1, 1, 0) and r["success_rate"] == report.rate(1, 2)
    d = summ["diagnosis"]
    assert d["calls"] == 1 and d["usd"] == pytest.approx(0.012)
    assert d["next_candidate_evaluated"] == 1 and d["next_candidate_passed"] == report.rate(1, 1)
    assert summ["escalations"] == {"attempts": 0, "passed": report.rate(0, 0)}
    assert summ["later_attempt_passed"] == report.rate(4, 5)


def test_small_counts_are_marked_and_every_rate_shows_its_counts(summ):
    text = report.render_summary(summ)
    assert "retries that succeeded: 1/2 (50%) (n<5)" in text
    assert "next candidate passed: 1/1 (100%) (n<5)" in text
    assert "escalated candidate passed: 0/0 (n<5)" in text
    assert "first-attempt pass rate: 2/6 (33%)" in text and "pass rate per tier" in text
    assert "  default: 5/10 (50%)\n" in text                                   # 10 observations: not marked
    assert "dollars per verified task: $0.0180 over 4 verified task(s) (n<5)" in text
    assert "verdicts reached without a sandbox: 3/10 (30%)" in text
    for line in text.splitlines():                                           # no bare percentage anywhere
        if "%" in line:
            assert "/" in line


def test_rate_helpers():
    assert report.fmt_rate(report.rate(3, 7)) == "3/7 (43%)"
    assert report.fmt_rate(report.rate(2, 4)) == "2/4 (50%) (n<5)"
    assert report.fmt_rate(report.rate(0, 0)) == "0/0 (n<5)"
    assert report.rate(0, 0)["rate"] is None
    s = report.summary([])
    assert s["attempts"] == 0 and s["dollars"]["per_verified_task"] is None
    assert "dollars per verified task: n/a" in report.render_summary(s)


def test_all_unsandboxed_passes_leave_no_verified_task(recs):
    import dataclasses
    s = report.summary([dataclasses.replace(r, unsandboxed=True) if r.verdict else r for r in recs])
    assert s["dollars"]["verified_tasks"] == 0 and s["dollars"]["per_verified_task"] is None
    assert s["dollars"]["total"] > 0


def test_filters(world):
    only = report.attempt_records(world.conn, task_id="toyb")
    assert [r.attempt_seq for r in only] == [1, 2, 3]
    assert report.attempt_records(world.conn, since=4_000_000_000) == []
    assert len(report.attempt_records(world.conn, since=0)) == 10
    assert report.attempt_records(world.conn, task_id="nope") == []


def test_cli_report_text_and_json_round_trip(world, capsys, summ, recs):
    assert cli.main(["report"]) == 0
    assert capsys.readouterr().out.strip() == report.render_summary(summ).strip()
    assert cli.main(["report", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["summary"] == json.loads(json.dumps(summ))
    assert [a["task_id"] for a in doc["attempts"]] == [r.task_id for r in recs]
    assert doc["attempts"][0]["in_process_only"] == ["R-1", "R-2", "R-3"]
    assert json.loads(json.dumps(doc)) == doc
    assert cli.main(["report", "--task", "toyb", "--json"]) == 0
    one = json.loads(capsys.readouterr().out)
    assert one["summary"]["attempts"] == 3 and one["summary"]["tasks"] == 1
    assert cli.main(["report", "--since", "2000-01-01", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["attempts"] == 10
    assert cli.main(["report", "--since", "2999-01-01"]) == 0
    assert "attempts: 0 over 0 task(s)" in capsys.readouterr().out
    assert cli.main(["report", "--since", "yesterday"]) == 2


def test_running_attempts_have_no_record_and_attempts_without_a_log_still_do(conn):
    conn.execute("INSERT INTO attempts(id,task_id,seq,tier,provider,model_id,candidate,status,started_at) "
                 "VALUES('x-a1','x',1,'cheap','p','m1','x-c1','running',1.0)")
    assert report.attempt_records(conn) == []
    conn.execute("INSERT INTO attempts(id,task_id,seq,tier,provider,model_id,candidate,status,final_commit,"
                 "declared_scope_json,started_at,ended_at) VALUES('x-a2','x',2,'cheap','p','m1','x-c1','budget_censored',"
                 "'abc','[\"a.py\"]',2.0,3.0)")
    conn.execute("INSERT INTO turns(attempt_id,seq,request_sha256,usd) VALUES('x-a2',1,'h',0.25)")
    (r,) = report.attempt_records(conn)
    assert (r.status, r.turns, r.usd, r.verdict, r.attribution, r.plan, r.unsandboxed, r.out_of_scope) == \
        ("budget_censored", 1, 0.25, None, None, "", None, None)
    assert r.declared_scope == ("a.py",)
    s = report.summary([r])
    assert s["verdicts"]["not_verified"] == {"budget_censored": 1} and s["attribution"]["not_attributed"]["not verified"] == 1
