"""The recovery ladder (hypothesis H-R1) as a small, pure, data-driven state machine.

No I/O, no clock, no model: ``decide(state, outcome, ...)`` maps what just happened to what to do
next and the new state. The orchestrator replays a task's logged outcomes through the same function
to rebuild the state after a restart, so a decision is always a function of the log.

Rungs, in order:
  1  a fresh candidate from the default tier
  2  retries of the SAME candidate in the same conversation, with the oracle's feedback
  3  a one-shot diagnosis by the strong tier (a single call, no tools), then a fresh default candidate
     that is told the diagnosis
  4  escalation: a fresh strong-tier candidate; then stop

A critique or a diagnosis never changes a verdict; it only changes what the next attempt is told.
Thresholds live in ``RecoveryLimits`` (hard limits, from hera.toml) and ``Rules`` (the rest), so they
can change without touching the loop. See docs/AGENT.md for the table.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from ..config import RecoveryLimits

# task statuses
PASSED = "PASSED"
FAILED = "FAILED"
NEEDS_HUMAN = "NEEDS_HUMAN"
AWAITING_DISPUTE = "AWAITING_DISPUTE"
BUDGET_CENSORED = "BUDGET_CENSORED"
BLOCKED_PRIVACY = "BLOCKED_PRIVACY"
PROVIDER_ERROR = "PROVIDER_ERROR"
FINAL_STATUSES = (PASSED, FAILED, NEEDS_HUMAN, AWAITING_DISPUTE, BUDGET_CENSORED, BLOCKED_PRIVACY,
                  PROVIDER_ERROR)

# provider roles ("keys")
DEFAULT, ALT, LOCAL, STRONG = "default", "default_alt", "local", "strong"

# outcome kinds that are not oracle verdicts or attempt statuses
DISPUTE_FILED = "dispute_filed"
DISPUTE_REJECTED = "dispute_rejected"

FEEDBACK_VERDICT = "verdict"            # the oracle's rendered feedback
FEEDBACK_OVERFIT = "overfit"            # requirement-level feedback only
FEEDBACK_DISPUTE_REJECTED = "dispute_rejected"
FEEDBACK_DISPUTE_UPHELD = "dispute_upheld"      # a continued task: the dispute was reviewed, the check stands
FEEDBACK_CONTINUED = "continued"                # a continued task: the same candidate goes on after a cap stop


@dataclass(frozen=True)
class Rules:
    """Thresholds beyond the hard limits (every number can be changed here)."""
    retries_after_diagnosis: int = 1    # retries allowed for the candidate that follows a diagnosis
    retries_fresh: int = 1              # ... for any other fresh default-tier candidate
    retries_strong: int = 0             # ... for the escalated strong-tier candidate
    overfit_retries: int = 1            # OVERFIT: retries with requirement-level feedback
    dispute_rejection_retries: int = 1  # a rejected dispute is answered once
    progress_required_from_retry: int = 2   # the Nth retry only if the failing set changed
    diagnosis_max_tokens: int = 2048


@dataclass(frozen=True)
class Caps:
    """What the privacy routing made available. Fixed for the life of a task (it is logged)."""
    impl_key: str = DEFAULT      # "default", or "local" when the default provider is not permitted
    alt: bool = False            # a permitted `default_alt` provider exists
    strong: bool = False         # a permitted strong provider exists (diagnosis and escalation)
    local: bool = False          # a local provider exists (where privacy refuses a send mid-task)


@dataclass(frozen=True)
class LadderState:
    candidate: int = 0               # number of the current candidate (1-based)
    role: str = "impl"               # "impl" (default tier or local) | "strong"
    provider_key: str = DEFAULT
    impl_candidates: int = 0
    strong_candidates: int = 0
    retries_used: int = 0
    retries_cap: int = 0
    overfit_retried: bool = False
    dispute_rejections: int = 0
    prev_failing: tuple | None = None
    diagnosed: bool = False
    provider_errors: int = 0
    blocked_moves: int = 0
    local_only: bool = False         # privacy refused a send; everything from now on stays local


@dataclass(frozen=True)
class Outcome:
    """What just happened: an oracle verdict value (PASS, FAIL, ...), an attempt status (out_of_turns,
    no_progress, blocked, budget_censored, provider_error), or DISPUTE_FILED / DISPUTE_REJECTED.
    ``failing`` is the sorted tuple of failing check ids of a verification."""
    kind: str
    failing: tuple = ()


@dataclass(frozen=True)
class Action:
    kind: str                        # "fresh" | "retry" | "diagnose" | "stop"
    rung: int = 0
    role: str = ""                   # fresh: "impl" | "strong"
    provider_key: str = ""           # fresh
    feedback: str = ""               # retry: FEEDBACK_*
    status: str = ""                 # stop
    why: str = ""
    with_diagnosis: bool = False     # fresh: the first message carries the diagnosis
    then: "Action | None" = None     # diagnose: what follows the call


def _stop(status: str, why: str = "") -> Action:
    return Action("stop", status=status, why=why)


def _fresh_state(st: LadderState, role: str, key: str, cap: int) -> LadderState:
    return replace(st, candidate=st.candidate + 1, role=role, provider_key=key,
                   impl_candidates=st.impl_candidates + (role == "impl"),
                   strong_candidates=st.strong_candidates + (role == "strong"),
                   retries_used=0, retries_cap=cap, overfit_retried=False, dispute_rejections=0,
                   prev_failing=None)


def start(caps: Caps, limits: RecoveryLimits = RecoveryLimits(), rules: Rules = Rules()
          ) -> tuple[Action, LadderState]:
    """Rung 1: the first fresh candidate."""
    st = _fresh_state(LadderState(), "impl", caps.impl_key, limits.max_retries)
    return Action("fresh", rung=1, role="impl", provider_key=caps.impl_key, why="first candidate"), st


def _advance(st: LadderState, why: str, caps: Caps, limits: RecoveryLimits, rules: Rules
             ) -> tuple[Action, LadderState]:
    """The candidate is finished without passing: move down the ladder. ``why`` is one of
    "fail" (retries used up or no progress), "overfit", "tamper", "context" (out_of_turns,
    no_progress, a second rejected dispute) and "provider"."""
    final = PROVIDER_ERROR if why == "provider" else FAILED
    if st.role == "strong":
        return _stop(final, "the escalated candidate did not pass"), st
    room = st.impl_candidates < limits.max_candidates_per_tier
    if st.local_only:
        key = LOCAL
        if room:
            return (Action("fresh", rung=3, role="impl", provider_key=key, why=why),
                    _fresh_state(st, "impl", key, min(limits.max_retries, rules.retries_fresh)))
        return _stop(final, "the ladder is exhausted"), st
    if why == "fail" and room and not st.diagnosed and caps.strong:
        key = caps.impl_key
        fresh = Action("fresh", rung=3, role="impl", provider_key=key, why="after diagnosis", with_diagnosis=True)
        st = _fresh_state(replace(st, diagnosed=True), "impl", key,
                          min(limits.max_retries, rules.retries_after_diagnosis))
        return Action("diagnose", rung=3, why=why, then=fresh), st
    if room:
        key = ALT if (why in ("overfit", "tamper", "provider") and caps.alt) else caps.impl_key
        return (Action("fresh", rung=3, role="impl", provider_key=key, why=why),
                _fresh_state(st, "impl", key, min(limits.max_retries, rules.retries_fresh)))
    if caps.strong and st.strong_candidates < limits.max_candidates_per_tier:
        return (Action("fresh", rung=4, role="strong", provider_key=STRONG, why=why),
                _fresh_state(st, "strong", STRONG, min(limits.max_retries, rules.retries_strong)))
    return _stop(final, "the ladder is exhausted"), st


def decide(st: LadderState, out: Outcome, caps: Caps, limits: RecoveryLimits = RecoveryLimits(),
           rules: Rules = Rules()) -> tuple[Action, LadderState]:
    """What to do after ``out``, and the state after doing it."""
    k = out.kind
    if k == "PASS":
        return _stop(PASSED), st
    if k == "UNVERIFIABLE":
        return _stop(NEEDS_HUMAN, "the checks cannot verify the requirements"), st
    if k in ("ORACLE_DISPUTED", DISPUTE_FILED):
        return _stop(AWAITING_DISPUTE, "a dispute is open"), st
    if k == "budget_censored":
        return _stop(BUDGET_CENSORED, "a spending cap was reached"), st
    if k == "blocked":
        if caps.local and st.provider_key != LOCAL and st.blocked_moves == 0 \
                and st.impl_candidates < limits.max_candidates_per_tier:
            nxt = replace(st, blocked_moves=st.blocked_moves + 1, local_only=True)
            return (Action("fresh", rung=3, role="impl", provider_key=LOCAL, why="privacy refused a send"),
                    _fresh_state(nxt, "impl", LOCAL, min(limits.max_retries, rules.retries_fresh)))
        return _stop(BLOCKED_PRIVACY, "the privacy gateway refused a send"), st
    if k == "provider_error":
        if st.provider_errors >= 1:
            return _stop(PROVIDER_ERROR, "the provider failed again"), st
        return _advance(replace(st, provider_errors=st.provider_errors + 1), "provider", caps, limits, rules)
    if k in ("FAIL", "REGRESSION"):
        n = st.retries_used
        can = n < st.retries_cap and (n + 1 < rules.progress_required_from_retry
                                      or out.failing != st.prev_failing)
        st = replace(st, prev_failing=out.failing)
        if can:
            return Action("retry", rung=2, feedback=FEEDBACK_VERDICT, why=k), replace(st, retries_used=n + 1)
        return _advance(st, "fail", caps, limits, rules)
    if k == "OVERFIT":
        st = replace(st, prev_failing=out.failing)
        if st.retries_used < st.retries_cap and (1 if st.overfit_retried else 0) < rules.overfit_retries:
            return (Action("retry", rung=2, feedback=FEEDBACK_OVERFIT, why=k),
                    replace(st, retries_used=st.retries_used + 1, overfit_retried=True))
        return _advance(st, "overfit", caps, limits, rules)
    if k == "TAMPER":
        return _advance(st, "tamper", caps, limits, rules)
    if k in ("out_of_turns", "no_progress"):
        return _advance(st, "context", caps, limits, rules)
    if k == DISPUTE_REJECTED:
        if st.dispute_rejections < rules.dispute_rejection_retries and st.retries_used < st.retries_cap:
            return (Action("retry", rung=2, feedback=FEEDBACK_DISPUTE_REJECTED, why=k),
                    replace(st, retries_used=st.retries_used + 1, dispute_rejections=st.dispute_rejections + 1))
        return _advance(st, "context", caps, limits, rules)        # treated as no_progress
    raise ValueError(f"unknown outcome {k!r}")


def next_after_provider_error(st: LadderState, caps: Caps, limits: RecoveryLimits = RecoveryLimits(),
                              rules: Rules = Rules()) -> tuple[Action, LadderState]:
    """Continuing a task that stopped as PROVIDER_ERROR: the rung the ladder would have moved to. A
    ``stop`` result means no rung is left (nothing to continue)."""
    return _advance(st, "provider", caps, limits, rules)


def after_diagnosis(st: LadderState, act: Action, result: str) -> tuple[Action, LadderState]:
    """Continue after the diagnosis call: ``result`` is "ok", "blocked" (the gateway refused: go on
    without it), "provider_error" (go on without it once, then PROVIDER_ERROR) or "budget_censored"."""
    assert act.kind == "diagnose" and act.then is not None
    if result == "ok":
        return act.then, st
    if result == "budget_censored":
        return _stop(BUDGET_CENSORED, "a spending cap was reached"), st
    if result == "provider_error":
        if st.provider_errors >= 1:
            return _stop(PROVIDER_ERROR, "the provider failed again"), st
        st = replace(st, provider_errors=st.provider_errors + 1)
    return replace(act.then, with_diagnosis=False), st
