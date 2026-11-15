"""Validate a draft check set on the BASE commit, before any implementation exists.

Each of ``runs`` runs uses a fresh checkout of the base commit (``checkout_factory``), prepared
and executed by the adapter exactly like a real verification. Rules per check (the rule that
fired is recorded in ``CheckValidation.rule``):

* status differs between runs                      -> FLAKY    (status_differs)
* kind "new", passes every run                     -> WEAK     (passes_before_change)
* kind "new", fails every run                      -> VALID    (fails_before_change)
* kind "new", error every run, and the failure text shows the feature is missing
  (ImportError / ModuleNotFoundError / AttributeError / NameError)
                                                   -> VALID    (missing_feature)
* kind "new", any other constant status            -> INVALID  (error_unrelated / skipped /
                                                               timeout / not_executed)
* kind "regression", passes every run              -> VALID    (regression_passes)
* kind "regression", anything else on base         -> INVALID  (regression_fails_on_base)

A sentinel or integrity failure, an unparsable results file, a timeout or a check that was
never executed sets ``environment_broken``.

TODO(milestone 0.2): stub-adversary testing (a do-nothing and a hard-coding stub must fail the
checks) and diff-scoped mutation testing are NOT implemented here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..types import CheckHealth, CheckStatus
from .model import CheckSet

ABSENT = "absent"
_EXC_LINE = re.compile(r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception))\b", re.M)
_MISSING_FEATURE = {"ImportError", "ModuleNotFoundError", "AttributeError", "NameError"}


@dataclass(frozen=True)
class CheckValidation:
    check_id: str
    health: CheckHealth
    rule: str
    reason: str
    statuses: tuple[str, ...]


@dataclass(frozen=True)
class ValidationReport:
    checks: tuple[CheckValidation, ...]
    rejected: tuple[str, ...]            # flaky or invalid check ids
    weak: tuple[str, ...]
    environment_broken: bool
    runs: int

    @property
    def ok(self) -> bool:
        return not self.rejected and not self.environment_broken

    def health_of(self, check_id: str) -> CheckValidation:
        return next(c for c in self.checks if c.check_id == check_id)


def shows_missing_feature(text: str) -> bool:
    """True if the final exception in a failure text is one a not-yet-written feature causes."""
    if "SyntaxError" in text or "IndentationError" in text:
        return False
    found = _EXC_LINE.findall(text)
    return bool(found) and found[-1].rsplit(".", 1)[-1] in _MISSING_FEATURE


def validate_on_base(adapter, checkout_factory: Callable[[], tuple], checkset: CheckSet, *,
                     runs: int = 3, allow_unsandboxed: bool = False) -> ValidationReport:
    """``checkout_factory()`` returns ``(checkout_path, base_tree)`` for a fresh checkout of the
    base commit on every call; removing the checkouts is the caller's business."""
    if runs < 2:
        raise ValueError("runs: at least 2 runs are needed to detect flakiness")
    statuses: dict[str, list[str]] = {c.check_id: [] for c in checkset.checks}
    texts: dict[str, list[str]] = {c.check_id: [] for c in checkset.checks}
    broken = False
    for _ in range(runs):
        checkout, base_tree = checkout_factory()
        adapter.prepare(checkout, checkset, base_tree=base_tree, include_hidden=True)
        rep = adapter.run(checkout, checkset, include_hidden=True, allow_unsandboxed=allow_unsandboxed)
        if (not rep.results_parsed or not rep.sentinels_ok or not rep.exit_code_consistent
                or rep.timed_out):
            broken = True
        got = {r.check_id: r.status for r in rep.results}
        for c in checkset.checks:
            st = got.get(c.check_id)
            statuses[c.check_id].append(st.value if st else ABSENT)
            texts[c.check_id].append(rep.failure_texts.get(c.check_id, ""))
            if st is None:
                broken = True

    out: list[CheckValidation] = []
    for c in checkset.checks:
        seq = tuple(statuses[c.check_id])
        out.append(_judge(c.check_id, c.kind, seq, texts[c.check_id]))
    rejected = tuple(v.check_id for v in out if v.health in (CheckHealth.FLAKY, CheckHealth.INVALID))
    weak = tuple(v.check_id for v in out if v.health is CheckHealth.WEAK)
    return ValidationReport(tuple(out), rejected, weak, broken, runs)


def _judge(check_id: str, kind: str, seq: tuple[str, ...], texts: list[str]) -> CheckValidation:
    def v(health, rule, reason):
        return CheckValidation(check_id, health, rule, reason, seq)
    if len(set(seq)) > 1:
        return v(CheckHealth.FLAKY, "status_differs", "status differs between runs")
    st = seq[0]
    if st == ABSENT:
        return v(CheckHealth.INVALID, "not_executed", "check was not executed")
    if kind == "regression":
        if st == CheckStatus.PASS.value:
            return v(CheckHealth.VALID, "regression_passes", "passes before the change")
        return v(CheckHealth.INVALID, "regression_fails_on_base", "regression check does not pass on base")
    if st == CheckStatus.PASS.value:
        return v(CheckHealth.WEAK, "passes_before_change", "passes before the change")
    if st == CheckStatus.FAIL.value:
        return v(CheckHealth.VALID, "fails_before_change", "fails before the change")
    if st == CheckStatus.ERROR.value:
        if all(shows_missing_feature(t) for t in texts):
            return v(CheckHealth.VALID, "missing_feature", "errors because the feature does not exist yet")
        return v(CheckHealth.INVALID, "error_unrelated", "errors for a reason other than a missing feature")
    if st == CheckStatus.TIMEOUT.value:
        return v(CheckHealth.INVALID, "timeout", "check timed out on base")
    return v(CheckHealth.INVALID, "skipped", "check was skipped")
