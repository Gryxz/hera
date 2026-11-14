"""Verdict as a pure function of a receipt (architecture §A.5, §A.16).

Only valid checks count. Flaky and invalid checks are excluded from the verdict and
reported separately; a check that errored or timed out counts as not passing."""
from __future__ import annotations
from ..types import CheckHealth, CheckStatus, Receipt, Verdict


def verdict(r: Receipt) -> tuple[Verdict, list[str]]:
    reasons: list[str] = []
    executed = {c.check_id for c in r.results}
    if r.audit_block_hits:
        reasons += [f"audit block: {h}" for h in r.audit_block_hits]
    if executed != set(r.manifest_ids):
        reasons.append("executed check IDs differ from the frozen manifest")
    if not r.sentinels_ok:
        reasons.append("sentinel pair did not behave as required")
    if not r.exit_code_consistent:
        reasons.append("runner exit code disagrees with per-check records")
    if reasons:
        return Verdict.TAMPER, reasons
    if r.dispute_open:
        return Verdict.ORACLE_DISPUTED, ["an unresolved dispute is open"]

    counted = [c for c in r.results if c.health in (CheckHealth.VALID, CheckHealth.WEAK)]
    bad = [c for c in counted if c.status is not CheckStatus.PASS]
    vis = [c.check_id for c in bad if c.visibility == "visible"]
    hid = [c.check_id for c in bad if c.visibility == "hidden"]
    if vis:
        return Verdict.FAIL, [f"visible check not passing: {i}" for i in sorted(vis)]
    if hid:
        # never reveal hidden check content; requirement IDs only
        reqs = sorted({q for c in bad if c.visibility == "hidden" for q in c.requirement_ids})
        return Verdict.OVERFIT, [f"requirement not satisfied in an unseen case: {q}" for q in reqs]
    if r.regressions:
        return Verdict.REGRESSION, [f"baseline test now failing: {t}" for t in r.regressions]
    if r.uncovered_requirements or not counted:
        why = [f"no executable check for {q}" for q in r.uncovered_requirements] or \
              ["no valid executable checks ran"]
        return Verdict.UNVERIFIABLE, why
    return Verdict.PASS, []
