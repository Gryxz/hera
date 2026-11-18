"""End to end on the JavaScript toy project with the node adapter: candidate commit + frozen checks ->
audit, receipt, verdict, attribution, stored evidence and redacted feedback.

Mirrors the pytest end-to-end tests: PASS, OVERFIT, TAMPER, REGRESSION, plus the known in-process
gap and the contract-check route that closes it. All runs pass ``allow_unsandboxed=True`` so they
work everywhere; where a sandbox mechanism exists (CI) it is auto-detected and used anyway."""
import json

import pytest

from hera import cli
from hera.oracle.adapters import get_adapter
from hera.oracle.adapters.node_adapter import NodeAdapter
from hera.oracle.adapters.pytest_adapter import PytestAdapter
from hera.oracle.checkstore import freeze
from hera.oracle.verdict import verdict
from hera.oracle.verify import load_receipt, render_feedback, run_baseline, verify
from hera.sandbox import Limits
from hera.types import Attribution as A, Verdict as V
from toyrepo import approve_toy_summary
from toyrepo_js import (CANDIDATES, CHECK_FILES, CHECKS, MIXED_CHECKS, MIXED_FILES, REQUIREMENTS, VARIANT_CHECKS,
                        VARIANT_FILES, VARIANT_REQUIREMENTS, make_js_toy, needs_node)

pytestmark = needs_node
AD = NodeAdapter()
_BASELINE = []


def baseline_for(toy):
    """The base commit of every toy is identical in content, so the baseline is computed once."""
    if not _BASELINE:
        _BASELINE.append(run_baseline(toy.repo, AD, allow_unsandboxed=True))
    return _BASELINE[0]


@pytest.fixture
def jtoy(hera_home, tmp_path):
    return make_js_toy(tmp_path)


def mk(conn, checks=CHECKS, files=CHECK_FILES, requirements=REQUIREMENTS):
    return freeze(conn, task_id="toyjs", requirements=requirements, checks=tuple(checks), files=dict(files),
                  adapter="node")


@pytest.fixture
def cs(conn, hera_home):
    return mk(conn)


@pytest.fixture
def variant(conn, hera_home):
    return mk(conn, VARIANT_CHECKS, VARIANT_FILES, VARIANT_REQUIREMENTS)


@pytest.fixture
def mixed(conn, hera_home):
    return mk(conn, MIXED_CHECKS, MIXED_FILES)


def run(conn, toy, cs, commit, **kw):
    kw.setdefault("baseline", baseline_for(toy))
    return verify(conn, toy.repo, cs, commit, allow_unsandboxed=True, **kw)      # adapter chosen from cs.adapter


def audit_rules(out):
    return {(f.rule, f.path) for f in out.audit.block}


# ---- the required table -----------------------------------------------------------------------
def test_correct_passes_and_is_persisted_and_recomputable(conn, jtoy, cs):
    commit = jtoy.candidate(CANDIDATES["correct"])
    out = run(conn, jtoy, cs, commit)
    assert out.verdict is V.PASS and out.reasons == () and out.attribution is None
    assert out.audit.block == () and out.receipt.regressions == () and not out.timed_out
    assert not out.environment_suspect and out.advisories == ()
    assert out.receipt.manifest_ids == cs.check_ids and len(out.receipt.results) == 4
    row = conn.execute("SELECT * FROM receipts WHERE id=?", (out.receipt_id,)).fetchone()
    stored = json.loads(row["receipt_json"])
    assert stored["evidence"]["adapter"] == "node" and stored["evidence"]["mechanism"] == out.mechanism
    assert conn.execute("SELECT runner_cmd FROM checksets WHERE hash=?", (cs.hash,)).fetchone()[0] == "node"
    assert conn.execute("SELECT verdict FROM verdicts WHERE receipt_id=?", (out.receipt_id,)).fetchone()[0] == "PASS"
    again = load_receipt(conn, out.receipt_id)
    assert again == out.receipt and verdict(again) == (V.PASS, [])
    assert out.in_process_only == ("R-1", "R-2", "R-3")      # every requirement rests on node checks only


def test_hardcoded_is_overfit_and_feedback_is_redacted(conn, jtoy, cs):
    out = run(conn, jtoy, cs, jtoy.candidate(CANDIDATES["hardcoded"]))
    assert out.verdict is V.OVERFIT and out.attribution.label is A.IMPL_OVERFIT
    assert out.reasons == ("requirement not satisfied in an unseen case: R-1",)
    text = render_feedback(out)
    assert "R-1" in text and len(text) <= 6000
    blob = text + json.dumps(out.feedback) + " ".join(out.reasons)
    for secret in ("chk-hidden-r1-seven", "parse_hidden_zq", "zq_hidden_seven", "zq", "parseAmount('7')",
                   "hidden", "assert", ".mjs", ".js"):
        assert secret not in blob, secret


def test_base_commit_itself_fails(conn, jtoy, cs):
    out = run(conn, jtoy, cs, jtoy.base)
    assert out.verdict is V.FAIL and out.attribution.label is A.IMPL_WRONG and out.attribution.rule == "visible_check_failed"
    assert out.reasons == ("visible check not passing: chk-r1-basic", "visible check not passing: chk-r2-empty")
    text = render_feedback(out)
    assert "chk-r1-basic" in text and "parseAmount" in text and "zq" not in text
    assert not out.environment_suspect and out.audit.block == ()


def test_regression_is_caught_by_the_baseline_comparison_alone(conn, jtoy, variant):
    out = run(conn, jtoy, variant, jtoy.candidate(CANDIDATES["regression"]))
    assert out.verdict is V.REGRESSION and out.attribution.label is A.IMPL_WRONG
    assert out.attribution.rule == "baseline_test_regressed"
    assert out.receipt.regressions == ("test/calc.test.js::add works",)
    assert out.reasons == ("baseline test now failing: test/calc.test.js::add works",)
    assert "test/calc.test.js::add works" in render_feedback(out)
    assert all(r.status.value == "pass" for r in out.receipt.results)


def test_regression_with_the_full_check_set_fails_on_the_typescript_regression_check(conn, jtoy, cs):
    out = run(conn, jtoy, cs, jtoy.candidate(CANDIDATES["regression"]))
    assert out.verdict is V.FAIL and out.reasons == ("visible check not passing: chk-r3-add",)
    assert out.receipt.regressions == ("test/calc.test.js::add works",)      # both views agree


def test_edit_tests_is_tamper_and_with_allowance_is_only_a_fail(conn, jtoy, cs):
    commit = jtoy.candidate(CANDIDATES["edit_tests"])
    out = run(conn, jtoy, cs, commit)
    assert out.verdict is V.TAMPER and out.attribution.label is A.IMPL_TAMPER and out.attribution.rule == "audit_block"
    assert audit_rules(out) == {("protected_path_modified", "test/calc.test.js")}
    assert "protected_path_modified: test/calc.test.js" in render_feedback(out)
    allowed = run(conn, jtoy, cs, commit, allowed_protected=("test/**",))
    assert allowed.verdict is V.FAIL and allowed.attribution.label is A.IMPL_WRONG and allowed.audit.block == ()
    assert allowed.receipt.regressions == ()             # prepare restored the test, so nothing is "broken"


@pytest.mark.parametrize("name,rule,path", [
    ("noderc", "runner_control_changed", ".noderc"),
    ("npmrc_preload", "runner_control_changed", ".npmrc"),
    ("package_node_options", "runner_control_changed", "package.json"),
    ("tamper_checks", "protected_path_added", "_hera_checks/visible/x.test.js"),
    ("tamper_checks", "protected_path_added", "_hera_results/junit.xml")])
def test_runner_tampering_candidates_are_tamper(conn, jtoy, cs, name, rule, path):
    out = run(conn, jtoy, cs, jtoy.candidate({**CANDIDATES[name], **CANDIDATES["correct"]}))
    assert out.verdict is V.TAMPER and out.attribution.label is A.IMPL_TAMPER and out.attribution.confidence == "high"
    assert (rule, path) in audit_rules(out) and not out.environment_suspect
    # the run itself was honest because prepare neutralised the change: the candidate is correct
    assert all(r.status.value == "pass" for r in out.receipt.results)


def test_a_candidate_that_exits_early_is_tamper_through_the_missing_results(conn, jtoy, cs):
    out = run(conn, jtoy, cs, jtoy.candidate(CANDIDATES["exit_zero"]))
    assert out.verdict is V.TAMPER and out.audit.block == () and out.attribution.rule == "result_integrity"
    assert any("executed check IDs differ" in r for r in verdict(out.receipt)[1])
    assert "integrity checks" in render_feedback(out)


def test_slow_candidate_times_out_and_is_never_tamper(conn, jtoy, cs):
    out = run(conn, jtoy, cs, jtoy.candidate(CANDIDATES["slow"]), limits=Limits(wall_s=6))
    assert out.timed_out and out.verdict in (V.FAIL, V.OVERFIT) and out.verdict is not V.TAMPER
    assert out.receipt.sentinels_ok and out.receipt.exit_code_consistent       # a timeout is not tampering
    assert {r.check_id: r.status.value for r in out.receipt.results}["chk-r1-basic"] == "timeout"
    assert out.attribution.label is A.IMPL_WRONG and not out.environment_suspect and "time limit" in render_feedback(out)


def test_no_hidden_runs_only_visible_checks(conn, jtoy, cs):
    out = run(conn, jtoy, cs, jtoy.candidate(CANDIDATES["hardcoded"]), include_hidden=False)
    assert out.verdict is V.PASS and out.receipt.manifest_ids == {"chk-r1-basic", "chk-r2-empty", "chk-r3-add"}


def test_the_adapter_must_match_the_check_set(conn, jtoy, cs):
    with pytest.raises(ValueError):
        verify(conn, jtoy.repo, cs, jtoy.base, adapter=PytestAdapter(), allow_unsandboxed=True)
    assert get_adapter("node").name == "node" and get_adapter("pytest").name == "pytest"
    with pytest.raises(ValueError):
        get_adapter("jest")


# ---- the known gap, and the contract route that closes it ---------------------------------------------
def test_KNOWN_GAP_in_process_patching_yields_false_pass(conn, jtoy, cs):
    """Documents an OPEN tamper route (docs/ORACLE.md, "Node adapter"): candidate code imported by a
    check runs inside the node:test process and turns the assertions into no-ops, so this
    candidate (which must be OVERFIT) gets PASS. This test asserts the WRONG answer on purpose.
    The honest control just below shows the same candidate without the forgery is OVERFIT."""
    out = run(conn, jtoy, cs, jtoy.candidate(CANDIDATES["forge_assert"]))
    assert out.verdict is V.PASS and out.attribution is None
    assert out.audit.block == () and out.receipt.regressions == ()
    assert any("rests only on in-process checks" in n for n in out.notes)         # and the outcome says so


def test_the_same_candidate_without_the_forgery_is_overfit(conn, jtoy, cs):
    out = run(conn, jtoy, cs, jtoy.candidate(CANDIDATES["lazy"]))
    assert out.verdict is V.OVERFIT and out.reasons == ("requirement not satisfied in an unseen case: R-1",)


def test_in_process_forgery_is_caught_by_a_hidden_contract_check(conn, jtoy, mixed):
    """With a hidden contract check for R-1, the forging JavaScript candidate is no longer PASS: the
    node:test side is forged, the contract side (a separate process that prints parseAmount('7')) is not."""
    out = run(conn, jtoy, mixed, jtoy.candidate(CANDIDATES["forge_assert"]))
    by = {r.check_id: r.status.value for r in out.receipt.results}
    assert by["chk-hidden-r1-seven"] == "pass" and by["chk-r1-basic"] == "pass"       # node:test results are forged ...
    assert by["chk-c-hidden-r1-seven"] == "fail"                                      # ... the contract check is not
    assert out.verdict is V.OVERFIT and out.attribution.label is A.IMPL_OVERFIT
    assert out.reasons == ("requirement not satisfied in an unseen case: R-1",)
    text = render_feedback(out)
    assert "R-1" in text and "zq" not in text and "parse_seven" not in text
    assert out.in_process_only == ("R-3",)                                            # R-3 has only node:test evidence


def test_mixed_set_correct_passes(conn, jtoy, mixed):
    out = run(conn, jtoy, mixed, jtoy.candidate(CANDIDATES["correct"]))
    assert out.verdict is V.PASS and len(out.receipt.results) == 6 and out.in_process_only == ("R-3",)


# ---- CLI --------------------------------------------------------------------------------------
SPEC = '''
adapter = "node"

[[requirement]]
id = "R-1"
text = "parse_amount('12.50') returns 12.5"

[[requirement]]
id = "R-2"
text = "parse_amount('') raises ValueError"

[[requirement]]
id = "R-3"
text = "add is unchanged"

[[check]]
id = "chk-r1-basic"
requirements = ["R-1"]
visibility = "visible"
file = "parse_amount.test.js"
node = "basic_amount"

[[check]]
id = "chk-r2-empty"
requirements = ["R-2"]
visibility = "visible"
file = "parse_amount.test.js"
node = "empty_raises"

[[check]]
id = "chk-hidden-r1-seven"
requirements = ["R-1"]
visibility = "hidden"
file = "parse_hidden_zq.test.mjs"
node = "zq_hidden_seven"

[[check]]
id = "chk-r3-add"
requirements = ["R-3"]
visibility = "visible"
file = "calc_unchanged.test.ts"
node = "add_unchanged"
kind = "regression"
'''


@pytest.fixture
def world(hera_home, tmp_path):
    toy = make_js_toy(tmp_path / "t")
    assert cli.main(["init"]) == 0
    specdir = tmp_path / "spec"
    specdir.mkdir()
    for name, data in CHECK_FILES.items():
        (specdir / name).write_bytes(data)
    (specdir / "spec.toml").write_text(SPEC, encoding="utf-8")
    (specdir / "unknown.toml").write_text(SPEC.replace('adapter = "node"', 'adapter = "ruby"'), encoding="utf-8")
    (specdir / "mismatch.toml").write_text(SPEC.replace('node = "zq_hidden_seven"\n', 'node = "zq_hidden_seven"\nrunner = "pytest"\n'),
                                           encoding="utf-8")
    (specdir / "bad.toml").write_text(SPEC.replace('node = "basic_amount"\n', 'node = "basic_amount"\nkind = "regression"\n'),
                                      encoding="utf-8")
    return type("World", (), {"toy": toy, "specdir": specdir, "home": hera_home})()


def freeze_cli(world, task, spec="spec.toml", *extra):
    summary = approve_toy_summary(task, world.specdir)
    return cli.main(["checks", "freeze", "--task", task, "--repo", str(world.toy.source), "--summary", summary,
                     "--spec", str(world.specdir / spec), "--allow-unsandboxed", *extra])


def test_cli_freeze_validates_on_base_and_selects_the_node_adapter(world, capsys):
    from hera import paths
    from hera.oracle import checkstore
    from hera.store import db
    assert freeze_cli(world, "jt1") == 0
    out = capsys.readouterr().out
    assert "chk-r1-basic: valid (missing_feature" in out and "chk-hidden-r1-seven: valid" in out
    assert "chk-r3-add: valid (regression_passes" in out and "not frozen" not in out
    h = out.split("check-set hash: ")[1].split()[0]
    conn = db.connect(paths.db_path())
    try:
        cs_ = checkstore.load(conn, h)
        assert cs_.adapter == "node" and {c.runner for c in cs_.checks} == {"node"}
    finally:
        conn.close()
    assert freeze_cli(world, "jt1") == 0 and h in capsys.readouterr().out           # idempotent


def test_cli_freeze_refuses_a_spec_that_fails_validation_on_the_base_commit(world, capsys):
    from hera import paths
    assert freeze_cli(world, "jt2", "bad.toml") == 3          # a "regression" check that does not pass on base
    out = capsys.readouterr().out
    assert "chk-r1-basic: invalid (regression_fails_on_base" in out and "not frozen" in out
    assert not (paths.checks_dir() / "jt2").exists()                                  # nothing was frozen


def test_cli_freeze_errors_for_an_unknown_adapter_or_a_runner_that_does_not_fit_the_file(world, capsys):
    assert freeze_cli(world, "jt3", "unknown.toml") == 2
    assert "adapter: must be one of" in capsys.readouterr().err
    assert freeze_cli(world, "jt4", "mismatch.toml") == 2                    # runner "pytest" on a .test.mjs file
    assert "error: ValueError" in capsys.readouterr().err


def test_cli_verify_uses_the_adapter_of_the_check_set(world, capsys):
    assert freeze_cli(world, "jt5") == 0
    h = capsys.readouterr().out.split("check-set hash: ")[1].split()[0]
    from hera.workspace import TaskRepo
    repo = TaskRepo.open("jt5")
    wt = repo.new_candidate("c1")
    (wt.path / "calc.js").write_text(CANDIDATES["correct"]["calc.js"])
    good = wt.commit_all("good")
    wt2 = repo.new_candidate("c2")
    (wt2.path / "calc.js").write_text(CANDIDATES["hardcoded"]["calc.js"])
    bad = wt2.commit_all("bad")
    base = ["verify", "--task", "jt5", "--checkset", h, "--allow-unsandboxed"]
    assert cli.main([*base, "--commit", good]) == 0
    assert "verdict: PASS" in capsys.readouterr().out
    assert cli.main([*base, "--commit", bad]) == 3
    out = capsys.readouterr().out
    assert "verdict: OVERFIT" in out and "R-1" in out and "zq" not in out
