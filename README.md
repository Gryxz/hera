# HERA

A coordinator that gets coding tasks done by AI models and only calls them done when they pass checks the model could not tamper with — on a fixed monthly budget, with a default-deny rule on what code may leave your machine.

Private project for a group of four. Each member runs their own copy.

> **Status: milestone 0.1, step 7b of 10 is built (foundations, egress gateway and labeller, scanners v0, provider adapters, workspaces and sandbox, and the acceptance oracle: checks, frozen check store, pytest adapter, check validation, diff audit, verification receipts, verdicts, attribution, redacted feedback and disputes); S5 (local model host) is deferred until someone has a Mac, and S4 and S7b are pending review.** One tamper route is still open: candidate code that monkeypatches pytest inside the checks' process gets a false `PASS` (documented, tested, see docs/ORACLE.md). Nothing here has called a real model or read a real repository yet; the egress gateway is the only code that can open a network connection and the provider adapters are exercised against loopback mock servers only. Next up is step S7c. See [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md).

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
pytest -q                       # 553 tests; 7 skip where no sandbox mechanism is available
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
| `src/hera/providers/grok.py` | xAI (Grok) adapter on the OpenAI-compatible base | done (mock servers only) |
| `src/hera/workspace.py` | Bare repository per task, worktree per candidate, verification checkout from a commit hash with no `.git`; hardened git | done |
| `src/hera/sandbox.py` | Run candidate code with no network, scrubbed environment, limits and a narrow filesystem (`sandbox-exec` / `bwrap`); see `docs/SANDBOX.md` | done (macOS path untested by author) |
| `src/hera/oracle/verdict.py` | Verdict as a pure function of a verification receipt | done |
| `src/hera/oracle/model.py` | Requirement, draft check, check set, prepare and run reports | done |
| `src/hera/oracle/checkstore.py` | Frozen, hashed, read-only check store (visible and hidden), integrity check, supersede; see `docs/ORACLE.md` | done |
| `src/hera/oracle/adapters/` | pytest adapter: protected-path restoration, sentinels, JUnit parsing, redacted feedback | done (Python only; same interpreter as HERA) |
| `src/hera/oracle/validation.py` | Validate a draft check set on the base commit (fail-before, stability) | done (no stub adversaries or mutation testing yet) |
| `src/hera/oracle/audit.py` | Diff audit: protected paths, skip markers, declared scope, debris, dependency manifests, twin search; folds in what `prepare` undid | done |
| `src/hera/oracle/verify.py` | Verify a commit: audit, check run, regressions against the base-commit run of the project's own tests, receipt, verdict, attribution, persistence, redacted feedback | done (in-process tamper gap open) |
| `src/hera/oracle/disputes.py` | Disputes about visible checks; independent resolution; schema version 3 | done |
| `src/hera/types.py` | Shared data types and enums | done |
| `src/hera/interfaces.py` | The replaceable boundaries: model provider, test adapter, gateway, worker, artefact store | defined, mostly unimplemented |
| `src/hera/cli.py` | `hera init`, `hera status`, `hera verify` | done |
| `config/*.example.toml` | Example settings and privacy policy | done |
| `tests/` | One test file per module, plus portability checks and an invented toy project (`toyrepo.py`) | 553 (7 need a sandbox mechanism) |
| `docs/BUILD_PLAN.md` | Order of work with "done when" for each step | current |

Not built yet: local model host, check authoring by models, requirements approval screen, out-of-process checks (S7c), agent loop, recovery, attribution learning ledger.

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

## Next step: S7c — out-of-process (contract) checks

Pulled forward from milestone 0.2 to close the one known tamper route: native pytest checks import candidate code into the runner's process (see docs/ORACLE.md, "Residual risk"). Built in S7a and S7b: check objects, frozen check store, pytest adapter, validation, diff audit, verify → receipt → verdict → attribution, redacted feedback, disputes, `hera verify`. Build now:
- A language-neutral declarative check file: run this command, send this stdin, read this file, then expect this exit code, stdout or JSON shape.
- An oracle-owned runner process that executes those files and never imports candidate code; the candidate runs as a separate sandboxed subprocess.
- Hidden contract checks are never copied into the candidate's checkout.
- Freeze, hash, validate on the base commit and feed the same receipt and verdict path as the pytest checks.

Done when `test_KNOWN_GAP_in_process_monkeypatch_yields_false_pass` can be flipped: the monkeypatching candidate no longer gets `PASS`, and the toy repository's verdicts are unchanged.

Still open from S7b: requirements summary and approval screen (terminal); two blind check authors.

S5 (local model host) is deferred: its Mac part needs a Mac to run and test.

## Known gaps and honest caveats

- Tests have run on Linux with Python 3.13 only. macOS and 3.11 run in CI on the first push.
- The privacy gate decides correctly only if labels are right. Labelling (S2) and first scanners (S3) are built; the scanners are measured on a small synthetic corpus only, see `docs/SCANNERS.md`.
- Automated secret and personal-data detection will never be complete. The design relies on default-deny classification first; scanners are a second line.
- Spending caps and prices in the example config are first values, not measurements.
- Running one model across several machines is a later milestone and is unmeasured for the models in question.
