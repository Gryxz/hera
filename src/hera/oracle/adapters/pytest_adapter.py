"""pytest adapter: prepare an oracle-owned run, execute it in the sandbox, parse JUnit XML.

What it guarantees and what it does not (see docs/ORACLE.md): it restores every protected path
to its base content and deletes protected files the candidate added, so candidate-chosen
config, conftest, sitecustomize and test files cannot steer the run. It does NOT isolate the
runner from the candidate's own modules: checks import candidate code into the same process
as pytest.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import xml.etree.ElementTree as ET
from fnmatch import fnmatchcase
from pathlib import Path

from ... import sandbox
from ...types import CheckResult, CheckStatus
from ...workspace import _rm_tree
from ..checkstore import verify_integrity
from ..model import CheckSet, PrepareReport, ProjectRunReport, RunReport, module_name

CHECKS_DIR = "_hera_checks"
RESULTS_DIR = "_hera_results"
JUNIT_REL = f"{RESULTS_DIR}/junit.xml"
PROJECT_DIR = "_hera_project"
PROJECT_JUNIT_REL = f"{PROJECT_DIR}/junit.xml"
SENTINEL_MODULE = f"{CHECKS_DIR}._sentinel.test_hera_sentinel"
MUST_PASS, MUST_FAIL = "test_hera_must_pass", "test_hera_must_fail"
MAX_RESULTS_BYTES = 5 * 1024 * 1024
FAILURE_TEXT_CAP = 20_000
FEEDBACK_TEXT_CAP = 2_000

_SENTINEL_SRC = ("def test_hera_must_pass():\n    assert True\n\n\n"
                 "def test_hera_must_fail():\n    assert False\n")
_INI = ("[pytest]\n"
        "pythonpath = .. ../src\n"
        "addopts = --import-mode=importlib --continue-on-collection-errors --tb=short\n"
        "python_files = test_*.py\n"
        "norecursedirs = __pycache__\n")

_PROJECT_INI = ("[pytest]\n"
                "pythonpath = .. ../src\n"
                "addopts = --import-mode=importlib --continue-on-collection-errors --tb=short\n"
                "norecursedirs = __pycache__\n")

PROTECTED_GLOBS = (
    "conftest.py", "**/conftest.py", "pytest.ini", "tox.ini", "setup.cfg",
    "tests/**", "test/**", "**/test_*.py", "**/*_test.py", ".github/**",
    "sitecustomize.py", "usercustomize.py", "**/sitecustomize.py", "**/usercustomize.py",
    "pytest_*.py",
)

_SUMMARY = re.compile(r"^(?:=+ )?((?:\d+ [a-z]+(?:, )?)+)\s+in [0-9.]+s(?: \(\d+:\d+:\d+\))?(?: =+)?$")
_COUNT = re.compile(r"(\d+) ([a-z]+)")


# ---- glob matching -------------------------------------------------------------------
def _match_parts(path: list[str], pat: list[str]) -> bool:
    if not pat:
        return not path
    head = pat[0]
    if head == "**":
        rest = pat[1:]
        if not rest:                       # trailing "**": one or more further segments
            return len(path) >= 1
        return any(_match_parts(path[i:], rest) for i in range(len(path) + 1))
    return bool(path) and fnmatchcase(path[0], head) and _match_parts(path[1:], pat[1:])


def match_glob(path: str, pattern: str) -> bool:
    """``**`` as a whole segment matches zero or more directories (one or more when it is the
    last segment); ``*`` and ``?`` never cross ``/``. Case-insensitive, because the verifier may
    run on a case-insensitive file system."""
    return _match_parts(path.casefold().split("/"), pattern.casefold().split("/"))


def _norm(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


class PytestAdapter:
    name = "pytest"

    def __init__(self) -> None:
        self._prepared: dict[str, tuple[str, bool]] = {}

    # ---- detection -------------------------------------------------------------------
    def detect(self, repo_root) -> float:
        root = Path(repo_root)
        if not root.is_dir():
            return 0.0

        def text(name):
            try:
                return (root / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
        if (root / "pytest.ini").is_file() or "[tool.pytest" in text("pyproject.toml") \
                or "[pytest]" in text("tox.ini") or "[tool:pytest]" in text("setup.cfg"):
            return 1.0
        skip = {".git", "node_modules", ".venv", "venv", "__pycache__", "_hera_checks", "_hera_results"}
        any_py, seen = False, 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip]
            rel = os.path.relpath(dirpath, root)
            in_tests = rel.split(os.sep)[0] in ("tests", "test")
            for f in filenames:
                seen += 1
                if f.endswith(".py"):
                    any_py = True
                    if in_tests and f.startswith("test_"):
                        return 1.0
            if seen > 20000:
                break
        return 0.3 if any_py else 0.0

    # ---- protected paths -------------------------------------------------------------
    def protected_globs(self) -> tuple[str, ...]:
        return PROTECTED_GLOBS

    def is_protected(self, path: str) -> bool:
        p = _norm(path)
        return bool(p) and any(match_glob(p, g) for g in PROTECTED_GLOBS)

    # ---- prepare ---------------------------------------------------------------------
    def prepare(self, checkout, checkset: CheckSet, *, base_tree: dict[str, bytes],
                include_hidden: bool) -> PrepareReport:
        checkout = Path(checkout)
        if not checkout.is_dir() or (checkout / ".git").exists():
            raise ValueError("checkout must be a directory without .git")
        verify_integrity(checkset)
        for rel in base_tree:
            parts = rel.split("/")
            if rel.startswith("/") or any(p in ("", ".", "..") for p in parts) or not self.is_protected(rel):
                raise ValueError("base_tree: contains a path that is not a protected relative path")

        deleted: list[str] = []
        for name in (CHECKS_DIR, RESULTS_DIR):          # 0. oracle-owned names
            p = checkout / name
            if os.path.lexists(p):
                _remove(p)
                deleted.append(name)

        # 1a. delete protected files the candidate added
        for dirpath, dirnames, filenames in os.walk(checkout, followlinks=False):
            for n in (*dirnames, *filenames):
                full = Path(dirpath) / n
                is_real_dir = full.is_dir() and not full.is_symlink()
                if is_real_dir:
                    continue
                rel = full.relative_to(checkout).as_posix()
                if self.is_protected(rel) and rel not in base_tree:
                    os.unlink(full)
                    deleted.append(rel)
        # 1b. restore protected base paths
        restored: list[str] = []
        modified: list[str] = []
        for rel, data in sorted(base_tree.items()):
            outcome = _restore(checkout, rel, data)
            if outcome is not None:
                restored.append(rel)
                if outcome == "modified":
                    modified.append(rel)

        # 2. frozen checks
        vis = {c.relpath: c.visibility for c in checkset.checks}
        for rel in sorted(checkset.files):
            if vis[rel] == "hidden" and not include_hidden:
                continue
            data = (checkset.root / vis[rel] / rel).read_bytes()
            dest = checkout / CHECKS_DIR / vis[rel] / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        # 3. sentinels, 4. oracle-owned ini
        sdir = checkout / CHECKS_DIR / "_sentinel"
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "test_hera_sentinel.py").write_text(_SENTINEL_SRC, encoding="utf-8")
        (checkout / CHECKS_DIR / "pytest.ini").write_text(_INI, encoding="utf-8")

        self._prepared[os.path.realpath(checkout)] = (checkset.hash, include_hidden)
        return PrepareReport(tuple(sorted(restored)), tuple(sorted(deleted)), tuple(sorted(modified)))

    # ---- command ---------------------------------------------------------------------
    def command(self, checkout) -> list[str]:
        # -P: do not put the working directory first on sys.path, so candidate files cannot
        # shadow pytest or the standard library at startup. Project code is added by the
        # oracle ini (pythonpath = .. ../src, relative to the ini file), which covers flat and src/ layouts.
        return [sys.executable, "-P", "-m", "pytest", "-c", f"{CHECKS_DIR}/pytest.ini",
                "--rootdir", ".", "--confcutdir", CHECKS_DIR, "-p", "no:cacheprovider",
                "-o", "junit_family=xunit2", f"--junitxml={JUNIT_REL}", "-q", CHECKS_DIR]

    # ---- run -------------------------------------------------------------------------
    def run(self, checkout, checkset: CheckSet, *, include_hidden: bool = True, limits=None,
            allow_unsandboxed: bool = False) -> RunReport:
        checkout = Path(checkout)
        state = self._prepared.get(os.path.realpath(checkout))
        if state != (checkset.hash, include_hidden):
            raise RuntimeError("run: this checkout was not prepared by this adapter for this check set")
        results_dir = checkout / RESULTS_DIR
        if os.path.lexists(results_dir):
            _remove(results_dir)
        kwargs = {"limits": limits} if limits is not None else {}
        rr = sandbox.run(self.command(checkout), cwd=checkout,
                         env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONSAFEPATH": "1"},
                         allow_unsandboxed=allow_unsandboxed, **kwargs)
        return _parse(checkout, checkset, include_hidden, rr)

    # ---- the project's own pre-existing tests ----------------------------------------
    def project_test_paths(self, base_tree: dict[str, bytes]) -> tuple[str, ...]:
        """Test files of the project at base: ``*.py`` files of ``base_tree`` named ``test_*.py``
        or ``*_test.py``. These are the oracle's choice of what 'the existing tests' means."""
        out = []
        for rel in sorted(base_tree):
            name = rel.rsplit("/", 1)[-1]
            if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
                out.append(rel)
        return tuple(out)

    def project_command(self, test_paths) -> list[str]:
        return [sys.executable, "-P", "-m", "pytest", "-c", f"{PROJECT_DIR}/pytest.ini",
                "--rootdir", ".", "--confcutdir", ".", "-p", "no:cacheprovider",
                "-o", "junit_family=xunit2", f"--junitxml={PROJECT_JUNIT_REL}", "-q", *test_paths]

    def run_project_tests(self, checkout, test_paths, *, limits=None,
                          allow_unsandboxed: bool = False) -> ProjectRunReport:
        """Run the project's pre-existing tests in ``checkout`` under the sandbox.

        Unlike the check run, the project's own ``conftest.py`` files are loaded (they hold its
        fixtures; ``prepare`` has restored them to base content, or the checkout is a base
        checkout), with ``--confcutdir`` at the checkout root. Nothing from ``_hera_checks`` is
        used: the oracle directories (frozen checks, results) are removed first and the ini is
        oracle-owned, so the project's own configuration is never read."""
        checkout = Path(checkout)
        if not checkout.is_dir() or (checkout / ".git").exists():
            raise ValueError("checkout must be a directory without .git")
        test_paths = tuple(test_paths)
        for rel in test_paths:
            parts = rel.split("/")
            if not rel or rel.startswith(("/", "-")) or any(p in ("", ".", "..") for p in parts):
                raise ValueError("test_paths: must be relative POSIX paths")
        for name in (CHECKS_DIR, RESULTS_DIR, PROJECT_DIR):
            p = checkout / name
            if os.path.lexists(p):
                _remove(p)
        pdir = checkout / PROJECT_DIR
        pdir.mkdir()
        (pdir / "pytest.ini").write_text(_PROJECT_INI, encoding="utf-8")
        kwargs = {"limits": limits} if limits is not None else {}
        rr = sandbox.run(self.project_command(test_paths), cwd=checkout,
                         env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONSAFEPATH": "1"},
                         allow_unsandboxed=allow_unsandboxed, **kwargs)
        return _parse_project(checkout, rr)

    # ---- feedback --------------------------------------------------------------------
    def feedback_for_implementer(self, report: RunReport, checkset: CheckSet) -> list[dict]:
        """What the implementer may be told about a failed verification.

        Visible failures: check id, requirement ids and the first 2,000 characters of failure
        text. Hidden failures: requirement ids ONLY (no check id, file name or text)."""
        by_id = {c.check_id: c for c in checkset.checks}
        visible: list[dict] = []
        hidden_reqs: set[tuple[str, ...]] = set()
        for r in sorted(report.results, key=lambda r: r.check_id):
            if r.status is CheckStatus.PASS or r.check_id not in by_id:
                continue
            c = by_id[r.check_id]
            if c.visibility == "visible":
                visible.append({"check_id": c.check_id, "requirement_ids": sorted(c.requirement_ids),
                                "failure_text": report.failure_texts.get(c.check_id, "")[:FEEDBACK_TEXT_CAP]})
            else:
                hidden_reqs.add(tuple(sorted(c.requirement_ids)))
        return visible + [{"requirement_ids": list(t)} for t in sorted(hidden_reqs)]


# ---- helpers -----------------------------------------------------------------------
def _remove(p: Path) -> None:
    if p.is_symlink() or not p.is_dir():
        os.unlink(p)
    else:
        _rm_tree(p)


def _restore(checkout: Path, rel: str, data: bytes) -> str | None:
    """Make ``checkout/rel`` a regular file with ``data``. Returns None if it was already
    correct, "missing" if it had to be created, "modified" if something different was there
    (other content, a link, a directory, or a link/file in a parent position). Symlinks are
    never followed: a link in the path or at the target is replaced."""
    parts = rel.split("/")
    cur = checkout
    parent_replaced = False
    for part in parts[:-1]:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            cur.mkdir()
            continue
        if not stat.S_ISDIR(st.st_mode):         # a link or file where a directory belongs
            os.unlink(cur)
            cur.mkdir()
            parent_replaced = True
    target = cur / parts[-1]
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        st = None
    outcome = "modified" if parent_replaced else "missing"
    if st is not None:
        if stat.S_ISREG(st.st_mode) and st.st_size == len(data) and not parent_replaced \
                and target.read_bytes() == data:
            return None
        outcome = "modified"
        if stat.S_ISDIR(st.st_mode):
            _rm_tree(target)
        else:
            os.unlink(target)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return outcome


def _read_results(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            return None
        data = f.read(MAX_RESULTS_BYTES + 1)
    if len(data) > MAX_RESULTS_BYTES or b"\0" in data:
        return None
    low = data.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        return None
    return data


def _status_of(tc: ET.Element) -> tuple[CheckStatus, str]:
    for tag, status in (("failure", CheckStatus.FAIL), ("error", CheckStatus.ERROR),
                        ("skipped", CheckStatus.SKIP)):
        el = tc.find(tag)
        if el is not None:
            text = (el.text or "").strip() or (el.get("message") or "")
            return status, text
    return CheckStatus.PASS, ""


def _combine(cases: list[tuple[CheckStatus, str, float]]) -> tuple[CheckStatus, str, float]:
    statuses = {s for s, _t, _d in cases}
    if CheckStatus.FAIL in statuses:
        st = CheckStatus.FAIL
    elif CheckStatus.ERROR in statuses:
        st = CheckStatus.ERROR
    elif statuses == {CheckStatus.PASS}:
        st = CheckStatus.PASS
    else:
        st = CheckStatus.SKIP
    text = "\n---\n".join(t for s, t, _d in cases if s is not CheckStatus.PASS and t)
    return st, text[:FAILURE_TEXT_CAP], sum(d for _s, _t, d in cases)


def _summary_counts(stdout: bytes, truncated: bool) -> dict[str, int] | None:
    if truncated:
        return None
    for line in reversed(stdout.decode("utf-8", "replace").splitlines()):
        m = _SUMMARY.match(line.strip())
        if m:
            counts: dict[str, int] = {}
            for n, word in _COUNT.findall(m.group(1)):
                counts[word] = counts.get(word, 0) + int(n)
            return counts
    return None


def _parse(checkout: Path, checkset: CheckSet, include_hidden: bool, rr) -> RunReport:
    expected = [c for c in checkset.checks if include_hidden or c.visibility == "visible"]
    modules: dict[str, tuple[str, str]] = {}      # dotted module -> (visibility, relpath)
    by_key: dict[tuple[str, str], object] = {}    # (classname, function) -> DraftCheck
    for c in expected:
        mod = f"{CHECKS_DIR}.{c.visibility}.{module_name(c.relpath)}"
        modules[mod] = (c.visibility, c.relpath)
        by_key[(mod, c.node)] = c
    modules[SENTINEL_MODULE] = ("sentinel", "")

    data = _read_results(checkout / JUNIT_REL)
    root = None
    if data is not None:
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            root = None
    base = dict(returncode=rr.returncode, timed_out=rr.timed_out)
    if root is None and not rr.timed_out:
        return RunReport(sentinels_ok=False, exit_code_consistent=False, **base)

    per_check: dict[str, list] = {}
    sentinel: dict[str, list] = {MUST_PASS: [], MUST_FAIL: []}
    unmatched: list[str] = []
    tally = {"pass": 0, "fail": 0, "skip": 0}
    any_fail = False
    if root is not None:
        for tc in root.iter("testcase"):
            cls, name = tc.get("classname") or "", tc.get("name") or ""
            status, text = _status_of(tc)
            try:
                dur = float(tc.get("time") or 0.0)
            except ValueError:
                dur = 0.0
            tally["pass" if status is CheckStatus.PASS else "skip" if status is CheckStatus.SKIP else "fail"] += 1
            any_fail = any_fail or status in (CheckStatus.FAIL, CheckStatus.ERROR)
            if cls == "" and name in modules:            # collection error: the whole file
                kind, rel = modules[name]
                if kind == "sentinel":
                    for k in sentinel:
                        sentinel[k].append((status, text, dur))
                for c in expected:
                    if (c.visibility, c.relpath) == (kind, rel):
                        per_check.setdefault(c.check_id, []).append((status, text, dur))
                continue
            fn = name.split("[", 1)[0]
            if cls == SENTINEL_MODULE and fn in sentinel:
                sentinel[fn].append((status, text, dur))
                continue
            c = by_key.get((cls, fn))
            if c is None:
                if len(unmatched) < 200:
                    unmatched.append(f"{cls}::{name}"[:200])
                continue
            per_check.setdefault(c.check_id, []).append((status, text, dur))

    results: list[CheckResult] = []
    texts: dict[str, str] = {}
    for c in sorted(expected, key=lambda c: c.check_id):
        cases = per_check.get(c.check_id)
        if cases is None:
            if not rr.timed_out:
                continue                                  # absent: S7b treats it as tampering
            status, text, dur = CheckStatus.TIMEOUT, "", 0.0
        else:
            status, text, dur = _combine(cases)
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest() if (
            status is not CheckStatus.PASS and text) else ""
        if status is not CheckStatus.PASS:
            texts[c.check_id] = text
        results.append(CheckResult(c.check_id, tuple(sorted(c.requirement_ids)), status,
                                   c.visibility, duration_s=dur, output_digest=digest))

    mp, mf = sentinel[MUST_PASS], sentinel[MUST_FAIL]
    sentinels_ok = (root is not None and len(mp) == 1 and len(mf) == 1
                    and mp[0][0] is CheckStatus.PASS and mf[0][0] is CheckStatus.FAIL)

    consistent = False
    if root is not None and not rr.timed_out and rr.returncode == (1 if any_fail else 0):
        counts = _summary_counts(rr.stdout, rr.truncated)
        if counts is not None:
            consistent = (counts.get("passed", 0) + counts.get("xpassed", 0) == tally["pass"]
                          and counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
                          == tally["fail"]
                          and counts.get("skipped", 0) + counts.get("xfailed", 0) == tally["skip"])
    return RunReport(results=tuple(results), executed_ids=frozenset(r.check_id for r in results),
                     sentinels_ok=sentinels_ok, exit_code_consistent=consistent,
                     raw_unmatched=tuple(unmatched), results_parsed=root is not None,
                     failure_texts=texts, **base)


def _parse_project(checkout: Path, rr) -> ProjectRunReport:
    data = _read_results(checkout / PROJECT_JUNIT_REL)
    root = None
    if data is not None:
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            root = None
    base = dict(returncode=rr.returncode, timed_out=rr.timed_out)
    if root is None:
        return ProjectRunReport(**base)
    cases: dict[str, list[CheckStatus]] = {}
    tally = {"pass": 0, "fail": 0, "skip": 0}
    any_fail = False
    for tc in root.iter("testcase"):
        cls, name = tc.get("classname") or "", tc.get("name") or ""
        status, _text = _status_of(tc)
        tally["pass" if status is CheckStatus.PASS else "skip" if status is CheckStatus.SKIP else "fail"] += 1
        any_fail = any_fail or status in (CheckStatus.FAIL, CheckStatus.ERROR)
        cases.setdefault(f"{cls}::{name}" if cls else name, []).append(status)
    statuses: dict[str, CheckStatus] = {}
    for tid, sts in cases.items():
        if CheckStatus.FAIL in sts:
            statuses[tid] = CheckStatus.FAIL
        elif CheckStatus.ERROR in sts:
            statuses[tid] = CheckStatus.ERROR
        elif all(s is CheckStatus.PASS for s in sts):
            statuses[tid] = CheckStatus.PASS
        else:
            statuses[tid] = CheckStatus.SKIP
    consistent = False
    if not rr.timed_out and rr.returncode == (1 if any_fail else 0):
        counts = _summary_counts(rr.stdout, rr.truncated)
        if counts is not None:
            consistent = (counts.get("passed", 0) + counts.get("xpassed", 0) == tally["pass"]
                          and counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
                          == tally["fail"]
                          and counts.get("skipped", 0) + counts.get("xfailed", 0) == tally["skip"])
    return ProjectRunReport(statuses=statuses, results_parsed=True, exit_code_consistent=consistent, **base)
