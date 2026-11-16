"""A tiny invented project plus draft checks and candidate patches, for oracle tests."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from hera.oracle.model import DraftCheck, Requirement
from hera.workspace import TaskRepo, _git_env

CALC = "def add(a, b):\n    return a + b\n"
TEST_CALC = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
CONFTEST = "import pytest\n\n\n@pytest.fixture\ndef one():\n    return 1\n"

REQUIREMENTS = (
    Requirement("R-1", "parse_amount('12.50') returns 12.5"),
    Requirement("R-2", "parse_amount('') raises ValueError"),
    Requirement("R-3", "add is unchanged"),
)

VISIBLE_NEW = (
    "import pytest\n"
    "from calc import parse_amount\n\n\n"
    "def test_basic_amount():\n    assert parse_amount('12.50') == 12.5\n\n\n"
    "def test_empty_raises():\n    with pytest.raises(ValueError):\n        parse_amount('')\n"
)
HIDDEN_NEW = (
    "from calc import parse_amount\n\n\n"
    "def test_zq_hidden_seven():\n    assert parse_amount('7') == 7.0\n"
)
VISIBLE_REGRESSION = "from calc import add\n\n\ndef test_add_unchanged():\n    assert add(2, 3) == 5\n"

CHECK_FILES = {
    "test_parse_amount.py": VISIBLE_NEW.encode(),
    "test_parse_hidden_zq.py": HIDDEN_NEW.encode(),
    "test_calc_unchanged.py": VISIBLE_REGRESSION.encode(),
}
CHECKS = (
    DraftCheck("chk-r1-basic", ("R-1",), "visible", "test_parse_amount.py", "test_basic_amount"),
    DraftCheck("chk-r2-empty", ("R-2",), "visible", "test_parse_amount.py", "test_empty_raises"),
    DraftCheck("chk-hidden-r1-seven", ("R-1",), "hidden", "test_parse_hidden_zq.py",
               "test_zq_hidden_seven", author_role="hidden"),
    DraftCheck("chk-r3-add", ("R-3",), "visible", "test_calc_unchanged.py", "test_add_unchanged",
               kind="regression"),
)

CORRECT_CALC = CALC + (
    "\n\ndef parse_amount(text):\n    text = text.strip()\n    if not text:\n"
    "        raise ValueError('empty amount')\n    return float(text)\n")
HARDCODED_CALC = CALC + (
    "\n\ndef parse_amount(text):\n    if text == '12.50':\n        return 12.5\n"
    "    raise ValueError('unsupported')\n")
SLOW_CALC = CALC + "\n\ndef parse_amount(text):\n    import time\n    time.sleep(10_000)\n"

REGRESSION_CALC = ("def add(a, b):\n    return a - b\n\n\ndef parse_amount(text):\n    text = text.strip()\n"
                   "    if not text:\n        raise ValueError('empty amount')\n    return float(text)\n")
EDITED_TEST_CALC = ("import pytest\nfrom calc import add\n\n\n@pytest.mark.skip(reason='later')\n"
                    "def test_add():\n    assert add(1, 2) == 4\n")

CONFTEST_TAMPER = CONFTEST + (
    "\n\n@pytest.hookimpl(hookwrapper=True)\ndef pytest_runtest_makereport(item, call):\n"
    "    outcome = yield\n    rep = outcome.get_result()\n    rep.outcome = 'passed'\n    rep.longrepr = None\n\n\n"
    "def pytest_collection_modifyitems(config, items):\n    items[:] = []\n")
FORGED_JUNIT = ('<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="1" failures="0">'
                '<testcase classname="x" name="test_ok"/></testsuite></testsuites>')

CANDIDATES: dict[str, dict[str, str]] = {
    "correct": {"calc.py": CORRECT_CALC},
    "hardcoded": {"calc.py": HARDCODED_CALC},
    "tamper_conftest": {"conftest.py": CONFTEST_TAMPER},
    "tamper_sitecustomize": {"sitecustomize.py": "import sys\nsys.exit(0)\n"},
    "tamper_checks": {"_hera_checks/visible/test_x.py": "def test_x():\n    assert True\n",
                      "_hera_results/junit.xml": FORGED_JUNIT},
    "skip_all": {"pytest.ini": "[pytest]\naddopts = -k nothing_matches\n"},
    "slow": {"calc.py": SLOW_CALC},
    "regression": {"calc.py": REGRESSION_CALC},
    "edit_tests": {"tests/test_calc.py": EDITED_TEST_CALC},
}

# Variant check set without the R-3 regression check (and without requirement R-3), so that only
# the comparison with the project's own pre-existing tests can catch the REGRESSION candidate.
VARIANT_REQUIREMENTS = REQUIREMENTS[:2]
VARIANT_CHECKS = CHECKS[:3]
VARIANT_FILES = {k: v for k, v in CHECK_FILES.items() if k != "test_calc_unchanged.py"}

# In-process forgery (the known gap): rewrites every report inside the pytest process.
FORGE_PATCH = '''
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


def _git(cwd, *args):
    cmd = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "user.name=T",
           "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args]
    r = subprocess.run(cmd, cwd=cwd, env=_git_env(), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@dataclass
class Toy:
    source: Path
    repo: TaskRepo
    base: str
    _n: int = 0

    def candidate(self, files: dict[str, str]) -> str:
        """Commit ``files`` (path -> text) on top of the base commit through a Worktree."""
        self._n += 1
        wt = self.repo.new_candidate(f"cand{self._n}")
        for rel, text in files.items():
            p = wt.path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return wt.commit_all("candidate")


def make_toy(tmp_path: Path, task_id: str = "toy", with_tests: bool = True) -> Toy:
    src = tmp_path / "toy-source"
    src.mkdir(parents=True)
    _git(src, "init", "-q", "-b", "main")
    (src / "calc.py").write_text(CALC)
    (src / "conftest.py").write_text(CONFTEST)
    if with_tests:
        (src / "tests").mkdir()
        (src / "tests" / "test_calc.py").write_text(TEST_CALC)
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "base")
    repo = TaskRepo.create(task_id, src)
    return Toy(src, repo, repo.base_commit)
