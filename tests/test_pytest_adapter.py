"""pytest adapter against the toy project: honest, hard-coded and tampering candidates."""
import json
import os
import time
from pathlib import Path

import pytest

from hera.oracle.adapters.pytest_adapter import PytestAdapter, match_glob
from hera.oracle.checkstore import freeze
from hera.sandbox import Limits, detect_mechanism
from hera.types import CheckStatus
from toyrepo import CANDIDATES, CHECK_FILES, CHECKS, CONFTEST, REQUIREMENTS

AD = PytestAdapter()
MANIFEST = {c.check_id for c in CHECKS}
needs_mech = pytest.mark.skipif(detect_mechanism() == "none", reason="no sandbox mechanism on this machine")


@pytest.fixture
def cs(conn, hera_home):
    return freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES))


def verify(toy, cs, commit, *, include_hidden=True, limits=None, unsandboxed=True):
    checkout = toy.repo.verification_checkout(commit)
    base_tree = toy.repo.read_tree(toy.base, AD.is_protected)
    prep = AD.prepare(checkout, cs, base_tree=base_tree, include_hidden=include_hidden)
    rep = AD.run(checkout, cs, include_hidden=include_hidden, limits=limits, allow_unsandboxed=unsandboxed)
    return checkout, prep, rep


def statuses(rep):
    return {r.check_id: r.status for r in rep.results}


def test_base_tree_is_protected_files_only(toy):
    bt = toy.repo.read_tree(toy.base, AD.is_protected)
    assert set(bt) == {"conftest.py", "tests/test_calc.py"}
    assert bt["conftest.py"].decode() == CONFTEST
    assert set(toy.repo.read_tree(toy.base)) == {"calc.py", "conftest.py", "tests/test_calc.py"}


def test_base_commit(toy, cs):
    _checkout, prep, rep = verify(toy, cs, toy.base)
    assert prep.restored == prep.deleted == prep.modified_protected == ()
    st = statuses(rep)
    assert st["chk-r1-basic"] is CheckStatus.ERROR and st["chk-r2-empty"] is CheckStatus.ERROR
    assert st["chk-hidden-r1-seven"] is CheckStatus.ERROR
    assert st["chk-r3-add"] is CheckStatus.PASS
    assert rep.sentinels_ok and rep.exit_code_consistent and rep.results_parsed and not rep.timed_out
    assert rep.executed_ids == MANIFEST and rep.raw_unmatched == ()
    assert "parse_amount" in rep.failure_texts["chk-r1-basic"]


def test_correct_candidate(toy, cs):
    commit = toy.candidate(CANDIDATES["correct"])
    _c, prep, rep = verify(toy, cs, commit)
    assert all(r.status is CheckStatus.PASS for r in rep.results) and rep.executed_ids == MANIFEST
    assert rep.sentinels_ok and rep.exit_code_consistent and rep.returncode == 1
    assert prep.deleted == () and all(r.output_digest == "" for r in rep.results)
    assert {r.check_id: r.visibility for r in rep.results}["chk-hidden-r1-seven"] == "hidden"
    assert all(r.duration_s >= 0 for r in rep.results)


def test_hardcoded_candidate_fails_hidden_only(toy, cs):
    _c, _p, rep = verify(toy, cs, toy.candidate(CANDIDATES["hardcoded"]))
    st = statuses(rep)
    assert st["chk-r1-basic"] is st["chk-r2-empty"] is st["chk-r3-add"] is CheckStatus.PASS
    assert st["chk-hidden-r1-seven"] is CheckStatus.FAIL
    assert rep.sentinels_ok and rep.exit_code_consistent
    hidden = {r.check_id: r for r in rep.results}["chk-hidden-r1-seven"]
    assert len(hidden.output_digest) == 64


@needs_mech
def test_correct_and_hardcoded_under_real_sandbox(toy, cs):
    _c, _p, rep = verify(toy, cs, toy.candidate(CANDIDATES["correct"]), unsandboxed=False)
    assert all(r.status is CheckStatus.PASS for r in rep.results) and rep.sentinels_ok and rep.exit_code_consistent
    _c, _p, rep = verify(toy, cs, toy.candidate(CANDIDATES["hardcoded"]), unsandboxed=False)
    assert statuses(rep)["chk-hidden-r1-seven"] is CheckStatus.FAIL and rep.sentinels_ok


def test_tamper_conftest_is_restored(toy, cs):
    checkout, prep, rep = verify(toy, cs, toy.candidate(CANDIDATES["tamper_conftest"]))
    assert (checkout / "conftest.py").read_text() == CONFTEST
    assert "conftest.py" in prep.modified_protected and "conftest.py" in prep.restored
    st = statuses(rep)                       # honest results for the unimplemented base code
    assert st["chk-r1-basic"] is CheckStatus.ERROR and st["chk-r3-add"] is CheckStatus.PASS
    assert rep.sentinels_ok and rep.exit_code_consistent and rep.executed_ids == MANIFEST


def test_tamper_conftest_control_unrestored(toy, cs):
    """Control. Without restoration the tampering conftest does rewrite outcomes if pytest loads
    it (confcutdir at the checkout root). The oracle run is defended twice: restoration, and
    --confcutdir=_hera_checks, which keeps any conftest outside _hera_checks from loading."""
    from hera import sandbox
    commit = toy.candidate(CANDIDATES["tamper_conftest"])
    checkout = toy.repo.verification_checkout(commit)
    AD.prepare(checkout, cs, base_tree={"conftest.py": (checkout / "conftest.py").read_bytes()},
               include_hidden=True)                                        # pretend base == tampered
    assert "makereport" in (checkout / "conftest.py").read_text()
    cmd = AD.command(checkout)
    loose = cmd[:cmd.index("--confcutdir")] + ["--confcutdir", "."] + cmd[cmd.index("--confcutdir") + 2:]
    r = sandbox.run(loose, cwd=checkout, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}, allow_unsandboxed=True)
    assert b"FAILED" not in r.stdout                                     # the must-fail sentinel was deselected
    rep = AD.run(checkout, cs, allow_unsandboxed=True)                   # real command: still honest
    assert rep.sentinels_ok and rep.exit_code_consistent


def test_tamper_sitecustomize_deleted(toy, cs):
    commit = toy.candidate({**CANDIDATES["tamper_sitecustomize"], **CANDIDATES["correct"]})
    checkout, prep, rep = verify(toy, cs, commit)
    assert not (checkout / "sitecustomize.py").exists()
    assert "sitecustomize.py" in prep.deleted and prep.modified_protected == ()
    assert all(r.status is CheckStatus.PASS for r in rep.results) and rep.sentinels_ok and rep.exit_code_consistent


def test_tamper_checks_wiped(toy, cs):
    commit = toy.candidate({**CANDIDATES["tamper_checks"], **CANDIDATES["correct"]})
    checkout, prep, rep = verify(toy, cs, commit)
    assert "_hera_checks" in prep.deleted and "_hera_results" in prep.deleted
    assert not (checkout / "_hera_checks" / "visible" / "test_x.py").exists()
    assert all(r.status is CheckStatus.PASS for r in rep.results) and rep.executed_ids == MANIFEST
    assert rep.raw_unmatched == () and rep.sentinels_ok and rep.exit_code_consistent
    assert "test_ok" not in (checkout / "_hera_results" / "junit.xml").read_text()


def test_skip_all_pytest_ini_deleted(toy, cs):
    checkout, prep, rep = verify(toy, cs, toy.candidate({**CANDIDATES["skip_all"], **CANDIDATES["correct"]}))
    assert "pytest.ini" in prep.deleted and not (checkout / "pytest.ini").exists()
    assert rep.executed_ids == MANIFEST and rep.sentinels_ok and rep.exit_code_consistent
    assert all(r.status is CheckStatus.PASS for r in rep.results)


def test_protected_symlinks_and_directories_are_replaced(toy, cs):
    wt = toy.repo.new_candidate("links")
    (wt.path / "conftest.py").unlink()
    os.symlink("calc.py", wt.path / "conftest.py")           # link where a protected file belongs
    (wt.path / "tests" / "test_calc.py").unlink()
    (wt.path / "tests").rmdir()
    os.symlink("src_dir", wt.path / "tests")                  # link where a protected directory belongs
    (wt.path / "src_dir").mkdir()
    (wt.path / "src_dir" / "marker.txt").write_text("m")
    os.symlink("calc.py", wt.path / "pytest_evil.py")        # added protected link
    commit = wt.commit_all("links")
    checkout, prep, rep = verify(toy, cs, commit)
    assert (checkout / "conftest.py").read_text() == CONFTEST and not (checkout / "conftest.py").is_symlink()
    assert (checkout / "tests").is_dir() and not (checkout / "tests").is_symlink()
    assert (checkout / "tests" / "test_calc.py").is_file()
    assert not (checkout / "src_dir" / "test_calc.py").exists()   # nothing was written through the link
    assert {"conftest.py", "tests/test_calc.py"} <= set(prep.modified_protected)
    assert "pytest_evil.py" in prep.deleted
    assert rep.sentinels_ok


def test_slow_candidate_times_out(toy, cs):
    t0 = time.monotonic()
    _c, _p, rep = verify(toy, cs, toy.candidate(CANDIDATES["slow"]), limits=Limits(wall_s=6))
    assert time.monotonic() - t0 < 60
    assert rep.timed_out and rep.returncode is None
    assert rep.executed_ids == MANIFEST and all(r.status is CheckStatus.TIMEOUT for r in rep.results)
    assert not rep.sentinels_ok and not rep.exit_code_consistent


def test_exclude_hidden(toy, cs):
    checkout, _p, rep = verify(toy, cs, toy.candidate(CANDIDATES["correct"]), include_hidden=False)
    assert not (checkout / "_hera_checks" / "hidden").exists()
    assert "zq_hidden" not in "".join(p.read_text() for p in checkout.rglob("*.py") if "_hera_results" not in str(p))
    assert rep.executed_ids == MANIFEST - {"chk-hidden-r1-seven"}
    assert all(r.visibility == "visible" for r in rep.results) and rep.sentinels_ok and rep.exit_code_consistent


def test_run_requires_prepare(toy, cs):
    checkout = toy.repo.verification_checkout(toy.base)
    with pytest.raises(RuntimeError):
        AD.run(checkout, cs, allow_unsandboxed=True)
    AD.prepare(checkout, cs, base_tree={}, include_hidden=True)
    with pytest.raises(RuntimeError):
        AD.run(checkout, cs, include_hidden=False, allow_unsandboxed=True)


def test_prepare_refuses_tampered_store(toy, cs):
    f = cs.root / "visible" / "test_parse_amount.py"
    os.chmod(f.parent, 0o755)
    os.chmod(f, 0o644)
    f.write_bytes(b"def test_basic_amount(): pass\n")
    from hera.oracle.checkstore import CheckStoreTampered
    with pytest.raises(CheckStoreTampered):
        AD.prepare(toy.repo.verification_checkout(toy.base), cs, base_tree={}, include_hidden=True)


def test_prepare_rejects_bad_base_tree(toy, cs):
    checkout = toy.repo.verification_checkout(toy.base)
    for bad in ("../conftest.py", "/conftest.py", "calc.py"):
        with pytest.raises(ValueError):
            AD.prepare(checkout, cs, base_tree={bad: b""}, include_hidden=True)


# ---- results integrity ------------------------------------------------------------------
def run_with_results(toy, cs, junit_text: bytes | None):
    """Prepare a checkout, run the real pytest, then replace the results file the way a forger would
    after pytest finished; parse again through the adapter's parser."""
    from hera.oracle.adapters import pytest_adapter as pa
    checkout, _p, rep = verify(toy, cs, toy.candidate(CANDIDATES["correct"]))
    class R:  # the process outcome of the honest run
        returncode, timed_out, truncated, stdout = rep.returncode, False, False, b""
    path = checkout / "_hera_results" / "junit.xml"
    if junit_text is None:
        path.unlink()
    else:
        path.write_bytes(junit_text)
    return pa._parse(checkout, cs, True, R)


def test_results_with_doctype_or_entity_refused(toy, cs):
    for doc in (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><testsuites/>',
                b'<?xml version="1.0"?><!doctype x><testsuites/>',
                b'<?xml version="1.0"?><!ENTITY a "b"><testsuites/>'):
        rep = run_with_results(toy, cs, doc)
        assert rep.results == () and not rep.sentinels_ok and not rep.exit_code_consistent and not rep.results_parsed


def test_results_missing_oversized_or_garbage(toy, cs):
    for doc in (None, b"<not xml", b"x" * (5 * 1024 * 1024 + 1), b"\xff\xfe<\0t\0/\0>\0"):
        rep = run_with_results(toy, cs, doc)
        assert rep.results == () and not rep.sentinels_ok and not rep.exit_code_consistent


def test_forged_results_without_failing_sentinel_are_not_ok(toy, cs):
    doc = ('<testsuites><testsuite>' + "".join(
        f'<testcase classname="_hera_checks.visible.{m}" name="{n}"/>' for m, n in
        [("test_parse_amount", "test_basic_amount")]) + '</testsuite></testsuites>').encode()
    rep = run_with_results(toy, cs, doc)
    assert not rep.sentinels_ok and rep.executed_ids == {"chk-r1-basic"}


def test_unmatched_testcases_reported(toy, cs):
    doc = b'<testsuites><testsuite><testcase classname="elsewhere.test_z" name="test_z"/></testsuite></testsuites>'
    rep = run_with_results(toy, cs, doc)
    assert rep.raw_unmatched == ("elsewhere.test_z::test_z",) and rep.executed_ids == frozenset()


def test_parametrized_check_passes_only_if_all_cases_pass(conn, hera_home, toy):
    from hera.oracle.model import DraftCheck
    src = ("import pytest\nfrom calc import add\n\n\n@pytest.mark.parametrize('a,b,c', [(1, 1, 2), (2, 2, 5), (3, 3, 6)])\n"
           "def test_param_add(a, b, c):\n    assert add(a, b) == c\n")
    chk = (DraftCheck("chk-param", ("R-3",), "visible", "test_param.py", "test_param_add", kind="regression"),)
    cs = freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=chk, files={"test_param.py": src.encode()})
    _c, _p, rep = verify(toy, cs, toy.base)
    assert statuses(rep) == {"chk-param": CheckStatus.FAIL} and rep.sentinels_ok and rep.exit_code_consistent
    assert rep.raw_unmatched == ()
    assert "assert 4 == 5" in rep.failure_texts["chk-param"]


def test_syntax_error_in_check_file_is_error_and_others_still_run(conn, hera_home, toy):
    from hera.oracle.model import DraftCheck
    files = {"test_broken.py": b"def test_a(:\n", "test_calc_unchanged.py": CHECK_FILES["test_calc_unchanged.py"]}
    chk = (DraftCheck("chk-broken", ("R-1",), "visible", "test_broken.py", "test_a"),
           DraftCheck("chk-r3-add", ("R-3",), "visible", "test_calc_unchanged.py", "test_add_unchanged", kind="regression"))
    cs = freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=chk, files=files)
    _c, _p, rep = verify(toy, cs, toy.base)
    assert statuses(rep) == {"chk-broken": CheckStatus.ERROR, "chk-r3-add": CheckStatus.PASS}
    assert rep.sentinels_ok and rep.exit_code_consistent
    assert "SyntaxError" in rep.failure_texts["chk-broken"]


# ---- a teardown error after a passing body (S7a limit: false-TAMPER risk) --------------------
_TEARDOWN_PASS = ("import pytest\nfrom calc import add\n\n\n@pytest.fixture\ndef res():\n    yield 1\n"
                  "    raise RuntimeError('teardown boom')\n\n\n"
                  "def test_add_teardown(res):\n    assert add(2, 3) == 5\n")
_TEARDOWN_FAIL = _TEARDOWN_PASS.replace("== 5", "== 6")
_SETUP_ERR = _TEARDOWN_PASS.replace("    yield 1\n    raise", "    raise").replace("def test_add_teardown", "def test_add_setup")


def _teardown_checkset(conn, src, node="test_add_teardown"):
    from hera.oracle.model import DraftCheck
    chk = (DraftCheck("chk-td", ("R-3",), "visible", "test_td.py", node, kind="regression"),
           DraftCheck("chk-r3-add", ("R-3",), "visible", "test_calc_unchanged.py", "test_add_unchanged",
                      kind="regression"))
    files = {"test_td.py": src.encode(), "test_calc_unchanged.py": CHECK_FILES["test_calc_unchanged.py"]}
    return freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=chk, files=files)


def test_teardown_error_after_passing_body_is_an_error_not_an_inconsistency(conn, hera_home, toy):
    cs_ = _teardown_checkset(conn, _TEARDOWN_PASS)
    _c, _p, rep = verify(toy, cs_, toy.base)
    assert statuses(rep) == {"chk-td": CheckStatus.ERROR, "chk-r3-add": CheckStatus.PASS}
    assert rep.sentinels_ok and rep.results_parsed and rep.returncode == 1
    assert rep.exit_code_consistent                      # junit, summary line and exit code agree
    assert "teardown boom" in rep.failure_texts["chk-td"]


def test_teardown_error_after_failing_body_keeps_the_failure(conn, hera_home, toy):
    cs_ = _teardown_checkset(conn, _TEARDOWN_FAIL)
    _c, _p, rep = verify(toy, cs_, toy.base)
    assert statuses(rep)["chk-td"] is CheckStatus.FAIL and statuses(rep)["chk-r3-add"] is CheckStatus.PASS
    assert rep.exit_code_consistent and rep.sentinels_ok
    assert "teardown boom" in rep.failure_texts["chk-td"] and "assert 5 == 6" in rep.failure_texts["chk-td"]


def test_setup_error_is_still_one_error_and_consistent(conn, hera_home, toy):
    cs_ = _teardown_checkset(conn, _SETUP_ERR, node="test_add_setup")
    _c, _p, rep = verify(toy, cs_, toy.base)
    assert statuses(rep)["chk-td"] is CheckStatus.ERROR and rep.exit_code_consistent and rep.sentinels_ok


def test_teardown_error_is_not_tamper_end_to_end(conn, hera_home, toy):
    from hera.oracle.verify import run_baseline, verify as full_verify
    from hera.types import Verdict
    cs_ = _teardown_checkset(conn, _TEARDOWN_PASS)
    base = run_baseline(toy.repo, AD, allow_unsandboxed=True)
    out = full_verify(conn, toy.repo, cs_, toy.candidate(CANDIDATES["correct"]), baseline=base,
                      allow_unsandboxed=True)
    assert out.verdict is not Verdict.TAMPER and out.receipt.exit_code_consistent
    assert out.verdict is Verdict.FAIL                   # the erroring check does not count as passed


def test_forged_teardown_error_that_hides_a_failure_is_still_inconsistent(toy, cs):
    """A forger who rewrites a failure into a teardown-style error leaves the counts wrong."""
    import xml.etree.ElementTree as ET
    from hera.oracle.adapters import pytest_adapter as pa
    checkout, _p, rep = verify(toy, cs, toy.candidate(CANDIDATES["hardcoded"]))

    class R:
        returncode, timed_out, truncated = rep.returncode, False, False
        stdout = b"4 passed, 2 failed in 0.1s"          # pytest's own line for this run
    assert pa._parse(checkout, cs, True, R).exit_code_consistent       # control: untouched file agrees
    path = checkout / "_hera_results" / "junit.xml"
    t = ET.parse(path)
    for tc in t.getroot().iter("testcase"):
        if tc.get("name") == "test_zq_hidden_seven":
            for ch in list(tc):
                tc.remove(ch)
            ET.SubElement(tc, "error", message='failed on teardown with "x"')
    t.write(path)
    assert not pa._parse(checkout, cs, True, R).exit_code_consistent


def test_skipped_body_with_teardown_error_is_consistent(conn, hera_home, toy):
    src = _TEARDOWN_PASS.replace("    assert add(2, 3) == 5", "    pytest.skip('later')")
    _c, _p, rep = verify(toy, _teardown_checkset(conn, src), toy.base)
    assert statuses(rep)["chk-td"] is CheckStatus.ERROR and rep.exit_code_consistent and rep.sentinels_ok


# ---- feedback ---------------------------------------------------------------------------
def test_feedback_redacts_hidden_checks(toy, cs):
    _c, _p, rep = verify(toy, cs, toy.candidate(CANDIDATES["hardcoded"]))
    fb = AD.feedback_for_implementer(rep, cs)
    assert fb == [{"requirement_ids": ["R-1"]}]
    blob = json.dumps(fb)
    for secret in ("chk-hidden-r1-seven", "test_zq_hidden_seven", "test_parse_hidden_zq", "hidden", "7.0", "assert"):
        assert secret not in blob


def test_feedback_for_visible_failures_is_capped_and_complete(toy, cs):
    _c, _p, rep = verify(toy, cs, toy.base)
    fb = AD.feedback_for_implementer(rep, cs)
    visible = [f for f in fb if "check_id" in f]
    assert {f["check_id"] for f in visible} == {"chk-r1-basic", "chk-r2-empty"}
    assert all(len(f["failure_text"]) <= 2000 and f["failure_text"] for f in visible)
    hidden = [f for f in fb if "check_id" not in f]
    assert hidden == [{"requirement_ids": ["R-1"]}]
    assert set(hidden[0]) == {"requirement_ids"}
    blob = json.dumps(hidden)
    assert "zq" not in blob and "chk-hidden" not in blob


def test_feedback_cap_applies():
    from hera.oracle.model import RunReport
    from hera.types import CheckResult
    cs_ = type("C", (), {"checks": CHECKS})()
    rep = RunReport(results=(CheckResult("chk-r1-basic", ("R-1",), CheckStatus.FAIL, "visible"),),
                    failure_texts={"chk-r1-basic": "x" * 5000})
    assert len(AD.feedback_for_implementer(rep, cs_)[0]["failure_text"]) == 2000


# ---- detection, command, matcher --------------------------------------------------------
def test_detect(tmp_path):
    assert AD.detect(tmp_path) == 0.0
    (tmp_path / "a.py").write_text("x=1")
    assert AD.detect(tmp_path) == 0.3
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("")
    assert AD.detect(tmp_path) == 1.0
    other = tmp_path / "o"
    other.mkdir()
    (other / "pytest.ini").write_text("[pytest]\n")
    assert AD.detect(other) == 1.0
    assert AD.detect(tmp_path / "missing") == 0.0


def test_command_shape(tmp_path):
    cmd = AD.command(tmp_path)
    assert cmd[0] == __import__("sys").executable and cmd[2:4] == ["-m", "pytest"]
    assert cmd[-1] == "_hera_checks" and "--junitxml=_hera_results/junit.xml" in cmd
    assert cmd[cmd.index("-c") + 1] == "_hera_checks/pytest.ini"
    assert not any(a.startswith("/") and a != cmd[0] for a in cmd)


@pytest.mark.parametrize("path,expected", [
    ("conftest.py", True), ("a/conftest.py", True), ("a/b/conftest.py", True), ("conftest.pyc", False),
    ("pytest.ini", True), ("tox.ini", True), ("setup.cfg", True), ("a/pytest.ini", False), ("pyproject.toml", False),
    ("tests/x.py", True), ("tests/a/b/x.txt", True), ("tests", False), ("test/x.py", True), ("src/tests/x.py", False),
    ("test_a.py", True), ("src/test_a.py", True), ("a/b/c/test_a.py", True), ("test_a.txt", False), ("src/atest_a.py", False),
    ("a_test.py", True), ("src/a_test.py", True), ("src/a_test.pyi", False),
    (".github/workflows/ci.yml", True), (".github", False), ("github/x.yml", False),
    ("sitecustomize.py", True), ("usercustomize.py", True), ("a/sitecustomize.py", True), ("a/usercustomize.py", True),
    ("pytest_evil.py", True), ("src/pytest_evil.py", False), ("pytest_plugin/x.py", False),
    ("calc.py", False), ("src/pkg/mod.py", False), ("Conftest.py", True), ("./conftest.py", True),
    ("README.md", False), ("", False),
])
def test_is_protected(path, expected):
    assert AD.is_protected(path) is expected


def test_match_glob_semantics():
    assert match_glob("a/b/c.py", "**/c.py") and match_glob("c.py", "**/c.py")
    assert match_glob("a/x/y/c.py", "a/**/c.py") and match_glob("a/c.py", "a/**/c.py")
    assert not match_glob("a", "a/**") and match_glob("a/b", "a/**") and match_glob("a/b/c", "a/**")
    assert not match_glob("a/b.py", "*.py") and match_glob("b.py", "*.py")


# ---- candidate code forging results from inside the runner process -------------------------
_FORGE_ATEXIT = '''
import atexit, os, xml.etree.ElementTree as ET
def _forge():
    p = "_hera_results/junit.xml"
    t = ET.parse(p)
    for tc in t.getroot().iter("testcase"):
        if tc.get("name") != "test_hera_must_fail":
            for ch in list(tc):
                if ch.tag in ("failure", "error"):
                    tc.remove(ch)
    t.write(p)
    os._exit(1)
atexit.register(_forge)
'''
_FORGE_PATCH = '''
import _pytest.reports as R
_orig = R.TestReport.from_item_and_call.__func__
def _patched(cls, item, call):
    rep = _orig(cls, item, call)
    if item.name != "test_hera_must_fail":
        rep.outcome = "passed"
        rep.longrepr = None
    return rep
R.TestReport.from_item_and_call = classmethod(_patched)
'''


def test_atexit_rewrite_of_results_is_caught_by_summary_cross_check(toy, cs):
    from toyrepo import HARDCODED_CALC
    _c, _p, rep = verify(toy, cs, toy.candidate({"calc.py": HARDCODED_CALC + _FORGE_ATEXIT}))
    assert statuses(rep)["chk-hidden-r1-seven"] is CheckStatus.PASS      # the forged file says so ...
    assert rep.sentinels_ok and rep.returncode == 1                       # ... sentinels and exit code are faked well
    assert not rep.exit_code_consistent                                   # ... but pytest's own summary line disagrees


def test_KNOWN_GAP_in_process_monkeypatch_is_not_detected(toy, cs):
    """Documents an OPEN tamper route (docs/ORACLE.md, residual risk): candidate code imported by a
    check runs in the pytest process and can rewrite every report before any plugin sees it. The
    sentinel pair and the exit-code cross-check cannot see this. If this test starts failing,
    something closed the gap: update the documentation."""
    from toyrepo import HARDCODED_CALC
    _c, _p, rep = verify(toy, cs, toy.candidate({"calc.py": HARDCODED_CALC + _FORGE_PATCH}))
    assert statuses(rep)["chk-hidden-r1-seven"] is CheckStatus.PASS
    assert rep.sentinels_ok and rep.exit_code_consistent
