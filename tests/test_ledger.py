import time
import pytest
from hera.config import Budget, TierPrice
from hera.ledger import BudgetExceeded, Ledger, cost_usd
from hera.types import Usage

PRICES = {"default": TierPrice(2.0, 10.0, 0.10, 2.50), "strong": TierPrice(8.0, 40.0, 0.20, 10.0),
          "local": TierPrice(0.0, 0.0)}


def test_cost_arithmetic():
    u = Usage(input_tokens=100_000, output_tokens=10_000, cache_read_tokens=1_000_000)
    assert cost_usd(PRICES["default"], u) == pytest.approx(0.2 + 0.1 + 0.1)


def test_record_and_task_cap(conn):
    led = Ledger(conn, Budget(per_task_usd=0.50), PRICES)
    usd = led.record(tier="default", usage=Usage(100_000, 20_000), task_id="t1")
    assert usd == pytest.approx(0.40)
    led.check(task_id="t1", tier="default", estimate_usd=0.05)
    with pytest.raises(BudgetExceeded) as e:
        led.check(task_id="t1", tier="default", estimate_usd=0.20)
    assert e.value.cap == "per-task"
    led.check(task_id="t2", tier="default", estimate_usd=0.20)     # other task unaffected


def test_day_month_and_reserve(conn):
    now = time.time()
    led = Ledger(conn, Budget(per_task_usd=100, per_day_usd=1.0, per_month_usd=10.0,
                              month_reserve_fraction=0.10), PRICES)
    led.record(tier="default", usage=Usage(450_000, 0), at=now)          # $0.90 today
    with pytest.raises(BudgetExceeded) as e:
        led.check(task_id=None, tier="default", estimate_usd=0.20, now=now)
    assert e.value.cap == "per-day"
    led2 = Ledger(conn, Budget(per_task_usd=100, per_day_usd=100, per_month_usd=1.0,
                               month_reserve_fraction=0.10), PRICES)
    with pytest.raises(BudgetExceeded) as e:
        led2.check(task_id=None, tier="default", estimate_usd=0.05, now=now)   # 0.95 > 0.90
    assert e.value.cap == "per-month"
    led2.check(task_id=None, tier="default", estimate_usd=0.05, now=now, use_reserve=True)


def test_strong_share_and_local_free(conn):
    led = Ledger(conn, Budget(per_task_usd=100, per_day_usd=100, per_month_usd=10.0,
                              strong_tier_max_share=0.35), PRICES)
    led.record(tier="strong", usage=Usage(400_000, 0))                    # $3.20
    with pytest.raises(BudgetExceeded) as e:
        led.check(task_id=None, tier="strong", estimate_usd=0.40)         # 3.60 > 3.50
    assert e.value.cap == "strong-tier share"
    led.check(task_id=None, tier="default", estimate_usd=0.40)
    led.check(task_id=None, tier="local", estimate_usd=999)


def test_unpriced_tier_is_refused(conn):
    led = Ledger(conn, Budget(), PRICES)
    with pytest.raises(KeyError):
        led.record(tier="mystery", usage=Usage(1, 1))
