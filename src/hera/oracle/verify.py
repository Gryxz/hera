"""Verification: candidate commit + frozen check set -> audit, receipt, verdict, attribution.

Sequence (docs/ORACLE.md): integrity check of the frozen store; fresh checkout; ``prepare``;
check run in the sandbox; diff audit (including what ``prepare`` had to undo); a second run of
the project's pre-existing tests in another fresh checkout, compared with the same run on the
base commit; receipt; ``hera.oracle.verdict.verdict`` (pure); deterministic attribution;
persistence; feedback that is safe to show the implementer.

The verdict function stays pure: everything that needs I/O happens here and is folded into the
``Receipt``, which is stored in full so the verdict can be recomputed from stored evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field

from .. import sandbox
from ..types import Attribution, CheckHealth, CheckResult, CheckStatus, Receipt, Verdict
from ..workspace import TaskRepo, _check_hash
from . import checkstore
from .adapters.pytest_adapter import PytestAdapter
from .audit import AuditReport, Finding, audit
from .contract_runner import ContractRunner
from .disputes import has_open_dispute
from .model import CheckSet, RunReport, checks_by_runner
from .verdict import verdict as compute_verdict

FEEDBACK_MAX_CHARS = 6000


# ---- baseline of the project's pre-existing tests --------------------------------------
@dataclass(frozen=True)
class BaselineResult:
    """The project's own tests on the base commit. ``passing`` are tests that passed in every
    run; ``flaky`` tests whose result differed between runs (excluded from regression
    detection); ``not_passing`` tests that did not pass in any run (ignored, reported)."""
    has_tests: bool
    ok: bool
    test_paths: tuple[str, ...] = ()
    passing: frozenset[str] = frozenset()
    flaky: tuple[str, ...] = ()
    not_passing: tuple[str, ...] = ()
    consistent: bool = True
    runs: int = 0


def run_baseline(repo: TaskRepo, adapter, *, allow_unsandboxed: bool = False, limits=None,
                 runs: int = 2) -> BaselineResult:
    if runs < 2:
        raise ValueError("runs: at least 2 runs are needed to detect flaky tests")
    base_tree = repo.read_tree(repo.base_commit, adapter.is_protected)
    paths = adapter.project_test_paths(base_tree)
    if not paths:
        return BaselineResult(has_tests=False, ok=True, runs=0)
    reports = []
    for _ in range(runs):
        checkout = repo.verification_checkout(repo.base_commit)
        try:
            reports.append(adapter.run_project_tests(checkout, paths, limits=limits,
                                                     allow_unsandboxed=allow_unsandboxed))
        finally:
            repo.cleanup_verification(checkout)
    ok = all(r.results_parsed and not r.timed_out for r in reports)
    consistent = all(r.exit_code_consistent for r in reports if r.results_parsed)
    ids = sorted({t for r in reports for t in r.statuses})
    passing, flaky, not_passing = [], [], []
    for t in ids:
        seq = [r.statuses.get(t) for r in reports]
        if len(set(seq)) > 1:
            flaky.append(t)
        elif seq[0] is CheckStatus.PASS:
            passing.append(t)
        else:
            not_passing.append(t)
    return BaselineResult(True, ok, tuple(paths), frozenset(passing), tuple(flaky), tuple(not_passing),
                          consistent, runs)


# ---- outcome ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AttributionRecord:
    label: Attribution
    confidence: str
    rule: str
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationOutcome:
    receipt: Receipt
    receipt_id: str
    verdict: Verdict
    reasons: tuple[str, ...]
    attribution: AttributionRecord | None
    audit: AuditReport
    feedback: tuple[dict, ...]
    timed_out: bool
    environment_suspect: bool
    mechanism: str
    advisories: tuple[AttributionRecord, ...] = ()
    notes: tuple[str, ...] = ()
    project_timed_out: bool = False
    # Requirement ids that PASSED where every passing check is a native pytest check (no passing
    # contract check backs them), so a candidate that forges pytest results could fake them.
    # Shown by `hera verify` and in notes; never part of implementer feedback.
    in_process_only: tuple[str, ...] = ()


# ---- helpers ---------------------------------------------------------------------------
def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _finding_dict(f: Finding) -> list[str]:
    return [f.rule, f.path, f.detail]


def _receipt_dict(r: Receipt) -> dict:
    return {
        "task_id": r.task_id, "candidate_commit": r.candidate_commit, "checkset_hash": r.checkset_hash,
        "results": [{"check_id": c.check_id, "requirement_ids": list(c.requirement_ids),
                     "status": c.status.value, "visibility": c.visibility, "health": c.health.value,
                     "output_digest": c.output_digest} for c in r.results],
        "manifest_ids": sorted(r.manifest_ids),
        "audit_block_hits": list(r.audit_block_hits), "audit_flags": list(r.audit_flags),
        "sentinels_ok": r.sentinels_ok, "exit_code_consistent": r.exit_code_consistent,
        "regressions": list(r.regressions), "uncovered_requirements": list(r.uncovered_requirements),
        "dispute_open": r.dispute_open,
    }


def _receipt_from_dict(d: dict) -> Receipt:
    results = tuple(CheckResult(c["check_id"], tuple(c["requirement_ids"]), CheckStatus(c["status"]),
                                c["visibility"], CheckHealth(c["health"]), 0.0, c["output_digest"])
                    for c in d["results"])
    return Receipt(d["task_id"], d["candidate_commit"], d["checkset_hash"], results,
                   frozenset(d["manifest_ids"]), tuple(d["audit_block_hits"]), tuple(d["audit_flags"]),
                   d["sentinels_ok"], d["exit_code_consistent"], tuple(d["regressions"]),
                   tuple(d["uncovered_requirements"]), d["dispute_open"])


def load_receipt(conn: sqlite3.Connection, receipt_id: str) -> Receipt:
    """Rebuild the stored Receipt, so ``verdict(load_receipt(...))`` can be recomputed later."""
    row = conn.execute("SELECT receipt_json FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if row is None:
        raise KeyError("unknown receipt")
    return _receipt_from_dict(json.loads(row["receipt_json"])["receipt"])


def _load_health(conn: sqlite3.Connection, checkset: CheckSet) -> dict[str, CheckHealth]:
    health = {c.check_id: CheckHealth.VALID for c in checkset.checks}
    for row in conn.execute("SELECT id, health FROM checks WHERE checkset_hash=?", (checkset.hash,)):
        if row["id"] in health:
            try:
                health[row["id"]] = CheckHealth(row["health"] or "valid")
            except ValueError as e:
                raise ValueError("checks.health: unrecognised value in the state store") from e
    return health


def attribute(v: Verdict, *, results, audit_report: AuditReport, results_parsed: bool,
              timed_out: bool, receipt: Receipt) -> tuple[AttributionRecord | None, bool,
                                                          tuple[AttributionRecord, ...]]:
    """Deterministic attribution. Returns (attribution or None, environment_suspect, advisories).
    Never defaults to IMPL_WRONG: anything not matched is UNKNOWN."""
    advisories: tuple[AttributionRecord, ...] = ()
    weak = sorted(c.check_id for c in results if c.health is CheckHealth.WEAK)
    if v is Verdict.PASS and weak:
        advisories = (AttributionRecord(Attribution.CHECK_WEAK, "low", "weak_check_counted",
                                        {"checks": weak}),)
    if not timed_out and not results_parsed and not audit_report.block and v is not Verdict.ORACLE_DISPUTED:
        # The runner produced nothing. That can be a broken environment or a candidate that killed
        # the runner; nothing here proves which, so neither the environment nor the model is blamed.
        return (AttributionRecord(Attribution.UNKNOWN, "low", "no_results_parsed",
                                  {"verdict": v.value}), True, advisories)
    if v is Verdict.TAMPER:
        if audit_report.block:
            return (AttributionRecord(Attribution.IMPL_TAMPER, "high", "audit_block",
                                      {"rules": sorted({f.rule for f in audit_report.block})}),
                    False, advisories)
        return (AttributionRecord(Attribution.IMPL_TAMPER, "medium", "result_integrity", {}), False, advisories)
    if v is Verdict.OVERFIT:
        return (AttributionRecord(Attribution.IMPL_OVERFIT, "high", "unseen_case_failed",
                                  {"requirements": sorted({q for c in results if c.visibility == "hidden"
                                                           and c.status is not CheckStatus.PASS
                                                           and c.health in (CheckHealth.VALID, CheckHealth.WEAK)
                                                           for q in c.requirement_ids})}),
                False, advisories)
    if v is Verdict.FAIL:
        failing = sorted(c.check_id for c in results if c.visibility == "visible"
                         and c.status is not CheckStatus.PASS
                         and c.health in (CheckHealth.VALID, CheckHealth.WEAK))
        if not failing:                                   # cannot happen via verdict(); stay safe
            return AttributionRecord(Attribution.UNKNOWN, "low", "fail_without_counted_check", {}), False, advisories
        return (AttributionRecord(Attribution.IMPL_WRONG, "medium" if timed_out else "high",
                                  "visible_check_failed", {"checks": failing, "timed_out": timed_out}),
                False, advisories)
    if v is Verdict.REGRESSION:
        return (AttributionRecord(Attribution.IMPL_WRONG, "high", "baseline_test_regressed",
                                  {"tests": list(receipt.regressions)}), False, advisories)
    if v is Verdict.ORACLE_DISPUTED:
        return AttributionRecord(Attribution.UNKNOWN, "low", "dispute_open", {}), False, advisories
    if v is Verdict.UNVERIFIABLE:
        return (AttributionRecord(Attribution.UNKNOWN, "low", "no executable checks",
                                  {"uncovered": list(receipt.uncovered_requirements)}), False, advisories)
    if v is Verdict.PASS:
        return None, False, advisories
    return AttributionRecord(Attribution.UNKNOWN, "low", "unmatched", {}), False, advisories


_INTEGRITY_REASONS = ("sentinel pair did not behave as required",
                      "runner exit code disagrees with per-check records",
                      "executed check IDs differ from the frozen manifest")


def _safe_reasons(reasons) -> list[str]:
    out: list[str] = []
    integrity = False
    for r in reasons:
        if r in _INTEGRITY_REASONS:
            integrity = True
        elif r not in out:
            out.append(r)
    if integrity:
        out.append("the integrity checks on the verification run failed")
    return out


def _in_process_only(results, runner_of: dict[str, str]) -> tuple[str, ...]:
    """Requirements whose counted checks all passed and none of those is a contract check."""
    per_req: dict[str, list] = {}
    for r in results:
        if r.health in (CheckHealth.VALID, CheckHealth.WEAK):
            for q in r.requirement_ids:
                per_req.setdefault(q, []).append(r)
    return tuple(sorted(q for q, rs in per_req.items()
                        if all(r.status is CheckStatus.PASS for r in rs)
                        and not any(runner_of.get(r.check_id) == "contract" for r in rs)))


def _build_feedback(adapter, run, checkset, regressions, audit_report, scrub=(),
                    contract_feedback=()) -> tuple[dict, ...]:
    fb: list[dict] = [dict(x) for x in adapter.feedback_for_implementer(run, checkset)]
    seen_hidden = {tuple(x["requirement_ids"]) for x in fb if "check_id" not in x and "requirement_ids" in x}
    for x in contract_feedback:                    # unseen failures are listed once per requirement set
        if "check_id" not in x and "requirement_ids" in x:
            key = tuple(x["requirement_ids"])
            if key in seen_hidden:
                continue
            seen_hidden.add(key)
        fb.append(dict(x))
    for item in fb:                      # failure text must not carry the oracle's checkout location
        if "failure_text" in item:
            for p in scrub:
                item["failure_text"] = item["failure_text"].replace(p, "<checkout>")
    fb += [{"regression_test": t} for t in regressions]
    fb += [{"audit_block": f.rule, "path": f.path} for f in audit_report.block]
    return tuple(fb)


def render_feedback(outcome: VerificationOutcome) -> str:
    """Plain text for the implementer, at most 6,000 characters. Contains requirement ids for
    unseen failures and nothing else about hidden checks."""
    lines = [f"Verification result: {outcome.verdict.value}"]
    reasons = _safe_reasons(outcome.reasons)
    if reasons:
        lines += ["", "Reasons:"] + [f"- {r}" for r in reasons]
    blocks = [f for f in outcome.feedback if "audit_block" in f]
    if blocks:
        lines += ["", "Changes that are not allowed (they were not applied):"]
        lines += [f"- {f['audit_block']}: {f['path']}" for f in blocks]
    visible = [f for f in outcome.feedback if "check_id" in f]
    if visible:
        lines += ["", "Checks you can see that are failing:"]
        for f in visible:
            lines.append(f"- {f['check_id']} (requirements: {', '.join(f['requirement_ids'])})")
            if f.get("failure_text"):
                lines.append(f["failure_text"])
    if outcome.timed_out:
        lines += ["", "The verification run exceeded its time limit."]
    unseen = sorted({q for f in outcome.feedback if "check_id" not in f and "requirement_ids" in f
                     for q in f["requirement_ids"]})
    if unseen:
        lines += ["", "Not satisfied in cases you have not been shown (requirement ids only): "
                  + ", ".join(unseen)]
    reg = [f["regression_test"] for f in outcome.feedback if "regression_test" in f]
    if reg:
        lines += ["", "Existing tests that passed before your change and no longer pass:"]
        lines += [f"- {t}" for t in reg]
    if outcome.audit.flags:
        lines += ["", "Notes:"] + [f"- {f.rule}: {f.path} ({f.detail})" for f in outcome.audit.flags]
    text = "\n".join(lines)
    if len(text) > FEEDBACK_MAX_CHARS:
        text = text[:FEEDBACK_MAX_CHARS - 20].rstrip() + "\n[truncated]"
    return text


# ---- verify ----------------------------------------------------------------------------
def verify(conn: sqlite3.Connection, repo: TaskRepo, checkset: CheckSet, commit: str, *,
           adapter=None, include_hidden: bool = True, limits=None, allow_unsandboxed: bool = False,
           declared_scope=None, allowed_protected=(), twin_pattern=None,
           baseline: BaselineResult | None = None, dispute_open: bool = False,
           cleanup: bool = True) -> VerificationOutcome:
    """Verify ``commit`` against the frozen ``checkset``. Raises CheckStoreTampered when the
    store does not match its hash (that is not a verdict about the candidate), and
    SandboxUnavailable when there is no sandbox and ``allow_unsandboxed`` is False.

    ``baseline`` is the project's own tests on the base commit (``run_baseline``); when None it
    is computed here. ``allowed_protected`` globs exempt paths from the protected-path BLOCK
    rules in the audit, but the verification checkout still restores them to base content."""
    adapter = adapter if adapter is not None else PytestAdapter()
    if checkset.adapter != adapter.name:
        raise ValueError("the check set was frozen for a different adapter")
    if checkset.task_id != repo.task_id:
        raise ValueError("the check set belongs to a different task")
    commit = _check_hash(commit)
    checkstore.verify_integrity(checkset)                       # 1. not a candidate verdict
    mechanism = sandbox.detect_mechanism()
    if mechanism == "none" and not allow_unsandboxed:
        raise sandbox.SandboxUnavailable("no sandbox mechanism available; pass allow_unsandboxed=True "
                                         "to run with no network or filesystem isolation")
    health = _load_health(conn, checkset)
    base = repo.base_commit
    base_tree = repo.read_tree(base, adapter.is_protected)
    if baseline is None:
        baseline = run_baseline(repo, adapter, allow_unsandboxed=allow_unsandboxed, limits=limits)

    included = [c for c in checkset.checks if include_hidden or c.visibility == "visible"]
    has_pytest = any(c.runner == "pytest" for c in included)
    has_contract = any(c.runner == "contract" for c in included)
    contract = None
    checkouts = []
    try:
        # 2. check run
        co = repo.verification_checkout(commit)
        checkouts.append(co)
        prep = adapter.prepare(co, checkset, base_tree=base_tree, include_hidden=include_hidden)
        if has_pytest:
            run = adapter.run(co, checkset, include_hidden=include_hidden, limits=limits,
                              allow_unsandboxed=allow_unsandboxed)
        else:                                  # nothing in-process to run: no sentinels to fail
            run = RunReport(sentinels_ok=True, exit_code_consistent=True, results_parsed=True)
        # 3. audit
        aud = audit(repo, base, commit, adapter, prepare_report=prep, declared_scope=declared_scope,
                    allowed_protected=tuple(allowed_protected), twin_pattern=twin_pattern)
        # 3b. contract checks: the candidate runs in separate sandboxed subprocesses, in copies of
        # a fresh checkout of its own; the comparison with the expectations happens here.
        if has_contract:
            co_c = repo.verification_checkout(commit)
            checkouts.append(co_c)
            adapter.prepare(co_c, checkset, base_tree=base_tree, include_hidden=False)
            contract = ContractRunner().run(co_c, checkset, include_hidden=include_hidden, limits=limits,
                                            allow_unsandboxed=allow_unsandboxed)
        # 4. the project's pre-existing tests, in a second fresh checkout
        project = None
        if baseline.has_tests and baseline.ok:
            co2 = repo.verification_checkout(commit)
            checkouts.append(co2)
            adapter.prepare(co2, checkset, base_tree=base_tree, include_hidden=False)
            project = adapter.run_project_tests(co2, baseline.test_paths, limits=limits,
                                                allow_unsandboxed=allow_unsandboxed)
    finally:
        if cleanup:
            for c in checkouts:
                repo.cleanup_verification(c)

    notes: list[str] = []
    regressions: tuple[str, ...] = ()
    project_timed_out = False
    project_consistent = True
    if not baseline.has_tests:
        notes.append("regression detection: the project has no pre-existing tests at base")
    elif not baseline.ok:
        notes.append("regression detection unavailable: the baseline run produced no usable results")
    else:
        project_timed_out = project.timed_out
        if project.results_parsed:
            now = project.statuses
            regressions = tuple(sorted(t for t in baseline.passing if now.get(t) is not CheckStatus.PASS))
            if baseline.consistent and not project.timed_out:
                project_consistent = project.exit_code_consistent
        else:                                                    # candidate broke the run: fail closed
            regressions = tuple(sorted(baseline.passing))
            notes.append("project test run produced no results; every baseline-passing test is counted "
                         "as not passing" + (" (timed out)" if project.timed_out else ""))
        notes.append("regression detection: compared with the baseline run on the base commit")
        if baseline.flaky:
            notes.append(f"{len(baseline.flaky)} flaky baseline test(s) excluded")
        if baseline.not_passing:
            notes.append(f"{len(baseline.not_passing)} test(s) did not pass at base and are ignored "
                         "(baseline_not_passing)")

    # 5. receipt. TIMEOUT RULE: a timed-out run cannot satisfy the sentinel or exit-code checks,
    # which is not tampering; the TIMEOUT statuses produce FAIL or OVERFIT.
    # A timeout of the pytest run excuses only the pytest integrity flags; a timeout in a contract
    # check yields TIMEOUT statuses and nothing else.
    timed_out = run.timed_out or bool(contract and contract.timed_out_any)
    covered = {r for c in included for r in c.requirement_ids}
    uncovered = tuple(sorted(r.id for r in checkset.requirements if r.id not in covered))
    raw_results = sorted([*run.results, *(contract.results if contract else ())], key=lambda r: r.check_id)
    results = tuple(CheckResult(r.check_id, r.requirement_ids, r.status, r.visibility,
                                health.get(r.check_id, CheckHealth.VALID), 0.0, r.output_digest)
                    for r in raw_results)
    sentinels_ok = True if run.timed_out else run.sentinels_ok
    exit_ok = True if run.timed_out else (run.exit_code_consistent and project_consistent)
    disputed = bool(dispute_open) or has_open_dispute(conn, repo.task_id)
    receipt = Receipt(
        task_id=repo.task_id, candidate_commit=commit, checkset_hash=checkset.hash, results=results,
        manifest_ids=frozenset(c.check_id for c in included),
        audit_block_hits=tuple(f.render() for f in aud.block),
        audit_flags=tuple(f.render() for f in aud.flags),
        sentinels_ok=sentinels_ok, exit_code_consistent=exit_ok, regressions=regressions,
        uncovered_requirements=uncovered, dispute_open=disputed)

    # 6. verdict and attribution
    in_process_only = _in_process_only(results, {c.check_id: c.runner for c in checkset.checks})
    if mechanism == "none":
        notes.append("verified WITHOUT sandbox isolation: a hostile candidate could have read hidden "
                     "checks or tampered with later checks, so this verdict is not trustworthy against "
                     "one")
    if in_process_only:
        notes.append(f"PASS for {', '.join(in_process_only)} rests only on in-process checks, "
                     "which a candidate can forge")
    v, reasons = compute_verdict(receipt)
    attribution, env_suspect, advisories = attribute(
        v, results=results, audit_report=aud, results_parsed=run.results_parsed,
        timed_out=timed_out, receipt=receipt)

    # 7. persist
    env_hash = _sha(f"{sys.version}|{adapter.name}|{mechanism}".encode())
    evidence = {
        "audit": {"block": [_finding_dict(f) for f in aud.block], "flags": [_finding_dict(f) for f in aud.flags]},
        "timed_out": timed_out, "project_timed_out": project_timed_out, "mechanism": mechanism,
        "env_hash": env_hash, "adapter": adapter.name, "include_hidden": include_hidden,
        "environment_suspect": env_suspect, "results_parsed": run.results_parsed,
        "contract": {"ran": has_contract, "mechanism": contract.mechanism if contract else "",
                     "timed_out": bool(contract and contract.timed_out_any)},
        "in_process_only": list(in_process_only),
        "raw_sentinels_ok": run.sentinels_ok, "raw_exit_code_consistent": run.exit_code_consistent,
        "raw_unmatched": len(run.raw_unmatched), "notes": notes,
        "baseline": {"has_tests": baseline.has_tests, "ok": baseline.ok, "runs": baseline.runs,
                     "flaky": list(baseline.flaky), "not_passing": list(baseline.not_passing),
                     "passing": len(baseline.passing)},
        "prepare": {"restored": list(prep.restored), "deleted": list(prep.deleted),
                    "modified_protected": list(prep.modified_protected)},
    }
    body = {"receipt": _receipt_dict(receipt), "evidence": evidence}
    receipt_id = _sha(_canon(body))
    stored = {**body, "timing": {r.check_id: round(r.duration_s, 3) for r in run.results}}
    _persist(conn, receipt, receipt_id, env_hash, stored, v, reasons, attribution, advisories)

    scrub = tuple(sorted({str(c) for c in checkouts} | {os.path.realpath(c) for c in checkouts}, key=len, reverse=True))
    contract_fb = ContractRunner().feedback_for_implementer(contract, checkset) if contract else []
    feedback = _build_feedback(adapter, run, checkset, regressions, aud, scrub, contract_fb)
    return VerificationOutcome(
        receipt=receipt, receipt_id=receipt_id, verdict=v, reasons=tuple(reasons), attribution=attribution,
        audit=aud, feedback=feedback, timed_out=timed_out, environment_suspect=env_suspect,
        mechanism=mechanism, advisories=advisories, notes=tuple(notes), project_timed_out=project_timed_out,
        in_process_only=in_process_only)


def _persist(conn, receipt, receipt_id, env_hash, stored, v, reasons, attribution, advisories) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO receipts(id, task_id, candidate_commit, checkset_hash, env_hash, receipt_json,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (receipt_id, receipt.task_id, receipt.candidate_commit, receipt.checkset_hash, env_hash,
             _canon(stored).decode("ascii"), time.time()))
        if cur.rowcount:                                         # same content -> same id -> nothing more
            conn.execute("INSERT OR IGNORE INTO verdicts(receipt_id, verdict, reasons_json) VALUES (?,?,?)",
                         (receipt_id, v.value, json.dumps(list(reasons))))
            for rec in ([attribution] if attribution else []) + list(advisories):
                conn.execute("INSERT INTO attributions(receipt_id, label, confidence, rule, evidence_json)"
                             " VALUES (?,?,?,?,?)",
                             (receipt_id, rec.label.value, rec.confidence, rec.rule,
                              json.dumps(rec.evidence, sort_keys=True)))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
