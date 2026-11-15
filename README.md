# HERA

A coordinator that gets coding tasks done by AI models and only calls them done when they pass checks the model could not tamper with — on a fixed monthly budget, with a default-deny rule on what code may leave your machine.

Private project for a group of four. Each member runs their own copy.

> **Status: milestone 0.1, step 4 of 10 is done (foundations, egress gateway and labeller, scanners v0, provider adapters; S4 pending review).** Nothing here has called a real model or read a repository yet; the egress gateway is the only code that can open a network connection and the provider adapters are exercised against loopback mock servers only. Next up is step S5. See [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md).

---

## If you are picking this up

1. Read this page, then `docs/BUILD_PLAN.md`.
2. Run the setup below and make sure the tests pass on your machine.
3. Take the first step in the plan whose status is not "Done". Each step has a **"done when"** line — that is the definition of finished.
4. Work on a branch, open a pull request, keep CI green.
5. If you use an AI assistant to help, paste it this README and the build plan first. Everything it needs to stay consistent is in these two files.

## Setup

```
git clone <repo url> && cd hera
python -m venv .venv
. .venv/bin/activate            # Windows: use WSL2, or .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q                       # 295 tests should pass
hera init                       # creates ~/.hera (or $HERA_HOME)
hera status
```

Python 3.11 or newer. The core has **no third-party dependencies**; keep it that way (see rules).

## What HERA does (the whole design in one screen)

```
 objective
    │
    ▼
 requirements summary ── you approve one screen (outcomes, examples, constraints, assumptions)
    │
    ▼
 checks written by a model that will NOT implement the task ── validated, frozen, hashed
    │
    ▼
 implementer model works in its own worktree; sees requirements + visible checks only
    │
    ▼
 coordinator verifies the commit in a fresh worktree with the frozen checks  ──► receipt
    │
    ▼
 verdict (pure function of the receipt):
   PASS · FAIL · OVERFIT · REGRESSION · TAMPER · UNVERIFIABLE · ORACLE_DISPUTED
    │
    ▼
 pass → done.   not pass → retry (max 2) → stronger model diagnoses → escalate → stop at budget cap
```

Around that loop:

- **Privacy gate, before any routing.** Every piece of data has a label. Public repositories may go to any provider. Personal or proprietary code needs an explicit approval bound to its exact content, a destination and an expiry. Sensitive repositories never leave; only text you read and approved as sanitized can, through an approval marked `declassified` — an ordinary approval never releases sensitive content. Anything unlabelled or uncertain is blocked.
- **One egress gateway.** The only component allowed to hold API keys or open a connection. Every send or block is written to a hash-chained log.
- **Cost ledger.** Every model call is priced and recorded. Caps: $3 per task, $4 per day, $45 per month (10% held in reserve), strong models at most 35% of the month.
- **Model tiers.** `local` (a 9B model on your own machine, private and free) · `cheap` · `default` (does most implementation) · `strong` (writes checks, diagnoses, escalations).
- **Friends' machines** are "remote" under the privacy rules, exactly like an API provider. Later milestones add a worker on a friend's PC and one model split across several machines.

## Repository map

| Path | What it is | State |
|---|---|---|
| `src/hera/store/` | SQLite state store, schema in `schema.sql`, versioned | done |
| `src/hera/config.py` | Loads `hera.toml` (budget, prices, model IDs) | done |
| `src/hera/ledger.py` | Cost ledger and spending caps | done |
| `src/hera/privacy/gate.py` | Which destinations a payload may go to — from labels and approvals only | done (decision logic) |
| `src/hera/privacy/translog.py` | Hash-chained transmission log; stores metadata and a hash, never the payload | done |
| `src/hera/privacy/policy.py` | Loads the local policy file; repository classes (no entry means sensitive) | done |
| `src/hera/privacy/labels.py` | Artefact labels, content hashes, strictest-class derivation | done |
| `src/hera/privacy/grants.py` | Approval store (hash-bound, destination-bound, expiring); local user command only | done |
| `src/hera/privacy/gateway.py` | The only network client; fail-closed; logs every send and block | done |
| `src/hera/privacy/scanners/` | Second-line scanners: secrets, personal-data shapes, data-file sniffing, deny-list terms; see `docs/SCANNERS.md` | done (v0) |
| `src/hera/providers/` | OpenAI-compatible and Anthropic Messages adapters; build and parse JSON only, every request goes through the gateway; see `docs/PROVIDERS.md` | done (mock servers only) |
| `src/hera/oracle/verdict.py` | Verdict as a pure function of a verification receipt | done |
| `src/hera/types.py` | Shared data types and enums | done |
| `src/hera/interfaces.py` | The replaceable boundaries: model provider, test adapter, gateway, worker, artefact store | defined, mostly unimplemented |
| `src/hera/cli.py` | `hera init`, `hera status` | done |
| `config/*.example.toml` | Example settings and privacy policy | done |
| `tests/` | One test file per module, plus portability checks | 295 passing |
| `docs/BUILD_PLAN.md` | Order of work with "done when" for each step | current |

Not built yet: local model host, sandbox and worktrees, check authoring and verification, agent loop, recovery, attribution.

## Rules — please do not break these

1. **Core is standard library only.** Third-party packages go at the edges, behind an interface in `interfaces.py` (model host, scanners, test-runner adapters).
2. **No machine-specific paths.** Everything lives under `HERA_HOME` via `hera/paths.py`. A test enforces this.
3. **Only the egress gateway touches the network or API keys.** No other module may import `urllib`, `http`, `socket`, `requests` or a vendor SDK. `tests/test_network_isolation.py` enforces it.
4. **Default-deny.** When a label is missing, a scanner fails, or anything is uncertain, block. Never "send and warn".
5. **No model is in the privacy or verdict path.** Those are plain deterministic code. Model output is data, never a policy decision, and a model's opinion never changes a verdict.
6. **The implementer never edits or sees more than it should.** Checks are frozen before implementation; hidden checks are never mounted in an implementer workspace; feedback on hidden failures names the requirement only.
7. **Hitting a budget cap is not a failure.** The task ends as "budget-censored".
8. **Nothing private in the repository.** No API keys, no `hera.toml`, no `policy/`, no deny-list terms, no customer names, no real repository content in tests. Use invented data. `.gitignore` covers the usual files; check your diff anyway.
9. **Every change comes with tests** and runs on macOS and Linux in CI.
10. **Each member runs their own coordinator** with their own keys, budget and policy. Do not add a shared server or shared key.

## Conventions

- Python 3.11+, type hints, dataclasses for data, `Protocol` for boundaries.
- Small modules; pure functions where possible (see `oracle/verdict.py`).
- State changes go through SQLite; schema changes bump `SCHEMA_VERSION` in `store/db.py` and add a migration — never edit an existing schema version in place.
- Tests use an in-memory database (`conn` fixture) or `tmp_path`.
- Commit messages: what changed and why, in plain sentences.
- One build-plan step per pull request where practical; update the step's status in `docs/BUILD_PLAN.md` in the same pull request.

## Next step: S5 — local model host

Build:
- A supervised child process for the 9B model with an explicit memory limit, a context cap taken from free memory, thinking off, and kill-to-evict and reload.
- On non-Mac hosts, an OpenAI-compatible local endpoint instead.

Done when the host survives 20 load/generate/evict cycles under the 9.0 GB limit and the second Mac benchmark pass is recorded.

S5's Mac part needs a Mac to run and test; it cannot be finished or verified on a Linux-only machine.

## Known gaps and honest caveats

- Tests have run on Linux with Python 3.13 only. macOS and 3.11 run in CI on the first push.
- The privacy gate decides correctly only if labels are right. Labelling (S2) and first scanners (S3) are built; the scanners are measured on a small synthetic corpus only, see `docs/SCANNERS.md`.
- Automated secret and personal-data detection will never be complete. The design relies on default-deny classification first; scanners are a second line.
- Spending caps and prices in the example config are first values, not measurements.
- Running one model across several machines is a later milestone and is unmeasured for the models in question.
