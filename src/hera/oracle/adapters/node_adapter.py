"""node adapter: prepare an oracle-owned run, execute it in the sandbox, parse junit and TAP.

Checks are files run by Node's built-in test runner (``node --test``): ``*.test.js`` and
``*.test.mjs``, and ``*.test.ts`` / ``*.test.mts`` through Node's type stripping. No npm package
is needed, so no network or install step exists. One check is one top-level ``test()`` or
``describe()`` of a check file; its subtests all have to pass.

What it guarantees and what it does not (see docs/ORACLE.md, "Node adapter"): ``prepare``
restores every protected path to its base content, deletes protected files the candidate added
and strips test-control settings from every ``package.json``; the run uses a scrubbed
environment and runs only oracle-owned check files, each in a process of its own, written from
the frozen store right before it runs. It does NOT isolate the check from the candidate's own
modules: a check imports candidate code into the same Node process as the test.

Why two reporters: junit (to a file the oracle owns) carries names, durations and failure
text but drops a failure of a test that happens after its subtests passed, and it is a file the
candidate's process can reach. TAP on stdout is written by the parent runner process, escapes
everything the candidate prints, and is exact about top-level results. The two are compared; the
worse result of the two counts, and any disagreement is an integrity failure.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from ... import sandbox
from ...types import CheckResult, CheckStatus
from ...workspace import _rm_tree
from ..checkstore import verify_integrity
from ..model import CheckSet, PrepareReport, ProjectRunReport, RunReport, checks_by_runner
from ..nodeenv import NodeInfo, readable_paths, require_node
from .pytest_adapter import (CHECKS_DIR, FAILURE_TEXT_CAP, PROJECT_DIR, RESULTS_DIR, PytestAdapter, _norm,
                             _read_results, _remove, _restore, match_glob)

SENTINEL_REL = f"{CHECKS_DIR}/_sentinel/hera_sentinel.test.mjs"
MUST_PASS, MUST_FAIL = "hera_must_pass", "hera_must_fail"
JUNIT_NAME = "junit.xml"
CONTROL_KEYS = frozenset({"node_options", "nodeoptions"})          # package.json keys, compared lower-case
DEFAULT_WALL_S = sandbox.Limits().wall_s

_SENTINEL_SRC = ("import test from 'node:test';\n"
                 "import assert from 'node:assert/strict';\n\n"
                 f"test('{MUST_PASS}', () => {{ assert.equal(1, 1); }});\n"
                 f"test('{MUST_FAIL}', () => {{ assert.equal(1, 2); }});\n")
# The oracle's directory is an ES-module scope of its own, so a candidate's package.json cannot
# change how check files are parsed (and no package.json above the checkout is ever consulted
# before this one).
_CHECKS_PACKAGE_JSON = '{"type": "module"}\n'
_ENV = {"NODE_OPTIONS": "", "NODE_PATH": "", "NODE_NO_WARNINGS": "1", "NO_COLOR": "1",
        "NODE_DISABLE_COLORS": "1"}

_EXTS = ("js", "mjs", "cjs", "ts", "mts", "cts")
_TEST_NAME_PATTERNS = ("*.test.{e}", "*.spec.{e}", "*-test.{e}", "*_test.{e}", "test-*.{e}", "test.{e}")
PROTECTED_GLOBS = (
    ".noderc", "**/.noderc", ".npmrc", "**/.npmrc", "node.config.json", "**/node.config.json",
    "test/**", "tests/**", "__tests__/**", "**/__tests__/**", ".github/**",
    *(f"**/{p.format(e=e)}" for p in _TEST_NAME_PATTERNS for e in _EXTS),
)

_TAP_TOP = re.compile(r"^(not )?ok \d+ - (.*)$")
_TAP_COUNT = re.compile(r"^# (tests|suites|pass|fail|cancelled|skipped|todo) (\d+)$")
_TAP_DIRECTIVE = re.compile(r"(?<!\\) # (SKIP|TODO)\b.*$")
_TAP_NOISE = re.compile(r"^# (?:Subtest: |duration_ms |(?:tests|suites|pass|fail|cancelled|skipped|todo) \d+$)")
_PROJECT_TEST = re.compile(r".*\.test\.(?:js|mjs|ts|mts)$")


# ---- small helpers ---------------------------------------------------------------------
def _cls(st: CheckStatus) -> str:
    return "fail" if st in (CheckStatus.FAIL, CheckStatus.ERROR, CheckStatus.TIMEOUT) else \
        "skip" if st is CheckStatus.SKIP else "pass"


def _worse(a: CheckStatus, b: CheckStatus) -> CheckStatus:
    for st in (CheckStatus.ERROR, CheckStatus.FAIL, CheckStatus.SKIP):
        if st in (a, b):
            return st
    return CheckStatus.PASS


def _combine(statuses: list[CheckStatus]) -> CheckStatus:
    """FAIL if any case failed, ERROR if any errored, PASS only if all passed, else SKIP."""
    s = set(statuses)
    if CheckStatus.FAIL in s:
        return CheckStatus.FAIL
    if CheckStatus.ERROR in s:
        return CheckStatus.ERROR
    return CheckStatus.PASS if s == {CheckStatus.PASS} else CheckStatus.SKIP


def _write_fresh(path: Path, data: bytes) -> None:
    """Write a regular file at ``path`` (never through a link), replacing whatever was there."""
    if os.path.lexists(path):
        _remove(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _status_of(tc: ET.Element) -> tuple[CheckStatus, str]:
    if tc.find("skipped") is not None:               # also a failing todo test (it does not fail the run)
        return CheckStatus.SKIP, ""
    for tag, status in (("failure", CheckStatus.FAIL), ("error", CheckStatus.ERROR)):
        el = tc.find(tag)
        if el is not None:
            return status, ((el.text or "").strip() or (el.get("message") or ""))
    return CheckStatus.PASS, ""


@dataclasses.dataclass
class _Tap:
    top: list[tuple[str, CheckStatus]]        # top-level entries in order
    counts: dict[str, int]
    notes: list[str]                          # what the child processes printed (stderr, console)


def _parse_tap(stdout: bytes, truncated: bool) -> _Tap | None:
    """Top-level results and the summary counts of the runner's TAP output; None if the output is
    truncated, is not TAP, or has no summary. Everything a candidate prints reaches this stream
    escaped and prefixed with ``# ``, so it can never look like a result line."""
    if truncated:
        return None
    lines = stdout.decode("utf-8", "replace").split("\n")
    if not lines or lines[0].strip() != "TAP version 13":
        return None
    top: list[tuple[str, CheckStatus]] = []
    counts: dict[str, int] = {}
    notes: list[str] = []
    for line in lines[1:]:
        m = _TAP_TOP.match(line)
        if m:
            name, directive = m.group(2), _TAP_DIRECTIVE.search(m.group(2))
            if directive:
                name = name[:directive.start()]
            status = CheckStatus.SKIP if directive else CheckStatus.FAIL if m.group(1) else CheckStatus.PASS
            top.append((name, status))
            continue
        c = _TAP_COUNT.match(line)
        if c:
            counts[c.group(1)] = int(c.group(2))
        elif line.startswith("# ") and not _TAP_NOISE.match(line):
            notes.append(line[2:])
    if not all(k in counts for k in ("fail", "cancelled")):
        return None
    return _Tap(top, counts, notes)


@dataclasses.dataclass
class _FileRun:
    rr: object
    parsed: bool = False                      # junit and TAP both usable
    consistent: bool = False                  # exit code, summary, junit and TAP agree
    top: dict = dataclasses.field(default_factory=dict)       # name -> (status, text, duration, is_suite)
    file_level: tuple | None = None           # (status, text) of a failure of the whole file
    leaves: list = dataclasses.field(default_factory=list)    # (trail tuple, status), junit leaves
    dups: set = dataclasses.field(default_factory=set)        # top-level names that occur twice in the file


def _evaluate(rr, root: ET.Element | None, tap: _Tap | None, rel: str) -> _FileRun:
    """Turn one file's run into per-top-level results and decide whether the evidence is consistent."""
    run = _FileRun(rr)
    if rr.timed_out or root is None or tap is None:
        return run
    run.parsed = True

    # junit: direct children of the root are the top-level entries; leaves are the testcases below
    j_names: list[str] = []
    cases: dict[str, list[tuple[tuple[str, ...], CheckStatus, str, float]]] = {}
    is_suite: dict[str, bool] = {}

    def walk(el: ET.Element, trail: tuple[str, ...]) -> None:
        for ch in el:
            if ch.tag == "testsuite":
                walk(ch, trail + (ch.get("name") or "",))
            elif ch.tag == "testcase":
                st, text = _status_of(ch)
                try:
                    dur = float(ch.get("time") or 0.0)
                except ValueError:
                    dur = 0.0
                cases.setdefault(trail[0] if trail else (ch.get("name") or ""), []).append(
                    (trail + (ch.get("name") or "",), st, text, dur))
    for ch in root:
        if ch.tag in ("testsuite", "testcase"):
            name = ch.get("name") or ""
            j_names.append(name)
            is_suite[name] = ch.tag == "testsuite"
    walk(root, ())

    tap_names = [n for n, _s in tap.top]
    fail_total = tap.counts["fail"] + tap.counts["cancelled"]
    ok = rr.returncode == (1 if fail_total > 0 else 0)
    ok = ok and (fail_total > 0) == any(s is CheckStatus.FAIL for _n, s in tap.top)
    ok = ok and Counter(j_names) == Counter(tap_names)
    count = Counter(j_names)

    note_text = "\n".join(tap.notes)
    for name in dict.fromkeys(j_names):
        entries = cases.get(name, [])
        statuses = [s for _t, s, _x, _d in entries]
        j_status = _combine(statuses) if statuses else None
        t_all = [s for n, s in tap.top if n == name]
        if not t_all:
            ok = False
            continue
        tstat = CheckStatus.PASS
        for s in t_all:
            tstat = _worse(tstat, s)
        if count[name] > 1:
            run.dups.add(name)
        if name == rel and not is_suite[name]:          # the whole file: it failed to load or crashed
            if _cls(tstat) != "pass" or (j_status is not None and _cls(j_status) != "pass"):
                run.file_level = (CheckStatus.ERROR, note_text or (entries[0][2] if entries else "the check file failed"))
            if j_status is not None and _cls(j_status) != _cls(tstat):
                ok = False
            continue
        if j_status is None:                             # a top-level suite without any test in it
            continue
        if is_suite[name]:
            if _cls(j_status) == "fail" and _cls(tstat) != "fail":
                ok = False
        elif _cls(j_status) != _cls(tstat):
            ok = False
        final = _worse(j_status, tstat)
        text = "\n---\n".join(x for _t, s, x, _d in entries if s is not CheckStatus.PASS and x)
        if final is not j_status and not text:
            text = "the test failed after its subtests ran (reported by the TAP stream only)"
        run.top[name] = (final, text[:FAILURE_TEXT_CAP], sum(d for *_a, d in entries), is_suite[name])
        for trail, st, _x, _d in entries:
            run.leaves.append((trail, CheckStatus.FAIL if (final is CheckStatus.FAIL and st is CheckStatus.PASS) else st))
    run.consistent = ok
    return run


def _id_of(rel: str, trail: tuple[str, ...]) -> str:
    return f"{rel}::{' > '.join(trail)}"


class NodeAdapter:
    name = "node"
    runner = "node"                    # DraftCheck.runner of the checks this adapter runs natively
    control_content_names = ("package.json",)   # files whose content the audit hands to controls_runner

    def __init__(self, node: str | None = None) -> None:
        self._node = node
        self._prepared: dict[str, tuple[str, bool]] = {}

    # ---- detection -------------------------------------------------------------------
    def detect(self, repo_root) -> float:
        root = Path(repo_root)
        if not root.is_dir():
            return 0.0
        skip = {".git", "node_modules", "_hera_checks", "_hera_results", "_hera_project"}
        any_js, seen = (root / "package.json").is_file(), 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip]
            for f in filenames:
                seen += 1
                if _PROJECT_TEST.match(f):
                    return 1.0
                if f.rsplit(".", 1)[-1] in _EXTS:
                    any_js = True
            if seen > 20000:
                break
        return 0.3 if any_js else 0.0

    # ---- protected paths -------------------------------------------------------------
    def protected_globs(self) -> tuple[str, ...]:
        return PROTECTED_GLOBS

    def is_protected(self, path: str) -> bool:
        p = _norm(path)
        return bool(p) and any(match_glob(p, g) for g in PROTECTED_GLOBS)

    def controls_runner(self, path: str, data: bytes | None = None) -> bool:
        """True for a file, or (``package.json``, given its content) a setting, that steers how
        Node runs: ``.noderc``, ``.npmrc``, ``node.config.json`` anywhere, and a ``package.json``
        that sets node options. Used by the audit to turn them into BLOCK findings."""
        low = _norm(path).casefold()
        name = low.rsplit("/", 1)[-1]
        if name in (".noderc", ".npmrc", "node.config.json"):
            return True
        return name == "package.json" and data is not None and _has_control_keys(data)

    # ---- node ------------------------------------------------------------------------
    def _info(self, *, need_typescript: bool = False) -> NodeInfo:
        return require_node(self._node, need_typescript=need_typescript)

    @staticmethod
    def _needs_ts(rels) -> bool:
        return any(r.endswith((".ts", ".mts")) for r in rels)

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
        mine = [c for c in checks_by_runner(checkset, "node") if include_hidden or c.visibility == "visible"]
        self._info(need_typescript=self._needs_ts({c.relpath for c in mine}))     # refuse early, clearly

        deleted: list[str] = []
        for name in (CHECKS_DIR, RESULTS_DIR):           # 0. oracle-owned names
            p = checkout / name
            if os.path.lexists(p):
                _remove(p)
                deleted.append(name)
        # 1a. delete protected files the candidate added
        for dirpath, dirnames, filenames in os.walk(checkout, followlinks=False):
            dirnames[:] = [d for d in dirnames if d != "node_modules"]
            for n in (*dirnames, *filenames):
                full = Path(dirpath) / n
                if full.is_dir() and not full.is_symlink():
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
        # 1c. test-control settings inside package.json files (they are not protected as a whole:
        # a candidate may legitimately change dependencies, scripts or the module type)
        for rel in _strip_control_settings(checkout):
            restored.append(rel)
            modified.append(rel)
        # The check files are NOT written here: run() writes each one from the frozen store right
        # before its own process starts, so nothing earlier can alter or read the later ones.
        self._prepared[os.path.realpath(checkout)] = (checkset.hash, include_hidden)
        return PrepareReport(tuple(sorted(set(restored))), tuple(sorted(deleted)), tuple(sorted(set(modified))))

    # ---- command ---------------------------------------------------------------------
    def _argv(self, info: NodeInfo, rel: str, results_dir: str) -> list[str]:
        return [info.path, "--no-warnings", *(info.ts_flags or ()), "--test", "--test-force-exit",
                "--test-reporter=junit", f"--test-reporter-destination={results_dir}/{JUNIT_NAME}",
                "--test-reporter=tap", "--test-reporter-destination=stdout", rel]

    def command(self, checkout) -> list[str]:
        """The command of one check-file run (shown for the sentinel file; the real runs differ only
        in the file name)."""
        return self._argv(self._info(), SENTINEL_REL, RESULTS_DIR)

    def project_command(self, test_paths) -> list[str]:
        """The command of one project-test run (one file per run)."""
        paths = tuple(test_paths)
        return self._argv(self._info(), paths[0] if paths else "", PROJECT_DIR)

    # ---- one file --------------------------------------------------------------------
    def _execute(self, checkout: Path, info: NodeInfo, rel: str, results_dir: str, limits,
                 remaining_s: float, allow_unsandboxed: bool) -> _FileRun:
        rdir = checkout / results_dir
        if os.path.lexists(rdir):
            _remove(rdir)
        rdir.mkdir()
        # V8 and WebAssembly (type stripping) reserve address space, so RLIMIT_AS cannot be used
        lim = dataclasses.replace(limits, memory_mb=None, wall_s=max(remaining_s, 1.0))
        rr = sandbox.run(self._argv(info, rel, results_dir), cwd=checkout, env=_ENV,
                         read_only=readable_paths(info.path), limits=lim, allow_unsandboxed=allow_unsandboxed)
        root = None
        data = _read_results(rdir / JUNIT_NAME)
        if data is not None:
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                root = None
        return _evaluate(rr, root, _parse_tap(rr.stdout, rr.truncated), rel)

    # ---- run -------------------------------------------------------------------------
    def run(self, checkout, checkset: CheckSet, *, include_hidden: bool = True, limits=None,
            allow_unsandboxed: bool = False) -> RunReport:
        checkout = Path(checkout)
        if self._prepared.get(os.path.realpath(checkout)) != (checkset.hash, include_hidden):
            raise RuntimeError("run: this checkout was not prepared by this adapter for this check set")
        verify_integrity(checkset)
        expected = [c for c in checks_by_runner(checkset, "node") if include_hidden or c.visibility == "visible"]
        files: dict[str, tuple[str, list]] = {}            # relpath -> (visibility, [checks])
        for c in sorted(expected, key=lambda c: (c.visibility, c.relpath, c.check_id)):
            files.setdefault(c.relpath, (c.visibility, []))[1].append(c)
        info = self._info(need_typescript=self._needs_ts(files))
        limits = limits if limits is not None else sandbox.Limits()
        deadline = time.monotonic() + limits.wall_s

        for name in (CHECKS_DIR, RESULTS_DIR):
            if os.path.lexists(checkout / name):
                _remove(checkout / name)
        jobs: list[tuple[str, str, str, bytes]] = [      # (kind, store file, relative path, bytes)
            ("sentinel", "", SENTINEL_REL, _SENTINEL_SRC.encode("utf-8"))]
        for rel, (vis, _cs) in files.items():
            data = (checkset.root / vis / rel).read_bytes()
            if hashlib.sha256(data).hexdigest() != checkset.files[rel]:
                raise RuntimeError("a check file does not match its hash")     # store changed since freeze
            jobs.append(("check", rel, f"{CHECKS_DIR}/{vis}/{rel}", data))

        runs: dict[str, _FileRun] = {}
        timed_out = False
        returncode: int | None = 0
        all_consistent = True
        for kind, rel, path_rel, data in jobs:
            key = rel or "<sentinel>"
            remaining = deadline - time.monotonic()
            if timed_out or remaining <= 0:
                timed_out = True
                continue
            _write_job(checkout, path_rel, data)
            try:
                fr = self._execute(checkout, info, path_rel, RESULTS_DIR, limits, remaining, allow_unsandboxed)
            finally:
                if os.path.lexists(checkout / CHECKS_DIR):
                    _remove(checkout / CHECKS_DIR)
            runs[key] = fr
            if fr.rr.timed_out:
                timed_out = True
                returncode = None
            elif returncode is not None and fr.rr.returncode not in (0, None):
                returncode = fr.rr.returncode
            all_consistent = all_consistent and fr.consistent
        base = dict(returncode=returncode, timed_out=timed_out)
        if not timed_out and any(not fr.parsed for fr in runs.values()):
            return RunReport(sentinels_ok=False, exit_code_consistent=False, **base)

        results: list[CheckResult] = []
        texts: dict[str, str] = {}
        unmatched: list[str] = []
        for rel, (vis, checks) in files.items():
            fr = runs.get(rel)
            own = {c.node for c in checks}
            if fr is not None:
                for name in fr.top:
                    if name not in own and len(unmatched) < 200:
                        unmatched.append(f"{rel}::{name}"[:200])
            for c in checks:
                if fr is None or fr.rr.timed_out:
                    status, text, dur = CheckStatus.TIMEOUT, "", 0.0
                elif c.node in fr.dups:
                    status, text, dur = CheckStatus.ERROR, "two top-level tests of the check file share this name", 0.0
                elif c.node in fr.top:
                    status, text, dur, _s = fr.top[c.node]
                elif fr.file_level is not None:
                    status, text, dur = fr.file_level[0], fr.file_level[1][:FAILURE_TEXT_CAP], 0.0
                else:
                    continue                                    # absent: the oracle treats it as tampering
                digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest() if (
                    status is not CheckStatus.PASS and text) else ""
                if status is not CheckStatus.PASS:
                    texts[c.check_id] = text
                results.append(CheckResult(c.check_id, tuple(sorted(c.requirement_ids)), status, c.visibility,
                                           duration_s=dur, output_digest=digest))
        results.sort(key=lambda r: r.check_id)

        sent = runs.get("<sentinel>")
        sentinels_ok = bool(sent and sent.parsed and not sent.file_level and sent.consistent
                            and sorted(sent.top) == sorted((MUST_PASS, MUST_FAIL))
                            and sent.top[MUST_PASS][0] is CheckStatus.PASS
                            and sent.top[MUST_FAIL][0] is CheckStatus.FAIL)
        consistent = bool(not timed_out and all_consistent)
        return RunReport(results=tuple(results), executed_ids=frozenset(r.check_id for r in results),
                         sentinels_ok=sentinels_ok, exit_code_consistent=consistent,
                         raw_unmatched=tuple(unmatched), results_parsed=bool(runs) and all(
                             fr.parsed for fr in runs.values()),
                         failure_texts=texts, **base)

    # ---- the project's own pre-existing tests ----------------------------------------
    def project_test_paths(self, base_tree: dict[str, bytes]) -> tuple[str, ...]:
        """Test files of the project at base: ``*.test.js``, ``*.test.mjs``, ``*.test.ts`` and
        ``*.test.mts`` files of ``base_tree``. These are the oracle's choice of what 'the existing
        tests' means."""
        return tuple(sorted(rel for rel in base_tree if _PROJECT_TEST.match(rel.rsplit("/", 1)[-1])))

    def run_project_tests(self, checkout, test_paths, *, limits=None,
                          allow_unsandboxed: bool = False) -> ProjectRunReport:
        """Run the project's pre-existing tests in ``checkout`` under the sandbox, one process per
        test file. The project's own ``package.json`` applies (module type); the oracle
        directories are removed first and nothing of them is used."""
        checkout = Path(checkout)
        if not checkout.is_dir() or (checkout / ".git").exists():
            raise ValueError("checkout must be a directory without .git")
        test_paths = tuple(test_paths)
        for rel in test_paths:
            parts = rel.split("/")
            if not rel or rel.startswith(("/", "-")) or any(p in ("", ".", "..") for p in parts):
                raise ValueError("test_paths: must be relative POSIX paths")
        info = self._info(need_typescript=self._needs_ts(test_paths))
        for name in (CHECKS_DIR, RESULTS_DIR, PROJECT_DIR):
            if os.path.lexists(checkout / name):
                _remove(checkout / name)
        limits = limits if limits is not None else sandbox.Limits()
        deadline = time.monotonic() + limits.wall_s
        statuses: dict[str, list[CheckStatus]] = {}
        timed_out, parsed, consistent = False, True, True
        returncode: int | None = 0
        for rel in test_paths:
            remaining = deadline - time.monotonic()
            if timed_out or remaining <= 0:
                timed_out = True
                break
            fr = self._execute(checkout, info, rel, PROJECT_DIR, limits, remaining, allow_unsandboxed)
            if fr.rr.timed_out:
                timed_out, returncode = True, None
                break
            if returncode is not None and fr.rr.returncode not in (0, None):
                returncode = fr.rr.returncode
            parsed = parsed and fr.parsed
            consistent = consistent and fr.consistent
            if fr.file_level is not None:
                statuses.setdefault(rel, []).append(fr.file_level[0])
            for trail, st in fr.leaves:
                statuses.setdefault(_id_of(rel, trail), []).append(st)
        if os.path.lexists(checkout / PROJECT_DIR):
            _remove(checkout / PROJECT_DIR)
        base = dict(returncode=returncode, timed_out=timed_out)
        if timed_out or not parsed:
            return ProjectRunReport(**base)
        final: dict[str, CheckStatus] = {}
        for tid, sts in statuses.items():
            final[tid] = CheckStatus.FAIL if CheckStatus.FAIL in sts else CheckStatus.ERROR if \
                CheckStatus.ERROR in sts else CheckStatus.PASS if all(s is CheckStatus.PASS for s in sts) \
                else CheckStatus.SKIP
        return ProjectRunReport(statuses=final, results_parsed=True, exit_code_consistent=consistent, **base)

    # ---- feedback --------------------------------------------------------------------
    # Visible failures: check id, requirement ids and failure text. Hidden failures: requirement
    # ids ONLY. Nothing here depends on the runner, so it is the pytest adapter's function.
    feedback_for_implementer = PytestAdapter.feedback_for_implementer


# ---- helpers -----------------------------------------------------------------------
def _write_job(checkout: Path, rel: str, data: bytes) -> None:
    """Build ``_hera_checks`` afresh with the oracle's package.json and exactly one check file."""
    top = checkout / CHECKS_DIR
    if os.path.lexists(top):
        _remove(top)
    parts = rel.split("/")
    cur = top
    cur.mkdir()
    _write_fresh(top / "package.json", _CHECKS_PACKAGE_JSON.encode("ascii"))
    for part in parts[1:-1]:
        cur = cur / part
        cur.mkdir(exist_ok=True)
    _write_fresh(cur / parts[-1], data)


def _has_control_keys(data: bytes) -> bool:
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(doc, dict) and any(isinstance(k, str) and k.lower() in CONTROL_KEYS for k in doc)


def _strip_control_settings(checkout: Path) -> list[str]:
    """Remove ``node_options`` style keys from every package.json below ``checkout``; returns the
    relative paths that had to be rewritten."""
    changed: list[str] = []
    for dirpath, dirnames, filenames in os.walk(checkout, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != "node_modules"]
        for fname in filenames:                       # a case-insensitive file system resolves any case
            if fname.casefold() == "package.json":
                changed += _strip_one(checkout, Path(dirpath) / fname)
    return changed


def _strip_one(checkout: Path, p: Path) -> list[str]:
    if p.is_symlink() or not p.is_file() or p.stat().st_size > 1024 * 1024:
        return []
    data = p.read_bytes()
    if not _has_control_keys(data):
        return []
    doc = json.loads(data.decode("utf-8"))
    for k in [k for k in doc if isinstance(k, str) and k.lower() in CONTROL_KEYS]:
        del doc[k]
    _write_fresh(p, (json.dumps(doc, indent=2) + "\n").encode("utf-8"))
    return [p.relative_to(checkout).as_posix()]
