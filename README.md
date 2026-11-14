# HERA

Coordinator for verified, budgeted, privacy-gated coding tasks. Private project for a group of four.

**Status: milestone 0.1, step 1 of 10 (foundations).** What exists and is tested:

| Module | What it does |
|---|---|
| `hera.store` | SQLite state store with schema versioning |
| `hera.ledger` | Cost ledger and spending caps (per task, day, month, strong-tier share) |
| `hera.privacy.gate` | Default-deny decision: which destinations a payload may go to, from labels and grants only |
| `hera.privacy.translog` | Hash-chained transmission log (metadata and payload hash, never the payload) |
| `hera.oracle.verdict` | Verdict as a pure function of a verification receipt |
| `hera.interfaces` | The replaceable boundaries (model provider, adapter, gateway, worker…) |
| `hera.cli` | `hera init`, `hera status` |

Nothing here calls a model, touches a repository or opens a network connection yet. See `docs/BUILD_PLAN.md` for the order of work.

## Setup

```
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q
hera init          # creates ~/.hera (or $HERA_HOME)
hera status
```

Requires Python 3.11 or newer. The core has no third-party dependencies.

## Rules that hold from the first commit

- Core is pure Python, tested on macOS and Linux in CI; Windows through WSL2.
- No machine-specific paths; everything lives under `HERA_HOME`.
- Privacy configuration and deny lists live under `HERA_HOME/policy/` on each member's own machine and are never committed.
- Only the egress gateway may hold API keys or open network connections (enforced by a test once the gateway exists).
- A model's opinion never changes a verdict; only execution evidence does.
