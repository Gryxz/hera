"""Cost ledger and spending caps. The router asks `check` before every paid call;
hitting a cap ends a task as budget-censored, never as a failure."""
from __future__ import annotations
import sqlite3
import time
from .config import Budget, TierPrice
from .types import Usage


class BudgetExceeded(Exception):
    def __init__(self, cap: str, spent: float, limit: float):
        super().__init__(f"{cap} cap reached: spent ${spent:.4f} of ${limit:.2f}")
        self.cap, self.spent, self.limit = cap, spent, limit


def cost_usd(price: TierPrice, u: Usage) -> float:
    return (u.input_tokens * price.input + u.output_tokens * price.output +
            u.cache_read_tokens * price.cache_read + u.cache_write_tokens * price.cache_write) / 1e6


class Ledger:
    def __init__(self, conn: sqlite3.Connection, budget: Budget, prices: dict[str, TierPrice]):
        self.conn, self.budget, self.prices = conn, budget, prices

    # ---- recording
    def record(self, *, tier: str, usage: Usage, task_id: str | None = None, purpose: str = "",
               destination: str = "", model_id: str = "", at: float | None = None) -> float:
        if tier not in self.prices:
            raise KeyError(f"no price for tier {tier!r}; refusing to record an unpriced call")
        usd = cost_usd(self.prices[tier], usage)
        self.conn.execute(
            "INSERT INTO model_calls(at,task_id,purpose,destination,tier,model_id,input_tokens,"
            "output_tokens,cache_read_tokens,cache_write_tokens,usd) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (time.time() if at is None else at, task_id, purpose, destination, tier, model_id,
             usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
             usage.cache_write_tokens, usd))
        return usd

    # ---- totals
    def _sum(self, where: str, args: tuple) -> float:
        return self.conn.execute(f"SELECT COALESCE(SUM(usd),0) FROM model_calls WHERE {where}",
                                 args).fetchone()[0]

    def spent_task(self, task_id: str) -> float:
        return self._sum("task_id=?", (task_id,))

    def spent_since(self, since: float, tier: str | None = None) -> float:
        if tier:
            return self._sum("at>=? AND tier=?", (since, tier))
        return self._sum("at>=?", (since,))

    # ---- caps
    def check(self, *, task_id: str | None, tier: str, estimate_usd: float = 0.0,
              now: float | None = None, use_reserve: bool = False) -> None:
        """Raise BudgetExceeded if a call of roughly `estimate_usd` would cross a cap."""
        if tier == "local":
            return
        now = time.time() if now is None else now
        lt = time.localtime(now)
        day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        month_start = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
        b = self.budget
        if task_id is not None:
            s = self.spent_task(task_id)
            if s + estimate_usd > b.per_task_usd:
                raise BudgetExceeded("per-task", s, b.per_task_usd)
        s = self.spent_since(day_start)
        if s + estimate_usd > b.per_day_usd:
            raise BudgetExceeded("per-day", s, b.per_day_usd)
        month_limit = b.per_month_usd * (1.0 if use_reserve else 1.0 - b.month_reserve_fraction)
        s = self.spent_since(month_start)
        if s + estimate_usd > month_limit:
            raise BudgetExceeded("per-month", s, month_limit)
        if tier == "strong":
            strong = self.spent_since(month_start, "strong")
            limit = b.per_month_usd * b.strong_tier_max_share
            if strong + estimate_usd > limit:
                raise BudgetExceeded("strong-tier share", strong, limit)
