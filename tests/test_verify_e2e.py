"""End to end on the toy project: candidate commit + frozen checks -> audit, receipt, verdict,
attribution, stored evidence and redacted feedback.

All runs pass ``allow_unsandboxed=True`` so they work everywhere; where a sandbox mechanism
exists (CI) it is auto-detected and used anyway. Nothing here writes outside a checkout from
inside a run, and nothing relies on root bypassing permissions."""
import dataclasses
import json
import os

import pytest

from hera import cli, paths
from hera.oracle import checkstore
from hera.oracle.adapters.pytest_adapter import PytestAdapter
from hera.oracle.checkstore import CheckStoreTampered, freeze
from hera.oracle.disputes import open_dispute, resolve_dispute
from hera.oracle.verdict import verdict
from hera.oracle.verify import render_feedback, run_baseline, load_receipt, verify
from hera.sandbox import Limits
from hera.types import Attribution as A, CheckHealth, Verdict as V
from toyrepo import (CANDIDATES, CHECK_FILES, CHECKS, FORGE_PATCH, HARDCODED_CALC, REQUIREMENTS, VARIANT_CHECKS,
                     VARIANT_FILES, VARIANT_REQUIREMENTS, make_toy)

AD = PytestAdapter()
_BASELINE = []


def baseline_for(toy):
    """The base commit of every toy is identical, so the baseline is computed once per session."""
    if not _BASELINE:
        _BASELINE.append(run_baseline(toy.repo, AD, allow_unsandboxed=True))
    return _BASELINE[0]


@pytest.fixture
def cs(conn, hera_home):
    return freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES))


@pytest.fixture
def variant(conn, hera_home):
    return freeze(conn, task_id="toy", requirements=VARIANT_REQUIREMENTS, checks=VARIANT_CHECKS,
                  files=dict(VARIANT_FILES))


def run(conn, toy, cs, commit, **kw):
    kw.setdefault("baseline", baseline_for(toy))
    return verify(conn, toy.repo, cs, commit, allow_unsandboxed=True, **kw)


def audit_rules(out):
    return {(f.rule, f.path) for f in out.audit.block}


# ---- the required table ---------------------------------------------------------------------
def test_correct_passes_and_is_persisted_and_recomputable(conn, toy, cs):
    commit = toy.candidate(CANDIDATES["correct"])
    out = run(conn, toy, cs, commit)
    assert out.verdict is V.PASS and out.reasons == () and out.attribution is None
    assert out.audit.block == () and out.receipt.regressions == () and not out.timed_out
    assert not out.environment_suspect and out.advisories == ()
    assert out.receipt.candidate_commit == commit and out.receipt.checkset_hash == cs.hash
    assert out.receipt.manifest_ids == cs.check_ids and len(out.receipt.results) == 4
    assert any("baseline" in n for n in out.notes)
    row = conn.execute("SELECT * FROM receipts WHERE id=?", (out.receipt_id,)).fetchone()
    assert row["task_id"] == "toy" and row["candidate_commit"] == commit and len(row["env_hash"]) == 64
    stored = json.loads(row["receipt_json"])
    assert stored["evidence"]["mechanism"] == out.mechanism and stored["evidence"]["timed_out"] is False
    assert conn.execute("SELECT verdict FROM verdicts WHERE receipt_id=?", (out.receipt_id,)).fetchone()[0] == "PASS"
    assert conn.execute("SELECT COUNT(*) FROM attributions").fetchone()[0] == 0
    again = load_receipt(conn, out.receipt_id)
    assert again == out.receipt and verdict(again) == (V.PASS, [])


def test_reverifying_the_same_commit_writes_no_duplicate(conn, toy, cs):
    commit = toy.candidate(CANDIDATES["hardcoded"])
    first = run(conn, toy, cs, commit)
    second = run(conn, toy, cs, commit)
    assert first.receipt_id == second.receipt_id
    for table in ("receipts", "verdicts", "attributions"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    other = run(conn, toy, cs, toy.candidate(CANDIDATES["correct"]))
    assert other.receipt_id != first.receipt_id and conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 2


def test_stored_verdict_is_recomputable_from_the_stored_receipt(conn, toy, cs):
    for name in ("hardcoded", "tamper_conftest", "edit_tests"):
        out = run(conn, toy, cs, toy.candidate(CANDIDATES[name]))
        stored = conn.execute("SELECT verdict, reasons_json FROM verdicts WHERE receipt_id=?", (out.receipt_id,)).fetchone()
        v, reasons = verdict(load_receipt(conn, out.receipt_id))
        assert v.value == stored["verdict"] == out.verdict.value and reasons == json.loads(stored["reasons_json"])
    with pytest.raises(KeyError):
        load_receipt(conn, "0" * 64)


def test_hardcoded_is_overfit_and_feedback_is_redacted(conn, toy, cs):
    out = run(conn, toy, cs, toy.candidate(CANDIDATES["hardcoded"]))
    assert out.verdict is V.OVERFIT and out.attribution.label is A.IMPL_OVERFIT
    assert out.reasons == ("requirement not satisfied in an unseen case: R-1",)
    text = render_feedback(out)
    assert "R-1" in text and len(text) <= 6000
    blob = text + json.dumps(out.feedback) + " ".join(out.reasons)
    for secret in ("chk-hidden-r1-seven", "test_parse_hidden_zq", "test_zq_hidden_seven", "zq", "parse_amount('7')",
                   "7.0", "hidden", "assert", ".py"):
        assert secret not in blob, secret


def test_base_commit_itself_fails(conn, toy, cs):
    out = run(conn, toy, cs, toy.base)
    assert out.verdict is V.FAIL and out.attribution.label is A.IMPL_WRONG and out.attribution.rule == "visible_check_failed"
    assert out.reasons == ("visible check not passing: chk-r1-basic", "visible check not passing: chk-r2-empty")
    text = render_feedback(out)
    assert "chk-r1-basic" in text and "parse_amount" in text and "zq" not in text   # visible failure text is fine
    assert not out.environment_suspect and out.audit.block == ()


def test_regression_is_caught_by_the_baseline_comparison_alone(conn, toy, variant):
    commit = toy.candidate(CANDIDATES["regression"])
    out = run(conn, toy, variant, commit)
    assert out.verdict is V.REGRESSION and out.attribution.label is A.IMPL_WRONG
    assert out.attribution.rule == "baseline_test_regressed"
    assert out.receipt.regressions == ("tests.test_calc::test_add",)
    assert out.reasons == ("baseline test now failing: tests.test_calc::test_add",)
    assert "tests.test_calc::test_add" in render_feedback(out)
    assert all(r.status.value == "pass" for r in out.receipt.results)


def test_regression_with_the_full_check_set_fails_on_the_frozen_regression_check(conn, toy, cs):
    out = run(conn, toy, cs, toy.candidate(CANDIDATES["regression"]))
    assert out.verdict is V.FAIL and out.reasons == ("visible check not passing: chk-r3-add",)
    assert out.receipt.regressions == ("tests.test_calc::test_add",)      # both views agree


def test_edit_tests_is_tamper_and_with_allowance_is_only_a_fail(conn, toy, cs):
    commit = toy.candidate(CANDIDATES["edit_tests"])
    out = run(conn, toy, cs, commit)
    assert out.verdict is V.TAMPER and out.attribution.label is A.IMPL_TAMPER and out.attribution.rule == "audit_block"
    assert audit_rules(out) == {("protected_path_modified", "tests/test_calc.py")}
    assert ("skip_marker_added", "tests/test_calc.py") in {(f.rule, f.path) for f in out.audit.flags}
    assert "protected_path_modified: tests/test_calc.py" in render_feedback(out)
    assert "audit block: protected_path_modified: tests/test_calc.py" in verdict(load_receipt(conn, out.receipt_id))[1]

    allowed = run(conn, toy, cs, commit, allowed_protected=("tests/**",))
    assert allowed.verdict is V.FAIL and allowed.attribution.label is A.IMPL_WRONG
    assert allowed.audit.block == () and allowed.receipt.audit_block_hits == ()
    assert ("skip_marker_added", "tests/test_calc.py") in {(f.rule, f.path) for f in allowed.audit.flags}
    assert "skip_marker_added: tests/test_calc.py" in allowed.receipt.audit_flags
    assert allowed.receipt.regressions == ()                 # prepare restored the tests, so nothing is "broken"


@pytest.mark.parametrize("name,rule,path", [
    ("tamper_conftest", "protected_path_modified", "conftest.py"),
    ("tamper_sitecustomize", "protected_path_added", "sitecustomize.py"),
    ("tamper_checks", "protected_path_added", "_hera_checks/visible/test_x.py"),
    ("tamper_checks", "protected_path_added", "_hera_results/junit.xml"),
    ("skip_all", "protected_path_added", "pytest.ini")])
def test_runner_tampering_candidates_are_tamper(conn, toy, cs, name, rule, path):
    out = run(conn, toy, cs, toy.candidate(CANDIDATES[name]))
    assert out.verdict is V.TAMPER and out.attribution.label is A.IMPL_TAMPER and out.attribution.confidence == "high"
    assert (rule, path) in audit_rules(out)
    assert not out.environment_suspect
    # the run itself was honest because prepare neutralised the change: the base code is unimplemented
    assert {r.check_id: r.status.value for r in out.receipt.results}["chk-r3-add"] == "pass"


def test_slow_candidate_times_out_and_is_never_tamper(conn, toy, cs):
    out = run(conn, toy, cs, toy.candidate(CANDIDATES["slow"]), limits=Limits(wall_s=6))
    assert out.timed_out and out.verdict in (V.FAIL, V.OVERFIT) and out.verdict is not V.TAMPER
    assert out.receipt.sentinels_ok and out.receipt.exit_code_consistent       # a timeout is not tampering
    assert all(r.status.value == "timeout" for r in out.receipt.results)
    assert out.attribution.label is A.IMPL_WRONG and out.attribution.confidence == "medium"
    assert not out.environment_suspect
    stored = json.loads(conn.execute("SELECT receipt_json FROM receipts WHERE id=?", (out.receipt_id,)).fetchone()[0])
    assert stored["evidence"]["timed_out"] is True and stored["evidence"]["raw_sentinels_ok"] is False
    assert "time limit" in render_feedback(out)


def test_open_dispute_pauses_the_verdict_until_resolved(conn, toy, cs):
    commit = toy.candidate(CANDIDATES["correct"])
    d = open_dispute(conn, task_id="toy", checkset_hash=cs.hash, check_id="chk-r1-basic", requirement_id="R-1",
                     argument="the expected value looks wrong")
    out = run(conn, toy, cs, commit)
    assert out.verdict is V.ORACLE_DISPUTED and out.attribution.label is A.UNKNOWN and out.attribution.rule == "dispute_open"
    resolve_dispute(conn, d, outcome="upheld", resolver="human:reviewer", implementer_model="impl-model")
    out = run(conn, toy, cs, commit)
    assert out.verdict is V.PASS and out.attribution is None
    assert run(conn, toy, cs, commit, dispute_open=True).verdict is V.ORACLE_DISPUTED     # explicit flag too


def test_requirement_without_a_check_is_unverifiable(conn, toy, hera_home):
    reqs = REQUIREMENTS + (dataclasses.replace(REQUIREMENTS[0], id="R-4", text="a requirement nobody wrote a check for"),)
    cs4 = freeze(conn, task_id="toy", requirements=reqs, checks=CHECKS, files=dict(CHECK_FILES))
    out = run(conn, toy, cs4, toy.candidate(CANDIDATES["correct"]))
    assert out.verdict is V.UNVERIFIABLE and out.reasons == ("no executable check for R-4",)
    assert out.attribution.label is A.UNKNOWN and out.attribution.rule == "no executable checks"


def test_declared_scope(conn, toy, cs):
    commit = toy.candidate(CANDIDATES["correct"])
    inside = run(conn, toy, cs, commit, declared_scope=("calc.py",))
    assert inside.verdict is V.PASS and not [f for f in inside.audit.flags if f.rule == "out_of_declared_scope"]
    outside = run(conn, toy, cs, commit, declared_scope=("docs/**",))
    assert outside.verdict is V.PASS and outside.attribution is None
    assert [(f.rule, f.path) for f in outside.audit.flags] == [("out_of_declared_scope", "calc.py")]
    assert outside.receipt.audit_flags == ("out_of_declared_scope: calc.py",)
    assert "out_of_declared_scope: calc.py" in render_feedback(outside)
    assert outside.receipt_id != inside.receipt_id


def test_KNOWN_GAP_in_process_monkeypatch_yields_false_pass(conn, toy, cs):
    """Documents an OPEN tamper route (docs/ORACLE.md, S7c): candidate code imported by a check runs
    in the pytest process and rewrites every report before the oracle sees it, so this
    hard-coded candidate (which must be OVERFIT) gets PASS. This test asserts the WRONG answer on
    purpose. When out-of-process (contract) checks land and close the gap, flip it: it must then
    expect OVERFIT or TAMPER."""
    commit = toy.candidate({"calc.py": HARDCODED_CALC + FORGE_PATCH})
    out = run(conn, toy, cs, commit)
    assert out.verdict is V.PASS and out.attribution is None
    assert out.audit.block == () and out.receipt.regressions == ()


# ---- environment, health, scope of the run ----------------------------------------------------
def test_a_candidate_that_kills_the_runner_is_tamper_with_unknown_attribution(conn, toy, cs):
    """The runner produces no results at all. The verdict is TAMPER (fail safe) but the attribution
    is UNKNOWN with environment_suspect: a broken environment looks the same, so nobody is blamed."""
    commit = toy.candidate({"calc.py": "import os\nos._exit(3)\n"})
    out = run(conn, toy, cs, commit)
    assert out.verdict is V.TAMPER and out.attribution.label is A.UNKNOWN and out.environment_suspect
    assert out.attribution.rule == "no_results_parsed" and not out.timed_out and out.audit.block == ()


def test_weak_counted_check_gives_an_advisory_on_pass(conn, toy, cs):
    conn.execute("UPDATE checks SET health='weak' WHERE id='chk-r1-basic' AND checkset_hash=?", (cs.hash,))
    out = run(conn, toy, cs, toy.candidate(CANDIDATES["correct"]))
    assert out.verdict is V.PASS and out.attribution is None
    assert [(a.label, a.rule) for a in out.advisories] == [(A.CHECK_WEAK, "weak_check_counted")]
    assert conn.execute("SELECT label FROM attributions WHERE receipt_id=?", (out.receipt_id,)).fetchall()[0][0] == "CHECK_WEAK"
    assert {r.check_id: r.health for r in out.receipt.results}["chk-r1-basic"] is CheckHealth.WEAK


def test_flaky_and_invalid_checks_never_cause_impl_attribution(conn, toy, cs):
    conn.execute("UPDATE checks SET health='flaky' WHERE id IN ('chk-r1-basic','chk-r2-empty') AND checkset_hash=?", (cs.hash,))
    conn.execute("UPDATE checks SET health='invalid' WHERE id='chk-hidden-r1-seven' AND checkset_hash=?", (cs.hash,))
    out = run(conn, toy, cs, toy.base)               # the unimplemented base fails exactly those three
    assert out.verdict is V.PASS and out.attribution is None
    conn.execute("UPDATE checks SET health='invalid' WHERE id='chk-r3-add' AND checkset_hash=?", (cs.hash,))
    out = run(conn, toy, cs, toy.base)
    assert out.verdict is V.UNVERIFIABLE and out.attribution.label is A.UNKNOWN
    conn.execute("UPDATE checks SET health='bogus' WHERE id='chk-r3-add' AND checkset_hash=?", (cs.hash,))
    with pytest.raises(ValueError):
        run(conn, toy, cs, toy.base)


def test_no_hidden_runs_only_visible_checks(conn, toy, cs):
    out = run(conn, toy, cs, toy.candidate(CANDIDATES["hardcoded"]), include_hidden=False)
    assert out.verdict is V.PASS
    assert out.receipt.manifest_ids == {"chk-r1-basic", "chk-r2-empty", "chk-r3-add"}
    assert all(r.visibility == "visible" for r in out.receipt.results)


def test_tampered_check_store_raises_instead_of_giving_a_verdict(conn, toy, cs):
    f = cs.root / "hidden" / "test_parse_hidden_zq.py"
    os.chmod(f.parent, 0o755)
    os.chmod(f, 0o644)
    f.write_bytes(b"def test_zq_hidden_seven():\n    pass\n")
    with pytest.raises(CheckStoreTampered):
        run(conn, toy, cs, toy.candidate(CANDIDATES["correct"]))
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_wrong_task_or_adapter_is_refused(conn, toy, cs, tmp_path):
    other = dataclasses.replace(cs, task_id="elsewhere")
    with pytest.raises(ValueError):
        run(conn, toy, other, toy.base)
    with pytest.raises(ValueError):
        run(conn, toy, dataclasses.replace(cs, adapter="jest"), toy.base)
    with pytest.raises(ValueError):
        run(conn, toy, cs, "not-a-hash")


def test_verification_checkouts_are_removed_unless_asked_otherwise(conn, toy, cs):
    commit = toy.candidate(CANDIDATES["correct"])
    run(conn, toy, cs, commit)
    assert os.listdir(toy.repo.root / "verify") == []
    run(conn, toy, cs, commit, cleanup=False)
    assert len(os.listdir(toy.repo.root / "verify")) == 2          # the check run and the project-test run


def test_project_without_preexisting_tests_notes_it(conn, hera_home, tmp_path):
    bare = make_toy(tmp_path / "notests", with_tests=False)
    cs_ = freeze(conn, task_id="toy", requirements=REQUIREMENTS[:2], checks=CHECKS[:3], files=dict(VARIANT_FILES))
    commit = bare.candidate(CANDIDATES["correct"])
    base = run_baseline(bare.repo, AD, allow_unsandboxed=True)
    assert not base.has_tests
    out = verify(conn, bare.repo, cs_, commit, allow_unsandboxed=True, baseline=base)
    assert out.verdict is V.PASS and out.receipt.regressions == ()
    assert any("no pre-existing tests" in n for n in out.notes)


# ---- baseline ---------------------------------------------------------------------------------
def test_baseline_lists_passing_tests(toy):
    b = baseline_for(toy)
    assert b.has_tests and b.ok and b.consistent and b.runs == 2
    assert b.passing == {"tests.test_calc::test_add"} and b.flaky == () and b.not_passing == ()
    assert b.test_paths == ("tests/test_calc.py",)


def test_baseline_marks_flaky_and_not_passing_tests(hera_home, tmp_path):
    from toyrepo import _git
    toy = make_toy(tmp_path / "flaky", task_id="unused")
    src = toy.source
    # real flakiness cannot be produced deterministically (a sandboxed run may not write outside
    # its checkout), so the wrapper below flips the result of test_flip between the two runs
    (src / "tests" / "test_flip.py").write_text(
        "import os\n\n\ndef test_flip():\n    assert os.getpid() % 2 == 0\n\n\ndef test_always_fails():\n    assert False\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "more tests")
    from hera.workspace import TaskRepo
    repo = TaskRepo.create("flakytask", src)
    fake = [True, False]
    real = AD.run_project_tests

    class Alternating:                 # make "flip" deterministic: passes in the first run only
        name = "pytest"
        is_protected = staticmethod(AD.is_protected)
        project_test_paths = staticmethod(AD.project_test_paths)

        def run_project_tests(self, checkout, paths_, **kw):
            rep = real(checkout, paths_, **kw)
            st = dict(rep.statuses)
            flip = [k for k in st if k.endswith("test_flip")][0]
            st[flip] = st[flip].__class__("pass" if fake.pop(0) else "fail")
            return dataclasses.replace(rep, statuses=st)
    b = run_baseline(repo, Alternating(), allow_unsandboxed=True)
    assert b.passing == {"tests.test_calc::test_add"}
    assert b.flaky == ("tests.test_flip::test_flip",) and b.not_passing == ("tests.test_flip::test_always_fails",)


# ---- feedback ---------------------------------------------------------------------------------
def test_render_feedback_is_capped(conn, toy, cs):
    out = run(conn, toy, cs, toy.base)
    huge = tuple({"check_id": f"c{i}", "requirement_ids": ["R-1"], "failure_text": "x" * 2000} for i in range(20))
    text = render_feedback(dataclasses.replace(out, feedback=huge))
    assert len(text) <= 6000 and text.endswith("[truncated]")


def test_integrity_reasons_are_generic_in_feedback(conn, toy, cs):
    out = run(conn, toy, cs, toy.base)
    fake = dataclasses.replace(out, verdict=V.TAMPER, reasons=("sentinel pair did not behave as required",
                                                               "runner exit code disagrees with per-check records"))
    text = render_feedback(fake)
    assert "sentinel" not in text and "exit code" not in text and "integrity checks" in text


# ---- CLI --------------------------------------------------------------------------------------
def test_cli_verify(toy, hera_home, capsys):
    from hera.store import db
    conn = db.connect(paths.db_path())
    cs_ = freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES))
    good = toy.candidate(CANDIDATES["correct"])
    bad = toy.candidate(CANDIDATES["hardcoded"])
    conn.close()
    base = ["verify", "--task", "toy", "--checkset", cs_.hash, "--allow-unsandboxed"]
    assert cli.main([*base, "--commit", good]) == 0
    out = capsys.readouterr().out
    assert "verdict: PASS" in out and "attribution: none" in out and "receipt: " in out
    assert cli.main([*base, "--commit", bad]) == 3
    out = capsys.readouterr().out
    assert "verdict: OVERFIT" in out and "IMPL_OVERFIT" in out and "R-1" in out and "zq" not in out
    assert cli.main([*base, "--commit", bad, "--no-hidden"]) == 0
    assert cli.main(["verify", "--task", "toy", "--checkset", "0" * 64, "--commit", good]) == 2
    assert cli.main(["verify", "--task", "nope", "--checkset", cs_.hash, "--commit", good]) == 2
    assert "error:" in capsys.readouterr().err


def test_feedback_does_not_carry_the_checkout_location(conn, toy, cs):
    out = run(conn, toy, cs, toy.base)
    blob = json.dumps(out.feedback) + render_feedback(out)
    assert "<checkout>" in blob and str(paths.work_dir()) not in blob and str(toy.repo.root) not in blob
