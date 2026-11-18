# Handover — 17 Nov 2026

For whoever takes HERA over from here. Read this, then `README.md` (especially "Rules — please do not
break these"), then `docs/BUILD_PLAN.md`. Everything below is what is **not** done yet, in the order it
should be done.

## Where things stand

- `main` has all of milestone 0.1 that can be built without a provider key or the Mac:
  - foundations (store, config, ledger with caps, transmission log);
  - egress gateway, labeller, grants, scanners;
  - provider adapters (OpenAI-compatible, Anthropic, Grok);
  - workspaces and sandbox (sandbox-exec on macOS, bwrap on Linux);
  - the acceptance oracle: pytest adapter, contract checks, validation with persisted check health, audit, verdicts, attribution, disputes, and the one-screen requirements summary that must be approved before checks are frozen;
  - the agent loop and orchestrator with the recovery ladder, resume, continuing a stopped task, and a one-runner-per-task lock;
  - the learning-ledger report;
  - the local model host supervisor;
  - a second check adapter for TypeScript/JavaScript on `node --test`.
- About 1,330 tests. CI runs them on macOS and Ubuntu, Python 3.11 and 3.13, with real sandboxes and Node 22.
- **Nothing has ever called a real model.** Every provider, agent and orchestrator test runs against mock servers on 127.0.0.1. Expect small wire-format fixes on the first real call, especially for tool calls.

## Working on it

- Setup: `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`, then `pytest -q`.
- If you use several git worktrees, give each worktree its own venv. An editable install points at one checkout only.
- Work on a branch, open a pull request, and keep CI green on all four jobs. A test that passes locally can still fail in CI.

### Traps that caught us more than once

- **Root or no sandbox locally.** If you run as root or without a sandbox, about 10 tests skip and some pass that CI fails.
- **macOS file systems.** They are case-insensitive (`.GIT` exists when `.git` does).
- **Signalling zombies on macOS.** `killpg(pgid, 0)` answers EPERM, not ESRCH, when only zombies are left in the group.
- **Slow lookup on CI macOS.** On GitHub's macOS runners, `socket.getfqdn("127.0.0.1")` takes about 35 s, and `http.server` calls it before it starts listening. The fake model server skips that lookup.
- **bwrap directories.** Under bwrap, `/tmp` and the home directory are private tmpfs. Never let a test walk `/` inside a sandbox.
- **Sandbox read access.** On macOS the sandbox denies `/Users` except for listed tool paths, so a tool installed there (Node on CI) must be passed as a readable path.

## What needs completing, in priority order

### 1. S10 — the pilot (blocked on the owner)
This is the gate on whether to go on to milestone 0.2. Steps:
1. Set up one provider in `hera.toml`. The API key goes in an environment variable named by `key_env`, never in a file. Run `hera providers check <name>`.
2. Pick two or three small real tasks from a **public** repository, and list that repository as `public` in the local policy.
3. For each task, write by hand:
   - a `SPEC.toml` with the requirements, the visible checks and the hidden checks;
   - a `SUMMARY.toml`.
4. `hera summary import`, then `hera summary approve` (by a person), then `hera checks freeze --summary HASH`, then `hera run`.
5. Eight gold tasks in total, each run with the bare loop and with the HERA loop. Then `hera report`.

The pilot measures the false-pass rate, the dollars per verified task and the `UNKNOWN` rate. The monthly API budget is $40–50, and the caps are $3 per task, $4 per day and $45 per month.

The owner also has to decide whether `Gryxz/hera` stays public.

### 2. S5 — the real local model on the Mac (needs the M1 16 GB MacBook)
- The supervisor (`docs/MODELHOST.md`, `hera host`) is built and tested against a fake server.
- Still to do:
  - an MLX worker that serves Qwen3.5-9B over the OpenAI-compatible protocol on 127.0.0.1, with thinking **off** on every call;
  - an explicit memory limit, which the worker must enforce itself;
  - the 20 load/generate/evict cycle test under the 9.0 GB limit;
  - the second Mac benchmark pass (`claude/hera_m1_bench.py` in the project).
- If `hera host start` times out, check the reverse lookup noted in the traps above.

### 3. Finish the TypeScript/JavaScript path
- The agent's tools and prompts still assume Python: `run_command` has only a `{python}` placeholder. So `hera run` on a node check set verifies correctly, but the model is told to use Python tooling.
- The audit's skip-marker and debris rules read `.py` files only. Add `test.skip(`, `.only(`, `console.log` and similar for JS and TS.
- jest and vitest adapters are later work; `node --test` needs no npm packages.

### 4. Known gaps to close (documented in the docs named)
- **Native-check forgery (`ORACLE.md`).** A candidate can monkeypatch the test runner in-process and get a false PASS. This applies to native pytest checks and native node checks. Contract checks are not affected. The planned fix is a verifier in a per-run VM or container (milestone 0.2). `in_process_only` in the verify outcome shows which requirements depend on this.
- **Model server sandbox (`MODELHOST.md`).** The local model server itself is not sandboxed. There is also a port race between HERA's check and the child binding the port.
- **Check authoring (`ORACLE.md`).** Models do not write checks yet. The design calls for two blind check authors, with hidden checks written as contract checks wherever possible. Models do not draft requirements summaries yet either.
- **The `may_leave_machine` flag (`ORACLE.md`).** It is recorded on a summary approval but nothing uses it yet. It is meant to be the declassification point for restricted repositories.
- **Personal and proprietary repositories (`AGENT.md`).** They run local-only in `hera run` by design, because approvals bind to exact content hashes. A remote route for them needs grant handling inside the agent loop.
- **The `--without-approved-summary` override.** It is recorded on the first `hera run` only, not on a resume.
- **The per-task lock (`AGENT.md`).** It is advisory (`flock`) and may not exclude across machines on a network file system.
- **Billing after a transport error (`PROVIDERS.md`).** It is recorded as a conservative estimate, not as the provider's real figure.

### 5. Milestone 0.2 and later (`docs/BUILD_PLAN.md`, "After 0.1")
- Diff-scoped mutation testing.
- A final-only hidden slice.
- The verifier in a VM or container.
- Later: distributed workers. One coordinator per member stays.
- **Sharding one model across the group's machines must be possible.** It is the point of the collaboration, though not urgent. See `claude/07_REVISED_ARCHITECTURE.md` §E.8 in the owner's Claude project.
- The OKF knowledge tier.

### 6. Features discussed with the owner (not yet chosen or started)
These came up as suggestions in a separate chat ("HERA UX feature ideas"). The owner has not picked between them yet.

**User experience.** The suggested first two both sit on data HERA already records:
- **Live task view.** A TUI or dashboard showing each candidate's progress, checks passing and failing, and dollars spent, updating as it runs. The orchestrator already emits every event (`on_event`, `task_events`).
- **Plain-English verdicts.** Turn `OVERFIT` into something like "it hard-coded the test answers, so that attempt was thrown out and another was tried", and add a one-line "what changed and why" after each task.

Also suggested:
- one-tap approval of the requirements summary, pushed to a phone;
- a cost estimate and per-task budget dial before a run;
- a mid-run hint that needs no restart;
- a diff grouped by intent with a safety badge;
- a morning digest;
- a "what left my machine today" panel built from the transmission log;
- a confidence meter and a replay of approaches tried.

**Connectors.** The suggested first two:
- **HERA as an MCP server.** Expose tools such as `hera_submit_task`, `hera_status` and `hera_approve`, so a task can be started and approved from Claude, from a phone, or by Claude Code handing work over.
- **GitHub integration.** Take tasks from issues, open a pull request with the verified patch, and post the verdict as a comment.

Also suggested: Slack or push notifications with approve and reject buttons, a Notion task board, calendar-scheduled overnight runs, an email digest, and saving write-ups to Drive or Docs.

**Rule for every connector.** Each outbound call must go through the egress gateway (rule 3 in `README.md`), so default-deny privacy holds. The "what left my machine" panel then comes for free. An approval arriving through a connector must still come from a person, never from a model.

## Housekeeping
- Branch `diag-macos-start` on GitHub was a throwaway diagnostic. Delete it.
- Branches of merged pull requests can be deleted.
- The design and review history lives in the owner's Claude project, in `claude/02_REVIEW_STATE.md` and the numbered documents. It is not in this repository.
