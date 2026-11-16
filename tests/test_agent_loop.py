"""The implementer loop end to end against scripted mock models: success, crash and resume without a
repeated request, privacy and budget stops, and the odd things a model can do."""
import json

import pytest

from hera.agent import loop as loop_mod
from hera.agent.loop import ResumeMismatch, run_attempt
from hera.config import Budget
from hera.oracle.adapters.pytest_adapter import PytestAdapter
from hera.oracle.checkstore import freeze
from hera.oracle.verify import render_feedback, run_baseline, verify
from hera.types import RepoClass, Verdict as V
from scripted_llm import KEY, MODEL_ID, PRICES, Crash, Env, ScriptedLLM, step
from toyrepo import CHECK_FILES, CHECKS, CORRECT_CALC, REQUIREMENTS

KINDS = ["openai", "anthropic"]
_BASELINE = []


def baseline_for(toy):
    if not _BASELINE:
        _BASELINE.append(run_baseline(toy.repo, PytestAdapter(), allow_unsandboxed=True))
    return _BASELINE[0]


def good_script():
    return [
        step("Look around.", ("list_files", {"path": "."})),
        step("Read it.", ("read_file", {"path": "calc.py"})),
        step("Write it.", ("write_file", {"path": "calc.py", "content": CORRECT_CALC})),
        step("Check.", ("run_visible_checks", {})),
        step("Done.", ("submit", {"summary": "added parse_amount", "declared_scope": ["calc.py"]})),
    ]


@pytest.fixture
def llms():
    made = []

    def make(kind="openai", script=(), fail_on=None):
        m = ScriptedLLM(kind, script, fail_on)
        made.append(m)
        return m
    yield make
    for m in made:
        m.close()


@pytest.fixture
def cs(conn, hera_home):
    return freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES))


def make_env(conn, toy, llm, name="cand1", **kw):
    wt = toy.repo.new_candidate(name)
    return Env(conn, llm, wt, **kw)


def visible_verifier(conn, toy, cs):
    def run(commit):
        out = verify(conn, toy.repo, cs, commit, include_hidden=False, allow_unsandboxed=True,
                     baseline=baseline_for(toy))
        return render_feedback(out)
    return run


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


def crash_in_tools(env, nth, *, stray=False):
    """The nth tool execution (1-based, across the run) runs for real, then the process 'dies'."""
    real, state = env.toolbox.execute, {"n": 0}

    def execute(call, *, artefact_id):
        state["n"] += 1
        if state["n"] == nth:
            if stray:
                real(call, artefact_id=artefact_id)
                (env.wt.path / "stray.txt").write_text("left behind by the crashed run")
            raise Crash()
        return real(call, artefact_id=artefact_id)
    env.toolbox.execute = execute


# ---- the headline: a scripted model fixes the toy repository and the oracle passes it ----------
@pytest.mark.parametrize("kind", KINDS)
def test_scripted_model_fixes_toy_repo_and_oracle_passes(conn, toy, cs, llms, kind):
    llm = llms(kind, good_script())
    env = make_env(conn, toy, llm, verify_visible=None)
    env.toolbox.verify_visible = visible_verifier(conn, toy, cs)
    res = env.run()
    assert res.status == "submitted" and res.turns == 5 and res.reason == ""
    assert res.summary == "added parse_amount" and res.declared_scope == ("calc.py",) and res.twin_pattern == ""
    assert len(llm.requests) == 5
    # the final commit verifies with the full check set
    out = verify(conn, toy.repo, cs, res.final_commit, allow_unsandboxed=True,
                 baseline=baseline_for(toy), declared_scope=list(res.declared_scope))
    assert out.verdict is V.PASS, (out.verdict, out.reasons)
    assert toy.repo.changed_paths(toy.base, res.final_commit) == ["calc.py"]
    # the ledger has one row per model call with the right dollars
    rows = env.calls()
    assert len(rows) == 5 and all(r["task_id"] == "toy" and r["purpose"] == "implement" for r in rows)
    assert all(r["model_id"] == MODEL_ID and r["tier"] == "default" for r in rows)
    assert [r["usd"] for r in rows] == [pytest.approx((1000 * 2.0 + 100 * 10.0) / 1e6)] * 5
    assert res.usd == pytest.approx(sum(r["usd"] for r in rows)) and res.usd == pytest.approx(0.015)
    # what the model was shown
    first = llm.requests[0]
    assert len(first["tools"]) == 10
    flat = json.dumps(llm.requests[3])
    assert "list_files" in json.dumps(llm.requests[1]) and "def add" in json.dumps(llm.requests[2])
    assert "parse_amount" in flat
    assert "Verification result: PASS" in json.dumps(llm.requests[4])
    # attempt and turn rows
    a = dict(conn.execute("SELECT * FROM attempts").fetchone())
    assert (a["id"], a["task_id"], a["seq"], a["status"]) == ("toy-a1", "toy", 1, "submitted")
    assert (a["tier"], a["provider"], a["model_id"], a["candidate"]) == ("default", "mock", "mock-model", "cand1")
    assert a["base_commit"] == toy.base and a["final_commit"] == res.final_commit
    assert json.loads(a["declared_scope_json"]) == ["calc.py"] and a["started_at"] and a["ended_at"]
    turns = [dict(r) for r in conn.execute("SELECT * FROM turns ORDER BY seq")]
    assert [t["seq"] for t in turns] == [1, 2, 3, 4, 5] and all(len(t["request_sha256"]) == 64 for t in turns)
    assert turns[-1]["checkpoint_commit"] == res.final_commit
    assert all(t["reply_json"] and t["tool_results_json"] and t["checkpoint_commit"] for t in turns)
    # the key reached the server in a header only; it is nowhere in a body or in the database
    assert all(KEY in h.get("x-api-key", h.get("authorization", "")) for h in llm.headers)
    assert all(KEY not in json.dumps(b) for b in llm.requests)
    assert KEY not in "\n".join(conn.iterdump())
    # an attempt that is over is returned as stored; nothing is requested again
    assert env.run() == res and len(llm.requests) == 5


# ---- resume -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", KINDS)
def test_crash_after_turn_three_resumes_without_repeating_a_request(conn, toy, cs, llms, kind):
    llm = llms(kind, good_script())
    env = make_env(conn, toy, llm)
    env.toolbox.verify_visible = visible_verifier(conn, toy, cs)
    real = env.provider
    env.provider = CrashingProvider(real, 4)
    with pytest.raises(Crash):
        env.run()
    assert len(llm.requests) == 3 and len(env.calls()) == 3
    assert conn.execute("SELECT status FROM attempts").fetchone()[0] == "running"
    assert conn.execute("SELECT COUNT(*) FROM turns WHERE checkpoint_commit IS NOT NULL").fetchone()[0] == 3
    env.provider = real
    res = env.run()
    assert res.status == "submitted" and res.turns == 5
    assert len(llm.requests) == 5 and len(env.calls()) == 5                  # exactly one request per turn
    out = verify(conn, toy.repo, cs, res.final_commit, allow_unsandboxed=True, baseline=baseline_for(toy))
    assert out.verdict is V.PASS


def test_crash_after_reply_stored_before_tools_ran(conn, toy, cs, llms):
    llm = llms("openai", good_script())
    env = make_env(conn, toy, llm)
    env.toolbox.verify_visible = visible_verifier(conn, toy, cs)
    real_execute = env.toolbox.execute
    crash_in_tools(env, 3)                  # turn 3's write_file: reply stored, tool dies first
    with pytest.raises(Crash):
        env.run()
    assert len(llm.requests) == 3
    t3 = conn.execute("SELECT reply_json, tool_results_json FROM turns WHERE seq=3").fetchone()
    assert t3["reply_json"] and t3["tool_results_json"] is None
    env.toolbox.execute = real_execute
    res = env.run()
    assert res.status == "submitted" and len(llm.requests) == 5 and len(env.calls()) == 5
    assert toy.repo.changed_paths(toy.base, res.final_commit) == ["calc.py"]


def test_partial_tool_effects_are_undone_before_the_tools_run_again(conn, toy, cs, llms):
    llm = llms("anthropic", good_script())
    env = make_env(conn, toy, llm)
    env.toolbox.verify_visible = visible_verifier(conn, toy, cs)
    real_execute = env.toolbox.execute
    crash_in_tools(env, 3, stray=True)      # the write happened, a stray file appeared, then death
    with pytest.raises(Crash):
        env.run()
    assert (env.wt.path / "stray.txt").exists() and "parse_amount" in (env.wt.path / "calc.py").read_text()
    env.toolbox.execute = real_execute
    res = env.run()
    assert res.status == "submitted" and len(llm.requests) == 5
    assert not (env.wt.path / "stray.txt").exists()
    assert toy.repo.changed_paths(toy.base, res.final_commit) == ["calc.py"]
    assert verify(conn, toy.repo, cs, res.final_commit, allow_unsandboxed=True,
                  baseline=baseline_for(toy)).verdict is V.PASS


def test_crash_while_the_first_tools_run_resets_to_the_attempt_start(conn, toy, llms):
    llm = llms("openai", [step("w", ("write_file", {"path": "a.txt", "content": "1"})),
                          step("s", ("submit", {"summary": "x", "declared_scope": ["a.txt"]}))])
    env = make_env(conn, toy, llm)
    real_execute = env.toolbox.execute
    crash_in_tools(env, 1, stray=True)
    with pytest.raises(Crash):
        env.run()
    env.toolbox.execute = real_execute
    res = env.run()
    assert res.status == "submitted" and len(llm.requests) == 2
    assert toy.repo.changed_paths(toy.base, res.final_commit) == ["a.txt"]


def test_resume_mismatch_when_messages_change(conn, toy, llms):
    llm = llms("openai", good_script())
    env = make_env(conn, toy, llm)
    real = env.provider
    env.provider = CrashingProvider(real, 3)
    with pytest.raises(Crash):
        env.run()
    assert len(llm.requests) == 2
    env.provider = real
    with pytest.raises(ResumeMismatch):
        env.run(messages=env.messages("A different objective."))
    assert len(llm.requests) == 2
    assert conn.execute("SELECT status FROM attempts").fetchone()[0] == "running"     # nothing was changed
    real.model_id = "another-model"                                                   # also part of the hash
    with pytest.raises(ResumeMismatch):
        env.run()
    real.model_id = "mock-model"
    assert env.run().status == "submitted" and len(llm.requests) == 5


def test_resume_mismatch_when_a_stored_reply_was_altered(conn, toy, llms):
    llm = llms("openai", good_script())
    env = make_env(conn, toy, llm)
    real = env.provider
    env.provider = CrashingProvider(real, 3)
    with pytest.raises(Crash):
        env.run()
    row = conn.execute("SELECT reply_json FROM turns WHERE seq=1").fetchone()[0]
    conn.execute("UPDATE turns SET reply_json=? WHERE seq=1", (row.replace('"."', '"tests"'),))
    env.provider = real
    with pytest.raises(ResumeMismatch):
        env.run()
    assert len(llm.requests) == 2


def test_a_second_run_of_a_new_attempt_number_is_independent(conn, toy, llms):
    llm = llms("openai", good_script() + good_script())
    env = make_env(conn, toy, llm)
    assert env.run(attempt_seq=1).status == "submitted"
    assert env.run(attempt_seq=2).status == "submitted" and len(llm.requests) == 10
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 2


# ---- privacy ------------------------------------------------------------------------------------------
def test_sensitive_repository_is_blocked_on_the_first_turn(conn, toy, llms):
    llm = llms("openai", good_script())
    env = make_env(conn, toy, llm, repo_class=RepoClass.SENSITIVE, scanners=True)
    res = env.run()
    assert res.status == "blocked" and res.reason == "destination not permitted for these labels"
    assert llm.requests == [] and env.calls() == [] and res.turns == 0 and res.usd == 0
    rows = env.transmissions()
    assert [r["decision"] for r in rows] == ["blocked"]
    a = conn.execute("SELECT status, summary FROM attempts").fetchone()
    assert (a["status"], a["summary"]) == ("blocked", res.reason)
    assert env.run() == res and llm.requests == []                      # terminal: no retry on its own


def test_unknown_repository_defaults_to_sensitive_and_is_blocked(conn, toy, llms):
    llm = llms("anthropic", good_script())
    env = make_env(conn, toy, llm)
    env.policy.repos.clear()
    assert env.run().status == "blocked" and llm.requests == []


@pytest.mark.parametrize("kind", KINDS)
def test_tool_result_with_a_planted_key_is_blocked(conn, toy, llms, kind):
    key = "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5"
    llm = llms(kind, [step("", ("read_file", {"path": "settings.py"})),
                      step("", ("submit", {"summary": "x", "declared_scope": []}))])
    env = make_env(conn, toy, llm, scanners=True)
    (env.wt.path / "settings.py").write_text(f"# settings\nTOKEN = '{key}'\n")
    res = env.run()
    assert res.status == "blocked" and res.reason == "restricted data finding"
    assert len(llm.requests) == 1 and key not in json.dumps(llm.requests)
    assert [r["decision"] for r in env.transmissions()] == ["sent", "blocked"]
    assert len(env.calls()) == 1


def test_a_secret_the_model_writes_is_blocked_on_the_way_back(conn, toy, llms):
    key = "AKIA" + "TESTFAKE0000KEY1"
    llm = llms("openai", [step("", ("write_file", {"path": "cfg.py", "content": "# c\n" + key + "\n"})),
                          step("", ("submit", {"summary": "x", "declared_scope": []}))])
    env = make_env(conn, toy, llm, scanners=True)
    res = env.run()
    assert res.status == "blocked" and len(llm.requests) == 1
    assert (env.wt.path / "cfg.py").exists()          # the tool ran; only sending it back was refused


# ---- budget -------------------------------------------------------------------------------------------
def test_per_task_cap_ends_the_attempt_as_budget_censored(conn, toy, llms):
    llm = llms("openai", [step("", ("list_files", {}), usage=(10000, 2000)) for _ in range(10)])
    env = make_env(conn, toy, llm, tier="cheap", budget=Budget(per_task_usd=0.05, per_day_usd=100, per_month_usd=1000))
    res = env.run()
    assert res.status == "budget_censored" and res.reason == "per-task cap reached"
    assert len(llm.requests) == 2 and len(env.calls()) == 2 and res.turns == 2
    assert res.usd == pytest.approx(2 * (10000 * 1.0 + 2000 * 4.0) / 1e6)
    assert env.run() == res and len(llm.requests) == 2


# ---- odd model behaviour ------------------------------------------------------------------------------
def tool_contents(request):
    """The text of every tool result in a request body, either wire format."""
    out = []
    for m in request["messages"]:
        if m["role"] == "tool":
            out.append(m["content"])
        elif isinstance(m["content"], list):
            out += [b["content"] for b in m["content"] if b.get("type") == "tool_result"]
    return out


@pytest.mark.parametrize("kind", KINDS)
def test_malformed_arguments_get_an_error_result_not_a_crash(conn, toy, llms, kind):
    bad = "{not json" if kind == "openai" else "garbage"
    llm = llms(kind, [step("", ("read_file", bad)),
                      step("", ("submit", {"summary": "x", "declared_scope": []}))])
    env = make_env(conn, toy, llm)
    res = env.run()
    assert res.status == "submitted" and len(llm.requests) == 2
    (content,) = tool_contents(llm.requests[1])
    assert content.startswith("error:") and "valid JSON" in content
    assert "__invalid__" in json.dumps(llm.requests[1])          # the echo of the bad call is well-formed


@pytest.mark.parametrize("kind", KINDS)
def test_unknown_tool_and_bad_argument_types(conn, toy, llms, kind):
    llm = llms(kind, [step("", ("format_disk", {"path": "/"}), ("read_file", {"path": 12})),
                      step("", ("submit", {"summary": "x", "declared_scope": []}))])
    env = make_env(conn, toy, llm)
    assert env.run().status == "submitted"
    contents = tool_contents(llm.requests[1])
    assert len(contents) == 2 and all(c.startswith("error:") for c in contents)


@pytest.mark.parametrize("kind", KINDS)
def test_several_calls_in_one_reply_run_in_order(conn, toy, llms, kind):
    llm = llms(kind, [step("", ("write_file", {"path": "a.txt", "content": "A"}),
                           ("read_file", {"path": "a.txt"}), ("list_files", {})),
                      step("", ("submit", {"summary": "x", "declared_scope": ["a.txt"]}))])
    env = make_env(conn, toy, llm)
    assert env.run().status == "submitted"
    c = tool_contents(llm.requests[1])
    assert c[0].startswith("ok:") and c[1] == "A" and "a.txt" in c[2]


def test_submit_stops_the_remaining_calls_of_that_reply(conn, toy, llms):
    llm = llms("openai", [step("", ("submit", {"summary": "x", "declared_scope": []}),
                               ("write_file", {"path": "late.txt", "content": "x"}))])
    env = make_env(conn, toy, llm)
    assert env.run().status == "submitted"
    assert not (env.wt.path / "late.txt").exists()


def test_dispute_ends_the_attempt_and_round_trips(conn, toy, llms):
    llm = llms("anthropic", [step("", ("dispute", {"check_id": "chk-r1-basic", "requirement_id": "R-1",
                                                   "argument": "the example contradicts the requirement"}))])
    env = make_env(conn, toy, llm)
    res = env.run()
    assert res.status == "disputed" and res.summary == ""
    assert res.dispute == {"check_id": "chk-r1-basic", "requirement_id": "R-1",
                           "argument": "the example contradicts the requirement"}
    assert env.run() == res and len(llm.requests) == 1


def test_empty_replies_get_two_nudges_then_no_progress(conn, toy, llms):
    llm = llms("openai", [step(""), step("I think I am done."), step("")])
    env = make_env(conn, toy, llm)
    res = env.run()
    assert res.status == "no_progress" and res.turns == 3 and len(llm.requests) == 3
    msgs = llm.requests[2]["messages"]
    assert [m["role"] for m in msgs[-4:]] == ["assistant", "user", "assistant", "user"]
    assert msgs[-1]["content"] == "Use the tools. Call submit when you are done."
    assert msgs[-4]["content"] == "(no reply)" and msgs[-2]["content"] == "I think I am done."


@pytest.mark.parametrize("kind", KINDS)
def test_a_nudge_can_recover(conn, toy, llms, kind):
    llm = llms(kind, [step(""), step("", ("submit", {"summary": "x", "declared_scope": []}))])
    env = make_env(conn, toy, llm)
    assert env.run().status == "submitted" and len(llm.requests) == 2


def test_nudge_count_resets_after_a_tool_call(conn, toy, llms):
    llm = llms("openai", [step(""), step(""), step("", ("list_files", {})), step(""), step(""),
                          step("", ("submit", {"summary": "x", "declared_scope": []}))])
    env = make_env(conn, toy, llm)
    assert env.run().status == "submitted" and len(llm.requests) == 6


def test_provider_failing_twice_on_a_turn_is_provider_error(conn, toy, llms):
    llm = llms("openai", good_script(), fail_on={1: 500, 2: 500})
    env = make_env(conn, toy, llm)
    res = env.run()
    assert res.status == "provider_error" and res.reason == "provider error (HTTP 500)"
    assert len(llm.requests) == 2 and env.calls() == [] and llm.served == 0
    assert "scripted failure" not in res.reason


def test_one_provider_failure_is_retried_within_the_turn(conn, toy, llms):
    llm = llms("anthropic", good_script(), fail_on={3: 500})
    env = make_env(conn, toy, llm)
    res = env.run()
    assert res.status == "submitted" and len(llm.requests) == 6 and llm.served == 5
    assert len(env.calls()) == 5


def test_an_exhausted_script_is_a_provider_error(conn, toy, llms):
    llm = llms("openai", [])
    env = make_env(conn, toy, llm)
    assert env.run().status == "provider_error" and len(llm.requests) == 2


def test_turn_limit(conn, toy, llms):
    llm = llms("openai", [step("", ("list_files", {})) for _ in range(5)])
    env = make_env(conn, toy, llm)
    res = env.run(max_turns=2)
    assert res.status == "out_of_turns" and res.reason == "turn limit" and res.turns == 2
    assert len(llm.requests) == 2
    assert res.final_commit == conn.execute("SELECT checkpoint_commit FROM turns WHERE seq=2").fetchone()[0]


def test_tool_result_budget(conn, toy, llms, monkeypatch):
    monkeypatch.setattr(loop_mod, "MAX_TOOL_RESULT_BYTES", 100)
    llm = llms("openai", [step("", ("read_file", {"path": "pad.txt"})) for _ in range(5)])
    env = make_env(conn, toy, llm)
    (env.wt.path / "pad.txt").write_text("p" * 500)
    res = env.run()
    assert res.status == "out_of_turns" and res.reason == "context budget" and len(llm.requests) == 1


def test_max_tokens_and_purpose_are_passed_through(conn, toy, llms):
    llm = llms("openai", [step("", ("submit", {"summary": "x", "declared_scope": []}))])
    env = make_env(conn, toy, llm)
    env.run(max_tokens=777, purpose="implement:retry")
    assert llm.requests[0]["max_tokens"] == 777
    assert env.calls()[0]["purpose"] == "implement:retry"


def test_the_system_and_user_messages_arrive_labelled_and_first(conn, toy, llms):
    llm = llms("openai", [step("", ("submit", {"summary": "x", "declared_scope": []}))])
    env = make_env(conn, toy, llm)
    env.run()
    first = llm.requests[0]["messages"]
    assert [m["role"] for m in first] == ["system", "user"]
    assert "never edit" in first[0]["content"].lower() and "Add parse_amount" in first[1]["content"]
    sent = env.transmissions()[0]
    assert sent["decision"] == "sent" and json.loads(sent["artefacts_json"])[0]["repo_class"] == "public"


# ---- Worktree.reset_to ------------------------------------------------------------------------------
def test_worktree_reset_to(toy):
    wt = toy.repo.new_candidate("r1")
    (wt.path / "calc.py").write_text("changed\n")
    c1 = wt.commit_all("one")
    (wt.path / "calc.py").write_text("changed again\n")
    (wt.path / "untracked.txt").write_text("u")
    (wt.path / "tests" / "new_dir").mkdir()
    (wt.path / "tests" / "new_dir" / "f.txt").write_text("u")
    (wt.path / "__pycache__").mkdir()
    (wt.path / "__pycache__" / "x.pyc").write_text("u")
    assert wt.reset_to(c1) == c1 and wt.head() == c1
    assert (wt.path / "calc.py").read_text() == "changed\n"
    assert not (wt.path / "untracked.txt").exists() and not (wt.path / "tests" / "new_dir").exists()
    assert not (wt.path / "__pycache__").exists()
    assert wt.reset_to(toy.base) == toy.base and (wt.path / "calc.py").read_text().startswith("def add")
    with pytest.raises(ValueError):
        wt.reset_to("main")
    with pytest.raises(ValueError):
        wt.reset_to("0" * 40)
    with pytest.raises(ValueError):
        wt.reset_to("--hard")
