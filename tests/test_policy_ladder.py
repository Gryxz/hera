"""The recovery ladder as a pure state machine: no providers, no database, no repository."""
import pytest

from hera.agent import policy as P
from hera.agent.policy import (ALT, DEFAULT, LOCAL, STRONG, Caps, Outcome, Rules, decide, start,
                               after_diagnosis)
from hera.config import RecoveryLimits

FULL = Caps(impl_key=DEFAULT, alt=True, strong=True, local=False)
NO_ALT = Caps(impl_key=DEFAULT, alt=False, strong=True, local=False)
LIM = RecoveryLimits()


def walk(outcomes, caps=FULL, limits=LIM, rules=Rules()):
    """Feed outcomes through the machine; return the list of actions (diagnose is followed through)."""
    act, st = start(caps, limits, rules)
    acts = [act]
    for o in outcomes:
        act, st = decide(st, o if isinstance(o, Outcome) else Outcome(o), caps, limits, rules)
        acts.append(act)
        if act.kind == "diagnose":
            act, st = after_diagnosis(st, act, "ok")
            acts.append(act)
        if act.kind == "stop":
            break
    return acts


def shape(acts):
    out = []
    for a in acts:
        if a.kind == "fresh":
            out.append(f"fresh:{a.role}:{a.provider_key}" + (":diag" if a.with_diagnosis else ""))
        elif a.kind == "retry":
            out.append(f"retry:{a.feedback}")
        elif a.kind == "stop":
            out.append("stop:" + a.status)
        else:
            out.append(a.kind)
    return out


def F(*ids):
    return Outcome("FAIL", tuple(ids))


def test_start_is_rung_one_on_the_default_provider():
    act, st = start(FULL)
    assert (act.kind, act.rung, act.role, act.provider_key) == ("fresh", 1, "impl", DEFAULT)
    assert st.candidate == 1 and st.impl_candidates == 1 and st.retries_cap == 2
    act, _ = start(Caps(impl_key=LOCAL))
    assert act.provider_key == LOCAL


def test_pass_stops_and_unverifiable_and_disputes_stop_with_their_status():
    assert shape(walk(["PASS"])) == ["fresh:impl:default", "stop:PASSED"]
    assert shape(walk(["UNVERIFIABLE"]))[-1] == "stop:NEEDS_HUMAN"
    assert shape(walk(["ORACLE_DISPUTED"]))[-1] == "stop:AWAITING_DISPUTE"
    assert shape(walk([P.DISPUTE_FILED]))[-1] == "stop:AWAITING_DISPUTE"
    assert shape(walk(["budget_censored"]))[-1] == "stop:BUDGET_CENSORED"


def test_ladder_table_for_a_candidate_that_keeps_changing_its_failures():
    outs = [F("a"), F("b"), F("c"), F("d"), F("e"), F("f"), F("g")]
    assert shape(walk(outs)) == [
        "fresh:impl:default", "retry:verdict", "retry:verdict", "diagnose", "fresh:impl:default:diag",
        "retry:verdict", "fresh:strong:strong", "stop:FAILED"]


def test_the_second_retry_needs_progress_in_the_failing_set():
    # same failing set after the first retry: straight to diagnosis, only one retry
    assert shape(walk([F("a"), F("a")]))[:4] == ["fresh:impl:default", "retry:verdict", "diagnose",
                                                 "fresh:impl:default:diag"]
    # a different set after the first retry: a second retry
    assert shape(walk([F("a"), F("b")])) == ["fresh:impl:default", "retry:verdict", "retry:verdict"]
    assert shape(walk([F("a"), F("b"), F("c")]))[3] == "diagnose"
    assert [a.rung for a in walk([F("a"), F("b"), F("c")]) if a.kind != "stop"][:4] == [1, 2, 2, 3]
    # REGRESSION is treated like FAIL
    assert shape(walk([Outcome("REGRESSION", ("regression:t",)), Outcome("REGRESSION", ("regression:t",))]))[2] == "diagnose"


def test_the_candidate_after_a_diagnosis_gets_one_retry_and_a_failed_diagnosis_candidate_escalates():
    acts = walk([F("a"), F("a"), F("x"), F("y")])
    assert shape(acts) == ["fresh:impl:default", "retry:verdict", "diagnose", "fresh:impl:default:diag",
                           "retry:verdict", "fresh:strong:strong"]
    assert acts[-1].rung == 4


def test_the_escalated_candidate_gets_no_retry_and_then_the_task_fails():
    outs = [F("a"), F("a"), "TAMPER", "FAIL"]
    assert shape(walk(outs))[-3:] == ["fresh:impl:default:diag", "fresh:strong:strong", "stop:FAILED"]
    assert shape(walk(["TAMPER", "TAMPER", "TAMPER"]))[-2:] == ["fresh:strong:strong", "stop:FAILED"]


def test_overfit_gets_one_retry_with_requirement_level_feedback_then_alt_provider_then_escalation():
    outs = [Outcome("OVERFIT", ("h",))] * 5
    assert shape(walk(outs)) == ["fresh:impl:default", "retry:overfit", "fresh:impl:default_alt",
                                 "retry:overfit", "fresh:strong:strong", "stop:FAILED"]


def test_overfit_without_an_alternate_provider_uses_the_same_provider_in_a_fresh_conversation():
    outs = [Outcome("OVERFIT", ("h",))] * 2
    assert shape(walk(outs, caps=NO_ALT))[:3] == ["fresh:impl:default", "retry:overfit", "fresh:impl:default"]


def test_overfit_after_a_failing_retry_still_gets_its_overfit_retry():
    assert shape(walk([F("a"), Outcome("OVERFIT", ("h",))])) == ["fresh:impl:default", "retry:verdict",
                                                                 "retry:overfit"]


def test_tamper_discards_the_candidate_and_never_retries_it():
    assert shape(walk(["TAMPER"])) == ["fresh:impl:default", "fresh:impl:default_alt"]
    assert shape(walk(["TAMPER"], caps=NO_ALT)) == ["fresh:impl:default", "fresh:impl:default"]
    assert shape(walk(["TAMPER", "TAMPER"]))[-1] == "fresh:strong:strong"


def test_out_of_turns_and_no_progress_start_a_fresh_candidate_on_the_same_provider():
    for status in ("out_of_turns", "no_progress"):
        assert shape(walk([status])) == ["fresh:impl:default", "fresh:impl:default"]
        assert shape(walk([status, status])) == ["fresh:impl:default", "fresh:impl:default", "fresh:strong:strong"]


def test_a_rejected_dispute_is_answered_once_then_treated_as_no_progress():
    outs = [P.DISPUTE_REJECTED, P.DISPUTE_REJECTED]
    assert shape(walk(outs)) == ["fresh:impl:default", "retry:dispute_rejected", "fresh:impl:default"]


def test_provider_error_moves_to_the_next_rung_once_then_stops():
    assert shape(walk(["provider_error"])) == ["fresh:impl:default", "fresh:impl:default_alt"]
    assert shape(walk(["provider_error", "provider_error"]))[-1] == "stop:PROVIDER_ERROR"
    # on the escalated candidate there is no next rung
    assert shape(walk(["TAMPER", "TAMPER", "provider_error"]))[-1] == "stop:PROVIDER_ERROR"


def test_blocked_stops_unless_a_local_provider_can_take_over():
    assert shape(walk(["blocked"]))[-1] == "stop:BLOCKED_PRIVACY"
    caps = Caps(impl_key=DEFAULT, alt=False, strong=True, local=True)
    acts = walk(["blocked", "blocked"], caps=caps)
    assert shape(acts) == ["fresh:impl:default", "fresh:impl:local", "stop:BLOCKED_PRIVACY"]
    # after the move, diagnosis and escalation are off: a failing local candidate ends the task
    acts = walk(["blocked", "FAIL", "FAIL"], caps=caps)
    assert shape(acts)[-1] == "stop:FAILED"
    assert "diagnose" not in shape(acts) and "fresh:strong:strong" not in shape(acts)


def test_without_a_permitted_strong_provider_diagnosis_and_escalation_are_skipped():
    caps = Caps(impl_key=DEFAULT, alt=False, strong=False)
    assert shape(walk([F("a"), F("a"), F("a"), F("a")], caps=caps)) == [
        "fresh:impl:default", "retry:verdict", "fresh:impl:default", "retry:verdict", "stop:FAILED"]
    caps = Caps(impl_key=LOCAL, strong=False, local=True)
    assert shape(walk([F("a"), F("a"), F("a"), F("a")], caps=caps))[0] == "fresh:impl:local"


def test_limits_change_the_ladder_without_touching_anything_else():
    lim = RecoveryLimits(max_retries=0, max_candidates_per_tier=1)
    assert shape(walk([F("a")], limits=lim)) == ["fresh:impl:default", "fresh:strong:strong"]
    lim = RecoveryLimits(max_retries=2, max_candidates_per_tier=2)
    rules = Rules(progress_required_from_retry=99)          # progress never required
    assert shape(walk([F("a"), F("a"), F("a")], rules=rules))[:4] == ["fresh:impl:default", "retry:verdict",
                                                                      "retry:verdict", "diagnose"]
    rules = Rules(retries_strong=1)
    outs = [F("a"), F("a"), "TAMPER", F("z")]
    assert shape(walk(outs, rules=rules))[-2:] == ["fresh:strong:strong", "retry:verdict"]


def test_ladder_exhausted_is_failed_never_budget_censored():
    assert shape(walk([F("a")] * 4 + ["FAIL"] * 6))[-1] == "stop:FAILED"


def test_diagnosis_outcomes():
    caps = FULL
    act, st = start(caps)
    act, st = decide(st, F("a"), caps)                  # retry
    act, st = decide(st, F("a"), caps)                  # no progress -> diagnose
    assert act.kind == "diagnose" and act.then.with_diagnosis
    assert after_diagnosis(st, act, "ok")[0].with_diagnosis
    nxt, _ = after_diagnosis(st, act, "blocked")
    assert nxt.kind == "fresh" and not nxt.with_diagnosis
    assert after_diagnosis(st, act, "budget_censored")[0].status == P.BUDGET_CENSORED
    nxt, st2 = after_diagnosis(st, act, "provider_error")
    assert nxt.kind == "fresh" and st2.provider_errors == 1
    assert after_diagnosis(st2, act, "provider_error")[0].status == P.PROVIDER_ERROR


def test_unknown_outcome_is_an_error():
    _, st = start(FULL)
    with pytest.raises(ValueError):
        decide(st, Outcome("MAYBE"), FULL)


def test_decide_is_pure():
    act, st = start(FULL)
    a1 = decide(st, F("a"), FULL)
    a2 = decide(st, F("a"), FULL)
    assert a1 == a2 and st == start(FULL)[1]


def test_no_network_or_io_imports_in_policy():
    import ast
    from pathlib import Path
    src = Path(P.__file__).read_text(encoding="utf-8")
    mods = {n.module if isinstance(n, ast.ImportFrom) else a.name
            for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.Import, ast.ImportFrom))
            for a in (n.names if isinstance(n, ast.Import) else [n])}
    assert not ({"sqlite3", "time", "os", "subprocess"} & {m.split(".")[0] for m in mods if m})


def test_recovery_limits_come_from_the_configuration(tmp_path):
    from hera import config
    base = '[prices.tiers.default]\ninput = 1.0\noutput = 1.0\n'
    f = tmp_path / "hera.toml"
    f.write_text(base, encoding="utf-8")
    assert config.load(f).recovery == RecoveryLimits(2, 2, 30, 4096)
    f.write_text(base + "[recovery]\nmax_retries = 1\nmax_candidates_per_tier = 3\nmax_turns = 12\nmax_tokens = 999\n",
                 encoding="utf-8")
    assert config.load(f).recovery == RecoveryLimits(1, 3, 12, 999)
    for bad in ("max_retries = -1", "max_turns = 0", 'max_turns = "30"', "max_turns = true", "surprise = 1"):
        f.write_text(base + "[recovery]\n" + bad + "\n", encoding="utf-8")
        with pytest.raises(ValueError):
            config.load(f)


def test_the_example_configuration_shows_the_default_limits():
    from pathlib import Path
    from hera import config
    example = Path(__file__).resolve().parents[1] / "config" / "hera.example.toml"
    assert config.load(example).recovery == RecoveryLimits()
