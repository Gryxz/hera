"""Validation of a draft check set on the base commit."""
import pytest

from hera.oracle.adapters.pytest_adapter import PytestAdapter
from hera.oracle.checkstore import freeze
from hera.oracle.model import DraftCheck, RunReport
from hera.oracle.validation import ABSENT, shows_missing_feature, validate_on_base
from hera.types import CheckHealth, CheckResult, CheckStatus
from toyrepo import CHECK_FILES, CHECKS, REQUIREMENTS

AD = PytestAdapter()


def factory(toy):
    def make():
        return toy.repo.verification_checkout(toy.base), toy.repo.read_tree(toy.base, AD.is_protected)
    return make


def freeze_with(conn, checks, files):
    return freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=tuple(checks), files=files)


def test_toy_base_validates(conn, toy):
    cs = freeze_with(conn, CHECKS, dict(CHECK_FILES))
    rep = validate_on_base(AD, factory(toy), cs, runs=3, allow_unsandboxed=True)
    assert rep.ok and not rep.environment_broken and rep.rejected == () and rep.weak == ()
    for cid in ("chk-r1-basic", "chk-r2-empty", "chk-hidden-r1-seven"):
        v = rep.health_of(cid)
        assert v.health is CheckHealth.VALID and v.rule == "missing_feature" and v.statuses == ("error",) * 3
    v = rep.health_of("chk-r3-add")
    assert v.health is CheckHealth.VALID and v.rule == "regression_passes"


def test_new_check_that_passes_on_base_is_weak(conn, toy):
    src = "from calc import add\n\n\ndef test_already_true():\n    assert add(1, 1) == 2\n"
    chk = (DraftCheck("chk-weak", ("R-1",), "visible", "test_weak.py", "test_already_true"),)
    rep = validate_on_base(AD, factory(toy), freeze_with(conn, chk, {"test_weak.py": src.encode()}),
                           runs=2, allow_unsandboxed=True)
    v = rep.health_of("chk-weak")
    assert v.health is CheckHealth.WEAK and v.reason == "passes before the change" and v.rule == "passes_before_change"
    assert rep.weak == ("chk-weak",) and rep.rejected == () and rep.ok


def test_new_check_failing_in_body_is_valid_and_regression_failing_is_invalid(conn, toy):
    src = ("from calc import add\n\n\ndef test_new_fails():\n    assert add(1, 1) == 3\n\n\n"
           "def test_reg_fails():\n    assert add(1, 1) == 3\n")
    chk = (DraftCheck("chk-newfail", ("R-1",), "visible", "test_f.py", "test_new_fails"),
           DraftCheck("chk-regfail", ("R-3",), "visible", "test_f.py", "test_reg_fails", kind="regression"))
    rep = validate_on_base(AD, factory(toy), freeze_with(conn, chk, {"test_f.py": src.encode()}),
                           runs=2, allow_unsandboxed=True)
    assert rep.health_of("chk-newfail").rule == "fails_before_change" and rep.health_of("chk-newfail").health is CheckHealth.VALID
    assert rep.health_of("chk-regfail").health is CheckHealth.INVALID
    assert rep.health_of("chk-regfail").rule == "regression_fails_on_base"
    assert rep.rejected == ("chk-regfail",) and not rep.ok


def test_syntax_error_check_is_invalid(conn, toy):
    chk = (DraftCheck("chk-syntax", ("R-1",), "visible", "test_s.py", "test_a"),
           DraftCheck("chk-r3-add", ("R-3",), "visible", "test_calc_unchanged.py", "test_add_unchanged", kind="regression"))
    files = {"test_s.py": b"def test_a(:\n", "test_calc_unchanged.py": CHECK_FILES["test_calc_unchanged.py"]}
    rep = validate_on_base(AD, factory(toy), freeze_with(conn, chk, files), runs=2, allow_unsandboxed=True)
    v = rep.health_of("chk-syntax")
    assert v.health is CheckHealth.INVALID and v.rule == "error_unrelated"
    assert rep.health_of("chk-r3-add").health is CheckHealth.VALID
    assert rep.rejected == ("chk-syntax",) and not rep.ok and not rep.environment_broken


def test_unrelated_error_on_a_new_check_is_invalid(conn, toy):
    src = "import pytest\n\n\n@pytest.fixture\ndef broken():\n    raise RuntimeError('boom')\n\n\ndef test_a(broken):\n    pass\n"
    chk = (DraftCheck("chk-fix", ("R-1",), "visible", "test_fix.py", "test_a"),)
    rep = validate_on_base(AD, factory(toy), freeze_with(conn, chk, {"test_fix.py": src.encode()}),
                           runs=2, allow_unsandboxed=True)
    assert rep.health_of("chk-fix").health is CheckHealth.INVALID


def test_flaky_check_is_rejected_with_real_runs(conn, toy):
    """Each validation run gets a fresh checkout, so flakiness has to come from something that
    differs between checkouts. The factory plants a marker whose value alternates per run; the
    check reads it from its working directory, which works inside a real sandbox too."""
    base = factory(toy)
    calls = {"n": 0}

    def alternating():
        out = base()
        checkout = out[0] if isinstance(out, tuple) else out
        (checkout / "run_marker.txt").write_text(str(calls["n"]))
        calls["n"] += 1
        return out

    src = "def test_flaky():\n    assert int(open('run_marker.txt').read()) % 2 == 1\n"
    chk = (DraftCheck("chk-flaky", ("R-1",), "visible", "test_flaky.py", "test_flaky"),)
    rep = validate_on_base(AD, alternating, freeze_with(conn, chk, {"test_flaky.py": src.encode()}),
                           runs=3, allow_unsandboxed=True)
    v = rep.health_of("chk-flaky")
    assert v.health is CheckHealth.FLAKY and v.rule == "status_differs" and rep.rejected == ("chk-flaky",) and not rep.ok


class FakeAdapter:
    """Scripted adapter: one status list per run."""
    def __init__(self, script, sentinels_ok=True):
        self.script, self.sentinels_ok, self.n = script, sentinels_ok, 0

    def prepare(self, checkout, checkset, *, base_tree, include_hidden):
        return None

    def run(self, checkout, checkset, *, include_hidden=True, limits=None, allow_unsandboxed=False):
        run = self.script[self.n]
        self.n += 1
        results = tuple(CheckResult(cid, ("R-1",), st) for cid, st in run.items() if st != ABSENT)
        return RunReport(results=results, executed_ids=frozenset(r.check_id for r in results),
                         sentinels_ok=self.sentinels_ok, exit_code_consistent=True, results_parsed=True,
                         failure_texts={})


def test_flaky_with_injected_adapter(conn, toy):
    cs = freeze_with(conn, CHECKS, dict(CHECK_FILES))
    ids = [c.check_id for c in CHECKS]
    def all_(st):
        return {i: st for i in ids}
    script = [{**all_(CheckStatus.FAIL), "chk-r3-add": CheckStatus.PASS},
              {**all_(CheckStatus.FAIL), "chk-r1-basic": CheckStatus.PASS, "chk-r3-add": CheckStatus.PASS},
              {**all_(CheckStatus.FAIL), "chk-r3-add": CheckStatus.PASS}]
    rep = validate_on_base(FakeAdapter(script), lambda: (None, {}), cs, runs=3)
    assert rep.health_of("chk-r1-basic").health is CheckHealth.FLAKY
    assert rep.health_of("chk-r2-empty").health is CheckHealth.VALID
    assert rep.rejected == ("chk-r1-basic",) and not rep.ok


def test_sentinel_failure_or_missing_check_means_environment_broken(conn, toy):
    cs = freeze_with(conn, CHECKS, dict(CHECK_FILES))
    ids = [c.check_id for c in CHECKS]
    good = {i: CheckStatus.FAIL for i in ids} | {"chk-r3-add": CheckStatus.PASS}
    assert validate_on_base(FakeAdapter([good, good], sentinels_ok=False), lambda: (None, {}), cs, runs=2).environment_broken
    missing = dict(good)
    missing["chk-r2-empty"] = ABSENT
    rep = validate_on_base(FakeAdapter([missing, missing]), lambda: (None, {}), cs, runs=2)
    assert rep.environment_broken and rep.health_of("chk-r2-empty").rule == "not_executed" and not rep.ok


def test_runs_must_allow_flakiness_detection(conn, toy):
    with pytest.raises(ValueError):
        validate_on_base(FakeAdapter([]), lambda: (None, {}), freeze_with(conn, CHECKS, dict(CHECK_FILES)), runs=1)


@pytest.mark.parametrize("text,expected", [
    ("E   ModuleNotFoundError: No module named 'calc'", True),
    ("E   ImportError: cannot import name 'parse_amount' from 'calc'", True),
    ("E   AttributeError: module 'calc' has no attribute 'x'", True),
    ("E   NameError: name 'x' is not defined", True),
    ("E   SyntaxError: invalid syntax", False),
    ("ImportError while importing test module\nE   File x\nE   SyntaxError: bad", False),
    ("E   ValueError: nope", False),
    ("E   ModuleNotFoundError: x\nE   ValueError: later", False),
    ("", False),
])
def test_missing_feature_detection(text, expected):
    assert shows_missing_feature(text) is expected
