"""Diff audit: every rule, on small synthetic commits of the toy repository."""
import os

import pytest

from hera.oracle.adapters.pytest_adapter import PytestAdapter
from hera.oracle.audit import audit
from hera.oracle.model import PrepareReport
from toyrepo import CONFTEST, REQUIREMENTS, CHECKS, CHECK_FILES

AD = PytestAdapter()


def commit_ops(toy, ops: dict):
    """Commit ``ops`` (path -> str | bytes | None for delete) on top of base through a worktree."""
    toy._n += 1
    wt = toy.repo.new_candidate(f"audit{toy._n}")
    for rel, content in ops.items():
        p = wt.path / rel
        if content is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
    return wt.commit_all("candidate")


def run_audit(toy, ops, **kw):
    return audit(toy.repo, toy.base, commit_ops(toy, ops), AD, **kw)


def rules(findings):
    return sorted((f.rule, f.path) for f in findings)


# ---- block rules ---------------------------------------------------------------------
def test_clean_candidate_has_no_findings(toy):
    a = run_audit(toy, {"calc.py": "def add(a, b):\n    return a + b\n\n\ndef f():\n    return 1\n"})
    assert a.block == () and a.flags == ()


def test_protected_path_modified(toy):
    a = run_audit(toy, {"tests/test_calc.py": "def test_add():\n    assert True\n", "conftest.py": CONFTEST + "\n# x\n"})
    assert rules(a.block) == [("protected_path_modified", "conftest.py"),
                              ("protected_path_modified", "tests/test_calc.py")]
    assert a.flags == ()                                      # no flag noise from a plain edit


def test_protected_mode_only_change_is_not_a_content_change(toy):
    toy._n += 1
    wt = toy.repo.new_candidate("modeonly")
    os.chmod(wt.path / "conftest.py", 0o755)
    a = audit(toy.repo, toy.base, wt.commit_all("mode"), AD)
    assert a.block == ()


def test_allowed_protected_exempts_modification_and_deletion(toy):
    ops = {"tests/test_calc.py": "def test_add():\n    assert True\n"}
    a = run_audit(toy, ops, allowed_protected=("tests/**",))
    assert a.block == ()
    a = run_audit(toy, {"tests/test_calc.py": None}, allowed_protected=("tests/**",))
    assert a.block == ()
    a = run_audit(toy, {"conftest.py": "x = 1\n"}, allowed_protected=("tests/**",))
    assert rules(a.block) == [("protected_path_modified", "conftest.py")]    # only the allowed glob


def test_protected_path_deleted(toy):
    a = run_audit(toy, {"tests/test_calc.py": None, "conftest.py": None})
    assert rules(a.block) == [("protected_path_deleted", "conftest.py"),
                              ("protected_path_deleted", "tests/test_calc.py")]


@pytest.mark.parametrize("path", [
    "sub/conftest.py", "sitecustomize.py", "usercustomize.py", "pkg/sitecustomize.py",
    "pytest.ini", "tox.ini", "setup.cfg", "pytest_plugin.py", "_hera_checks/visible/test_x.py",
    "_hera_results/junit.xml", "_HERA_CHECKS/x.py", "Conftest.py"])
def test_protected_path_added_blocks(toy, path):
    a = run_audit(toy, {path: "x = 1\n"})
    assert ("protected_path_added", path) in rules(a.block)


@pytest.mark.parametrize("path", ["pkg/pytest.ini", "pkg/tox.ini", "pkg/pytest_helper.py", "pytest_dir/mod.py", "src/mod.py"])
def test_nested_ini_and_nontop_pytest_files_do_not_block(toy, path):
    a = run_audit(toy, {path: "x = 1\n"})
    assert a.block == ()


def test_added_runner_files_ignore_allowed_protected(toy):
    a = run_audit(toy, {"sub/conftest.py": "x = 1\n"}, allowed_protected=("**",))
    assert rules(a.block) == [("protected_path_added", "sub/conftest.py")]


def test_new_test_file_is_a_flag_not_a_block(toy):
    a = run_audit(toy, {"tests/test_new.py": "def test_n():\n    assert 1\n", "tests/my test file.py": "x = 1\n"})
    assert a.block == ()
    assert rules(a.flags) == [("test_file_added", "tests/my test file.py"), ("test_file_added", "tests/test_new.py")]


# ---- flags ---------------------------------------------------------------------------
@pytest.mark.parametrize("line", ["@pytest.mark.skip", "@pytest.mark.skipif(True, reason='x')", "@pytest.mark.xfail",
                                  "    pytest.skip('no')", "@unittest.skip('x')", "@skip"])
def test_skip_marker_added(toy, line):
    a = run_audit(toy, {"src/mod.py": f"{line}\ndef f():\n    pass\n"})
    assert [f.rule for f in a.flags] == ["skip_marker_added"] and a.flags[0].path == "src/mod.py"
    assert "skip" not in a.flags[0].detail.replace("skip or expect-fail", "")      # no line content echoed


def test_skip_marker_only_counts_added_lines(toy):
    # the base test file has no markers; a marker that is only removed or untouched is not reported
    a = run_audit(toy, {"calc.py": "# pytest.mark.skip mentioned in a comment is still an added line\n"})
    assert [f.rule for f in a.flags] == ["skip_marker_added"]
    a = run_audit(toy, {"calc.py": "def add(a, b):\n    return a + b\n\n\ndef g():\n    return 2\n"})
    assert a.flags == ()


def test_skip_marker_in_non_python_file_is_ignored(toy):
    assert run_audit(toy, {"notes.txt": "@pytest.mark.skip\n"}).flags == ()


def test_out_of_declared_scope(toy):
    ops = {"calc.py": "def add(a, b):\n    return a + b\n# n\n", "docs/readme.md": "x\n", "other/x.py": "y = 1\n"}
    a = run_audit(toy, ops, declared_scope=("calc.py", "docs/**"))
    assert rules([f for f in a.flags if f.rule == "out_of_declared_scope"]) == [("out_of_declared_scope", "other/x.py")]
    assert not [f for f in run_audit(toy, ops, declared_scope=("**",)).flags if f.rule == "out_of_declared_scope"]
    assert not [f for f in run_audit(toy, ops).flags if f.rule == "out_of_declared_scope"]       # None: no scope


def test_debris_files(toy):
    names = ["a.orig", "a.rej", "a.bak", "a.tmp", "run.log", ".a.swp", ".DS_Store", "sub/.DS_Store",
             "scratch1.py", "debug_notes.txt", "tmp_x.json", "Scratch.txt"]
    a = run_audit(toy, {n: "x\n" for n in names})
    found = {f.path for f in a.flags if f.rule == "debris"}
    assert found == set(names)
    assert not run_audit(toy, {"src/normal.py": "x = 1\n", "notes.md": "x\n", "temp.txt": "x\n"}).flags


def test_debris_lines(toy):
    a = run_audit(toy, {"src/a.py": "def f():\n    breakpoint()\n", "src/b.py": "import pdb\n",
                        "src/c.py": "def f():\n    import pdb; pdb.set_trace()\n",
                        "src/d.py": "def f():\n    print('x')\n",
                        "src/__main__.py": "print('hi')\n", "tests/test_p.py": "def test_p():\n    print('x')\n",
                        "src/e.py": "def f():\n    sprint('x')\n    return 1  # print(\n"})
    got = rules(a.flags)
    assert ("debris", "src/a.py") in got and ("debris", "src/b.py") in got and ("debris", "src/c.py") in got
    assert ("debris", "src/d.py") in got
    assert not [g for g in got if g[1] in ("src/__main__.py", "src/e.py", "tests/test_p.py") and g[0] == "debris"]


def test_debugger_in_test_files_is_still_flagged(toy):
    a = run_audit(toy, {"tests/test_calc.py": "def test_add():\n    breakpoint()\n"})
    assert ("debris", "tests/test_calc.py") in rules(a.flags)


@pytest.mark.parametrize("path", ["requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py", "Pipfile",
                                  "Pipfile.lock", "poetry.lock", "uv.lock", "package.json", "web/package-lock.json",
                                  "sub/requirements.txt"])
def test_dependency_manifest_changed(toy, path):
    a = run_audit(toy, {path: "x\n"})
    assert ("dependency_manifest_changed", path) in rules(a.flags)


def test_twin_remaining_counts_only(toy):
    commit = commit_ops(toy, {"a.py": "OLD_NAME = 1\nx = OLD_NAME\ny = 2\n", "sub/b.txt": "no match\n",
                              "tests/test_t.py": "OLD_NAME\n", "bin.dat": b"\0\1OLD_NAME\0",
                              "dir with space/c.py": "OLD_NAME\n"})
    a = audit(toy.repo, toy.base, commit, AD, twin_pattern="OLD_NAME")
    twins = [f for f in a.flags if f.rule == "twin_remaining"]
    assert rules(twins) == [("twin_remaining", "a.py"), ("twin_remaining", "dir with space/c.py")]
    assert [f.detail for f in twins][0] == "2 line(s) still contain the pattern"
    assert all("OLD_NAME" not in f.detail for f in twins)
    # a literal, not a regex
    assert not [f for f in audit(toy.repo, toy.base, commit, AD, twin_pattern="OLD.NAME").flags if f.rule == "twin_remaining"]
    with pytest.raises(ValueError):
        audit(toy.repo, toy.base, commit, AD, twin_pattern="")


# ---- binary files, odd names ---------------------------------------------------------
def test_binary_files_are_skipped_for_line_rules_but_not_for_names(toy):
    payload = b"\0\1\2 @pytest.mark.skip breakpoint() print( \xff\xfe\n"
    a = run_audit(toy, {"src/blob.py": payload, "data.bin": payload, "debug.bin": payload})
    assert rules(a.flags) == [("debris", "debug.bin")]


def test_names_with_spaces_quotes_and_unicode(toy):
    names = ["src/my module.py", 'src/q"uote.py', "src/café ☃.py"]
    a = run_audit(toy, {n: "def f():\n    breakpoint()\n" for n in names})
    assert rules(a.flags) == sorted(("debris", n) for n in names)
    a = run_audit(toy, {"tests/spaced name.py": "@pytest.mark.skip\ndef t(): pass\n"})
    assert ("skip_marker_added", "tests/spaced name.py") in rules(a.flags)
    assert ("test_file_added", "tests/spaced name.py") in rules(a.flags)


def test_diff_added_lines_helper(toy):
    commit = commit_ops(toy, {"calc.py": "def add(a, b):\n    return a + b\nx = 1\ny = '+++ not a header'\n",
                              "n n.txt": "one\r\ntwo\n", "gone.bin": b"\0"})
    got = toy.repo.diff_added_lines(toy.base, commit)
    assert got["calc.py"] == ["x = 1", "y = '+++ not a header'"]
    assert got["n n.txt"] == ["one", "two"] and "gone.bin" not in got
    assert toy.repo.diff_name_status(toy.base, commit) == {"calc.py": "M", "n n.txt": "A", "gone.bin": "A"}


# ---- prepare report folding ---------------------------------------------------------------
def test_prepare_report_findings_not_already_reported_become_blocks(toy):
    commit = commit_ops(toy, {"calc.py": "x = 1\n"})
    rep = PrepareReport(restored=("tests/gone.py", "conftest.py"), deleted=("_hera_checks", "pytest_zz.py"),
                        modified_protected=("tests/other.py",))
    a = audit(toy.repo, toy.base, commit, AD, prepare_report=rep)
    assert rules(a.block) == [("prepare_deleted", "_hera_checks"), ("prepare_deleted", "pytest_zz.py"),
                              ("prepare_restored", "conftest.py"), ("prepare_restored", "tests/gone.py"),
                              ("prepare_restored", "tests/other.py")]
    b = audit(toy.repo, toy.base, commit, AD, prepare_report=rep, allowed_protected=("tests/**", "conftest.py"))
    assert rules(b.block) == [("prepare_deleted", "_hera_checks"), ("prepare_deleted", "pytest_zz.py")]


def test_diff_rules_and_prepare_agree_on_real_tampering(toy, conn, hera_home):
    """The adapter's own view of what it undid must add nothing to what the diff already shows."""
    from hera.oracle.checkstore import freeze
    from toyrepo import CANDIDATES
    cs = freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES))
    base_tree = toy.repo.read_tree(toy.base, AD.is_protected)
    ops = {**CANDIDATES["tamper_conftest"], **CANDIDATES["tamper_sitecustomize"], **CANDIDATES["tamper_checks"],
           **CANDIDATES["skip_all"], "tests/test_calc.py": None, "tests/test_extra.py": "def test_e(): pass\n"}
    commit = commit_ops(toy, ops)
    checkout = toy.repo.verification_checkout(commit)
    prep = AD.prepare(checkout, cs, base_tree=base_tree, include_hidden=True)
    plain = audit(toy.repo, toy.base, commit, AD)
    with_prep = audit(toy.repo, toy.base, commit, AD, prepare_report=prep)
    assert with_prep == plain
    assert {r for r, _p in rules(plain.block)} == {"protected_path_modified", "protected_path_added",
                                                   "protected_path_deleted"}
