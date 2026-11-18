# HERA

A coordinator that gets coding tasks done by AI models and only calls them done when they pass checks the model could not tamper with — on a fixed monthly budget, with a default-deny rule on what code may leave your machine.

Private project for a group of four. Each member runs their own copy.

> **Status: milestone 0.1, step 9 of 10 is built (foundations, egress gateway and labeller, scanners v0, provider adapters, workspaces and sandbox, the acceptance oracle: checks, frozen check store, pytest adapter, contract checks, check validation, diff audit, verification receipts, verdicts, attribution, redacted feedback and disputes, the implementer loop with resume, the task orchestrator with the recovery ladder, privacy routing before any call and a decision log that lets a task resume at any point, and now continuing a stopped task (`hera task continue`), resolving disputes from the command line (`hera dispute list/resolve`) and the learning ledger with its report (`hera report`: pass rates, the `UNKNOWN` rate, dollars per verified task)); S5b (the local model host supervisor, tested against a fake model process) is built; S5 (the real MLX worker and the Mac memory measurement) is deferred until someone has a Mac; S4, S5b, S7c, S7d and S9 are pending review.** The in-process tamper route is closed for contract checks (declarative checks run out of process, compared by the oracle) and still open for native pytest checks: a candidate that monkeypatches pytest gets a false `PASS` from a pytest-only check set (documented, tested; `in_process_only` in the verify outcome shows which requirements rest only on such checks; see docs/ORACLE.md). Nothing here has called a real model or read a real repository yet; the egress gateway is the only code that can send content over a network connection (the model host supervisor only probes 127.0.0.1) and the provider adapters, the agent loop and the orchestrator are exercised against loopback mock servers only. Personal and proprietary repositories run local-only in `hera run` by design (see docs/AGENT.md). Next up is step S10 (the pilot), which needs a provider key and real tasks. See [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md).

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
pytest -q                       # 1159 tests; 10 skip where no sandbox mechanism is available
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
| `src/hera/providers/registry.py` | Providers built from `[providers.<name>]` in `hera.toml`; the key comes from the environment variable the section names; a missing key leaves the provider out with a reason | done (mock servers only) |
| `src/hera/providers/grok.py` | xAI (Grok) adapter on the OpenAI-compatible base | done (mock servers only) |
| `src/hera/workspace.py` | Bare repository per task, worktree per candidate, verification checkout from a commit hash with no `.git`; hardened git | done |
| `src/hera/sandbox.py` | Run candidate code with no network, scrubbed environment, limits and a narrow filesystem (`sandbox-exec` / `bwrap`); see `docs/SANDBOX.md` | done (macOS path untested by author) |
| `src/hera/oracle/verdict.py` | Verdict as a pure function of a verification receipt | done |
| `src/hera/oracle/model.py` | Requirement, draft check, check set, prepare and run reports | done |
| `src/hera/oracle/checkstore.py` | Frozen, hashed, read-only check store (visible and hidden), integrity check, supersede; see `docs/ORACLE.md` | done |
| `src/hera/oracle/contract.py` | Contract checks: `*.contract.toml` parser and pure evaluator (exit code, stdout, JSON, files; no regular expressions); see `docs/ORACLE.md` | done |
| `src/hera/oracle/contract_runner.py` | Runs each contract check in a fresh sandboxed copy of the checkout and compares in the oracle; never imports candidate code; hidden contract files are never copied anywhere | done (sandbox-dependent tests first run in CI) |
| `src/hera/oracle/adapters/` | pytest adapter: protected-path restoration, sentinels, JUnit parsing, redacted feedback | done (Python only; same interpreter as HERA) |
| `src/hera/oracle/validation.py` | Validate a draft check set on the base commit (fail-before, stability) | done (no stub adversaries or mutation testing yet) |
| `src/hera/oracle/audit.py` | Diff audit: protected paths, skip markers, declared scope, debris, dependency manifests, twin search; folds in what `prepare` undid | done |
| `src/hera/oracle/verify.py` | Verify a commit: audit, pytest and contract check runs, regressions against the base-commit run of the project's own tests, receipt, verdict, attribution, persistence, redacted feedback, `in_process_only` | done (native pytest checks still forgeable in-process) |
| `src/hera/oracle/summary.py` | Requirements summary: `SUMMARY.toml` parser with the design's limits, canonical hash, the one-screen render (40 lines of 100 columns), immutable summaries and append-only approvals (schema v6), including the recorded `may_leave_machine` control; see `docs/ORACLE.md` | done (the command line enforces approval; the library does not) |
| `src/hera/oracle/disputes.py` | Disputes about visible checks; independent resolution; schema version 3 | done |
| `src/hera/agent/tools.py` | The implementer's tools (files, literal search, sandboxed commands, visible checks, submit, dispute); path rules; every result labelled by the repository's class; see `docs/AGENT.md` | done (mock providers only) |
| `src/hera/agent/prompts.py` | The implementer's fixed rules and first messages | done |
| `src/hera/agent/loop.py` | `run_attempt`: the tool loop, per-turn records (`attempts`, `turns`), resume without repeated requests, end statuses | done (mock providers only) |
| `src/hera/agent/policy.py` | The recovery ladder H-R1 as a pure state machine (`decide`): rungs, retry rules, limits and thresholds as data; see `docs/AGENT.md` | done (mock providers only) |
| `src/hera/agent/orchestrator.py` | `run_task`: attempts, verification of every submitted candidate, ladder decisions, disputes, privacy routing before any call, the `task_events` log and resume at any point; `continue_task`: continue a task stopped by a cap, a provider error or a resolved dispute | done (mock providers only) |
| `src/hera/report.py` | The learning ledger: one record per finished attempt joined from stored data (no schema change), the summary and its text; every rate with its counts and `(n<5)` below five observations | done |
| `src/hera/modelhost.py` | Local model host supervisor: settings, context cap from memory, free-memory probes, one child process on 127.0.0.1 with lease, evict, health check, restart budget and event log; see `docs/MODELHOST.md` | done (fake model process only; the server is not sandboxed) |
| `src/hera/types.py` | Shared data types and enums | done |
| `src/hera/interfaces.py` | The replaceable boundaries: model provider, test adapter, gateway, worker, artefact store | defined, mostly unimplemented |
| `src/hera/cli.py` | `hera init`, `hera status`, `hera verify`, `hera providers list`, `hera providers check <name>`, `hera summary import/show/approve/reject/list`, `hera checks freeze`, `hera run`, `hera task status`, `hera task continue`, `hera dispute list`, `hera dispute resolve`, `hera report`, `hera host status/start/stop/reset` | done |
| `config/*.example.toml` | Example settings and privacy policy | done |
| `tests/` | One test file per module, plus portability checks, an invented toy project (`toyrepo.py`) and a fake model server (`fake_model_server.py`) | 1159 (10 need a sandbox mechanism) |
| `docs/BUILD_PLAN.md` | Order of work with "done when" for each step | current |

Not built yet: the real local model worker and its Mac memory measurement (S5; the supervisor, S5b, is built), check authoring by models (and a model that drafts the requirements summary), a path for remote models to help on personal or proprietary repositories through approved sanitised descriptions.

## Rules — please do not break these

1. **Core is standard library only.** Third-party packages go at the edges, behind an interface in `interfaces.py` (model host, scanners, test-runner adapters).
2. **No machine-specific paths.** Everything lives under `HERA_HOME` via `hera/paths.py`. A test enforces this.
3. **Only the egress gateway touches the network or API keys.** No other module may import `urllib`, `http`, `socket`, `requests` or a vendor SDK. `tests/test_network_isolation.py` enforces it. The one exception is `hera/modelhost.py`, allowed `http.client` and `socket` for GET requests and connection probes to 127.0.0.1 only (a test checks every address it builds).
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

## How a task starts

A task begins with a requirements summary a person approves, never with the checks:

1. `hera summary import SUMMARY.toml` stores the summary under its hash and prints the one-screen text: outcomes, observable behaviour, constraints, assumptions, what cannot be verified automatically, what is out of scope, questions, and every requirement with its planned verification confidence (format in `docs/ORACLE.md`). To change anything, edit the file and import it again: a new hash, a new approval.
2. `hera summary approve HASH --by human:<name> --may-leave-machine yes|no` (or `reject ... --note TEXT`). Approval is refused while a question has no answer; the yes/no on whether the text may leave the machine has no default and is only recorded for now.
3. `hera checks freeze --task T --repo PATH --spec spec.toml --summary HASH` freezes the checks only against an approved summary of the same task whose requirements equal the spec's, and stores its hash with the check set.
4. `hera run --task T --checkset H --objective "..."` refuses a check set without an approved summary (`--without-approved-summary` overrides and says so in the task's log).

## First real call

No real provider call has been made yet. When a member has an account, this is the whole path from nothing to a verdict:

1. Copy `config/hera.example.toml` to `<HERA_HOME>/hera.toml` and fill in `[providers.<name>]`: `kind`, `url`, the exact dated `model` id, `tier` and `key_env`, the NAME of an environment variable. The key itself never goes in a file. Name a second default-tier provider `default_alt` and a strong-tier one `strong` if you have them (the ladder uses them for a different second opinion, the diagnosis and the escalation). Copy `config/policy.example.toml` to `<HERA_HOME>/policy/policy.toml` and list the repository with its class: a repository that is not listed counts as sensitive and no remote provider will be used for it.
2. Set that variable in your shell (`export ANTHROPIC_API_KEY=...`) and run `hera providers list`: the provider should show `key present: yes`.
3. Run `hera providers check <name>`. It sends the fixed prompt "Reply with the single word: ready" through the gateway and prints the reply, the tokens and the dollars; exit status 0 means it worked. Then `hera status` shows the call and its cost.
4. Write a spec by hand, with the requirements and the checks (visible and hidden), see the walkthrough in `docs/AGENT.md`, and write a `SUMMARY.toml` for it, `hera summary import` and `hera summary approve` it (see "How a task starts"), then run `hera checks freeze --task TASK --repo /path/to/repo --spec spec.toml --summary HASH`. It runs the checks on the base commit, refuses to freeze checks that are flaky or invalid, and prints the check-set hash.
5. Run `hera run --task TASK --checkset HASH --objective "what you want done" --max-usd 1`. It prints each event as it happens (the routing decision, every attempt, every verification, the ladder's decisions) and ends with the status, the verdict and the dollars. Exit 0 means `PASSED`; 3 means any other final status (`FAILED`, `BUDGET_CENSORED`, `BLOCKED_PRIVACY`, ...).
6. `hera task status TASK` shows the events again at any time. A task that was interrupted continues from where it stopped when you run the same `hera run` command again. A task that stopped as `BUDGET_CENSORED`, `PROVIDER_ERROR` or (once `hera dispute resolve` closed the dispute) `AWAITING_DISPUTE` goes on with `hera task continue TASK [--max-usd X]`; `hera report` shows the learning ledger.

Expect to fix small wire-format differences on this first call, especially for tool calls (see `docs/AGENT.md`).

## Next step: S10 (the pilot), or S5 if you have a Mac

Built through S9: the implementer loop, the orchestrator with the recovery ladder and `hera run`, continuing a stopped task (`hera task continue`), disputes from the command line (`hera dispute`), and the learning ledger (`hera report`). Nothing has met a real model yet, and nothing in S8b or S9 is meaningful until one real call has been made.

**S10 needs a provider key and real tasks.** The walkthrough is "First real call" above: put one provider in `hera.toml` (`kind`, `url`, the exact dated `model`, `tier`, `key_env`), export the key in your shell, `hera providers list` then `hera providers check <name>`, write a spec by hand for a small task from a public repository (listed as `public` in the policy) and `hera checks freeze`, then `hera run`. Eight gold tasks, run with the bare loop and with the HERA loop, then `hera report`: the pilot measures the false-pass rate, dollars per verified task and the `UNKNOWN` rate, and ends with the gate on whether to go on to 0.2. Expect to fix small wire-format differences on the first real call, especially for tool calls.

**S5 needs a Mac.** The supervisor (S5b) is built and tested against a fake model process; the real 9B MLX worker, its own memory limit and the second Mac benchmark pass wait for a Mac. See `docs/MODELHOST.md`.

Still open from S7b/S7c: two blind check authors; author hidden checks as contract checks wherever the task has an external surface (check authoring by models is not built; members write checks by hand with `hera checks freeze` until then).

## Known gaps and honest caveats

- Tests have run on Linux with Python 3.13 only. macOS and 3.11 run in CI on the first push.
- The privacy gate decides correctly only if labels are right. Labelling (S2) and first scanners (S3) are built; the scanners are measured on a small synthetic corpus only, see `docs/SCANNERS.md`.
- Automated secret and personal-data detection will never be complete. The design relies on default-deny classification first; scanners are a second line.
- Spending caps and prices in the example config are first values, not measurements.
- Running one model across several machines is a later milestone and is unmeasured for the models in question.
