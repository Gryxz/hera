"""Diff audit: what did the candidate commit change, and is any of it something that decides
what runs or how results are judged?

Pure git/diff analysis. Nothing of the candidate is executed and no file contents are copied
into findings: a ``detail`` is a short fixed description. BLOCK findings make the verdict TAMPER
(through ``Receipt.audit_block_hits``); FLAG findings are recorded and shown, and never change a
verdict. See docs/ORACLE.md for the rule table.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from ..workspace import TaskRepo
from .adapters.pytest_adapter import match_glob

# Files that steer the runner. Added by a candidate (not present at base) they are a BLOCK.
_RUNNER_ANYWHERE = frozenset({"conftest.py", "sitecustomize.py", "usercustomize.py"})
_RUNNER_TOP_LEVEL = frozenset({"pytest.ini", "tox.ini", "setup.cfg"})
_ORACLE_DIRS = ("_hera_checks", "_hera_results")

_SKIP_MARKERS = ("pytest.mark.skip", "pytest.mark.xfail", "pytest.skip(", "unittest.skip", "@skip")
_DEBUGGER = ("breakpoint()", "import pdb", "pdb.set_trace()")
_DEBRIS_GLOBS = ("*.orig", "*.rej", "*.bak", "*.tmp", "*.log", "*.swp", ".ds_store")
_DEBRIS_PREFIXES = ("scratch", "debug", "tmp_")
_DEP_GLOBS = ("requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg", "pipfile*", "poetry.lock",
              "uv.lock", "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
              "cargo.lock", "gemfile.lock", "go.sum")


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    detail: str

    def render(self) -> str:
        return f"{self.rule}: {self.path}"


@dataclass(frozen=True)
class AuditReport:
    block: tuple[Finding, ...] = ()
    flags: tuple[Finding, ...] = ()


def _base(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _is_oracle_path(path: str) -> bool:
    top = path.split("/", 1)[0].casefold()
    return top in _ORACLE_DIRS


def _controls_runner(path: str) -> bool:
    low = path.casefold()
    name = _base(low)
    if _is_oracle_path(path) or name in _RUNNER_ANYWHERE:
        return True
    if "/" not in low and (name in _RUNNER_TOP_LEVEL or fnmatch.fnmatchcase(name, "pytest_*.py")):
        return True
    return False


def _is_text(data: bytes) -> bool:
    return b"\0" not in data[:8192]


def audit(repo: TaskRepo, base: str, commit: str, adapter, *, prepare_report=None,
          declared_scope: tuple[str, ...] | None = None, allowed_protected: tuple[str, ...] = (),
          twin_pattern: str | None = None) -> AuditReport:
    status = repo.diff_name_status(base, commit)
    block: list[Finding] = []
    flags: list[Finding] = []
    allowed = tuple(allowed_protected)

    def is_allowed(path: str) -> bool:
        return any(match_glob(path, g) for g in allowed)

    # ---- protected paths ---------------------------------------------------------
    prot_changed = {p for p, st in status.items() if st in ("M", "T", "D") and adapter.is_protected(p)}
    base_content = repo.read_tree(base, lambda p: p in prot_changed) if prot_changed else {}
    commit_content = repo.read_tree(commit, lambda p: p in prot_changed) if prot_changed else {}
    for path, st in sorted(status.items()):
        protected = adapter.is_protected(path)
        if st == "A":
            if _controls_runner(path):
                block.append(Finding("protected_path_added", path,
                                     "a file that controls the test runner or the oracle was added"))
            elif protected:
                flags.append(Finding("test_file_added", path, "a new file under a protected pattern was added"))
        elif path in prot_changed and not is_allowed(path):
            if st == "D":
                block.append(Finding("protected_path_deleted", path, "a protected file present at base was deleted"))
            elif base_content.get(path) != commit_content.get(path):
                block.append(Finding("protected_path_modified", path, "a protected file differs from its base content"))

    # ---- added lines: skip markers, debugger and print calls ----------------------
    added = repo.diff_added_lines(base, commit)
    for path in sorted(added):
        if not path.lower().endswith(".py"):
            continue
        lines = added[path]
        skips = sum(1 for ln in lines if any(m in ln for m in _SKIP_MARKERS))
        if skips:
            flags.append(Finding("skip_marker_added", path, f"{skips} added line(s) skip or expect-fail tests"))
        dbg = sum(1 for ln in lines if any(m in ln for m in _DEBUGGER))
        if dbg:
            flags.append(Finding("debris", path, f"{dbg} added debugger statement(s)"))
        if not adapter.is_protected(path) and _base(path) != "__main__.py":
            prints = sum(1 for ln in lines if ln.strip().startswith("print("))
            if prints:
                flags.append(Finding("debris", path, f"{prints} added print call(s)"))

    # ---- debris files, dependency manifests, declared scope -------------------------
    for path, st in sorted(status.items()):
        name = _base(path).casefold()
        if st == "A" and (any(fnmatch.fnmatchcase(name, g) for g in _DEBRIS_GLOBS)
                          or name.startswith(_DEBRIS_PREFIXES)):
            flags.append(Finding("debris", path, "a scratch, backup or log file was added"))
        if any(fnmatch.fnmatchcase(name, g) for g in _DEP_GLOBS):
            flags.append(Finding("dependency_manifest_changed", path, "a dependency manifest or lockfile changed"))
    if declared_scope is not None:
        for path in sorted(status):
            if not any(match_glob(path, g) for g in declared_scope):
                flags.append(Finding("out_of_declared_scope", path, "changed path matches no declared scope pattern"))

    # ---- twin search ---------------------------------------------------------------
    if twin_pattern is not None:
        if not isinstance(twin_pattern, str) or not twin_pattern:
            raise ValueError("twin_pattern: must be a non-empty literal string")
        needle = twin_pattern.encode("utf-8")
        tree = repo.read_tree(commit, lambda p: not adapter.is_protected(p))
        for path in sorted(tree):
            data = tree[path]
            if needle in data and _is_text(data):
                n = sum(1 for ln in data.split(b"\n") if needle in ln)
                flags.append(Finding("twin_remaining", path, f"{n} line(s) still contain the pattern"))

    # ---- the adapter's own view of what it had to undo ---------------------------------
    if prepare_report is not None:
        reported = [f.path for f in (*block, *flags)]

        def covered(p: str) -> bool:
            return any(r == p or r.startswith(p + "/") for r in reported)
        for rule, items in (("prepare_restored", (*prepare_report.restored, *prepare_report.modified_protected)),
                            ("prepare_deleted", prepare_report.deleted)):
            for p in sorted(set(items)):
                if covered(p) or (rule == "prepare_restored" and is_allowed(p)):
                    continue
                block.append(Finding(rule, p, "the verification checkout had to undo a change to this path"))
                reported.append(p)

    return AuditReport(tuple(sorted(block, key=lambda f: (f.rule, f.path))),
                       tuple(sorted(flags, key=lambda f: (f.rule, f.path, f.detail))))
