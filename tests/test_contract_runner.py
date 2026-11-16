"""ContractRunner: out-of-process checks, fresh run directory per check, oracle-side comparison.

Everything runs with allow_unsandboxed=True so it works everywhere; where a sandbox mechanism
exists (CI) it is auto-detected and used. Tests that need real isolation skip otherwise. Nothing
here writes outside a run directory from a sandboxed child and nothing relies on root."""
import json
import os
import sys

import pytest

from hera import sandbox
from hera.oracle import checkstore
from hera.oracle.adapters.pytest_adapter import PytestAdapter
from hera.oracle.checkstore import CheckStoreTampered, freeze
from hera.oracle.contract_runner import ContractRunner
from hera.oracle.model import DraftCheck, checks_by_runner
from hera.oracle.validation import validate_on_base
from hera.oracle.verify import render_feedback, run_baseline, verify
from hera.sandbox import Limits, detect_mechanism
from hera.types import CheckHealth, CheckStatus as S, Verdict as V
from toyrepo import (CANDIDATES, CHECK_FILES, CHECKS, CONTRACT_CHECKS, CONTRACT_FILES, CORRECT_CALC, CT_BASIC,
                     FORGE_PATCH, HARDCODED_CALC, MIXED_CHECKS, MIXED_FILES, REQUIREMENTS, SLOW_CALC)

AD = PytestAdapter()
MECH = detect_mechanism()
needs_mech = pytest.mark.skipif(MECH == "none", reason="no sandbox mechanism on this machine")
_BASELINE = []


def baseline_for(toy):
    if not _BASELINE:
        _BASELINE.append(run_baseline(toy.repo, AD, allow_unsandboxed=True))
    return _BASELINE[0]


def mk(conn, checks=CONTRACT_CHECKS, files=CONTRACT_FILES):
    return freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=tuple(checks), files=dict(files))


@pytest.fixture
def cs(conn, hera_home):
    return mk(conn)


def run_on(toy, cs, commit, **kw):
    co = toy.repo.verification_checkout(commit)
    try:
        rep = ContractRunner().run(co, cs, allow_unsandboxed=True, **kw)
    finally:
        toy.repo.cleanup_verification(co)
    return rep


def st(rep):
    return {r.check_id: r.status for r in rep.results}


def cfile(*nodes):
    """nodes: (node, python code without single quotes, expect toml, optional extra check keys)."""
    out = []
    for name, code, expect, *extra in nodes:
        out.append(f'[[check]]\nnode = "{name}"\nargv = ["{{python}}", "-c", \'{code}\']\n'
                   f'{extra[0] if extra else ""}[check.expect]\n{expect}\n')
    return "".join(out).encode()


def dc(cid, node, rel, vis="visible", req="R-1", kind="new"):
    return DraftCheck(cid, (req,), vis, rel, node, author_role=vis, kind=kind, runner="contract")


# ---- the toy repository ----------------------------------------------------------------------------
def test_correct_passes_every_contract_check(toy, cs):
    rep = run_on(toy, cs, toy.candidate(CANDIDATES["correct"]))
    assert set(st(rep).values()) == {S.PASS} and len(rep.results) == 4
    assert rep.executed_ids == cs.check_ids and rep.failure_texts == {} and not rep.timed_out_any
    assert rep.mechanism == MECH and all(r.output_digest == "" for r in rep.results)
    by = {r.check_id: r for r in rep.results}
    assert by["chk-c-hidden-r1-seven"].visibility == "hidden" and by["chk-c-basic"].requirement_ids == ("R-1",)


def test_hardcoded_fails_only_the_hidden_contract_check(toy, cs):
    rep = run_on(toy, cs, toy.candidate(CANDIDATES["hardcoded"]))
    assert st(rep) == {"chk-c-basic": S.PASS, "chk-c-empty": S.PASS, "chk-c-add": S.PASS,
                       "chk-c-hidden-r1-seven": S.FAIL}
    assert rep.failure_texts == {}                                     # hidden failures never get a text
    bad = next(r for r in rep.results if r.status is S.FAIL)
    assert len(bad.output_digest) == 64                                # sha256 of stdout + stderr


def test_base_commit_fails_the_new_checks_and_passes_the_regression_check(toy, cs):
    rep = run_on(toy, cs, toy.base)
    assert st(rep) == {"chk-c-basic": S.FAIL, "chk-c-empty": S.FAIL, "chk-c-hidden-r1-seven": S.FAIL,
                       "chk-c-add": S.PASS}
    assert set(rep.failure_texts) == {"chk-c-basic", "chk-c-empty"}    # visible only


def test_include_hidden_false_skips_hidden_contract_checks(toy, cs):
    rep = run_on(toy, cs, toy.candidate(CANDIDATES["correct"]), include_hidden=False)
    assert set(st(rep)) == {"chk-c-basic", "chk-c-empty", "chk-c-add"}


def test_no_contract_checks_is_an_empty_report(conn, hera_home, toy):
    cs0 = freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES))
    rep = run_on(toy, cs0, toy.base)
    assert rep.results == () and rep.executed_ids == frozenset() and rep.mechanism == ""


def test_visible_failure_text_and_feedback(toy, cs):
    commit = toy.candidate({"calc.py": CORRECT_CALC + "\n\ndef parse_amount(text):\n    print('x' * 5000)\n    raise SystemExit(4)\n"})
    co = toy.repo.verification_checkout(commit)
    try:
        runner = ContractRunner()
        rep = runner.run(co, cs, allow_unsandboxed=True)
    finally:
        toy.repo.cleanup_verification(co)
    text = rep.failure_texts["chk-c-basic"]
    assert "failed expectations:" in text and "exit_code: expected 0, got 4" in text
    assert "stdout_equals" in text and "stdout (first 2000 characters):" in text and "stderr (first 2000" in text
    assert "x" * 2000 in text and "x" * 2001 not in text               # first 2,000 characters of stdout
    assert "hera-contract-" not in text and str(toy.repo.root) not in text
    fb = runner.feedback_for_implementer(rep, cs)
    assert {"requirement_ids": ["R-1"]} in fb and any(f.get("check_id") == "chk-c-basic" for f in fb)
    assert not any("chk-c-hidden-r1-seven" in json.dumps(f) or "zq" in json.dumps(f) for f in fb)


def test_hidden_failure_feedback_is_requirement_ids_only(toy, cs):
    runner = ContractRunner()
    co = toy.repo.verification_checkout(toy.candidate(CANDIDATES["hardcoded"]))
    try:
        rep = runner.run(co, cs, allow_unsandboxed=True)
    finally:
        toy.repo.cleanup_verification(co)
    assert runner.feedback_for_implementer(rep, cs) == [{"requirement_ids": ["R-1"]}]


# ---- fresh copy per check ----------------------------------------------------------------------------
def test_state_written_by_one_check_is_not_visible_to_the_next(conn, hera_home, toy):
    calc = CORRECT_CALC + ("\n\ndef touch():\n    import os\n    if os.path.exists('seen.txt'):\n        return 'carried'\n"
                           "    open('seen.txt', 'w').write('x')\n    return 'fresh'\n")
    node = ("{n}", "import calc; print(calc.touch())", 'stdout_equals = "fresh\\n"')
    files = {"a.contract.toml": cfile(("one", *node[1:]), ("two", *node[1:])),
             "b.contract.toml": cfile(("three", *node[1:]))}
    checks = [dc("c1", "one", "a.contract.toml"), dc("c2", "two", "a.contract.toml", req="R-2"),
              dc("c3", "three", "b.contract.toml", vis="hidden", req="R-3")]
    cs = mk(conn, checks, files)
    commit = toy.candidate({"calc.py": calc})
    co = toy.repo.verification_checkout(commit)
    try:
        rep = ContractRunner().run(co, cs, allow_unsandboxed=True)
        assert set(st(rep).values()) == {S.PASS}                        # every check saw a fresh copy
        assert not (co / "seen.txt").exists()                          # and the checkout itself is untouched
        assert [p for p in co.parent.iterdir() if p.name.startswith("hera-contract-")] == []   # run dirs removed
    finally:
        toy.repo.cleanup_verification(co)


def test_input_files_stdin_and_env(conn, hera_home, toy):
    code = ('import os,sys,json; print(json.dumps({"stdin": sys.stdin.read(), "f": open("in/x.txt").read(), '
            '"env": os.environ.get("MY_VAR"), "pp": os.environ["PYTHONPATH"].split(os.pathsep)}))')
    files = {"i.contract.toml": cfile(("io", code,
                                       'exit_code = 0\nstdout_json_subset = { stdin = "typed", f = "data", env = "v" }',
                                       'stdin = "typed"\nenv = { MY_VAR = "v" }\nfiles = { "in/x.txt" = "data" }\n'))}
    cs = mk(conn, [dc("c1", "io", "i.contract.toml")], files)
    rep = run_on(toy, cs, toy.base)
    assert st(rep) == {"c1": S.PASS}


def test_oracle_directories_never_reach_a_run(conn, hera_home, toy):
    files = {"i.contract.toml": cfile(("ls", "import os; print(chr(32).join(sorted(os.listdir(chr(46)))))",
                                       'stdout_contains = ["calc.py"]'),
                                      ("nols", "import os; print(chr(32).join(sorted(os.listdir(chr(46)))))",
                                       'stdout_contains = ["_hera_checks"]'))}
    cs = mk(conn, [dc("c1", "ls", "i.contract.toml"), dc("c2", "nols", "i.contract.toml", req="R-2")], files)
    co = toy.repo.verification_checkout(toy.base)
    for d in ("_hera_checks", "_hera_results", "_hera_project"):
        (co / d).mkdir()
    try:
        rep = ContractRunner().run(co, cs, allow_unsandboxed=True)
    finally:
        toy.repo.cleanup_verification(co)
    assert st(rep) == {"c1": S.PASS, "c2": S.FAIL}


def test_symlinks_in_the_checkout_cannot_redirect_input_files_or_reach_the_original(conn, hera_home, toy):
    files = {"i.contract.toml": cfile(("sl", "print(open(chr(105)+chr(110)).read(), open(chr(100)+chr(97)+chr(116)+chr(97)).read(), "
                                              "__import__(chr(111)+chr(115)).path.lexists(chr(108)+chr(107)))",
                                       'stdout_equals = "input data False\\n"',
                                       'files = { "in" = "input" }\n'))}
    files["i.contract.toml"] = files["i.contract.toml"].replace(b'"input"', b'"input"')
    cs = mk(conn, [dc("c1", "sl", "i.contract.toml")], files)
    co = toy.repo.verification_checkout(toy.base)
    (co / "data").write_text("data")
    (co / "in").symlink_to("data")                                      # a link where the oracle writes an input file
    (co / "lk").symlink_to(co / "calc.py")                              # an absolute link back into the oracle's checkout
    try:
        rep = ContractRunner().run(co, cs, allow_unsandboxed=True)
        assert (co / "data").read_text() == "data"                      # the input was not written through the link
    finally:
        toy.repo.cleanup_verification(co)
    assert st(rep) == {"c1": S.PASS}


# ---- failure modes of the program -------------------------------------------------------------------------------
def test_program_not_found_is_error_and_other_statuses(conn, hera_home, toy):
    files = {"x.contract.toml": (b'[[check]]\nnode = "nf"\nargv = ["no-such-program-hera"]\n[check.expect]\nexit_code = 0\n'
                                 b'[[check]]\nnode = "slow"\ntimeout_s = 1\nargv = ["{python}", "-c", "import time; time.sleep(60)"]\n'
                                 b'[check.expect]\nexit_code = 0\n')}
    cs = mk(conn, [dc("c1", "nf", "x.contract.toml"), dc("c2", "slow", "x.contract.toml", req="R-2")], files)
    rep = run_on(toy, cs, toy.base)
    assert st(rep) == {"c1": S.ERROR, "c2": S.TIMEOUT} and rep.timed_out_any
    assert "could not be started" in rep.failure_texts["c1"] and "timed out" in rep.failure_texts["c2"]


def test_a_crashing_or_noisy_program_cannot_crash_evaluation(conn, hera_home, toy):
    big = "import sys; sys.stdout.write(chr(91)*3000000)"                   # more than the capture limit
    deep = "print(chr(91)*300000 + chr(93)*300000)"
    nul = "import sys; sys.stdout.buffer.write(bytes([0, 255, 254, 123]))"
    fifo = "import os; os.mkfifo(chr(111)+chr(117)+chr(116))"
    files = {"x.contract.toml": cfile(("big", big, "stdout_json = []"), ("deep", deep, "stdout_json = []"),
                                      ("nul", nul, 'stdout_json_subset = { a = 1 }'),
                                      ("fifo", fifo, 'files_equal = { out = "x" }'))}
    cs = mk(conn, [dc(f"c{i}", n, "x.contract.toml", req="R-1") for i, n in enumerate(("big", "deep", "nul", "fifo"))], files)
    rep = run_on(toy, cs, toy.base)
    assert set(st(rep).values()) == {S.FAIL}                               # failed expectations, no exception, no hang


# ---- forgery and leak routes ------------------------------------------------------------------------------------
FORGE_FILES = (
    "\n\nimport os as _os\n"
    "try:\n"
    "    _os.makedirs('_hera_results', exist_ok=True)\n"
    "    open('_hera_results/junit.xml', 'w').write('<testsuites><testsuite tests=\"1\"><testcase classname=\"x\" name=\"test_ok\"/></testsuite></testsuites>')\n"
    "    import hera.oracle.contract as _c, hera.types as _t\n"
    "    _c.evaluate = lambda *a, **k: (_t.CheckStatus.PASS, [])\n"
    "except Exception:\n    pass\n")


def test_monkeypatching_pytest_and_forging_result_files_has_no_effect(toy, cs):
    commit = toy.candidate({"calc.py": HARDCODED_CALC + FORGE_PATCH + FORGE_FILES})
    rep = run_on(toy, cs, commit)
    assert st(rep)["chk-c-hidden-r1-seven"] is S.FAIL
    assert st(rep)["chk-c-add"] is S.PASS and st(rep)["chk-c-basic"] is S.PASS     # honest results are unchanged


def test_hidden_contract_files_are_never_in_any_checkout_or_run_directory(conn, hera_home, toy, monkeypatch):
    cs = mk(conn, MIXED_CHECKS, MIXED_FILES)
    seen = []
    real_run = sandbox.run

    def spy(argv, *, cwd, **kw):
        names, blob = [], b""
        for root in (cwd, toy.repo.root / "verify"):                  # the run directory and every checkout
            for dirpath, dirnames, filenames in os.walk(root):
                for f in filenames:
                    p = os.path.join(dirpath, f)
                    names.append(p)
                    if not os.path.islink(p):
                        blob += open(p, "rb").read()
        seen.append((names, blob))
        return real_run(argv, cwd=cwd, **kw)
    monkeypatch.setattr(sandbox, "run", spy)
    listing = ("import os; print([os.path.join(d, f) for d, _, fs in os.walk(chr(46)) for f in fs])")
    commit = toy.candidate({"calc.py": CORRECT_CALC})
    out = verify(conn, toy.repo, cs, commit, allow_unsandboxed=True, baseline=baseline_for(toy))
    assert out.verdict is V.PASS and len(seen) > 4
    for names, blob in seen:
        assert not [n for n in names if "zq.contract" in n or n.endswith(".contract.toml")], names
        assert b"parse_seven" not in blob and b"stdout_equals" not in blob and b"[check.expect]" not in blob
    # ... and from the candidate's own point of view
    files = {"v.contract.toml": cfile(("ls", listing, 'stdout_contains = ["__never__"]'))}
    cs2 = mk(conn, [dc("c1", "ls", "v.contract.toml"), C_HIDDEN_LS], {**files, **{"h.contract.toml": CT_HIDDEN_LS}})
    rep = run_on(toy, cs2, commit)
    text = rep.failure_texts["c1"]
    assert "calc.py" in text and "contract" not in text and "zq" not in text and "7.0" not in text


CT_HIDDEN_LS = cfile(("ls", "import os; print([os.path.join(d, f) for d, _, fs in os.walk(chr(46)) for f in fs])",
                      'stdout_contains = ["__never__"]'))
C_HIDDEN_LS = dc("c2", "ls", "h.contract.toml", vis="hidden", req="R-2")


def test_a_candidate_cannot_see_other_run_directories(conn, hera_home, toy):
    code = ("import os; print(sorted(n for n in os.listdir(chr(46)+chr(46)) if n.startswith(chr(104)+chr(101)+chr(114)+chr(97)+chr(45)+chr(99))))")
    files = {"x.contract.toml": cfile(("a", code, 'stdout_contains = ["__never__"]'), ("b", code, 'stdout_contains = ["__never__"]'))}
    cs = mk(conn, [dc("c1", "a", "x.contract.toml"), dc("c2", "b", "x.contract.toml", req="R-2")], files)
    rep = run_on(toy, cs, toy.base)
    for cid in ("c1", "c2"):
        listing = json.loads(rep.failure_texts[cid].split("stdout (first 2000 characters):\n")[1]
                             .split("\nstderr")[0].replace("'", '"'))
        assert len(listing) <= 1                                       # at most its own directory: earlier ones are gone


BG = ("import os, sys, glob, time\n"
      "if os.fork() == 0:\n"
      "    os.setsid()\n"
      "    if os.fork() == 0:\n"
      "        me = os.path.basename(os.getcwd())\n"
      "        parent = os.path.dirname(os.getcwd())\n"
      "        end = time.time() + 20\n"
      "        while time.time() < end:\n"
      "            for d in glob.glob(os.path.join(parent, 'hera-contract-*')):\n"
      "                if os.path.basename(d) != me:\n"
      "                    try:\n"
      "                        open(os.path.join(d, 'out.txt'), 'w').write('done\\n')\n"
      "                    except OSError:\n"
      "                        pass\n"
      "            time.sleep(0.02)\n"
      "    os._exit(0)\n")


@needs_mech
def test_a_background_process_cannot_tamper_with_a_later_check(conn, hera_home, toy):
    """Needs a real mechanism: with 'none' nothing isolates processes (docs/SANDBOX.md)."""
    calc = CORRECT_CALC + "\n\ndef spawn():\n" + "".join("    " + ln + "\n" for ln in BG.splitlines()) + "    return 1\n"
    files = {"x.contract.toml": cfile(("first", "import calc; calc.spawn()", "exit_code = 0"),
                                      ("later", "import time; time.sleep(1)", 'files_equal = { "out.txt" = "done\\n" }'))}
    cs = mk(conn, [dc("c1", "first", "x.contract.toml"), dc("c2", "later", "x.contract.toml", req="R-2")], files)
    rep = run_on(toy, cs, toy.candidate({"calc.py": calc}))
    assert st(rep) == {"c1": S.PASS, "c2": S.FAIL}


@needs_mech
def test_a_candidate_cannot_read_the_frozen_hidden_contract_file_from_disk(conn, hera_home, toy):
    """Strongest form: the candidate is told the exact path of the frozen hidden file and also
    searches the places a store could live (temp and home). Under a real mechanism it must find
    and read nothing. (Walking '/' is avoided: on CI runners it exceeds any sensible timeout.)"""
    from hera.oracle.checkstore import hidden_dir
    files = {"x.contract.toml": cfile(("steal", "import calc; calc.steal()", 'stdout_contains = ["__never__"]',
                                       "timeout_s = 60\n"))}
    hidden = {"hidden_zq.contract.toml": CONTRACT_FILES["hidden_zq.contract.toml"]}
    cs = mk(conn, [dc("c1", "steal", "x.contract.toml"), dc("c2", "parse_seven", "hidden_zq.contract.toml", vis="hidden")],
            {**files, **hidden})
    target = str(hidden_dir(cs) / "hidden_zq.contract.toml")
    assert open(target).read()                     # the file exists and the test process can read it
    calc = CORRECT_CALC + (
        "\n\ndef steal():\n    import os, tempfile\n"
        f"    target = {target!r}\n"
        "    try:\n        open(target).read()\n        print('READ-OK')\n"
        "    except OSError as e:\n        print('DENIED', type(e).__name__)\n"
        "    found = []\n"
        "    for root in {tempfile.gettempdir(), '/tmp', os.path.expanduser('~')}:\n"
        "        for d, ds, fs in os.walk(root):\n"
        "            found += [f for f in fs if f.endswith('.contract.toml')]\n"
        "    print('FOUND', sorted(found))\n")
    rep = run_on(toy, cs, toy.candidate({"calc.py": calc}))
    text = rep.failure_texts["c1"]
    assert "DENIED" in text and "READ-OK" not in text
    assert "FOUND []" in text and "hidden_zq" not in text.split("FOUND", 1)[1]



# ---- integrity, freeze-time rules --------------------------------------------------------------------------------
def test_tampered_contract_file_in_the_store_is_refused(toy, cs):
    f = cs.root / "hidden" / "hidden_zq.contract.toml"
    os.chmod(f.parent, 0o755)
    os.chmod(f, 0o644)
    f.write_bytes(f.read_bytes().replace(b"7.0", b"8.0"))
    co = toy.repo.verification_checkout(toy.base)
    try:
        with pytest.raises(CheckStoreTampered):
            ContractRunner().run(co, cs, allow_unsandboxed=True)
    finally:
        toy.repo.cleanup_verification(co)


def test_run_requires_a_sandbox_or_opt_in(toy, cs):
    if MECH != "none":
        pytest.skip("a sandbox mechanism exists here")
    co = toy.repo.verification_checkout(toy.base)
    try:
        with pytest.raises(sandbox.SandboxUnavailable):
            ContractRunner().run(co, cs)
    finally:
        toy.repo.cleanup_verification(co)


def test_freeze_parses_contract_files_and_matches_nodes(conn, hera_home):
    cs = mk(conn)
    assert [c.runner for c in cs.checks] == ["contract"] * 4 and len(checks_by_runner(cs, "contract")) == 4
    assert checks_by_runner(cs, "pytest") == ()
    for rel in (c.relpath for c in cs.checks):
        assert (cs.root / ("hidden" if "zq" in rel else "visible") / rel).is_file()
    m = mk(conn, MIXED_CHECKS, MIXED_FILES)
    assert len(checks_by_runner(m, "pytest")) == 4 and len(checks_by_runner(m, "contract")) == 2
    with pytest.raises(ValueError):
        checks_by_runner(m, "other")


def test_runner_is_part_of_the_hash(conn, hera_home):
    a = mk(conn, [dc("c1", "basic_amount", "basic.contract.toml")], {"basic.contract.toml": CT_BASIC})
    b = mk(conn, [dc("c1", "basic_amount", "basic.contract.toml")], {"basic.contract.toml": CT_BASIC})
    assert a.hash == b.hash
    assert mk(conn, CONTRACT_CHECKS, CONTRACT_FILES).hash != mk(conn, MIXED_CHECKS, MIXED_FILES).hash


def test_freeze_rejects_inconsistent_contract_sets(conn, hera_home):
    ok = dc("c1", "basic_amount", "basic.contract.toml")
    files = {"basic.contract.toml": CT_BASIC}
    with pytest.raises(ValueError, match="does not exist"):
        mk(conn, [dc("c1", "nope", "basic.contract.toml")], files)
    two = {"basic.contract.toml": cfile(("a", "pass", "exit_code = 0"), ("b", "pass", "exit_code = 0"))}
    with pytest.raises(ValueError, match="no check refers"):
        mk(conn, [dc("c1", "a", "basic.contract.toml")], two)                      # silent extra check
    with pytest.raises(ValueError, match="same function|same node"):
        mk(conn, [dc("c1", "a", "basic.contract.toml"), dc("c2", "a", "basic.contract.toml", req="R-2")], two)
    mixed_vis = [dc("c1", "a", "basic.contract.toml"), dc("c2", "b", "basic.contract.toml", vis="hidden", req="R-2")]
    with pytest.raises(ValueError, match="mix"):
        mk(conn, mixed_vis, two)                                                  # visibility rule applies
    with pytest.raises(ValueError, match="'bogus'"):
        mk(conn, [ok], {"basic.contract.toml": b"bogus = 1\n" + CT_BASIC})        # unparsable at freeze time
    with pytest.raises(ValueError):
        mk(conn, [ok], {"basic.contract.toml": CT_BASIC, "extra.contract.toml": CT_BASIC})   # unreferenced file
    mk(conn, [ok], files)


def test_draftcheck_runner_rules():
    with pytest.raises(ValueError):
        DraftCheck("c", ("R-1",), "visible", "test_x.py", "n", runner="contract")        # relpath must be *.contract.toml
    with pytest.raises(ValueError):
        DraftCheck("c", ("R-1",), "visible", "x.contract.toml", "bad-node", runner="contract")
    with pytest.raises(ValueError):
        DraftCheck("c", ("R-1",), "visible", "x.contract.toml", "test_x", runner="pytest")
    with pytest.raises(ValueError):
        DraftCheck("c", ("R-1",), "visible", "test_x.py", "test_x", runner="shell")
    assert DraftCheck("c", ("R-1",), "visible", "d/x.contract.toml", "Node_1", runner="contract").runner == "contract"
    assert DraftCheck("c", ("R-1",), "visible", "test_x.py", "test_x").runner == "pytest"


def test_absolute_argv0_is_rejected_at_freeze(conn, hera_home):
    bad = b'[[check]]\nnode = "n"\nargv = ["/bin/echo", "x"]\n[check.expect]\nexit_code = 0\n'
    with pytest.raises(ValueError, match="argv"):
        mk(conn, [dc("c1", "n", "x.contract.toml")], {"x.contract.toml": bad})


# ---- validation on the base commit ----------------------------------------------------------------------------------
def factory(toy):
    def make():
        return toy.repo.verification_checkout(toy.base), toy.repo.read_tree(toy.base, AD.is_protected)
    return make


def test_validation_handles_contract_and_mixed_sets(conn, hera_home, toy):
    rep = validate_on_base(AD, factory(toy), mk(conn), runs=3, allow_unsandboxed=True)
    assert rep.ok and rep.rejected == () and rep.weak == ()
    for cid in ("chk-c-basic", "chk-c-empty", "chk-c-hidden-r1-seven"):
        v = rep.health_of(cid)
        assert v.health is CheckHealth.VALID and v.rule == "fails_before_change" and v.statuses == ("fail",) * 3
    assert rep.health_of("chk-c-add").rule == "regression_passes"
    rep = validate_on_base(AD, factory(toy), mk(conn, MIXED_CHECKS, MIXED_FILES), runs=2, allow_unsandboxed=True)
    assert rep.ok and len(rep.checks) == 6


def test_validation_labels_weak_flaky_and_broken_contract_checks(conn, hera_home, toy):
    files = {"w.contract.toml": cfile(("weak", "print(1)", 'stdout_equals = "1\\n"'),
                                      ("slow", "import time; time.sleep(30)", "exit_code = 0", "timeout_s = 1\n"))}
    checks = [dc("c-weak", "weak", "w.contract.toml"), dc("c-slow", "slow", "w.contract.toml", req="R-2")]
    rep = validate_on_base(AD, factory(toy), mk(conn, checks, files), runs=2, allow_unsandboxed=True)
    assert rep.health_of("c-weak").health is CheckHealth.WEAK and rep.weak == ("c-weak",)
    assert rep.health_of("c-slow").rule == "timeout" and rep.environment_broken and not rep.ok


# ---- verification end to end -------------------------------------------------------------------------------------------
def vrun(conn, toy, cs, commit, **kw):
    kw.setdefault("baseline", baseline_for(toy))
    return verify(conn, toy.repo, cs, commit, allow_unsandboxed=True, **kw)


def test_contract_only_correct_hardcoded_and_base(conn, toy, cs):
    good = vrun(conn, toy, cs, toy.candidate(CANDIDATES["correct"]))
    assert good.verdict is V.PASS and good.in_process_only == () and good.attribution is None
    assert not any("in-process" in n for n in good.notes) and good.receipt.manifest_ids == cs.check_ids
    bad = vrun(conn, toy, cs, toy.candidate(CANDIDATES["hardcoded"]))
    assert bad.verdict is V.OVERFIT and bad.reasons == ("requirement not satisfied in an unseen case: R-1",)
    text = render_feedback(bad)
    blob = text + json.dumps(bad.feedback)
    for secret in ("chk-c-hidden-r1-seven", "zq", "parse_seven", "7.0", "contract.toml", "hidden", "stdout_equals"):
        assert secret not in blob, secret
    assert "R-1" in text
    base = vrun(conn, toy, cs, toy.base)
    assert base.verdict is V.FAIL and "chk-c-basic" in render_feedback(base) and "stdout_equals" in render_feedback(base)
    assert "zq" not in render_feedback(base) and "chk-c-hidden" not in render_feedback(base)


def test_slow_candidate_times_out_on_contract_checks_and_is_never_tamper(conn, toy, hera_home):
    files = {k: v.replace(b"[check.expect]", b"timeout_s = 1\n[check.expect]") for k, v in CONTRACT_FILES.items()}
    cs = mk(conn, CONTRACT_CHECKS, files)
    out = vrun(conn, toy, cs, toy.candidate(CANDIDATES["slow"]))
    assert out.timed_out and out.verdict in (V.FAIL, V.OVERFIT) and out.verdict is not V.TAMPER
    assert out.receipt.sentinels_ok and out.receipt.exit_code_consistent
    by = {r.check_id: r.status for r in out.receipt.results}
    assert by["chk-c-basic"] is S.TIMEOUT and by["chk-c-hidden-r1-seven"] is S.TIMEOUT and by["chk-c-add"] is S.PASS
    assert out.attribution.label.value == "IMPL_WRONG" and out.attribution.confidence == "medium"
    assert "time limit" in render_feedback(out)


def test_a_contract_timeout_does_not_excuse_a_pytest_integrity_failure(conn, toy, hera_home):
    """The TIMEOUT rule is per runner: a timeout in a contract check must not hide pytest tampering."""
    files = {k: (v.replace(b"[check.expect]", b"timeout_s = 1\n[check.expect]") if k.endswith(".contract.toml") else v)
             for k, v in MIXED_FILES.items()}
    cs = mk(conn, MIXED_CHECKS, files)
    calc = ("import os, sys, time\nif 'pytest' in sys.modules:\n    os._exit(3)\ntime.sleep(1000)\n")
    out = vrun(conn, toy, cs, toy.candidate({"calc.py": calc}))
    assert out.timed_out and out.verdict is V.TAMPER                  # contract checks timed out, pytest side did not
    assert not out.receipt.sentinels_ok and out.attribution.rule == "result_integrity"
    by = {r.check_id: r.status for r in out.receipt.results}
    assert by["chk-c-empty"] is S.TIMEOUT and by["chk-c-hidden-r1-seven"] is S.TIMEOUT
