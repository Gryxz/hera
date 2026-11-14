from __future__ import annotations
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Budget:
    per_task_usd: float = 3.0
    per_day_usd: float = 4.0
    per_month_usd: float = 45.0
    month_reserve_fraction: float = 0.10
    strong_tier_max_share: float = 0.35


@dataclass(frozen=True)
class TierPrice:          # USD per million tokens
    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0


@dataclass(frozen=True)
class Config:
    budget: Budget
    prices: dict[str, TierPrice]
    prices_as_of: str
    models: dict[str, str]
    local_model: dict


def load(path: Path) -> Config:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    prices = raw.get("prices", {})
    tiers = {name: TierPrice(**vals) for name, vals in prices.get("tiers", {}).items()}
    if "local" not in tiers:
        tiers["local"] = TierPrice(0.0, 0.0)
    return Config(budget=Budget(**raw.get("budget", {})), prices=tiers,
                  prices_as_of=prices.get("as_of", ""), models=raw.get("models", {}),
                  local_model=raw.get("local_model", {}))
