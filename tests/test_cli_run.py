"""`hera checks freeze`, `hera run` and `hera task status` against scripted loopback providers
configured in a temporary hera.toml."""
import re

import pytest

from hera import cli
from scripted_llm import ScriptedLLM, step
from toyrepo import CALC, CHECK_FILES, CORRECT_CALC, make_toy

OBJECTIVE = "Add parse_amount to calc.py."
WRONG_CALC = CALC + "\n\ndef parse_amount(text):\n    return 0.0\n"

SPEC = '''
[[requirement]]
id = "R-1"
text = "parse_amount('12.50') returns 12.5"

[[requirement]]
id = "R-2"
text = "parse_amount('') raises ValueError"

[[requirement]]
id = "R-3"
text = "add is unchanged"

[[check]]
id = "chk-r1-basic"
requirements = ["R-1"]
visibility = "visible"
file = "test_parse_amount.py"
node = "test_basic_amount"

[[check]]
id = "chk-r2-empty"
requirements = ["R-2"]
visibility = "visible"
file = "test_parse_amount.py"
node = "test_empty_raises"

[[check]]
id = "chk-hidden-r1-seven"
requirements = ["R-1"]
visibility = "hidden"
file = "test_parse_hidden_zq.py"
node = "test_zq_hidden_seven"

[[check]]
id = "chk-r3-add"
requirements = ["R-3"]
visibility = "visible"
file = "test_calc_unchanged.py"
node = "test_add_unchanged"
kind = "regression"
'''

BAD_SPEC = SPEC.replace('file = "test_calc_unchanged.py"', 'file = "test_broken_regression.py"')


def attempt(code):
    return [step("w", ("write_file", {"path": "calc.py", "content": code})),
            step("s", ("submit", {"summary": "done", "declared_scope": ["calc.py"]}))]


@pytest.fixture
def world(hera_home, tmp_path, monkeypatch):
    """An initialised HERA home, the toy source repository (class public in the policy) and a spec directory."""
    toy = make_toy(tmp_path / "t")
    assert cli.main(["init"]) == 0
    (hera_home / "policy" / "policy.toml").write_text(
        f'[[repo]]\nid = "toy-src"\npath = "{toy.source}"\nclass = "public"\n', encoding="utf-8")
    specdir = tmp_path / "spec"
    specdir.mkdir()
    for name, data in CHECK_FILES.items():
        (specdir / name).write_bytes(data)
    (specdir / "test_broken_regression.py").write_text(
        "from calc import add\n\n\ndef test_add_unchanged():\n    assert add(2, 3) == 6\n", encoding="utf-8")
    (specdir / "spec.toml").write_text(SPEC, encoding="utf-8")
    (specdir / "bad.toml").write_text(BAD_SPEC, encoding="utf-8")
    monkeypatch.setenv("HERA_TEST_KEY", "invented-key-for-cli-tests-0001")
    servers = []

    def configure(default=None, strong=None, extra=""):
        parts = ['[budget]\nper_task_usd = 3.0\n',
                 '[prices]\nas_of = "2026-11-16"\n[prices.tiers.default]\ninput = 2.0\noutput = 10.0\n'
                 '[prices.tiers.strong]\ninput = 8.0\noutput = 40.0\n']
        for name, tier, script in (("default", "default", default), ("strong", "strong", strong)):
            if script is None:
                continue
            llm = ScriptedLLM("openai", script)
            servers.append(llm)
            parts.append(f'[providers.{name}]\nkind = "openai_compat"\nurl = "{llm.url}"\nmodel = "mock-{name}"\n'
                         f'key_env = "HERA_TEST_KEY"\ntier = "{tier}"\n')
        (hera_home / "hera.toml").write_text("\n".join(parts) + extra, encoding="utf-8")
        return servers
    world_ = type("World", (), {})()
    world_.toy, world_.specdir, world_.configure, world_.home = toy, specdir, configure, hera_home
    yield world_
    for s in servers:
        s.close()


def freeze(world, task, spec="spec.toml", *extra, capsys=None):
    return cli.main(["checks", "freeze", "--task", task, "--repo", str(world.toy.source),
                     "--spec", str(world.specdir / spec), "--allow-unsandboxed", *extra])


def hash_of(out):
    m = re.search(r"check-set hash: ([0-9a-f]{64})", out)
    assert m, out
    return m.group(1)


def test_freeze_refuses_an_invalid_spec_and_freezes_a_valid_one(world, capsys):
    assert freeze(world, "ck1", "bad.toml") == 3
    out = capsys.readouterr().out
    assert "chk-r3-add: invalid" in out and "regression_fails_on_base" in out and "not frozen" in out
    assert "check-set hash" not in out
    from hera import paths
    assert not (paths.checks_dir() / "ck1").exists()                         # nothing was frozen
    assert freeze(world, "ck1", "bad.toml", "--skip-validation") == 0       # an explicit override
    capsys.readouterr()
    assert freeze(world, "ck2") == 0
    out = capsys.readouterr().out
    assert "chk-r1-basic: valid" in out and "chk-hidden-r1-seven: valid" in out
    h = hash_of(out)
    assert (paths.checks_dir() / "ck2" / h / "manifest.json").is_file()
    assert "sensitive" not in out                                            # the repository is in the policy
    assert freeze(world, "ck2") == 0 and hash_of(capsys.readouterr().out) == h       # idempotent


def test_freeze_reports_spec_errors(world, capsys):
    (world.specdir / "missing.toml").write_text(SPEC.replace("test_parse_amount.py", "test_no_such_file.py"))
    assert freeze(world, "ck3", "missing.toml") == 2
    assert "inside the spec's directory" in capsys.readouterr().err
    (world.specdir / "escape.toml").write_text(SPEC.replace('file = "test_parse_amount.py"', 'file = "../x/test_a.py"'))
    assert freeze(world, "ck4", "escape.toml") == 2
    assert cli.main(["checks", "freeze", "--task", "ck5", "--repo", str(world.toy.source),
                     "--spec", str(world.specdir / "nope.toml")]) == 2


def test_run_happy_path_and_task_status(world, capsys):
    servers = world.configure(default=attempt(CORRECT_CALC), strong=[])
    assert freeze(world, "run1") == 0
    h = hash_of(capsys.readouterr().out)
    code = cli.main(["run", "--task", "run1", "--checkset", h, "--objective", OBJECTIVE, "--allow-unsandboxed"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "status: PASSED" in out and "verdict: PASS" in out and "spent: $" in out and "attempts: 1" in out
    assert "in_process_only: R-1" in out                                      # a pytest-only check set says so
    assert re.search(r"\[1\] routing: implementer runs on `default`", out)
    assert "verified: PASS" in out and "stopped: PASSED" in out
    assert servers[1].requests == []                                          # the strong server was never asked
    assert cli.main(["task", "status", "run1"]) == 0
    out = capsys.readouterr().out
    assert "[1] routing" in out and "attempt 1 started" in out and "attempt 1 verified: PASS" in out
    assert out.strip().splitlines()[-1] == "status: PASSED"
    assert cli.main(["task", "status", "nope"]) == 2
    # running a finished task again changes nothing and still exits 0
    assert cli.main(["run", "--task", "run1", "--checkset", h, "--objective", OBJECTIVE, "--allow-unsandboxed"]) == 0
    assert len(servers[0].requests) == 2


def test_run_exits_3_when_the_task_fails(world, capsys):
    servers = world.configure(default=attempt(WRONG_CALC) * 4)
    assert freeze(world, "run2", "spec.toml", "--skip-validation") == 0
    h = hash_of(capsys.readouterr().out)
    code = cli.main(["run", "--task", "run2", "--checkset", h, "--objective", OBJECTIVE, "--allow-unsandboxed"])
    out = capsys.readouterr().out
    assert code == 3 and "status: FAILED" in out and "verdict: FAIL" in out
    assert out.count("verified: FAIL") == 4
    assert len(servers[0].requests) == 8


def test_run_exits_3_when_the_cap_ends_the_task(world, capsys):
    servers = world.configure(default=attempt(CORRECT_CALC))
    assert freeze(world, "run3", "spec.toml", "--skip-validation") == 0
    h = hash_of(capsys.readouterr().out)
    code = cli.main(["run", "--task", "run3", "--checkset", h, "--objective", OBJECTIVE, "--max-usd", "0.001",
                     "--allow-unsandboxed"])
    assert code == 3 and "status: BUDGET_CENSORED" in capsys.readouterr().out
    assert servers[0].requests == []


def test_run_exits_3_for_a_repository_that_is_not_in_the_policy(world, capsys):
    servers = world.configure(default=attempt(CORRECT_CALC))
    (world.home / "policy" / "policy.toml").write_text("", encoding="utf-8")
    assert freeze(world, "run4", "spec.toml", "--skip-validation") == 0
    out = capsys.readouterr().out
    assert "counts as sensitive" in out
    h = hash_of(out)
    code = cli.main(["run", "--task", "run4", "--checkset", h, "--objective", OBJECTIVE, "--allow-unsandboxed"])
    assert code == 3 and "status: BLOCKED_PRIVACY" in capsys.readouterr().out
    assert servers[0].requests == []


def test_run_configuration_errors_exit_2(world, capsys):
    world.configure(default=attempt(CORRECT_CALC))
    assert freeze(world, "run5", "spec.toml", "--skip-validation") == 0
    h = hash_of(capsys.readouterr().out)
    assert cli.main(["run", "--task", "run5", "--checkset", "0" * 64, "--objective", OBJECTIVE]) == 2
    assert "unknown check set" in capsys.readouterr().err
    assert cli.main(["run", "--task", "other", "--checkset", h, "--objective", OBJECTIVE]) == 2
    world.configure(default=None, strong=[])                                  # no provider for the default tier
    assert cli.main(["run", "--task", "run5", "--checkset", h, "--objective", OBJECTIVE]) == 2
    assert "no provider for the default tier" in capsys.readouterr().err
    (world.home / "hera.toml").unlink()
    assert cli.main(["run", "--task", "run5", "--checkset", h, "--objective", OBJECTIVE]) == 2
