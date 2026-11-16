"""The task orchestrator end to end on the toy repository, against scripted 127.0.0.1 mock servers:
the recovery ladder, privacy routing, budget stops, resume after a crash, and the rule that no
request body ever contains anything about a hidden check."""
import dataclasses
import json

import pytest

from hera.agent import orchestrator as orch
from hera.agent.orchestrator import TaskConfigError, TaskSpec, load_events, run_task
from hera.config import Budget, RecoveryLimits, TierPrice
from hera.ledger import Ledger
from hera.oracle.adapters.pytest_adapter import PytestAdapter
from hera.oracle.checkstore import freeze
from hera.oracle.disputes import open_dispute
from hera.oracle.verify import run_baseline
from hera.privacy.gateway import Destination, Gateway
from hera.privacy.grants import GrantStore
from hera.privacy.labels import LabelStore
from hera.privacy.policy import Policy
from hera.providers import AnthropicProvider, OpenAICompatProvider
from hera.types import RepoClass
from scripted_llm import KEY, PRICES as BASE_PRICES, Crash, ScriptedLLM, step
from toyrepo import (CALC, CHECK_FILES, CHECKS, CORRECT_CALC, EDITED_TEST_CALC, HARDCODED_CALC,
                     REQUIREMENTS)

PRICES = {**BASE_PRICES, "strong": TierPrice(8.0, 40.0)}
OBJECTIVE = "Add parse_amount to calc.py."
WRONG_CALC = CALC + "\n\ndef parse_amount(text):\n    return 0.0\n"
DIAGNOSIS = "The function ignores its input; parse the text as a float and reject empty text."
_BASELINE = []


@pytest.fixture(autouse=True)
def cached_baseline(monkeypatch):
    """The baseline run of the project's own tests is the same for every test on the toy base."""
    def cached(repo, adapter, **kw):
        if not _BASELINE:
            _BASELINE.append(run_baseline(repo, adapter, **{**kw, "allow_unsandboxed": True}))
        return _BASELINE[0]
    monkeypatch.setattr(orch, "run_baseline", cached)


def attempt(code, *, path="calc.py", scope=("calc.py",)):
    """A scripted attempt: write one file, then submit."""
    return [step("w", ("write_file", {"path": path, "content": code})),
            step("s", ("submit", {"summary": "done", "declared_scope": list(scope)}))]


GOOD = attempt(CORRECT_CALC)
BAD = attempt(WRONG_CALC)
OVERFIT = attempt(HARDCODED_CALC)
TAMPERING = attempt(EDITED_TEST_CALC, path="tests/test_calc.py", scope=("tests/test_calc.py",))


def hidden_tokens(cs):
    out = set()
    for c in cs.checks:
        if c.visibility == "hidden":
            out |= {c.check_id, c.relpath, c.relpath.rsplit("/", 1)[-1], c.node}
    return out


class CrashingProvider:
    """Delegates to a real provider but dies (like a killed process) before call number ``n``."""
    def __init__(self, inner, n):
        self.inner, self.n, self.count = inner, n, 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def complete(self, *a, **kw):
        self.count += 1
        if self.count == self.n:
            raise Crash()
        return self.inner.complete(*a, **kw)


class Rig:
    def __init__(self, conn, toy, cs):
        self.conn, self.toy, self.cs = conn, toy, cs
        self.servers: list[ScriptedLLM] = []
        self.providers: dict = {}
        self.dests: dict = {}
        self.policy = Policy(repos={"toy": (None, RepoClass.PUBLIC)})
        self.budget = Budget()
        self.limits = RecoveryLimits()
        self.hidden_may_be_echoed = False       # only for a test where the scripted model names a hidden id itself
        self.labels = LabelStore(conn)
        self.gw = Gateway(conn, self.labels, GrantStore(conn), {}, timeout=10)
        self.ledger = None

    def add(self, role, kind, script, *, tier=None, local=False, **kw):
        llm = ScriptedLLM(kind, script, **kw)
        self.servers.append(llm)
        key = "x-api-key" if kind == "anthropic" else "Authorization"
        headers = {"content-type": "application/json", key: KEY if kind == "anthropic" else "Bearer " + KEY}
        dest = "local-llm" if role == "local" else role         # "local" itself is not a network destination
        self.gw.add_destination(Destination(dest, llm.url, headers, local=local))
        if self.ledger is None:
            self.ledger = Ledger(self.conn, self.budget, PRICES)
        tier = tier or ("local" if local else {"default_alt": "default"}.get(role, role))
        cls = AnthropicProvider if kind == "anthropic" else OpenAICompatProvider
        self.providers[role] = cls(dest, tier, f"mock-{role}", self.gw, self.ledger)
        return llm

    def spec(self, **kw):
        kw.setdefault("limits", self.limits)
        return TaskSpec(task_id="toy", repo=self.toy.repo, base_commit=self.toy.base, checkset=self.cs,
                        objective=OBJECTIVE, repo_id="toy", allow_unsandboxed=True, **kw)

    def run(self, on_event=None, **kw):
        return run_task(self.conn, self.spec(**kw), providers=self.providers, policy=self.policy,
                        on_event=on_event)

    def kinds(self):
        return [e["kind"] for e in load_events(self.conn, "toy")]

    def events(self, kind):
        return [e["detail"] for e in load_events(self.conn, "toy") if e["kind"] == kind]


@pytest.fixture
def rig(conn, toy, hera_home):
    cs = freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES))
    r = Rig(conn, toy, cs)
    yield r
    try:
        if not r.hidden_may_be_echoed:           # rule 10: nothing about a hidden check ever leaves
            tokens = hidden_tokens(cs)
            for llm in r.servers:
                for body in llm.requests:
                    text = json.dumps(body, ensure_ascii=False)
                    leaked = sorted(t for t in tokens if t in text)
                    assert not leaked, f"a request body mentions hidden check data: {leaked}"
    finally:
        for llm in r.servers:
            llm.close()


def body_text(request):
    return json.dumps(request, ensure_ascii=False)


def last_user_text(request):
    last = request["messages"][-1]
    c = last["content"]
    return c if isinstance(c, str) else json.dumps(c)


def assert_no_duplicate_bodies(servers):
    for llm in servers:
        bodies = [json.dumps(r, sort_keys=True) for r in llm.requests]
        assert len(bodies) == len(set(bodies)), "a request was sent twice"


# ---- 1. the happy path ---------------------------------------------------------------------------------
def test_default_passes_first_time(rig, conn):
    default = rig.add("default", "openai", [step("c", ("run_visible_checks", {})), *GOOD])
    strong = rig.add("strong", "openai", [])
    out = rig.run()
    assert out.status == "PASSED" and out.attempts == 1 and out.verdict == "PASS"
    assert strong.requests == [] and len(default.requests) == 3
    assert rig.kinds() == ["routing", "fresh_candidate", "attempt_started", "attempt_ended", "verified", "stopped"]
    assert len(rig.events("verified")) == 1
    v = rig.events("verified")[0]
    assert v["receipt_id"] == out.receipt_id and v["commit"] == out.final_commit
    assert conn.execute("SELECT verdict FROM verdicts WHERE receipt_id=?", (out.receipt_id,)).fetchone()[0] == "PASS"
    assert conn.execute("SELECT status FROM tasks WHERE id='toy'").fetchone()[0] == "PASSED"
    assert out.usd == pytest.approx(3 * (1000 * 2.0 + 100 * 10.0) / 1e6)
    assert "Verification result:" in body_text(default.requests[2])          # the visible-check closure ran
    (routing,) = rig.events("routing")
    assert routing["impl"] == "default" and routing["blocked"] is False
    assert rig.toy.repo.changed_paths(rig.toy.base, out.final_commit) == ["calc.py"]
    again = rig.run()
    assert again.status == "PASSED" and len(default.requests) == 3 and again.events == out.events


# ---- 2. OVERFIT: requirement-level feedback only, same conversation ---------------------------------------
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_hardcoded_implementation_is_overfit_then_retried_with_requirement_level_feedback(rig, conn, kind):
    default = rig.add("default", kind, OVERFIT + GOOD)
    out = rig.run()
    assert out.status == "PASSED" and out.attempts == 2
    assert [v["verdict"] for v in rig.events("verified")] == ["OVERFIT", "PASS"]
    (retry,) = rig.events("retry")
    assert retry["feedback_kind"] == "overfit" and retry["parent"] == 1 and retry["rung"] == 2
    r1, r2 = default.requests[1], default.requests[2]          # last request of attempt 1, first of attempt 2
    msg = last_user_text(r2)
    assert "R-1" in msg and "OVERFIT" in msg and "unseen" in msg
    for secret in ("chk-hidden-r1-seven", "test_parse_hidden_zq", "test_zq_hidden_seven", "7.0", "zq"):
        assert secret not in msg
    # the SAME conversation: attempt 1's history is a prefix of attempt 2's first request
    m1, m2 = r1["messages"], r2["messages"]
    assert m2[:len(m1)] == m1 and m2[-1]["role"] == "user"
    if kind == "openai":
        assert len(m2) == len(m1) + 3                              # reply, tool result, feedback
    assert "unsupported" in body_text(r2)                       # the model's own earlier code is in the history
    rows = [dict(r) for r in conn.execute("SELECT id, candidate, status FROM attempts ORDER BY seq")]
    assert [(r["id"], r["candidate"], r["status"]) for r in rows] == [("toy-a1", "toy-c1", "submitted"),
                                                                       ("toy-a2", "toy-c1", "submitted")]
    # attempt 2 started from attempt 1's work in the same worktree
    assert rig.toy.repo.changed_paths(rig.toy.base, out.final_commit) == ["calc.py"]
    assert "Fix the problems" in msg


def test_overfit_twice_goes_to_the_alternate_default_provider(rig):
    default = rig.add("default", "openai", OVERFIT + OVERFIT)
    alt = rig.add("default_alt", "openai", GOOD)
    out = rig.run()
    assert out.status == "PASSED" and out.attempts == 3
    assert len(default.requests) == 4 and len(alt.requests) == 2
    fresh = rig.events("fresh_candidate")
    assert [f["provider_key"] for f in fresh] == ["default", "default_alt"] and fresh[1]["candidate"] == 2
    assert len(alt.requests[0]["messages"]) == 2                 # a fresh conversation: system + task only


def test_overfit_twice_without_alt_uses_the_same_provider_fresh(rig):
    default = rig.add("default", "openai", OVERFIT + OVERFIT + GOOD)
    out = rig.run()
    assert out.status == "PASSED"
    assert [f["provider_key"] for f in rig.events("fresh_candidate")] == ["default", "default"]
    assert len(default.requests[4]["messages"]) == 2


# ---- 3. no progress: one retry, then a diagnosis by the strong tier -------------------------------------
def test_same_failing_set_skips_the_second_retry_and_asks_for_a_diagnosis(rig, conn):
    default = rig.add("default", "openai", BAD + BAD + GOOD)
    strong = rig.add("strong", "openai", [step(DIAGNOSIS)])
    out = rig.run()
    assert out.status == "PASSED" and out.attempts == 3
    assert rig.kinds() == ["routing", "fresh_candidate", "attempt_started", "attempt_ended", "verified",
                           "retry", "attempt_started", "attempt_ended", "verified", "diagnosis",
                           "fresh_candidate", "attempt_started", "attempt_ended", "verified", "stopped"]
    assert len(strong.requests) == 1
    req = strong.requests[0]
    assert "tools" not in req and "tool_choice" not in req
    assert [m["role"] for m in req["messages"]] == ["system", "user"]
    prompt = req["messages"][1]["content"]
    assert "R-1" in prompt and "parse_amount" in prompt and "+def parse_amount" in prompt   # requirements, checks, diff
    assert "Verification result: FAIL" in prompt
    calls = [dict(r) for r in conn.execute("SELECT purpose, tier FROM model_calls ORDER BY id")]
    assert [c["purpose"] for c in calls].count("diagnose") == 1
    assert [c["tier"] for c in calls if c["purpose"] == "diagnose"] == ["strong"]
    # the fresh default candidate is told the diagnosis, in a conversation of its own
    first = default.requests[4]
    assert len(first["messages"]) == 2
    user = first["messages"][1]["content"]
    assert DIAGNOSIS in user and "Diagnosis from a reviewer (advice, may be wrong)" in user
    assert [d["why"] for d in rig.events("fresh_candidate")] == ["first candidate", "after diagnosis"]
    assert rig.events("fresh_candidate")[1]["diagnosis_seq"] == 10
    assert rig.events("fresh_candidate")[1]["rung"] == 3
    assert rig.events("diagnosis")[0]["text"] == DIAGNOSIS


def test_a_changed_failing_set_earns_the_second_retry(rig):
    half = attempt(CALC + "\n\ndef parse_amount(text):\n    return float(text) if text else 0.0\n")
    default = rig.add("default", "openai", BAD + half + GOOD)
    out = rig.run()
    assert out.status == "PASSED" and out.attempts == 3 and len(rig.events("retry")) == 2
    assert rig.events("diagnosis") == []


# ---- 4. everything fails: the full ladder -----------------------------------------------------------------
def test_everything_fails_climbs_the_whole_ladder_and_ends_failed(rig, conn):
    default = rig.add("default", "openai", BAD * 4)
    strong = rig.add("strong", "openai", [step(DIAGNOSIS), *BAD])
    out = rig.run()
    assert out.status == "FAILED" and out.attempts == 5 and out.verdict == "FAIL"
    rungs = [(e["kind"], e["detail"].get("rung")) for e in load_events(conn, "toy")
             if e["kind"] in ("fresh_candidate", "retry", "diagnosis", "escalate")]
    assert rungs == [("fresh_candidate", 1), ("retry", 2), ("diagnosis", None), ("fresh_candidate", 3),
                     ("retry", 2), ("escalate", 4)]
    assert len(default.requests) == 8 and len(strong.requests) == 3        # diagnosis + the escalated attempt
    assert rig.kinds()[-1] == "stopped" and rig.events("stopped")[0]["status"] == "FAILED"
    assert conn.execute("SELECT status FROM tasks WHERE id='toy'").fetchone()[0] == "FAILED"
    assert [a["tier"] for a in map(dict, conn.execute("SELECT tier FROM attempts ORDER BY seq"))] == \
        ["default"] * 4 + ["strong"]
    again = rig.run()
    assert again.status == "FAILED" and len(default.requests) == 8 and len(strong.requests) == 3


# ---- 5. TAMPER -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("with_alt", [True, False])
def test_tamper_discards_the_candidate_and_starts_a_fresh_one(rig, conn, with_alt):
    default = rig.add("default", "openai", TAMPERING + ([] if with_alt else GOOD))
    alt = rig.add("default_alt", "openai", GOOD) if with_alt else None
    out = rig.run()
    assert out.status == "PASSED" and out.attempts == 2
    assert [v["verdict"] for v in rig.events("verified")] == ["TAMPER", "PASS"]
    assert rig.events("retry") == []                                  # a tampering candidate is never retried
    fresh = rig.events("fresh_candidate")
    assert [f["provider_key"] for f in fresh] == ["default", "default_alt" if with_alt else "default"]
    assert fresh[1]["candidate"] == 2 and fresh[1]["why"] == "tamper"
    assert len((alt or default).requests[-2:][0]["messages"]) == 2
    assert [r["candidate"] for r in map(dict, conn.execute("SELECT candidate FROM attempts ORDER BY seq"))] == \
        ["toy-c1", "toy-c2"]
    assert "skip(reason" not in body_text((alt or default).requests[-1])    # nothing of the tampered candidate


# ---- 6. disputes ------------------------------------------------------------------------------------------------
def test_dispute_about_a_visible_check_is_filed_and_the_task_waits(rig, conn):
    default = rig.add("default", "anthropic", [step("d", ("dispute", {
        "check_id": "chk-r1-basic", "requirement_id": "R-1", "argument": "the example contradicts R-1"}))])
    out = rig.run()
    assert out.status == "AWAITING_DISPUTE" and out.attempts == 1 and len(default.requests) == 1
    d = dict(conn.execute("SELECT * FROM disputes").fetchone())
    assert (d["task_id"], d["check_id"], d["requirement_id"], d["status"]) == ("toy", "chk-r1-basic", "R-1", "open")
    assert rig.kinds() == ["routing", "fresh_candidate", "attempt_started", "attempt_ended", "dispute_filed", "stopped"]
    assert conn.execute("SELECT status FROM tasks WHERE id='toy'").fetchone()[0] == "AWAITING_DISPUTE"
    assert rig.run().status == "AWAITING_DISPUTE" and len(default.requests) == 1
    assert conn.execute("SELECT COUNT(*) FROM disputes").fetchone()[0] == 1


def dispute(check_id):
    return [step("d", ("dispute", {"check_id": check_id, "requirement_id": "R-1", "argument": "I disagree"}))]


@pytest.mark.parametrize("check_id", ["chk-no-such-check", "chk-hidden-r1-seven"])
def test_dispute_naming_an_unknown_or_hidden_check_is_rejected_once_then_treated_as_no_progress(rig, conn, check_id):
    rig.hidden_may_be_echoed = check_id == "chk-hidden-r1-seven"     # the scripted model quotes its own dispute
    default = rig.add("default", "openai", dispute(check_id) + dispute(check_id) + GOOD)
    out = rig.run()
    assert out.status == "PASSED" and out.attempts == 3
    assert conn.execute("SELECT COUNT(*) FROM disputes").fetchone()[0] == 0
    assert rig.kinds().count("dispute_rejected") == 2
    (retry,) = rig.events("retry")
    assert retry["feedback_kind"] == "dispute_rejected"
    msg = last_user_text(default.requests[1])
    assert "Dispute rejected" in msg and "visible check" in msg
    assert [f["why"] for f in rig.events("fresh_candidate")] == ["first candidate", "context"]
    assert len(default.requests[2]["messages"]) == 2                  # the third attempt is a fresh conversation


def test_an_open_dispute_makes_the_verdict_oracle_disputed(rig, conn):
    default = rig.add("default", "openai", GOOD)
    open_dispute(conn, task_id="toy", checkset_hash=rig.cs.hash, check_id="chk-r2-empty",
                 requirement_id="R-2", argument="the empty case is ambiguous")
    out = rig.run()
    assert out.status == "AWAITING_DISPUTE" and out.verdict == "ORACLE_DISPUTED" and out.attempts == 1


# ---- budget -----------------------------------------------------------------------------------------------------
def test_per_task_cap_is_budget_censored_and_nothing_is_sent_beyond_it(rig, conn):
    rig.budget = Budget(per_task_usd=0.1, per_day_usd=100, per_month_usd=1000)
    default = rig.add("default", "openai", [step("", ("list_files", {}), usage=(10000, 2000))] * 10)
    strong = rig.add("strong", "openai", [step(DIAGNOSIS)])
    out = rig.run()
    assert out.status == "BUDGET_CENSORED" and len(default.requests) == 2 and strong.requests == []
    assert out.usd == pytest.approx(0.08) and out.usd <= 0.1
    assert conn.execute("SELECT status FROM tasks WHERE id='toy'").fetchone()[0] == "BUDGET_CENSORED"
    assert rig.events("stopped")[0]["status"] == "BUDGET_CENSORED"
    assert rig.run().status == "BUDGET_CENSORED" and len(default.requests) == 2


# ---- privacy routing ----------------------------------------------------------------------------------------------
def test_sensitive_repository_without_a_local_provider_is_blocked_before_any_call(rig, conn):
    rig.policy = Policy(repos={"toy": (None, RepoClass.SENSITIVE)})
    default = rig.add("default", "openai", GOOD)
    strong = rig.add("strong", "openai", [step(DIAGNOSIS)])
    out = rig.run()
    assert out.status == "BLOCKED_PRIVACY" and out.attempts == 0 and out.usd == 0
    assert default.requests == [] and strong.requests == []
    assert conn.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
    assert rig.kinds() == ["routing", "stopped"]
    (routing,) = rig.events("routing")
    assert routing["blocked"] is True and routing["repo_class"] == "sensitive"
    assert routing["permitted"] == ["local"] and routing["impl"] is None
    assert "toy" not in json.dumps(routing)                         # destination names only
    assert conn.execute("SELECT status FROM tasks WHERE id='toy'").fetchone()[0] == "BLOCKED_PRIVACY"


def test_sensitive_repository_with_a_local_provider_runs_locally_and_skips_diagnosis_and_escalation(rig, conn):
    rig.policy = Policy(repos={"toy": (None, RepoClass.SENSITIVE)})
    default = rig.add("default", "openai", GOOD)
    strong = rig.add("strong", "openai", [step(DIAGNOSIS), *GOOD])
    local = rig.add("local", "openai", GOOD, local=True)
    out = rig.run()
    assert out.status == "PASSED" and len(local.requests) == 2
    assert default.requests == [] and strong.requests == []
    (routing,) = rig.events("routing")
    assert routing["impl"] == "local" and routing["skipped_rungs"] == ["diagnosis", "escalation"]
    assert rig.events("attempt_started")[0]["tier"] == "local"
    assert out.usd == 0.0


def test_a_failing_local_task_never_touches_the_strong_provider(rig, conn):
    rig.policy = Policy(repos={"toy": (None, RepoClass.SENSITIVE)})
    default = rig.add("default", "openai", GOOD)
    strong = rig.add("strong", "openai", [step(DIAGNOSIS), *GOOD])
    local = rig.add("local", "openai", BAD * 4, local=True)
    out = rig.run()
    assert out.status == "FAILED" and out.attempts == 4 and len(local.requests) == 8
    assert default.requests == [] and strong.requests == []
    assert "diagnosis" not in rig.kinds() and "escalate" not in rig.kinds()


def test_a_grant_free_personal_repository_is_not_sent_to_a_remote_provider(rig):
    rig.policy = Policy(repos={"toy": (None, RepoClass.PERSONAL)})
    default = rig.add("default", "openai", GOOD)
    assert rig.run().status == "BLOCKED_PRIVACY" and default.requests == []


def test_no_provider_for_the_default_tier_is_a_configuration_error(rig):
    rig.add("strong", "openai", [])
    with pytest.raises(TaskConfigError):
        rig.run()


# ---- statuses and odd outcomes ---------------------------------------------------------------------------------
def test_unverifiable_checks_need_a_human(rig, conn):
    cs = freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=CHECKS[:3], files={
        k: v for k, v in CHECK_FILES.items() if k != "test_calc_unchanged.py"})      # R-3 has no check
    rig.cs = cs
    default = rig.add("default", "openai", GOOD)
    out = rig.run()
    assert out.status == "NEEDS_HUMAN" and out.verdict == "UNVERIFIABLE" and len(default.requests) == 2


def test_a_provider_that_keeps_failing_ends_as_provider_error(rig, conn):
    default = rig.add("default", "openai", [])
    alt = rig.add("default_alt", "openai", [])
    out = rig.run()
    assert out.status == "PROVIDER_ERROR" and out.attempts == 2
    assert len(default.requests) == 2 and len(alt.requests) == 2        # one fresh candidate on the other provider
    assert [e["detail"]["status"] for e in load_events(conn, "toy") if e["kind"] == "attempt_ended"] == \
        ["provider_error", "provider_error"]


@pytest.mark.parametrize("first, max_turns", [
    ([step("", ("list_files", {})), step("", ("list_files", {}))], 2),         # out_of_turns
    ([step(""), step(""), step("")], 30),                                      # no_progress
])
def test_out_of_turns_and_no_progress_get_a_fresh_candidate(rig, first, max_turns):
    rig.limits = RecoveryLimits(max_turns=max_turns)
    default = rig.add("default", "openai", first + GOOD)
    out = rig.run()
    assert out.status == "PASSED" and out.attempts == 2
    assert rig.events("attempt_ended")[0]["status"] == ("out_of_turns" if max_turns == 2 else "no_progress")
    assert [f["why"] for f in rig.events("fresh_candidate")] == ["first candidate", "context"]
    assert rig.events("retry") == []


def test_a_task_cannot_be_rerun_with_another_objective(rig):
    rig.add("default", "openai", GOOD)
    rig.run()
    with pytest.raises(TaskConfigError):
        run_task(rig.conn, dataclasses.replace(rig.spec(), objective="Something else."),
                 providers=rig.providers, policy=rig.policy)


def test_a_stopped_task_is_returned_as_stored(rig):
    default = rig.add("default", "openai", GOOD)
    first = rig.run()
    second = rig.run()
    assert second.status == first.status and second.events == first.events and len(default.requests) == 2


# ---- 9. resume after a crash --------------------------------------------------------------------------------------
def crash_on(kind, *, status=None):
    state = {"done": False}

    def on_event(ev):
        if not state["done"] and ev["kind"] == kind and (status is None or ev["detail"].get("status") == status):
            state["done"] = True
            raise Crash()
    return on_event


@pytest.fixture
def verify_spy(monkeypatch):
    calls = []
    real = orch.verify

    def spy(conn, repo, cs, commit, **kw):
        if kw.get("include_hidden"):
            calls.append(commit)
        return real(conn, repo, cs, commit, **kw)
    monkeypatch.setattr(orch, "verify", spy)
    return calls


@pytest.mark.parametrize("crash_call", [2, 3, 4])
def test_resume_after_a_crash_in_the_middle_of_an_attempt(rig, verify_spy, crash_call):
    default = rig.add("default", "openai", OVERFIT + GOOD)
    real = rig.providers["default"]
    rig.providers["default"] = CrashingProvider(real, crash_call)
    with pytest.raises(Crash):
        rig.run()
    assert len(default.requests) == crash_call - 1
    rig.providers["default"] = real
    out = rig.run()
    assert out.status == "PASSED" and out.attempts == 2
    assert len(default.requests) == 4 and default.served == 4              # exactly one request per scripted turn
    assert_no_duplicate_bodies(rig.servers)
    assert len(verify_spy) == 2
    assert rig.kinds() == ["routing", "fresh_candidate", "attempt_started", "attempt_ended", "verified", "retry",
                           "attempt_started", "attempt_ended", "verified", "stopped"]


def test_resume_after_submit_before_verify(rig, verify_spy):
    default = rig.add("default", "openai", OVERFIT + GOOD)
    with pytest.raises(Crash):
        rig.run(on_event=crash_on("attempt_ended", status="submitted"))
    assert verify_spy == [] and rig.kinds()[-1] == "attempt_ended"
    out = rig.run()
    assert out.status == "PASSED" and out.attempts == 2
    assert len(verify_spy) == 2                                             # the first candidate was verified once
    assert len(default.requests) == 4 and default.served == 4
    assert_no_duplicate_bodies(rig.servers)
    assert [v["verdict"] for v in rig.events("verified")] == ["OVERFIT", "PASS"]


def test_resume_after_verify_before_the_next_decision(rig, verify_spy):
    default = rig.add("default", "openai", OVERFIT + GOOD)
    with pytest.raises(Crash):
        rig.run(on_event=crash_on("verified"))
    assert len(verify_spy) == 1 and rig.kinds()[-1] == "verified"
    out = rig.run()
    assert out.status == "PASSED" and out.attempts == 2
    assert len(verify_spy) == 2                                             # the stored verdict was used, not repeated
    assert len(default.requests) == 4 and default.served == 4
    assert_no_duplicate_bodies(rig.servers)
    assert [v["verdict"] for v in rig.events("verified")] == ["OVERFIT", "PASS"]
    assert out.receipt_id == rig.events("verified")[1]["receipt_id"]


def test_resume_after_the_diagnosis_was_logged(rig, verify_spy):
    default = rig.add("default", "openai", BAD + BAD + GOOD)
    strong = rig.add("strong", "openai", [step(DIAGNOSIS)])
    with pytest.raises(Crash):
        rig.run(on_event=crash_on("diagnosis"))
    assert len(strong.requests) == 1
    out = rig.run()
    assert out.status == "PASSED" and len(strong.requests) == 1             # no second diagnosis call
    assert DIAGNOSIS in default.requests[4]["messages"][1]["content"]
    assert_no_duplicate_bodies(rig.servers)


def test_resume_matches_an_uninterrupted_run(conn, toy, hera_home, tmp_path):
    """Same scripts, one run straight through and one crashed in the middle: the same events in the same order."""
    def build(conn_, toy_):
        cs = freeze(conn_, task_id="toy", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES))
        r = Rig(conn_, toy_, cs)
        r.add("default", "openai", BAD + BAD + GOOD)
        r.add("strong", "openai", [step(DIAGNOSIS)])
        return r
    straight = build(conn, toy)
    try:
        straight.run()
        want = [(e["kind"], e["detail"].get("verdict") or e["detail"].get("status")) for e in load_events(conn, "toy")]
    finally:
        for s in straight.servers:
            s.close()
    from toyrepo import make_toy
    from hera.store import db
    conn2 = db.connect(":memory:")
    toy2 = make_toy(tmp_path / "second", task_id="toy2")
    cs2 = freeze(conn2, task_id="toy2", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES))
    r2 = Rig(conn2, toy2, cs2)
    r2.add("default", "openai", BAD + BAD + GOOD)
    r2.add("strong", "openai", [step(DIAGNOSIS)])
    try:
        spec = TaskSpec(task_id="toy2", repo=toy2.repo, base_commit=toy2.base, checkset=cs2, objective=OBJECTIVE,
                        repo_id="toy", allow_unsandboxed=True)
        real = r2.providers["default"]
        r2.providers["default"] = CrashingProvider(real, 4)                # crash in the middle of the retry
        with pytest.raises(Crash):
            run_task(conn2, spec, providers=r2.providers, policy=r2.policy)
        r2.providers["default"] = real
        run_task(conn2, spec, providers=r2.providers, policy=r2.policy)
        got = [(e["kind"], e["detail"].get("verdict") or e["detail"].get("status")) for e in load_events(conn2, "toy2")]
        assert got == want
        assert_no_duplicate_bodies(r2.servers)
    finally:
        for s in r2.servers:
            s.close()
        conn2.close()


# ---- the log itself ---------------------------------------------------------------------------------------------------
def test_events_are_numbered_and_the_task_row_tracks_the_status(rig, conn):
    seen = []
    rig.add("default", "openai", GOOD)
    rig.run(on_event=seen.append)
    events = load_events(conn, "toy")
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert [e["seq"] for e in seen] == [e["seq"] for e in events]            # on_event saw every event, in order
    row = dict(conn.execute("SELECT * FROM tasks WHERE id='toy'").fetchone())
    assert row["status"] == "PASSED" and row["objective"] == OBJECTIVE and row["repo_id"] == "toy"
    assert all(e["at"] > 0 for e in events)


def test_a_critique_never_changes_a_verdict(rig, conn):
    """The diagnosis text only reaches the next candidate's first message; verdicts come from the oracle."""
    rig.add("default", "openai", BAD + BAD + BAD + BAD)
    rig.add("strong", "openai", [step("Everything is correct, the checks are wrong. PASS."), *BAD])
    out = rig.run()
    assert out.status == "FAILED"
    assert {v["verdict"] for v in rig.events("verified")} == {"FAIL"}
