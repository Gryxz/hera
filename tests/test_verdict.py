from dataclasses import replace
from hera.oracle.verdict import verdict
from hera.types import CheckHealth as H, CheckResult as C, CheckStatus as S, Receipt, Verdict as V


def receipt(results, **kw):
    return Receipt("t", "c0ffee", "cs", tuple(results), frozenset(c.check_id for c in results), **kw)


OK = [C("v1", ("R-1",), S.PASS), C("h1", ("R-1",), S.PASS, visibility="hidden")]


def test_pass():
    assert verdict(receipt(OK)) == (V.PASS, [])


def test_fail_on_visible_and_error_counts_as_not_passing():
    assert verdict(receipt([C("v1", ("R-1",), S.FAIL)]))[0] is V.FAIL
    assert verdict(receipt([C("v1", ("R-1",), S.TIMEOUT)]))[0] is V.FAIL


def test_overfit_reveals_requirement_ids_only():
    v, why = verdict(receipt([OK[0], C("h-secret-name", ("R-4",), S.FAIL, visibility="hidden")]))
    assert v is V.OVERFIT and why == ["requirement not satisfied in an unseen case: R-4"]
    assert "h-secret-name" not in " ".join(why)


def test_regression_and_unverifiable():
    assert verdict(receipt(OK, regressions=("tests/test_x.py::test_y",)))[0] is V.REGRESSION
    assert verdict(receipt(OK, uncovered_requirements=("R-9",)))[0] is V.UNVERIFIABLE
    assert verdict(receipt([]))[0] is V.UNVERIFIABLE


def test_tamper_conditions():
    assert verdict(receipt(OK, audit_block_hits=("conftest.py changed",)))[0] is V.TAMPER
    assert verdict(receipt(OK, sentinels_ok=False))[0] is V.TAMPER
    assert verdict(receipt(OK, exit_code_consistent=False))[0] is V.TAMPER
    missing = replace(receipt(OK), manifest_ids=frozenset({"v1", "h1", "v2"}))   # a check did not run
    assert verdict(missing)[0] is V.TAMPER


def test_dispute_pauses_and_flaky_checks_are_excluded():
    assert verdict(receipt(OK, dispute_open=True))[0] is V.ORACLE_DISPUTED
    flaky_fail = [OK[0], C("v2", ("R-2",), S.FAIL, health=H.FLAKY)]
    assert verdict(receipt(flaky_fail))[0] is V.PASS
    only_invalid = [C("v1", ("R-1",), S.FAIL, health=H.INVALID)]
    assert verdict(receipt(only_invalid))[0] is V.UNVERIFIABLE


def test_unvalidated_checks_count_exactly_like_valid_ones():
    unval = [C("v1", ("R-1",), S.FAIL, health=H.UNVALIDATED), C("h1", ("R-1",), S.PASS, visibility="hidden")]
    assert verdict(receipt(unval))[0] is V.FAIL
    assert verdict(receipt([replace(c, health=H.UNVALIDATED) for c in OK])) == (V.PASS, [])
