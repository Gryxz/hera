"""Run contract checks (S7c): the candidate runs in a sandboxed subprocess, the oracle compares.

For EACH contract check the runner copies the verification checkout into a fresh run directory
(next to the checkout, so inside the task's verify area, which the sandbox may write to), writes
the check's input files, runs the check's command under ``hera.sandbox``, evaluates the result
with ``hera.oracle.contract.evaluate`` in THIS process, and deletes the run directory. Each check
gets its own copy, so a candidate cannot carry state from one check to the next or learn one
check's inputs from a previous one.

What the candidate can see: the checkout copy, the check's argv, stdin, environment and input
files (inputs are not secret). What it cannot see: the contract files, expected values and any
other check, because contract files are read from the frozen store by this process and are never
copied anywhere. Nothing from the checkout is imported, executed or evaluated here: this module
and ``contract`` import neither pytest nor any import machinery (a test enforces it).
"""
from __future__ import annotations

import hashlib
import math
import os
import secrets
import shutil
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from .. import sandbox
from ..types import CheckResult, CheckStatus
from ..workspace import _rm_tree, find_escaping_symlinks
from .checkstore import CheckStoreTampered, verify_integrity
from .contract import OWNED_DIRS, ContractCheck, evaluate, load_contract_file
from .model import CheckSet, DraftCheck, checks_by_runner
from .nodeenv import find_node, readable_paths

FEEDBACK_OUTPUT_CHARS = 2_000
FEEDBACK_TEXT_CAP = 5_000
_EXEC_FAILURES = {"bwrap": b"bwrap: execvp", "sandbox-exec": b"sandbox-exec: execvp"}


@dataclass(frozen=True)
class ContractRunReport:
    """results: one per included contract check (always; a check that cannot run is ERROR).
    failure_texts: visible checks only (hidden checks never get a text). timed_out_any: some
    check hit its wall limit. mechanism: the sandbox mechanism used ("" if nothing ran)."""
    results: tuple[CheckResult, ...] = ()
    executed_ids: frozenset[str] = frozenset()
    failure_texts: dict[str, str] = field(default_factory=dict)
    timed_out_any: bool = False
    mechanism: str = ""


def _force_rm(path: Path) -> None:
    _rm_tree(path)
    if os.path.lexists(path):                      # a candidate may have removed permissions
        for dirpath, dirnames, _f in os.walk(path):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), 0o700, follow_symlinks=False)
                except (OSError, NotImplementedError):
                    pass
            try:
                os.chmod(dirpath, 0o700)
            except OSError:
                pass
        _rm_tree(path)


def _write_input(run_dir: Path, rel: str, text: str) -> None:
    """Write an input file without following symlinks the checkout may contain."""
    cur = run_dir
    parts = rel.split("/")
    for part in parts[:-1]:
        cur = cur / part
        if os.path.islink(cur) or (os.path.lexists(cur) and not cur.is_dir()):
            os.unlink(cur)
        cur.mkdir(exist_ok=True)
    target = cur / parts[-1]
    if os.path.lexists(target):
        if target.is_dir() and not target.is_symlink():
            _rm_tree(target)
        else:
            os.unlink(target)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(text.encode("utf-8"))


def _limits(base: sandbox.Limits | None, timeout_s: float) -> sandbox.Limits:
    cpu = int(math.ceil(timeout_s)) + 2
    if base is None:
        return sandbox.Limits(wall_s=timeout_s, cpu_s=cpu)
    return replace(base, wall_s=min(base.wall_s, timeout_s), cpu_s=min(base.cpu_s, cpu))


class ContractRunner:
    name = "contract"

    def __init__(self, python: str | None = None, node: str | None = None) -> None:
        self.python = python or sys.executable        # the interpreter that replaces {python}
        self.node = node                              # replaces {node}; None: $HERA_NODE, then PATH

    # ---- loading ---------------------------------------------------------------------
    def _load(self, checkset: CheckSet, drafts: list[DraftCheck]) -> dict[tuple[str, str], ContractCheck]:
        by_key: dict[tuple[str, str], ContractCheck] = {}
        for rel in sorted({d.relpath for d in drafts}):
            vis = next(d.visibility for d in drafts if d.relpath == rel)
            data = (checkset.root / vis / rel).read_bytes()
            if hashlib.sha256(data).hexdigest() != checkset.files[rel]:
                raise CheckStoreTampered("a contract file does not match its hash")
            for cc in load_contract_file(data, rel):
                by_key[(rel, cc.node)] = cc
        return by_key

    # ---- run -------------------------------------------------------------------------
    def run(self, checkout, checkset: CheckSet, *, include_hidden: bool = True, limits=None,
            allow_unsandboxed: bool = False) -> ContractRunReport:
        checkout = Path(checkout)
        if not checkout.is_dir():
            raise ValueError("checkout must be a directory")
        verify_integrity(checkset)
        drafts = [c for c in checks_by_runner(checkset, "contract")
                  if include_hidden or c.visibility == "visible"]
        if not drafts:
            return ContractRunReport()
        defs = self._load(checkset, drafts)
        results: list[CheckResult] = []
        texts: dict[str, str] = {}
        timed_out_any = False
        mechanism = ""
        for d in sorted(drafts, key=lambda d: d.check_id):
            cc = defs[(d.relpath, d.node)]
            status, text, digest, mech = self._run_one(checkout, cc, d, limits, allow_unsandboxed)
            mechanism = mechanism or mech
            timed_out_any = timed_out_any or status is CheckStatus.TIMEOUT
            if status is not CheckStatus.PASS and d.visibility == "visible":
                texts[d.check_id] = text
            results.append(CheckResult(d.check_id, tuple(sorted(d.requirement_ids)), status,
                                       d.visibility, output_digest=digest))
        return ContractRunReport(tuple(results), frozenset(r.check_id for r in results), texts,
                                 timed_out_any, mechanism)

    def _run_one(self, checkout: Path, cc: ContractCheck, d: DraftCheck, limits, allow_unsandboxed):
        """Returns (status, visible failure text, output digest, mechanism)."""
        run_dir = checkout.parent / f"hera-contract-{secrets.token_hex(8)}"
        root_real = os.path.realpath(checkout)

        def ignore(src, names):                       # the oracle's own directories never reach a run
            return [n for n in names if n in OWNED_DIRS] if os.path.realpath(src) == root_real else []
        try:
            shutil.copytree(checkout, run_dir, symlinks=True, ignore=ignore)
            for rel in find_escaping_symlinks(run_dir):     # links into the oracle's own checkout
                os.unlink(run_dir / rel)
            for rel, text in cc.files:
                _write_input(run_dir, rel, text)
            real = os.path.realpath(run_dir)
            env = dict(cc.env)
            env["PYTHONPATH"] = os.pathsep.join([real, os.path.join(real, "src")])
            kw = {"stdin_data": cc.stdin.encode("utf-8")} if cc.stdin else {}
            node = find_node(self.node) if cc.needs_node else None
            if node is not None:                       # Node's install directory must be readable inside
                kw["read_only"] = readable_paths(node)
                env["NODE_OPTIONS"] = ""
            argv = cc.resolved_argv(self.python, node)
            try:
                if cc.needs_node and node is None:
                    raise OSError("node executable not found for a {node} contract check")
                lim = _limits(limits, cc.timeout_s)
                if cc.needs_node:                      # V8 and Wasm reserve address space: no RLIMIT_AS
                    lim = replace(lim, memory_mb=None)
                rr = sandbox.run(argv, cwd=run_dir, limits=lim, env=env,
                                 allow_unsandboxed=allow_unsandboxed, **kw)
            except (OSError, ValueError) as e:        # program not found or not executable
                rr = sandbox.RunResult(None, b"", f"{type(e).__name__}: {e}".encode("utf-8", "replace"),
                                       False, False, 0.0, sandbox.detect_mechanism())
            marker = _EXEC_FAILURES.get(rr.mechanism)
            if marker and rr.returncode not in (0, None) and rr.stderr.startswith(marker):
                rr = replace(rr, returncode=None)
            status, failed = evaluate(cc, rr, run_dir)
            digest = ""
            text = ""
            if status is not CheckStatus.PASS:
                digest = hashlib.sha256(rr.stdout + rr.stderr).hexdigest()
                if d.visibility == "visible":
                    text = _failure_text(status, failed, rr, (str(run_dir), real))
            return status, text, digest, rr.mechanism
        finally:
            _force_rm(run_dir)

    # ---- feedback --------------------------------------------------------------------
    def feedback_for_implementer(self, report: ContractRunReport, checkset: CheckSet) -> list[dict]:
        """Visible failures: check id, requirement ids, failed expectations and the first
        2,000 characters of stdout and stderr. Hidden failures: requirement ids ONLY."""
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


def _failure_text(status: CheckStatus, failed: list[str], rr, scrub) -> str:
    def clip(b: bytes) -> str:
        t = b.decode("utf-8", "replace").replace("\r\n", "\n")[:FEEDBACK_OUTPUT_CHARS]
        for p in sorted(set(scrub), key=len, reverse=True):
            t = t.replace(p, "<checkout>")
        return t
    lines = ["failed expectations:" if status is CheckStatus.FAIL else f"{status.value}:"]
    lines += [f"- {f}" for f in failed]
    lines += ["stdout (first 2000 characters):", clip(rr.stdout),
              "stderr (first 2000 characters):", clip(rr.stderr)]
    return "\n".join(lines)
