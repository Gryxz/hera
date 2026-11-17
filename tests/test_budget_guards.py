"""Caps fail closed. Every comparison with NaN is false, so a NaN cap or estimate would silently
switch a cap off; TOML accepts `nan` and `inf`, and so did `--max-usd`. A task's providers must
share one ledger, or a continuation's per-task cap would apply to some calls only."""
import math

import pytest

from hera import cli
from hera.agent.orchestrator import TaskConfigError
from hera.config import Budget, TierPrice, load
from hera.ledger import BudgetExceeded, Ledger
from test_orchestrator import GOOD, rig  # noqa: F401


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, -0.01, "3", True])
@pytest.mark.parametrize("field", ["per_task_usd", "per_day_usd", "per_month_usd"])
def test_a_cap_must_be_a_finite_amount(field, bad):
    with pytest.raises(ValueError, match=field):
        Budget(**{field: bad})


@pytest.mark.parametrize("field", ["month_reserve_fraction", "strong_tier_max_share"])
@pytest.mark.parametrize("bad", [math.nan, -0.1, 1.5])
def test_a_fraction_must_be_between_0_and_1(field, bad):
    with pytest.raises(ValueError, match=field):
        Budget(**{field: bad})


def test_zero_caps_and_fractions_are_allowed():
    Budget(per_task_usd=0, per_day_usd=0, per_month_usd=0, month_reserve_fraction=0, strong_tier_max_share=0)
    Budget(month_reserve_fraction=1, strong_tier_max_share=1)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -1.0])
def test_a_price_must_be_a_finite_amount(bad):
    with pytest.raises(ValueError, match="price"):
        TierPrice(input=bad, output=1.0)
    with pytest.raises(ValueError, match="price"):
        TierPrice(input=1.0, output=1.0, cache_read=bad)


@pytest.mark.parametrize("text", ["nan", "inf", "-1.0"])
def test_a_nan_or_infinite_cap_in_the_config_file_is_refused(tmp_path, text):
    p = tmp_path / "hera.toml"
    p.write_text(f"[budget]\nper_task_usd = {text}\n")
    with pytest.raises(ValueError, match="per_task_usd"):
        load(p)


@pytest.mark.parametrize("est", [math.nan, math.inf, -0.5])
def test_a_nan_or_negative_estimate_is_refused_before_any_cap_comparison(conn, est):
    led = Ledger(conn, Budget(), {"default": TierPrice(1.0, 1.0)})
    with pytest.raises(BudgetExceeded):
        led.check(task_id="t", tier="default", estimate_usd=est)
    led.check(task_id="t", tier="local", estimate_usd=est)       # local calls are never priced


@pytest.mark.parametrize("cmd", [["run"], ["task", "continue", "x"]])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-2", "abc"])
def test_max_usd_on_the_command_line_must_be_a_finite_amount_above_0(cmd, value, capsys):
    argv = [*cmd, "--max-usd", value]
    if cmd == ["run"]:
        argv += ["--task", "x", "--checkset", "h", "--objective", "o"]
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2
    assert "--max-usd" in capsys.readouterr().err


def test_providers_with_separate_ledgers_are_refused(rig):
    rig.add("default", "openai", list(GOOD))
    rig.add("strong", "anthropic", [])
    other = Ledger(rig.conn, Budget(per_task_usd=1000), rig.ledger.prices)
    rig.providers["strong"].ledger = other
    with pytest.raises(TaskConfigError, match="one ledger"):
        rig.run()
