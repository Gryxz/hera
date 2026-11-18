"""A tiny invented JavaScript project plus draft checks and candidate patches for the node adapter.

Same story as tests/toyrepo.py: R-1 and R-2 ask for ``parseAmount``, R-3 says ``add`` is unchanged.
Check files import candidate code relative to their place in the oracle's check directory
(``_hera_checks/<visibility>/``), so a file at the top of the set reaches the project root as
``../../``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hera.oracle.model import DraftCheck
from hera.oracle.nodeenv import find_node, node_info
from hera.workspace import TaskRepo
from toyrepo import REQUIREMENTS, Toy, _git  # noqa: F401  (REQUIREMENTS re-exported)

# ---- Node availability: tests that need it skip with a clear reason, except on CI -------------
_NODE = find_node()
_INFO = node_info(_NODE) if _NODE else None
if _NODE is None:
    NODE_SKIP_REASON = "node is not installed (looked at $HERA_NODE, then PATH); the node adapter needs Node.js 22.6+"
elif not _INFO.usable:
    NODE_SKIP_REASON = "Node.js %s is too old: the node adapter needs 22.6+" % (
        ".".join(map(str, _INFO.version)) if _INFO.version else "?")
elif _INFO.ts_flags is None:
    NODE_SKIP_REASON = "this Node.js cannot strip TypeScript types"
else:
    NODE_SKIP_REASON = ""
NODE_OK = not NODE_SKIP_REASON
needs_node = pytest.mark.skipif(not NODE_OK, reason=NODE_SKIP_REASON or "node is usable")

PACKAGE_JSON = '{\n  "name": "toy-js",\n  "private": true,\n  "type": "module"\n}\n'
CALC = "export function add(a, b) {\n  return a + b;\n}\n"
TEST_CALC = ("import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
             "import { add } from '../calc.js';\n\n"
             "test('add works', () => {\n  assert.equal(add(1, 2), 3);\n});\n")

VISIBLE_NEW = (
    "import test from 'node:test';\n"
    "import assert from 'node:assert/strict';\n"
    "import { parseAmount } from '../../calc.js';\n\n"
    "test('basic_amount', () => {\n  assert.equal(parseAmount('12.50'), 12.5);\n});\n\n"
    "test('empty_raises', () => {\n  assert.throws(() => parseAmount(''), Error);\n});\n"
)
HIDDEN_NEW = (
    "import test from 'node:test';\n"
    "import assert from 'node:assert/strict';\n"
    "import { parseAmount } from '../../calc.js';\n\n"
    "test('zq_hidden_seven', () => {\n  assert.equal(parseAmount('7'), 7);\n});\n"
)
# TypeScript: the annotation is stripped by Node, never type-checked
VISIBLE_REGRESSION_TS = (
    "import test from 'node:test';\n"
    "import assert from 'node:assert/strict';\n"
    "import { add } from '../../calc.js';\n\n"
    "const expected: number = 5;\n\n"
    "test('add_unchanged', () => {\n  assert.equal(add(2, 3), expected);\n});\n"
)

CHECK_FILES = {
    "parse_amount.test.js": VISIBLE_NEW.encode(),
    "parse_hidden_zq.test.mjs": HIDDEN_NEW.encode(),
    "calc_unchanged.test.ts": VISIBLE_REGRESSION_TS.encode(),
}
CHECKS = (
    DraftCheck("chk-r1-basic", ("R-1",), "visible", "parse_amount.test.js", "basic_amount", runner="node"),
    DraftCheck("chk-r2-empty", ("R-2",), "visible", "parse_amount.test.js", "empty_raises", runner="node"),
    DraftCheck("chk-hidden-r1-seven", ("R-1",), "hidden", "parse_hidden_zq.test.mjs", "zq_hidden_seven",
               author_role="hidden", runner="node"),
    DraftCheck("chk-r3-add", ("R-3",), "visible", "calc_unchanged.test.ts", "add_unchanged",
               kind="regression", runner="node"),
)

CORRECT_CALC = CALC + (
    "\nexport function parseAmount(text) {\n  const t = text.trim();\n"
    "  if (!t) throw new Error('empty amount');\n  return Number(t);\n}\n")
HARDCODED_CALC = CALC + (
    "\nexport function parseAmount(text) {\n  if (text === '12.50') return 12.5;\n"
    "  throw new Error('unsupported');\n}\n")
SLOW_CALC = CALC + "\nexport function parseAmount() {}\nawait new Promise((resolve) => setTimeout(resolve, 600000));\n"
REGRESSION_CALC = ("export function add(a, b) {\n  return a - b;\n}\n\nexport function parseAmount(text) {\n"
                   "  const t = text.trim();\n  if (!t) throw new Error('empty amount');\n  return Number(t);\n}\n")
EDITED_TEST_CALC = ("import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
                    "import { add } from '../calc.js';\n\n"
                    "test.skip('add works', () => {\n  assert.equal(add(1, 2), 4);\n});\n")
EXIT_CALC = HARDCODED_CALC + "\nprocess.exit(0);\n"
# Wrong for everything except what the visible checks ask, and it does not throw for other input,
# so only a failing assertion can reveal it.
LAZY_CALC = CALC + (
    "\nexport function parseAmount(text) {\n  if (text === '') throw new Error('empty amount');\n"
    "  return text === '12.50' ? 12.5 : 0;\n}\n")

# In-process forgery (the known gap): the shared assert functions become no-ops, so every check
# that imports this module in its own process passes. The sentinel file does not import it.
FORGE_ASSERT = '''
import assert from 'node:assert';
for (const target of [assert, assert.strict]) {
  for (const key of Object.keys(target)) {
    if (typeof target[key] === 'function' && key !== 'AssertionError') target[key] = () => {};
  }
}
'''

CANDIDATES: dict[str, dict[str, str]] = {
    "correct": {"calc.js": CORRECT_CALC},
    "hardcoded": {"calc.js": HARDCODED_CALC},
    "slow": {"calc.js": SLOW_CALC},
    "regression": {"calc.js": REGRESSION_CALC},
    "edit_tests": {"test/calc.test.js": EDITED_TEST_CALC},
    "noderc": {".noderc": '{"nodeOptions": {"import": ["./evil.mjs"]}}\n'},
    "package_node_options": {"package.json": '{"name": "toy-js", "type": "module", "node_options": "--import ./evil.mjs"}\n'},
    "npmrc_preload": {".npmrc": "node-options=--import ./evil.mjs\n"},
    "tamper_checks": {"_hera_checks/visible/x.test.js": "import test from 'node:test';\ntest('x', () => {});\n",
                      "_hera_results/junit.xml": "<testsuites/>"},
    "exit_zero": {"calc.js": EXIT_CALC},
    "lazy": {"calc.js": LAZY_CALC},
    "forge_assert": {"calc.js": LAZY_CALC + FORGE_ASSERT},
}

# Variant check set without the R-3 regression check, so that only the comparison with the
# project's own pre-existing tests can catch the REGRESSION candidate.
VARIANT_REQUIREMENTS = REQUIREMENTS[:2]
VARIANT_CHECKS = CHECKS[:3]
VARIANT_FILES = {k: v for k, v in CHECK_FILES.items() if k != "calc_unchanged.test.ts"}

# ---- contract checks that run the JavaScript candidate with {node} --------------------------------
_PARSE = "import('./calc.js').then((m) => console.log(m.parseAmount(process.argv[1])))"


def _ct(node: str, arg: str, expect: str) -> bytes:
    return (f'[[check]]\nnode = "{node}"\nargv = ["{{node}}", "-e", "{_PARSE}", "{arg}"]\n'
            f"[check.expect]\n{expect}\n").encode()


CT_SEVEN = _ct("parse_seven", "7", 'exit_code = 0\nstdout_equals = "7\\n"')
CT_EMPTY = _ct("empty_raises", "", 'exit_code = 1\nstderr_contains = ["empty amount"]')
C_SEVEN = DraftCheck("chk-c-hidden-r1-seven", ("R-1",), "hidden", "hidden_zq.contract.toml", "parse_seven",
                     author_role="hidden", runner="contract")
C_EMPTY = DraftCheck("chk-c-empty", ("R-2",), "visible", "empty.contract.toml", "empty_raises", runner="contract")
MIXED_CHECKS = CHECKS + (C_SEVEN, C_EMPTY)
MIXED_FILES = {**CHECK_FILES, "hidden_zq.contract.toml": CT_SEVEN, "empty.contract.toml": CT_EMPTY}


def make_js_toy(tmp_path: Path, task_id: str = "toyjs", with_tests: bool = True) -> Toy:
    src = tmp_path / "toyjs-source"
    src.mkdir(parents=True)
    _git(src, "init", "-q", "-b", "main")
    (src / "package.json").write_text(PACKAGE_JSON)
    (src / "calc.js").write_text(CALC)
    if with_tests:
        (src / "test").mkdir()
        (src / "test" / "calc.test.js").write_text(TEST_CALC)
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "base")
    repo = TaskRepo.create(task_id, src)
    return Toy(src, repo, repo.base_commit)

