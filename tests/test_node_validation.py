"""Validation of a draft node check set on the base commit (fail-before, 3x stability)."""
import pytest

from hera.oracle.adapters.node_adapter import NodeAdapter
from hera.oracle.checkstore import freeze
from hera.oracle.model import DraftCheck
from hera.oracle.validation import shows_missing_feature, validate_on_base
from hera.types import CheckHealth
from toyrepo_js import CHECK_FILES, CHECKS, REQUIREMENTS, make_js_toy, needs_node

pytestmark = needs_node
AD = NodeAdapter()


@pytest.fixture
def jtoy(hera_home, tmp_path):
    return make_js_toy(tmp_path)


def factory(toy):
    def make():
        return toy.repo.verification_checkout(toy.base), toy.repo.read_tree(toy.base, AD.is_protected)
    return make


def freeze_with(conn, checks, files):
    return freeze(conn, task_id="toyjs", requirements=REQUIREMENTS, checks=tuple(checks), files=files, adapter="node")


def check(cid, rel, node, kind="new"):
    return DraftCheck(cid, ("R-1",), "visible", rel, node, kind=kind, runner="node")


def test_toy_base_validates_three_times(conn, jtoy):
    cs = freeze_with(conn, CHECKS, dict(CHECK_FILES))
    rep = validate_on_base(AD, factory(jtoy), cs, runs=3, allow_unsandboxed=True)
    assert rep.ok and not rep.environment_broken and rep.rejected == () and rep.weak == ()
    for cid in ("chk-r1-basic", "chk-r2-empty", "chk-hidden-r1-seven"):
        v = rep.health_of(cid)
        assert v.health is CheckHealth.VALID and v.rule == "missing_feature" and v.statuses == ("error",) * 3
    v = rep.health_of("chk-r3-add")
    assert v.health is CheckHealth.VALID and v.rule == "regression_passes" and v.statuses == ("pass",) * 3


def test_new_check_that_passes_on_base_is_weak(conn, jtoy):
    src = ("import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
           "import * as calc from '../../calc.js';\n\n"
           "test('already_true', () => { assert.equal(calc.add(1, 1), 2); });\n")
    cs = freeze_with(conn, (check("chk-weak", "weak.test.js", "already_true"),), {"weak.test.js": src.encode()})
    rep = validate_on_base(AD, factory(jtoy), cs, runs=2, allow_unsandboxed=True)
    v = rep.health_of("chk-weak")
    assert v.health is CheckHealth.WEAK and v.rule == "passes_before_change" and rep.weak == ("chk-weak",) and rep.ok


def test_new_check_failing_in_its_body_is_valid_and_a_failing_regression_is_invalid(conn, jtoy):
    src = ("import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
           "import { add } from '../../calc.js';\n\n"
           "test('new_fails', () => { assert.equal(add(1, 1), 3); });\n"
           "test('reg_fails', () => { assert.equal(add(1, 1), 3); });\n")
    cs = freeze_with(conn, (check("chk-newfail", "f.test.mjs", "new_fails"),
                            check("chk-regfail", "f.test.mjs", "reg_fails", kind="regression")),
                     {"f.test.mjs": src.encode()})
    rep = validate_on_base(AD, factory(jtoy), cs, runs=2, allow_unsandboxed=True)
    assert rep.health_of("chk-newfail").rule == "fails_before_change" and rep.health_of("chk-newfail").health is CheckHealth.VALID
    assert rep.health_of("chk-regfail").rule == "regression_fails_on_base"
    assert rep.rejected == ("chk-regfail",) and not rep.ok


def test_syntax_error_in_a_check_file_is_invalid_and_a_missing_module_is_valid(conn, jtoy):
    good = ("import test from 'node:test';\nimport { add } from '../../calc.js';\n"
            "test('add_unchanged', () => { if (add(2, 3) !== 5) throw new Error('no'); });\n")
    missing = "import x from '../../not_there.js';\nimport test from 'node:test';\ntest('uses_x', () => { x(); });\n"
    cs = freeze_with(conn, (check("chk-syntax", "s.test.js", "a"), check("chk-missing", "m.test.mjs", "uses_x"),
                            check("chk-ok", "g.test.js", "add_unchanged", kind="regression")),
                     {"s.test.js": b"test('a', (\n", "m.test.mjs": missing.encode(), "g.test.js": good.encode()})
    rep = validate_on_base(AD, factory(jtoy), cs, runs=2, allow_unsandboxed=True)
    assert rep.health_of("chk-syntax").health is CheckHealth.INVALID and rep.health_of("chk-syntax").rule == "error_unrelated"
    assert rep.health_of("chk-missing").health is CheckHealth.VALID and rep.health_of("chk-missing").rule == "missing_feature"
    assert rep.health_of("chk-ok").health is CheckHealth.VALID and not rep.environment_broken


@pytest.mark.parametrize("text,expected", [
    ("SyntaxError: The requested module '../../calc.js' does not provide an export named 'parseAmount'", True),
    ("Error [ERR_MODULE_NOT_FOUND]: Cannot find module '/x/nope.js' imported from /x/a.test.js", True),
    ("Error: Cannot find module './nope'\ncode: 'MODULE_NOT_FOUND'", True),
    ("ReferenceError: parseAmount is not defined", True),
    ("SyntaxError: Unexpected token ')'", False),
    ("Expected values to be strictly equal:\n\n1 !== 2", False),
    ("E   ImportError: cannot import name 'x'", True),                       # pytest texts keep their rule
    ("E   AssertionError: assert 1 == 2\nE   SyntaxError", False),
    ("", False)])
def test_shows_missing_feature(text, expected):
    assert shows_missing_feature(text) is expected
