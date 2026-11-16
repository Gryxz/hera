# HERA — Build Plan

Version 1, 2026-11-14. Derived from the review documents (`07_REVISED_ARCHITECTURE`, `10_MAC_BASELINE`, `08_MODEL_MATRIX`, `05_ALGORITHM_COMPARISON`, `06_OKF_INTEGRATION`). Each step has a "done when" that can be checked by running something.

## Ground rules

1. Single machine first (MacBook Pro M1 Pro, 16 GB). No distributed code until milestone 0.1 passes its gate.
2. Every step lands with tests; CI runs on macOS and Linux.
3. Core stays standard-library only. Third-party packages enter only at the edges (MLX model host, scanners, test-runner adapters) behind an interface.
4. Each member runs their own coordinator, policy, keys and budget.
5. Default-deny privacy is built before the first real model call, not after.

## Milestone 0.1 — one verified task on one machine

| Step | Content | Done when | Status |
|---|---|---|---|
| S1 Foundations | Layout, state store, config, cost ledger and caps, privacy-gate decision logic, transmission log, verdict function, CLI, CI | `pytest` green on macOS and Linux; `hera init` and `hera status` work | **Done** (Linux; macOS runs in CI on first push) |
| S2 Egress gateway and labeller | Single network client; labels for artefacts (repository class from local policy, content hash, derivation); grant store; local policy file loading; fail-closed on missing label, unknown destination, oversize, scanner error; every decision written to the transmission log | Containment tests pass against a loopback sink: unlabelled text blocked; sensitive raw content blocked; changed content invalidates a grant; no module except the gateway imports a network library (static test) | **Done** |
| S3 Scanners v0 | Secret patterns and entropy; structural detectors (email, phone, address shape, order/tracking/payment formats); field-combination rule; deny-list terms with match mode and action; block notice never echoes a term | Seeded-corpus test reports recall and false-block rate per category; known misses listed, not hidden | **Done** |
| S4 Provider adapters | OpenAI-compatible adapter (covers llama-server, a friend's endpoint, a pooled model, and one vendor) and a second vendor adapter; stateless endpoints only; usage recorded to the ledger; caps checked before each call | Mock-server tests; one real call per configured provider shows up in `hera status` with dollars; a capped call raises instead of sending | Done (pending review; mock servers only, no real provider call made). A Grok (xAI) adapter was added afterwards (`providers/grok.py`), also mock-tested only. |
| S5 Local model host | Supervised child process for the 9B model: explicit memory limit, context cap from free memory, thinking off, kill-to-evict, reload; on non-Mac hosts an OpenAI-compatible local endpoint instead | Host survives 20 load/generate/evict cycles under the 9.0 GB limit; coordinator memory unaffected; second Mac benchmark pass recorded | Deferred — needs a Mac to run and test |
| S6 Workspaces and sandbox | Bare repository per task, worktree per candidate, fresh worktree per verification fetched by commit hash; process sandbox with network off; resource limits | A candidate cannot read the check store or another worktree; a verification run starts from nothing but the commit | **Done**. See [SANDBOX.md](SANDBOX.md). macOS `sandbox-exec` path first exercised in CI (see docs/SANDBOX.md); Linux `bwrap` path runs where bubblewrap works. |
| S7a Acceptance oracle: checks | Check objects; frozen, hashed check store (visible/hidden, outside every worktree); pytest adapter (protected-path restoration, sentinels, result-integrity fields, redacted feedback); validation on the base commit (fail-before, 3× stability). See [ORACLE.md](ORACLE.md) | Toy repository: honest, hard-coded and tampering candidates produce the expected per-check results, tampering routes listed in ORACLE.md are neutralised or reported, validation labels checks valid / weak / flaky / invalid | **Done**. Stub adversaries and mutation testing are not built (milestone 0.2). |
| S7b Acceptance oracle: verdicts | Requirements summary and approval screen (terminal); two blind check authors; diff audit (protected paths, declared scope, debris, twin search); verify → receipt → verdict; redacted feedback; disputes | End-to-end on a toy repository: correct patch → `PASS`; test-editing patch → `TAMPER`; hard-coded patch → `OVERFIT`; regression → `REGRESSION` | Done (pending review). Built: diff audit, verify → receipt → verdict → attribution, redacted feedback, disputes, `hera verify`. Not built here: requirements summary and approval screen, two blind check authors. |
| S7c Out-of-process (contract) checks | Pulled forward from milestone 0.2 to close the in-process tampering gap. Language-neutral declarative check files run by an oracle-owned runner process that never imports candidate code; the candidate runs as a separate sandboxed subprocess; hidden contract checks are never copied into the candidate's checkout | `test_KNOWN_GAP_in_process_monkeypatch_yields_false_pass` flipped: the monkeypatch candidate is no longer `PASS` | Next |
| S8 Agent loop and recovery | Tool loop with tool scope from grants and every tool result through the gateway; recovery ladder (retry ×2 → strong-tier diagnosis → escalation; fresh candidate on `OVERFIT`/`TAMPER`); checkpoint and resume after sleep or crash | Kill the process mid-task and resume without repeating paid calls; budget cap ends a task as budget-censored | |
| S9 Attribution and logging | Ten attribution labels by deterministic rules; learning-ledger fields per attempt; model identifier on every record | Every finished attempt has exactly one attribution; `UNKNOWN` rate reported | |
| S10 Pilot | Eight gold tasks, bare loop vs HERA loop | False-pass rate, dollars per verified task and real per-task cost reported; **gate: decide whether to continue to 0.2** | Needs task examples |

## After 0.1

| Milestone | Content |
|---|---|
| 0.2 Second language and stronger checks | TypeScript/JavaScript adapter; diff-scoped mutation testing; final-only hidden slice; verifier in a per-run VM or container |
| 0.3 First worker | Execution worker on the x86 PC (WSL2 or Linux), outbound connection over a private network, capability probe, leases; conformance test = same check set gives the same results on both machines |
| 0.4 Shared inference | 9B and 30B endpoints on the PC; two-machine split on one network; split across two homes; the 80B coder pooled across three or more machines; decision rule from `08_MODEL_MATRIX` §7 |
| 0.5 Knowledge | OKF bundles for distilled knowledge, trust tiers, lexical retrieval, one bundle per privacy level |
| 0.6 Evaluation at full size | 20 tasks, four arms, privacy red-team suite as a release gate |
| Later, gated by measurements | Tier predictor (≥ 300 labels); capability-aware scheduling; installers |

## Not planned

Fine-tuning of generative models; a shared coordinator; tree search over local rollouts. Requirements for each are listed in the architecture document.

## What is needed from the owner

- Which provider account(s) exist — decides the first adapter in S4.
- An empty private repository to push this to.
- Two or three real tasks from a public repository for S7 and S10.
- Local entries for `policy.toml` (repository classes, deny-list terms) on each member's machine.
