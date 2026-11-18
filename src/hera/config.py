from __future__ import annotations
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Budget:
    per_task_usd: float = 3.0
    per_day_usd: float = 4.0
    per_month_usd: float = 45.0
    month_reserve_fraction: float = 0.10
    strong_tier_max_share: float = 0.35

    def __post_init__(self) -> None:
        # Every comparison with NaN is false, so a NaN cap would silently switch that cap off (TOML
        # accepts `nan` and `inf`). Caps must be finite and non-negative; fractions within [0, 1].
        for name in ("per_task_usd", "per_day_usd", "per_month_usd"):
            _finite_non_negative(f"budget.{name}", getattr(self, name))
        for name in ("month_reserve_fraction", "strong_tier_max_share"):
            v = _finite_non_negative(f"budget.{name}", getattr(self, name))
            if v > 1:
                raise ValueError(f"budget.{name}: must be between 0 and 1")


@dataclass(frozen=True)
class TierPrice:          # USD per million tokens
    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0

    def __post_init__(self) -> None:
        for name in ("input", "output", "cache_read", "cache_write"):
            _finite_non_negative(f"price.{name}", getattr(self, name))


def _finite_non_negative(name: str, v) -> float:
    import math
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise ValueError(f"{name}: must be a finite number of at least 0, got {v!r}")
    return float(v)


PROVIDER_KINDS = ("anthropic", "openai_compat", "grok")
_PROVIDER_KEYS = frozenset({"kind", "url", "model", "key_env", "tier", "extra_headers", "local",
                            "send_store_false"})


@dataclass(frozen=True)
class ProviderSpec:
    """One ``[providers.<name>]`` section. The API key itself is never in the file: ``key_env``
    names the environment variable that holds it."""
    name: str
    kind: str
    url: str = ""
    model: str = ""
    key_env: str = ""
    tier: str = "default"
    extra_headers: dict = field(default_factory=dict, repr=False)
    local: bool = False
    send_store_false: bool = False


_RECOVERY_KEYS = ("max_retries", "max_candidates_per_tier", "max_turns", "max_tokens")


@dataclass(frozen=True)
class RecoveryLimits:
    """Hard limits of the recovery ladder (``[recovery]`` in hera.toml); see docs/AGENT.md."""
    max_retries: int = 2               # retries of one candidate in the same conversation
    max_candidates_per_tier: int = 2   # fresh candidates per tier
    max_turns: int = 30                # model turns in one attempt
    max_tokens: int = 4096             # per model reply


@dataclass(frozen=True)
class Config:
    budget: Budget
    prices: dict[str, TierPrice]
    prices_as_of: str
    models: dict[str, str]
    local_model: dict
    providers: dict[str, ProviderSpec] = field(default_factory=dict)
    recovery: RecoveryLimits = field(default_factory=RecoveryLimits)


def _recovery(raw) -> RecoveryLimits:
    if not isinstance(raw, dict):
        raise ValueError("recovery: must be a table")
    unknown = set(raw) - set(_RECOVERY_KEYS)
    if unknown:
        raise ValueError(f"recovery: unknown setting(s): {', '.join(sorted(unknown))}")
    vals = {}
    for key in _RECOVERY_KEYS:
        if key in raw:
            v = raw[key]
            low = 0 if key == "max_retries" else 1
            if not isinstance(v, int) or isinstance(v, bool) or v < low:
                raise ValueError(f"recovery.{key}: must be an integer of at least {low}")
            vals[key] = v
    return RecoveryLimits(**vals)


def _provider_spec(name: str, raw) -> ProviderSpec:
    if not isinstance(raw, dict):
        raise ValueError(f"providers.{name}: must be a table")
    unknown = set(raw) - _PROVIDER_KEYS
    if unknown:
        raise ValueError(f"providers.{name}: unknown setting(s): {', '.join(sorted(unknown))}")
    kind = raw.get("kind")
    if kind not in PROVIDER_KINDS:
        raise ValueError(f"providers.{name}: kind must be one of {', '.join(PROVIDER_KINDS)}")
    headers = raw.get("extra_headers", {})
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                for k, v in headers.items()):
        raise ValueError(f"providers.{name}: extra_headers must be a table of strings")
    for key in ("url", "model", "key_env", "tier"):
        if not isinstance(raw.get(key, ""), str):
            raise ValueError(f"providers.{name}: {key} must be a string")
    return ProviderSpec(name=name, kind=kind, url=raw.get("url", ""), model=raw.get("model", ""),
                        key_env=raw.get("key_env", ""), tier=raw.get("tier", "default"),
                        extra_headers=dict(headers), local=bool(raw.get("local", False)),
                        send_store_false=bool(raw.get("send_store_false", False)))


def load(path: Path) -> Config:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    prices = raw.get("prices", {})
    tiers = {name: TierPrice(**vals) for name, vals in prices.get("tiers", {}).items()}
    if "local" not in tiers:
        tiers["local"] = TierPrice(0.0, 0.0)
    local_model = raw.get("local_model", {})
    from .modelhost import HostSettings          # the one parser: a bad table fails here, at load time
    HostSettings.from_table(local_model)
    return Config(budget=Budget(**raw.get("budget", {})), prices=tiers,
                  prices_as_of=prices.get("as_of", ""), models=raw.get("models", {}),
                  local_model=local_model,
                  providers={n: _provider_spec(n, v) for n, v in raw.get("providers", {}).items()},
                  recovery=_recovery(raw.get("recovery", {})))
