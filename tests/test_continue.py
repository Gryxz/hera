"""Continuing a stopped task (`continue_task`): budget, provider-error and dispute stops continue;
every other stop is refused with nothing logged; a crash during a continuation resumes without a
repeated request. Uses the orchestrator test rig (scripted 127.0.0.1 providers on the toy repository),
whose teardown also checks that no request body ever mentions a hidden check."""
import pytest

from hera.agent.orchestrator import ContinueRefused, continue_task, load_events, run_task
from hera.config import Budget
from hera.oracle.checkstore import freeze
from hera.oracle.disputes import open_dispute, resolve_dispute
from hera.privacy.policy import Policy
from hera.types import RepoClass
from scripted_llm import Crash, step
from test_orchestrator import (BAD, DIAGNOSIS, GOOD, CrashingProvider, assert_no_duplicate_bodies,  # noqa: F401
                               body_text, cached_baseline, crash_on, last_user_text, rig)
from toyrepo import CHECK_FILES, CHECKS, REQUIREMENTS

BIG = step("", ("list_files", {}), usage=(10000, 2000))           # $0.04 at the default price
DISPUTE = [step("d", ("dispute", {"check_id": "chk-r1-basic", "requirement_id": "R-1",
                                  "argument": "the example contradicts R-1"}))]
RESOLVER = "human:ana"
IMPLEMENTER = "mock-default"


def cont(rig, **kw):
    return continue_task(rig.conn, "toy", spec=rig.spec(), providers=rig.providers, policy=rig.policy, **kw)


def snapshot(conn):
    return (conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
            conn.execute("SELECT status FROM tasks WHERE id='toy'").fetchone()[0])


def refused(rig, match, **kw):
    before = snapshot(rig.conn)
    with pytest.raises(ContinueRefused, match=match):
        cont(rig, **kw)
    assert snapshot(rig.conn) == before                      # nothing logged, nothing sent


def dispute_id(conn):
    return conn.execute("SELECT id FROM disputes ORDER BY opened_at DESC LIMIT 1").fetchone()[0]


# ---- BUDGET_CENSORED -------------------------------------------------------------------------------------
def test_a_budget_censored_attempt_continues_in_the_same_conversation_once_a_cap_leaves_room(rig, conn):
    rig.budget = Budget(per_task_usd=0.1, per_day_usd=100, per_month_usd=1000)
    default = rig.add("default", "openai", [BIG, BIG])
    out = rig.run()
    assert out.status == "BUDGET_CENSORED" and len(default.requests) == 2
    old = load_events(conn, "toy")
    refused(rig, "per-task")                                  # the cap that stopped it still leaves no room
    refused(rig, "per-task", max_usd=0.05)
    default.script.extend(GOOD)
    seen = []
    out = cont(rig, max_usd=1.0, on_event=seen.append)
    assert out.status == "PASSED" and out.verdict == "PASS" and out.attempts == 2
    new = load_events(conn, "toy")
    assert new[:len(old)] == old                              # the log is append-only
    assert [e["kind"] for e in new[len(old):]] == ["continued", "retry", "attempt_started", "attempt_ended",
                                                   "verified", "stopped"]
    assert [e["seq"] for e in seen] == [e["seq"] for e in new[len(old):]]
    (cdetail,) = [e["detail"] for e in new if e["kind"] == "continued"]
    assert cdetail["reason"] == "budget" and cdetail["max_usd"] == 1.0 and cdetail["from_status"] == "BUDGET_CENSORED"
    retry = rig.events("retry")[0]
    assert retry["feedback_kind"] == "continued" and retry["parent"] == 1 and retry["candidate"] == 1
    r2, r3 = default.requests[1]["messages"], default.requests[2]["messages"]
    assert r3[:len(r2)] == r2 and "Continued" in last_user_text(default.requests[2])
    assert len(default.requests) == 4 and default.served == 4
    assert_no_duplicate_bodies(rig.servers)
    assert rig.ledger.budget.per_task_usd == 0.1              # the raised cap was for this continuation only
    assert conn.execute("SELECT status FROM tasks WHERE id='toy'").fetchone()[0] == "PASSED"
    refused(rig, "PASSED")


def test_a_budget_stop_at_the_diagnosis_continues_with_the_diagnosis(rig, conn):
    rig.budget = Budget(strong_tier_max_share=0.0)            # no strong-tier spending at all
    default = rig.add("default", "openai", BAD + BAD + GOOD)
    strong = rig.add("strong", "openai", [step(DIAGNOSIS)])
    out = rig.run()
    assert out.status == "BUDGET_CENSORED" and out.attempts == 2 and strong.requests == []
    assert rig.events("diagnosis")[0]["skipped"] == "budget_censored"
    refused(rig, "strong-tier share")
    rig.ledger.budget = Budget()
    out = cont(rig)
    assert out.status == "PASSED" and out.attempts == 3 and len(strong.requests) == 1
    assert [d.get("skipped") or "ok" for d in rig.events("diagnosis")] == ["budget_censored", "ok"]
    assert DIAGNOSIS in default.requests[4]["messages"][1]["content"]
    assert_no_duplicate_bodies(rig.servers)


# ---- PROVIDER_ERROR --------------------------------------------------------------------------------------
def test_a_provider_error_stop_continues_from_the_next_rung(rig, conn):
    default = rig.add("default", "openai", [])                # every request fails
    strong = rig.add("strong", "openai", GOOD)
    out = rig.run()
    assert out.status == "PROVIDER_ERROR" and out.attempts == 2 and strong.requests == []
    gone = rig.providers.pop("strong")                        # no provider for the next rung any more
    refused(rig, "no provider is configured")
    rig.providers["strong"] = gone
    out = cont(rig)
    assert out.status == "PASSED" and out.attempts == 3 and len(strong.requests) == 2
    (c,) = rig.events("continued")
    assert c["reason"] == "provider_error" and c["from_status"] == "PROVIDER_ERROR"
    (esc,) = rig.events("escalate")
    assert esc["provider_key"] == "strong" and esc["rung"] == 4 and esc["candidate"] == 3
    assert [r["tier"] for r in map(dict, conn.execute("SELECT tier FROM attempts ORDER BY seq"))] == \
        ["default", "default", "strong"]
    assert len(default.requests) == 4                         # the failing provider was not asked again


def test_a_provider_error_stop_with_no_rung_left_is_refused(rig):
    rig.add("default", "openai", [])
    out = rig.run()
    assert out.status == "PROVIDER_ERROR"
    refused(rig, "no rung is left")


# ---- AWAITING_DISPUTE ------------------------------------------------------------------------------------
def test_an_unresolved_dispute_cannot_be_continued(rig, conn):
    rig.add("default", "openai", DISPUTE)
    assert rig.run().status == "AWAITING_DISPUTE"
    refused(rig, "still open")


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_an_upheld_dispute_gives_the_candidate_one_retry_that_says_so(rig, conn, kind):
    default = rig.add("default", kind, DISPUTE + GOOD)
    assert rig.run().status == "AWAITING_DISPUTE"
    resolve_dispute(conn, dispute_id(conn), outcome="upheld", resolver=RESOLVER, implementer_model=IMPLEMENTER)
    out = cont(rig)
    assert out.status == "PASSED" and out.attempts == 2
    msg = last_user_text(default.requests[1])
    assert "your dispute was reviewed and the check stands" in msg.replace("Your", "your")
    m1, m2 = default.requests[0]["messages"], default.requests[1]["messages"]
    assert m2[:len(m1)] == m1                                 # the same conversation goes on
    (c,) = rig.events("continued")
    assert c["reason"] == "dispute_upheld" and c["filed_by_attempt"] == 1 and c["outcome"] == "upheld"
    assert rig.events("retry")[0]["feedback_kind"] == "dispute_upheld"
    assert_no_duplicate_bodies(rig.servers)
    refused(rig, "PASSED")


def test_an_amended_check_is_verified_again_against_the_new_check_set(rig, conn):
    default = rig.add("default", "openai", DISPUTE + GOOD)
    assert rig.run().status == "AWAITING_DISPUTE"
    files = dict(CHECK_FILES)
    files["test_parse_amount.py"] += b"\n# amended after the dispute\n"
    new = freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=CHECKS, files=files)
    assert new.hash != rig.cs.hash
    resolve_dispute(conn, dispute_id(conn), outcome="check_amended", resolver=RESOLVER,
                    implementer_model=IMPLEMENTER, new_checkset_hash=new.hash)
    out = cont(rig)
    assert out.status == "PASSED" and out.attempts == 2
    (c,) = rig.events("continued")
    assert c["reason"] == "dispute_check_amended" and c["checkset_hash"] == new.hash
    assert c["previous_checkset_hash"] == rig.cs.hash
    verified = rig.events("verified")
    assert [v["verdict"] for v in verified] == ["FAIL", "PASS"]     # the unchanged candidate fails the amended set
    assert {conn.execute("SELECT checkset_hash FROM receipts WHERE id=?", (v["receipt_id"],)).fetchone()[0]
            for v in verified} == {new.hash}
    assert "amended after the dispute" in body_text(default.requests[1])     # the retry sees the new visible check
    assert_no_duplicate_bodies(rig.servers)


def test_an_amended_requirement_stops_the_task_as_needs_human(rig, conn):
    default = rig.add("default", "openai", DISPUTE + GOOD)
    assert rig.run().status == "AWAITING_DISPUTE"
    resolve_dispute(conn, dispute_id(conn), outcome="requirement_amended", resolver=RESOLVER,
                    implementer_model=IMPLEMENTER)
    out = cont(rig)
    assert out.status == "NEEDS_HUMAN" and len(default.requests) == 1
    kinds = rig.kinds()
    assert kinds[-2:] == ["continued", "stopped"]
    assert rig.events("stopped")[-1] == {"status": "NEEDS_HUMAN", "reason": "requirement changed: start a new task"}
    assert conn.execute("SELECT status FROM tasks WHERE id='toy'").fetchone()[0] == "NEEDS_HUMAN"
    refused(rig, "NEEDS_HUMAN")


def test_a_dispute_opened_outside_the_run_is_resolved_and_the_candidate_verified_again(rig, conn):
    default = rig.add("default", "openai", GOOD)
    open_dispute(conn, task_id="toy", checkset_hash=rig.cs.hash, check_id="chk-r2-empty",
                 requirement_id="R-2", argument="the empty case is ambiguous")
    out = rig.run()
    assert out.status == "AWAITING_DISPUTE" and out.verdict == "ORACLE_DISPUTED" and len(default.requests) == 2
    refused(rig, "still open")
    resolve_dispute(conn, dispute_id(conn), outcome="upheld", resolver=RESOLVER, implementer_model=IMPLEMENTER)
    out = cont(rig)
    assert out.status == "PASSED" and out.verdict == "PASS" and out.attempts == 1
    assert len(default.requests) == 2                          # verified again, nothing asked of the model
    assert [v["verdict"] for v in rig.events("verified")] == ["ORACLE_DISPUTED", "PASS"]
    assert rig.events("continued")[0]["filed_by_attempt"] is None


def test_a_resolved_dispute_is_used_once(rig, conn):
    default = rig.add("default", "openai", DISPUTE + DISPUTE)
    assert rig.run().status == "AWAITING_DISPUTE"
    resolve_dispute(conn, dispute_id(conn), outcome="upheld", resolver=RESOLVER, implementer_model=IMPLEMENTER)
    assert cont(rig).status == "AWAITING_DISPUTE"              # the candidate disputes again
    assert len(rig.events("dispute_filed")) == 2
    refused(rig, "still open")                                 # the first dispute is spent; the second is open


# ---- everything else is refused ----------------------------------------------------------------------------
def _passed(rig):
    rig.add("default", "openai", GOOD)
    assert rig.run().status == "PASSED"


def _failed(rig):
    rig.add("default", "openai", BAD * 4)
    assert rig.run().status == "FAILED"


def _needs_human(rig):
    rig.cs = freeze(rig.conn, task_id="toy", requirements=REQUIREMENTS, checks=CHECKS[:3],
                    files={k: v for k, v in CHECK_FILES.items() if k != "test_calc_unchanged.py"})
    rig.add("default", "openai", GOOD)
    assert rig.run().status == "NEEDS_HUMAN"


def _blocked(rig):
    rig.policy = Policy(repos={"toy": (None, RepoClass.SENSITIVE)})
    rig.add("default", "openai", GOOD)
    assert rig.run().status == "BLOCKED_PRIVACY"


@pytest.mark.parametrize("setup, status", [(_passed, "PASSED"), (_failed, "FAILED"),
                                           (_needs_human, "NEEDS_HUMAN"), (_blocked, "BLOCKED_PRIVACY")])
def test_every_other_stop_is_refused_and_nothing_is_logged(rig, setup, status):
    setup(rig)
    refused(rig, status)
    refused(rig, status, max_usd=50.0)


def test_a_task_that_has_not_stopped_or_does_not_exist_is_refused(rig, conn):
    rig.add("default", "openai", GOOD)
    with pytest.raises(ContinueRefused):
        cont(rig)                                              # no log at all
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0
    with pytest.raises(Crash):
        rig.run(on_event=crash_on("attempt_started"))
    refused(rig, "has not stopped")                            # `hera run` resumes a task that is merely interrupted


# ---- crashes during a continuation ---------------------------------------------------------------------------
@pytest.mark.parametrize("resume", ["continue", "run"])
@pytest.mark.parametrize("crash_call", [1, 2])
def test_a_crash_in_the_middle_of_a_continuation_resumes_without_a_repeated_request(rig, conn, resume, crash_call):
    rig.budget = Budget(per_task_usd=0.1, per_day_usd=100, per_month_usd=1000)
    default = rig.add("default", "openai", [BIG, BIG])
    assert rig.run().status == "BUDGET_CENSORED"
    default.script.extend(GOOD)
    real = rig.providers["default"]
    rig.providers["default"] = CrashingProvider(real, crash_call)
    with pytest.raises(Crash):
        cont(rig, max_usd=1.0)
    assert len(default.requests) == 2 + crash_call - 1
    assert rig.ledger.budget.per_task_usd == 0.1               # restored even though the process "died"
    rig.providers["default"] = real
    out = cont(rig) if resume == "continue" else rig.run()     # the cap the continuation got still applies
    assert out.status == "PASSED" and out.attempts == 2
    assert len(default.requests) == 4 and default.served == 4
    assert_no_duplicate_bodies(rig.servers)
    assert len(rig.events("continued")) == 1 and rig.kinds().count("stopped") == 2


def test_a_crash_right_after_the_continued_event_resumes(rig, conn):
    rig.add("default", "openai", [])
    strong = rig.add("strong", "openai", GOOD)
    assert rig.run().status == "PROVIDER_ERROR"
    with pytest.raises(Crash):
        cont(rig, on_event=crash_on("continued"))
    assert rig.kinds()[-1] == "continued" and strong.requests == []
    out = cont(rig)
    assert out.status == "PASSED" and len(strong.requests) == 2 and len(rig.events("continued")) == 1
    assert_no_duplicate_bodies([strong])               # (the failing default server saw its failed request again, by design)


def test_run_task_on_a_continued_task_that_has_since_stopped_returns_it_as_stored(rig):
    default = rig.add("default", "openai", DISPUTE + GOOD)
    rig.run()
    resolve_dispute(rig.conn, dispute_id(rig.conn), outcome="upheld", resolver=RESOLVER, implementer_model=IMPLEMENTER)
    first = cont(rig)
    again = run_task(rig.conn, rig.spec(), providers=rig.providers, policy=rig.policy)
    assert again.status == first.status == "PASSED" and again.events == first.events and len(default.requests) == 3
