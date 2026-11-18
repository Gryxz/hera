"""`hera task continue`, `hera dispute ...` and `hera report` around a scripted loopback provider
(the same world as test_cli_run)."""
import re

from hera import cli
from scripted_llm import step
from test_cli_run import OBJECTIVE, attempt, freeze as freeze_spec, hash_of, world  # noqa: F401
from toyrepo import CORRECT_CALC

DISPUTE = [step("d", ("dispute", {"check_id": "chk-r1-basic", "requirement_id": "R-1",
                                  "argument": "the example contradicts R-1"}))]


def run(world, capsys, task, *extra):
    assert freeze_spec(world, task, "spec.toml", "--skip-validation") == 0
    h = hash_of(capsys.readouterr().out)
    code = cli.main(["run", "--task", task, "--checkset", h, "--objective", OBJECTIVE, "--allow-unsandboxed", *extra])
    return code, capsys.readouterr().out


def test_a_budget_censored_task_continues_with_a_raised_cap(world, capsys):
    servers = world.configure(default=attempt(CORRECT_CALC))
    code, out = run(world, capsys, "cb1", "--max-usd", "0.001")
    assert code == 3 and "status: BUDGET_CENSORED" in out and servers[0].requests == []
    assert cli.main(["task", "continue", "cb1", "--max-usd", "1.5", "--allow-unsandboxed"]) == 0
    out = capsys.readouterr().out
    assert "continued after BUDGET_CENSORED (budget): cap $1.5000 for this continuation" in out
    assert "status: PASSED" in out and "verdict: PASS" in out
    assert cli.main(["task", "status", "cb1"]) == 0
    out = capsys.readouterr().out
    assert out.count("stopped:") == 2 and "continued after" in out and out.strip().splitlines()[-1] == "status: PASSED"
    assert len(servers[0].requests) == 2
    assert cli.main(["task", "continue", "cb1"]) == 3                        # a passed task is not continued
    err = capsys.readouterr().err
    assert "not continued" in err and "PASSED" in err
    assert cli.main(["task", "status", "cb1"]) == 0
    assert capsys.readouterr().out.count("[") == out.count("[")             # nothing was logged by the refusal


def test_continue_with_an_unknown_task_or_no_configuration(world, capsys):
    world.configure(default=attempt(CORRECT_CALC))
    assert cli.main(["task", "continue", "nope"]) == 2
    assert "no such task" in capsys.readouterr().err
    (world.home / "hera.toml").unlink()
    assert cli.main(["task", "continue", "nope"]) == 2


def test_a_disputed_task_is_listed_resolved_and_continued(world, capsys):
    servers = world.configure(default=DISPUTE)
    code, out = run(world, capsys, "cd1")
    assert code == 3 and "status: AWAITING_DISPUTE" in out
    assert cli.main(["task", "continue", "cd1", "--allow-unsandboxed"]) == 3
    assert "still open" in capsys.readouterr().err
    assert cli.main(["dispute", "list", "--open"]) == 0
    listing = capsys.readouterr().out
    did = re.search(r"(disp-[0-9a-f]+)  task=cd1  check=chk-r1-basic", listing).group(1)
    assert "argument: the example contradicts R-1" in listing
    assert cli.main(["dispute", "resolve", did, "--outcome", "upheld", "--resolver", "human:ana"]) == 0
    capsys.readouterr()
    servers[0].script.extend(attempt(CORRECT_CALC))
    assert cli.main(["task", "continue", "cd1", "--allow-unsandboxed"]) == 0
    out = capsys.readouterr().out
    assert f"dispute {did}: upheld" in out and "status: PASSED" in out
    assert "the check stands" in str(servers[0].requests[1]["messages"][-1]["content"])
    assert cli.main(["dispute", "list", "--open"]) == 0
    assert "no open disputes" in capsys.readouterr().out


def test_report_after_a_cli_run(world, capsys):
    world.configure(default=attempt(CORRECT_CALC))
    code, _ = run(world, capsys, "cr1")
    assert code == 0
    assert cli.main(["report", "--task", "cr1"]) == 0
    out = capsys.readouterr().out
    assert "attempts: 1 over 1 task(s)" in out and "UNKNOWN attribution rate: 0/0 (n<5)" in out
    assert "first-attempt pass rate: 1/1 (100%) (n<5)" in out
