"""node adapter against the JavaScript toy project: honest, hard-coded and tampering candidates.

Mirrors tests/test_pytest_adapter.py. Runs pass ``allow_unsandboxed=True`` so they work everywhere;
where a sandbox mechanism exists (CI) it is auto-detected and used anyway."""
import json
import os
import time

import pytest

from hera import sandbox
from hera.oracle.adapters import node_adapter as na
from hera.oracle.adapters.node_adapter import NodeAdapter
from hera.oracle.checkstore import CheckStoreTampered, freeze
from hera.oracle.model import DraftCheck
from hera.sandbox import Limits, detect_mechanism
from hera.types import CheckStatus
from toyrepo_js import (CANDIDATES, CHECK_FILES, CHECKS, HARDCODED_CALC, PACKAGE_JSON,
                        REQUIREMENTS, TEST_CALC, make_js_toy, needs_node)

pytestmark = needs_node
AD = NodeAdapter()
MANIFEST = {c.check_id for c in CHECKS}
needs_mech = pytest.mark.skipif(detect_mechanism() == "none", reason="no sandbox mechanism on this machine")


@pytest.fixture
def jtoy(hera_home, tmp_path):
    return make_js_toy(tmp_path)


@pytest.fixture
def cs(conn, hera_home):
    return freeze(conn, task_id="toyjs", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES),
                  adapter="node")


def verify(toy, cs, commit, *, include_hidden=True, limits=None, unsandboxed=True):
    checkout = toy.repo.verification_checkout(commit)
    base_tree = toy.repo.read_tree(toy.base, AD.is_protected)
    prep = AD.prepare(checkout, cs, base_tree=base_tree, include_hidden=include_hidden)
    rep = AD.run(checkout, cs, include_hidden=include_hidden, limits=limits, allow_unsandboxed=unsandboxed)
    return checkout, prep, rep


def statuses(rep):
    return {r.check_id: r.status for r in rep.results}


def freeze_one(conn, files, checks, requirements=REQUIREMENTS):
    return freeze(conn, task_id="toyjs", requirements=requirements, checks=tuple(checks), files=files, adapter="node")


def chk(cid, rel, node, req="R-1", kind="new", vis="visible"):
    return DraftCheck(cid, (req,), vis, rel, node, kind=kind, runner="node")


def test_base_tree_is_protected_files_only(jtoy):
    bt = jtoy.repo.read_tree(jtoy.base, AD.is_protected)
    assert set(bt) == {"test/calc.test.js"} and bt["test/calc.test.js"].decode() == TEST_CALC
    assert set(jtoy.repo.read_tree(jtoy.base)) == {"package.json", "calc.js", "test/calc.test.js"}


def test_base_commit(jtoy, cs):
    _checkout, prep, rep = verify(jtoy, cs, jtoy.base)
    assert prep.restored == prep.deleted == prep.modified_protected == ()
    st = statuses(rep)
    assert st["chk-r1-basic"] is st["chk-r2-empty"] is st["chk-hidden-r1-seven"] is CheckStatus.ERROR
    assert st["chk-r3-add"] is CheckStatus.PASS                       # the TypeScript check file ran
    assert rep.sentinels_ok and rep.exit_code_consistent and rep.results_parsed and not rep.timed_out
    assert rep.executed_ids == MANIFEST and rep.raw_unmatched == ()
    assert "parseAmount" in rep.failure_texts["chk-r1-basic"]


def test_correct_candidate(jtoy, cs):
    _c, prep, rep = verify(jtoy, cs, jtoy.candidate(CANDIDATES["correct"]))
    assert all(r.status is CheckStatus.PASS for r in rep.results) and rep.executed_ids == MANIFEST
    assert rep.sentinels_ok and rep.exit_code_consistent and rep.returncode == 1   # the must-fail sentinel
    assert prep.deleted == () and all(r.output_digest == "" for r in rep.results)
    assert {r.check_id: r.visibility for r in rep.results}["chk-hidden-r1-seven"] == "hidden"
    assert all(r.duration_s >= 0 for r in rep.results)


def test_hardcoded_candidate_fails_hidden_only(jtoy, cs):
    _c, _p, rep = verify(jtoy, cs, jtoy.candidate(CANDIDATES["hardcoded"]))
    st = statuses(rep)
    assert st["chk-r1-basic"] is st["chk-r2-empty"] is st["chk-r3-add"] is CheckStatus.PASS
    assert st["chk-hidden-r1-seven"] is CheckStatus.FAIL
    assert rep.sentinels_ok and rep.exit_code_consistent
    assert len({r.check_id: r for r in rep.results}["chk-hidden-r1-seven"].output_digest) == 64


@needs_mech
def test_correct_and_hardcoded_under_real_sandbox(jtoy, cs):
    _c, _p, rep = verify(jtoy, cs, jtoy.candidate(CANDIDATES["correct"]), unsandboxed=False)
    assert all(r.status is CheckStatus.PASS for r in rep.results) and rep.sentinels_ok and rep.exit_code_consistent
    _c, _p, rep = verify(jtoy, cs, jtoy.candidate(CANDIDATES["hardcoded"]), unsandboxed=False)
    assert statuses(rep)["chk-hidden-r1-seven"] is CheckStatus.FAIL and rep.sentinels_ok


def test_node_install_directory_is_made_readable_and_the_environment_is_scrubbed(jtoy, cs, monkeypatch):
    seen = []
    real = sandbox.run

    def spy(argv, **kw):
        seen.append((list(argv), kw))
        return real(argv, **kw)
    monkeypatch.setattr(sandbox, "run", spy)
    verify(jtoy, cs, jtoy.base)
    node = os.path.realpath(seen[0][0][0])
    prefix = os.path.dirname(os.path.dirname(node))
    assert len(seen) == 4                                            # sentinel + three check files
    for argv, kw in seen:
        assert os.path.realpath(argv[0]) == node and "--test" in argv
        assert kw["read_only"] == ([] if prefix == "/" or prefix.startswith("/usr/") or prefix == "/usr" else [prefix])
        assert kw["env"]["NODE_OPTIONS"] == "" and kw["limits"].memory_mb is None
        assert argv[-1].startswith("_hera_checks/") and not any(a.startswith("/") for a in argv[1:])
    assert [a[-1] for a, _k in seen].count("_hera_checks/_sentinel/hera_sentinel.test.mjs") == 1


# ---- the candidate cannot steer the run -----------------------------------------------------
def test_noderc_and_npmrc_are_deleted(jtoy, cs):
    commit = jtoy.candidate({**CANDIDATES["noderc"], **CANDIDATES["npmrc_preload"], **CANDIDATES["correct"]})
    checkout, prep, rep = verify(jtoy, cs, commit)
    assert not (checkout / ".noderc").exists() and not (checkout / ".npmrc").exists()
    assert {".noderc", ".npmrc"} <= set(prep.deleted) and prep.modified_protected == ()
    assert all(r.status is CheckStatus.PASS for r in rep.results) and rep.sentinels_ok and rep.exit_code_consistent


def test_node_options_in_package_json_are_stripped_but_the_rest_is_kept(jtoy, cs):
    commit = jtoy.candidate({**CANDIDATES["package_node_options"], **CANDIDATES["correct"]})
    checkout, prep, rep = verify(jtoy, cs, commit)
    doc = json.loads((checkout / "package.json").read_text())
    assert doc == {"name": "toy-js", "type": "module"}
    assert "package.json" in prep.modified_protected and "package.json" in prep.restored
    assert all(r.status is CheckStatus.PASS for r in rep.results)


def test_an_untouched_or_legitimately_changed_package_json_is_left_alone(jtoy, cs):
    changed = '{"name": "toy-js", "type": "module", "scripts": {"test": "node --test"}}\n'
    checkout, prep, _rep = verify(jtoy, cs, jtoy.candidate({"package.json": changed, **CANDIDATES["correct"]}))
    assert (checkout / "package.json").read_text() == changed and prep.modified_protected == ()
    checkout, prep, _rep = verify(jtoy, cs, jtoy.base)
    assert (checkout / "package.json").read_text() == PACKAGE_JSON and prep.restored == ()


def test_edited_project_test_is_restored(jtoy, cs):
    checkout, prep, rep = verify(jtoy, cs, jtoy.candidate(CANDIDATES["edit_tests"]))
    assert (checkout / "test" / "calc.test.js").read_text() == TEST_CALC
    assert "test/calc.test.js" in prep.modified_protected and "test/calc.test.js" in prep.restored
    assert rep.sentinels_ok and rep.exit_code_consistent and rep.executed_ids == MANIFEST


def test_planted_oracle_directories_are_wiped(jtoy, cs):
    commit = jtoy.candidate({**CANDIDATES["tamper_checks"], **CANDIDATES["correct"]})
    checkout, prep, rep = verify(jtoy, cs, commit)
    assert "_hera_checks" in prep.deleted and "_hera_results" in prep.deleted
    assert all(r.status is CheckStatus.PASS for r in rep.results) and rep.executed_ids == MANIFEST
    assert rep.raw_unmatched == () and rep.sentinels_ok and rep.exit_code_consistent
    assert not (checkout / "_hera_checks").exists()                  # nothing of the checks stays behind


def test_protected_symlinks_and_directories_are_replaced(jtoy, cs):
    wt = jtoy.repo.new_candidate("links")
    (wt.path / "test" / "calc.test.js").unlink()
    (wt.path / "test").rmdir()
    os.symlink("src_dir", wt.path / "test")                          # link where a protected directory belongs
    (wt.path / "src_dir").mkdir()
    (wt.path / "src_dir" / "marker.txt").write_text("m")
    os.symlink("calc.js", wt.path / "evil.test.js")                 # added protected link
    checkout, prep, rep = verify(jtoy, cs, wt.commit_all("links"))
    assert (checkout / "test").is_dir() and not (checkout / "test").is_symlink()
    assert (checkout / "test" / "calc.test.js").is_file()
    assert not (checkout / "src_dir" / "calc.test.js").exists()      # nothing was written through the link
    assert "test/calc.test.js" in prep.modified_protected and "evil.test.js" in prep.deleted
    assert rep.sentinels_ok


def test_candidate_cannot_plant_results_for_a_later_run(jtoy, conn, hera_home):
    """The first process that imports candidate code writes into the checkout; the files of later
    runs are written afterwards, and the results directory is recreated for every run."""
    evil = HARDCODED_CALC + (
        "\nimport { mkdirSync, writeFileSync, symlinkSync } from 'node:fs';\n"
        "try { mkdirSync('_hera_checks/hidden', { recursive: true }); } catch {}\n"
        "try { writeFileSync('_hera_checks/hidden/parse_hidden_zq.test.mjs', "
        "\"import test from 'node:test';\\ntest('zq_hidden_seven', () => {});\\n\"); } catch {}\n"
        "try { symlinkSync('/etc/hostname', '_hera_results/junit.xml'); } catch {}\n")
    cs_ = freeze(conn, task_id="toyjs", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES),
                 adapter="node")
    _c, _p, rep = verify(jtoy, cs_, jtoy.candidate({"calc.js": evil}))
    assert statuses(rep)["chk-hidden-r1-seven"] is CheckStatus.FAIL          # the real hidden file ran
    assert rep.sentinels_ok and rep.exit_code_consistent


def test_slow_candidate_times_out(jtoy, cs):
    t0 = time.monotonic()
    _c, _p, rep = verify(jtoy, cs, jtoy.candidate(CANDIDATES["slow"]), limits=Limits(wall_s=6))
    assert time.monotonic() - t0 < 60
    assert rep.timed_out and rep.returncode is None
    assert rep.executed_ids == MANIFEST
    assert not rep.exit_code_consistent
    st = statuses(rep)
    assert st["chk-r1-basic"] is st["chk-r2-empty"] is st["chk-hidden-r1-seven"] is CheckStatus.TIMEOUT


def test_exclude_hidden(jtoy, cs):
    checkout, _p, rep = verify(jtoy, cs, jtoy.candidate(CANDIDATES["correct"]), include_hidden=False)
    assert not (checkout / "_hera_checks").exists()
    assert rep.executed_ids == MANIFEST - {"chk-hidden-r1-seven"}
    assert all(r.visibility == "visible" for r in rep.results) and rep.sentinels_ok and rep.exit_code_consistent


def test_run_requires_prepare(jtoy, cs):
    checkout = jtoy.repo.verification_checkout(jtoy.base)
    with pytest.raises(RuntimeError):
        AD.run(checkout, cs, allow_unsandboxed=True)
    AD.prepare(checkout, cs, base_tree={}, include_hidden=True)
    with pytest.raises(RuntimeError):
        AD.run(checkout, cs, include_hidden=False, allow_unsandboxed=True)


def test_prepare_refuses_tampered_store(jtoy, cs):
    f = cs.root / "visible" / "parse_amount.test.js"
    os.chmod(f.parent, 0o755)
    os.chmod(f, 0o644)
    f.write_bytes(b"import test from 'node:test';\ntest('basic_amount', () => {});\n")
    with pytest.raises(CheckStoreTampered):
        AD.prepare(jtoy.repo.verification_checkout(jtoy.base), cs, base_tree={}, include_hidden=True)


def test_prepare_rejects_bad_base_tree(jtoy, cs):
    checkout = jtoy.repo.verification_checkout(jtoy.base)
    for bad in ("../x.test.js", "/x.test.js", "calc.js"):
        with pytest.raises(ValueError):
            AD.prepare(checkout, cs, base_tree={bad: b""}, include_hidden=True)


# ---- how results are read ---------------------------------------------------------------------
def test_a_failure_after_the_subtests_passed_is_still_a_failure(jtoy, conn, hera_home):
    """junit drops the failure of a test that fails after its subtests passed; the TAP stream does not."""
    src = ("import test from 'node:test';\n"
           "test('late_failure', async (t) => {\n  await t.test('sub', () => {});\n  throw new Error('late');\n});\n"
           "test('all_subtests', async (t) => {\n  await t.test('one', () => {});\n  await t.test('two', () => {});\n});\n"
           "test('one_bad_subtest', async (t) => {\n  await t.test('one', () => {});\n"
           "  await t.test('two', () => { throw new Error('x'); });\n});\n")
    cs_ = freeze_one(conn, {"sub.test.js": src.encode()},
                     [chk("c-late", "sub.test.js", "late_failure"), chk("c-all", "sub.test.js", "all_subtests"),
                      chk("c-one", "sub.test.js", "one_bad_subtest")])
    _c, _p, rep = verify(jtoy, cs_, jtoy.base)
    assert statuses(rep) == {"c-late": CheckStatus.FAIL, "c-all": CheckStatus.PASS, "c-one": CheckStatus.FAIL}
    assert rep.sentinels_ok and rep.exit_code_consistent and rep.raw_unmatched == ()


def test_describe_skip_todo_and_load_errors(jtoy, conn, hera_home):
    src = ("import test, { describe, it } from 'node:test';\nimport assert from 'node:assert/strict';\n"
           "describe('suite_ok', () => { it('a', () => {}); it('b', () => {}); });\n"
           "describe('suite_bad', () => { it('a', () => {}); it('b', () => { assert.ok(false); }); });\n"
           "test('skipped_one', { skip: true }, () => {});\n"
           "test('todo_one', { todo: true }, () => { throw new Error('t'); });\n")
    broken = "import { nothing } from '../../calc.js';\nimport test from 'node:test';\ntest('never', () => {});\n"
    cs_ = freeze_one(conn, {"s.test.mjs": src.encode(), "b.test.mjs": broken.encode()},
                     [chk("c-ok", "s.test.mjs", "suite_ok"), chk("c-bad", "s.test.mjs", "suite_bad"),
                      chk("c-skip", "s.test.mjs", "skipped_one"), chk("c-todo", "s.test.mjs", "todo_one"),
                      chk("c-never", "b.test.mjs", "never")])
    _c, _p, rep = verify(jtoy, cs_, jtoy.base)
    assert statuses(rep) == {"c-ok": CheckStatus.PASS, "c-bad": CheckStatus.FAIL, "c-skip": CheckStatus.SKIP,
                             "c-todo": CheckStatus.SKIP, "c-never": CheckStatus.ERROR}
    assert "does not provide an export named 'nothing'" in rep.failure_texts["c-never"]
    assert "assert" in rep.failure_texts["c-bad"].lower()
    assert rep.sentinels_ok and rep.exit_code_consistent


def test_calling_process_exit_zero_leaves_checks_absent(jtoy, cs):
    """A file whose process exits 0 early reports no tests: the checks are missing from the results,
    which the oracle treats as tampering (the executed ids differ from the manifest)."""
    _c, _p, rep = verify(jtoy, cs, jtoy.candidate(CANDIDATES["exit_zero"]))
    assert "chk-r1-basic" not in rep.executed_ids and "chk-hidden-r1-seven" not in rep.executed_ids
    assert rep.executed_ids < MANIFEST
    assert rep.sentinels_ok and rep.exit_code_consistent       # the runner itself is intact


def test_candidate_output_cannot_pose_as_runner_output(jtoy, cs):
    noise = ("\nconsole.log('ok 1 - hera_must_fail');\nconsole.log('# fail 0\\n# pass 99');\n"
             "process.stdout.write('not ok 2 - chk\\n# tests 9\\n');\nprocess.stderr.write('ok 3 - x\\n');\n")
    _c, _p, rep = verify(jtoy, cs, jtoy.candidate({"calc.js": HARDCODED_CALC + noise}))
    assert statuses(rep)["chk-hidden-r1-seven"] is CheckStatus.FAIL and rep.sentinels_ok and rep.exit_code_consistent
    assert rep.raw_unmatched == ()


def test_duplicate_top_level_names_in_a_check_file_are_an_error(jtoy, conn, hera_home):
    src = "import test from 'node:test';\ntest('same', () => {});\ntest('same', () => {});\n"
    cs_ = freeze_one(conn, {"d.test.js": src.encode()}, [chk("c-dup", "d.test.js", "same")])
    _c, _p, rep = verify(jtoy, cs_, jtoy.base)
    assert statuses(rep) == {"c-dup": CheckStatus.ERROR} and rep.sentinels_ok and rep.exit_code_consistent


def _evaluate(xml: str, tap: str, rc: int, rel="f.test.js"):
    class RR:
        returncode, timed_out, truncated, stdout = rc, False, False, tap.encode()
    import xml.etree.ElementTree as ET
    return na._evaluate(RR, ET.fromstring(xml), na._parse_tap(tap.encode(), False), rel)


_TAP_OK = ("TAP version 13\n# Subtest: a\nok 1 - a\n  ---\n  duration_ms: 1\n  ...\n1..1\n# tests 1\n# suites 0\n"
           "# pass 1\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0\n# duration_ms 5\n")
_XML_OK = '<testsuites><testcase name="a" time="0.001" classname="test"/></testsuites>'


def test_evaluate_accepts_honest_evidence_and_rejects_every_disagreement():
    assert _evaluate(_XML_OK, _TAP_OK, 0).consistent
    assert not _evaluate(_XML_OK, _TAP_OK, 1).consistent                      # exit code vs TAP summary
    failing = _TAP_OK.replace("ok 1 - a", "not ok 1 - a").replace("# pass 1", "# pass 0").replace("# fail 0", "# fail 1")
    assert not _evaluate(_XML_OK, failing, 1).consistent                      # a forged junit file: pass vs fail
    assert _evaluate(_XML_OK.replace('classname="test"/>', 'classname="test"><failure message="x"/></testcase>'),
                     failing, 1).consistent
    assert not _evaluate(_XML_OK.replace('name="a"', 'name="b"'), _TAP_OK, 0).consistent          # other test names
    assert not _evaluate('<testsuites/>', _TAP_OK, 0).consistent                                   # junit emptied
    assert not _evaluate(_XML_OK, _TAP_OK.replace("# fail 0", "# fail 2"), 0).consistent           # summary vs exit
    assert not _evaluate(_XML_OK, _TAP_OK.replace("# fail 0", "# fail 1"), 0).consistent           # summary vs lines


def test_parse_tap_refuses_truncated_non_tap_and_summaryless_output():
    assert na._parse_tap(_TAP_OK.encode(), True) is None
    assert na._parse_tap(b"hello\n", False) is None
    assert na._parse_tap(_TAP_OK.split("# tests")[0].encode(), False) is None
    t = na._parse_tap(_TAP_OK.replace("ok 1 - a", "ok 1 - a # SKIP why").encode(), False)
    assert t.top == [("a", CheckStatus.SKIP)] and t.counts["pass"] == 1


# ---- the known gap -----------------------------------------------------------------------------
def test_KNOWN_GAP_in_process_patching_is_not_detected(jtoy, cs):
    """Documents an OPEN tamper route (docs/ORACLE.md, "Node adapter"): candidate code imported by a
    check runs in the same Node process as node:test and can rewrite what the check asserts with.
    Here the shared assert functions become no-ops, so the hard-coded candidate passes the hidden
    check; the sentinel pair (its own process, which never imports candidate code), the exit-code
    cross-check and the junit/TAP comparison cannot see it. Contract checks close it for the
    requirements they cover. If this test starts failing, something closed the gap: update the
    documentation."""
    _c, _p, rep = verify(jtoy, cs, jtoy.candidate(CANDIDATES["forge_assert"]))
    assert statuses(rep)["chk-hidden-r1-seven"] is CheckStatus.PASS
    assert rep.sentinels_ok and rep.exit_code_consistent


def test_the_lazy_candidate_without_the_forgery_is_caught(jtoy, cs):
    _c, _p, rep = verify(jtoy, cs, jtoy.candidate(CANDIDATES["lazy"]))
    assert statuses(rep)["chk-hidden-r1-seven"] is CheckStatus.FAIL and rep.sentinels_ok


# ---- feedback ----------------------------------------------------------------------------------
def test_feedback_redacts_hidden_checks(jtoy, cs):
    _c, _p, rep = verify(jtoy, cs, jtoy.candidate(CANDIDATES["hardcoded"]))
    fb = AD.feedback_for_implementer(rep, cs)
    assert fb == [{"requirement_ids": ["R-1"]}]
    blob = json.dumps(fb)
    for secret in ("chk-hidden-r1-seven", "zq_hidden_seven", "parse_hidden_zq", "hidden", "7", "assert", ".mjs"):
        assert secret not in blob


def test_feedback_for_visible_failures_is_capped_and_complete(jtoy, cs):
    _c, _p, rep = verify(jtoy, cs, jtoy.base)
    fb = AD.feedback_for_implementer(rep, cs)
    visible = [f for f in fb if "check_id" in f]
    assert {f["check_id"] for f in visible} == {"chk-r1-basic", "chk-r2-empty"}
    assert all(0 < len(f["failure_text"]) <= 2000 for f in visible)
    hidden = [f for f in fb if "check_id" not in f]
    assert hidden == [{"requirement_ids": ["R-1"]}] and set(hidden[0]) == {"requirement_ids"}
    assert "zq" not in json.dumps(hidden) and "chk-hidden" not in json.dumps(hidden)


# ---- detection, command, protected paths ---------------------------------------------------------
def test_detect(tmp_path):
    assert AD.detect(tmp_path) == 0.0
    (tmp_path / "a.js").write_text("x=1")
    assert AD.detect(tmp_path) == 0.3
    (tmp_path / "a.test.mjs").write_text("")
    assert AD.detect(tmp_path) == 1.0
    other = tmp_path / "o"
    other.mkdir()
    (other / "package.json").write_text("{}")
    assert AD.detect(other) == 0.3
    nm = tmp_path / "p" / "node_modules" / "x"
    nm.mkdir(parents=True)
    (nm / "x.test.js").write_text("")
    assert AD.detect(tmp_path / "p") == 0.0 and AD.detect(tmp_path / "missing") == 0.0


def test_command_shape(tmp_path):
    cmd = AD.command(tmp_path)
    assert os.path.isabs(cmd[0]) and "--test" in cmd and cmd[-1] == "_hera_checks/_sentinel/hera_sentinel.test.mjs"
    assert "--test-reporter=junit" in cmd and "--test-reporter-destination=_hera_results/junit.xml" in cmd
    assert "--test-reporter=tap" in cmd and "--test-reporter-destination=stdout" in cmd
    assert not any(a.startswith("/") for a in cmd[1:])


def test_project_test_paths(jtoy):
    bt = {"test/calc.test.js": b"", "a/b.test.ts": b"", "x.test.mts": b"", "y.test.mjs": b"", "z.spec.js": b"",
          ".npmrc": b"", "tests/helper.js": b"", "w.test.cjs": b""}
    assert AD.project_test_paths(bt) == ("a/b.test.ts", "test/calc.test.js", "x.test.mts", "y.test.mjs")


@pytest.mark.parametrize("path,expected", [
    (".noderc", True), ("a/.noderc", True), (".npmrc", True), ("a/b/.npmrc", True), ("node.config.json", True),
    ("test/x.js", True), ("tests/a/b/x.txt", True), ("__tests__/x.js", True), ("src/__tests__/x.js", True),
    ("calc.test.js", True), ("src/a.test.mjs", True), ("src/a.test.ts", True), ("a/b/c.test.mts", True),
    ("a.spec.js", True), ("a-test.js", True), ("a_test.mjs", True), ("test-a.js", True), ("test.js", True),
    ("a.test.json", False), ("a.test.js.map", False), ("src/atest.js", False), ("contest.js", False),
    (".github/workflows/ci.yml", True), (".github", False), ("package.json", False), ("calc.js", False),
    ("src/index.ts", False), ("src/tests/x.js", False), ("NPMRC", False), ("Calc.TEST.JS", True),
    ("./.noderc", True), ("README.md", False), ("", False),
])
def test_is_protected(path, expected):
    assert AD.is_protected(path) is expected


def test_controls_runner():
    assert AD.controls_runner(".noderc") and AD.controls_runner("x/.npmrc") and AD.controls_runner("node.config.json")
    assert not AD.controls_runner("package.json") and not AD.controls_runner("package.json", b"{}")
    assert AD.controls_runner("package.json", b'{"node_options": "--import x"}')
    assert AD.controls_runner("a/package.json", b'{"nodeOptions": "--import x"}')
    assert not AD.controls_runner("package.json", b"not json") and not AD.controls_runner("calc.js", b"x")
