# Implementer loop (S8a) and task orchestrator (S8b)

Code: `src/hera/agent/{tools,prompts,loop,policy,orchestrator}.py`, `src/hera/providers/registry.py`,
`src/hera/cli.py`. Tests: `tests/test_agent_tools.py`, `tests/test_agent_loop.py`,
`tests/test_provider_toolcalls.py`, `tests/test_registry.py`, `tests/test_policy_ladder.py`,
`tests/test_orchestrator.py`, `tests/test_cli_run.py` (helper `tests/scripted_llm.py`).

**Nothing here has talked to a real model.** Every test runs against a 127.0.0.1 server that plays a
script of replies in the OpenAI-compatible or the Anthropic wire format. Expect small differences on
the first real call (see "First real call" in the README).

## What one attempt is

`run_attempt(conn, task_id=, attempt_seq=, provider=, toolbox=, messages=, max_turns=30, max_tokens=4096)`
runs a conversation between one model and one candidate worktree. The model replies with tool calls;
HERA runs them in the worktree and sends the results back; this repeats until the model calls
`submit` or `dispute`, or something stops it. The recovery ladder across attempts (retry, diagnosis,
escalation), filing disputes and `hera run` are the orchestrator (below); it calls `run_attempt`
repeatedly with a new `attempt_seq`. `replay_messages` rebuilds the conversation of a finished attempt
from its stored turns so a retry can continue it (a call that `submit` cut off gets a fixed "not run"
result, because both wire formats want a result for every call).

## Tools

All paths are relative POSIX paths inside the worktree. `..`, absolute paths, any component named
`.git` (any case) or starting `_hera_`, and symlinks that leave the worktree are rejected, and
writes never go through a symlink at the target. Errors come back as results starting `error:`,
with fixed wording (no exception text, no repository content).

| Tool | Limits |
|---|---|
| `list_files(path=".")` | not recursive, 500 entries, reserved names hidden |
| `read_file(path, start_line=1, max_lines=400)` | 400 lines and 64 KB per call; binary files refused |
| `search(text, path=".")` | literal substring (no regular expressions), 200 hits, excerpt 200 characters, skips symlinks and binary files |
| `write_file(path, content)` | 256 KB; creates parent directories |
| `replace_in_file(path, old, new)` | `old` must occur exactly once; result at most 256 KB |
| `run_command(argv, timeout_s=60)` | in the sandbox with the worktree as cwd, no network, at most 120 s, last 16 KB of stdout+stderr; `{python}` in `argv[0]` is this interpreter; `pytest` runs without its cache directory |
| `run_visible_checks()` | commits the worktree, then calls the injected `verify_visible(commit)`; visible checks only |
| `read_visible_check(path)` | read-only view of the visible check directory |
| `submit(summary, declared_scope, twin_pattern="")` | ends the attempt |
| `dispute(check_id, requirement_id, argument)` | ends the attempt as "disputed" (S8b files it) |

## Statuses

`submitted`, `disputed`, `out_of_turns` (turn limit, or more than 2 MB of tool results sent in one
attempt: reason "context budget"), `no_progress` (three replies in a row without a tool call; the
first two get a nudge), `blocked` (the gateway refused; the reason is the gateway's fixed notice),
`budget_censored` (a cap was reached; not a failure), `provider_error` (the same turn failed twice).
A crash stores nothing: the row stays `running` and the next call resumes it. For statuses other
than `submitted`/`disputed` the `attempts.summary` column holds the fixed reason; for `disputed` it
holds the dispute as JSON.

## Resume, and why it does not repeat a paid call

Every turn is a row in `turns`: a SHA-256 of the request, the model's reply (stored before any tool
runs), the tool results, a checkpoint commit and the cost. Calling `run_attempt` again for the same
task and attempt number rebuilds each request from the stored history. If its hash differs from the
stored one the run stops with `ResumeMismatch` (changed messages, a different provider or model, an
edited stored reply). Otherwise the stored reply is used and the provider is not called. A turn that
has a reply but no tool results re-runs its tools, after `Worktree.reset_to` puts the worktree back to
the previous checkpoint (so half-finished effects from the crashed run are gone). A finished attempt is
returned as stored.

One window remains: between the provider's answer arriving and the turn row being written. A crash
there loses a reply that was already billed, and the resumed run asks again. That window is a few
statements long and cannot be closed without the provider's cooperation.

## Privacy

Every message to the model goes through the gateway as a labelled segment. Tool results are labelled
by the repository's class (`label_bytes`), exactly like file content, so a sensitive or unknown
repository's results can never go to a remote provider; fixed sentences (errors, acknowledgements) use
the built-in label. The model's own output is labelled by the repository's class too, including the
arguments of its tool calls: they are rendered as a second artefact (`<message id>#calls`) holding the
JSON line of each call and then every string inside the arguments as raw text, so scanners see line
starts exactly as the model wrote them. A tool result or call that a scanner flags blocks the next
request (status `blocked`). The tool definitions in a request are HERA's own fixed text and are not
separate segments.

## Providers from configuration

```toml
[providers.default]
kind = "anthropic"            # anthropic | openai_compat | grok
url = "https://api.anthropic.com/v1/messages"
model = ""                    # exact dated model id
key_env = "ANTHROPIC_API_KEY" # name of the environment variable; the key itself is never in a file
tier = "default"
extra_headers = { "anthropic-version" = "2023-06-01" }
```

`build_providers(config, gateway, ledger, env)` registers a destination for each usable provider and
returns them; a provider with a missing key variable, an empty model, an unpriced tier or an unsafe
setting is left out and `result.absent[name]` says why (never with a key value). Credentials in
`extra_headers` are refused. `hera providers list` shows name, kind, tier, host and whether the key
variable is set. `hera providers check <name>` sends the one fixed prompt "Reply with the single
word: ready" through the gateway and prints the reply, tokens and dollars; exit 0 means it worked.


# The task orchestrator (S8b)

`run_task(conn, TaskSpec(...), providers=, policy=, on_event=)` runs a task from objective to a final
status: it runs attempts, verifies every submitted candidate with the oracle (all checks, the
declared scope and twin pattern from the attempt) and decides the next step with the recovery policy.
Mock providers only: nothing here has been run against a real model.

**Candidates and attempts.** A candidate is a worktree `cand/<task>-c<n>`; an attempt is one
`run_attempt` call (`<task>-a<seq>`). A retry is a new attempt on the SAME candidate: its messages are
the previous attempt's full history (rebuilt from the stored turns) plus the oracle's feedback as a
user message, labelled with the repository's class because it quotes repository code. A fresh
candidate is a new worktree and a new conversation. `ToolBox.run_visible_checks` calls a closure over
`oracle.verify(..., include_hidden=False)` on the worktree's commit.

## The ladder (hypothesis H-R1, `agent/policy.py`)

Rungs: (1) a fresh default-tier candidate; (2) retries of that candidate with the oracle's feedback;
(3) one strong-tier diagnosis (one call, no tools, purpose `diagnose`) and a fresh default candidate
whose first message ends with the heading "Diagnosis from a reviewer (advice, may be wrong)"; (4)
escalation: a fresh strong-tier candidate; then stop. A critique or a diagnosis never changes a
verdict; it only changes what the next attempt is told.

| What happened | What happens next |
|---|---|
| `PASS` | task `PASSED` |
| `FAIL`, `REGRESSION` | retry with feedback, at most 2 per candidate; the 2nd only if the set of failing check ids changed since the previous verification, otherwise straight to diagnosis. After the retries: diagnosis, then a fresh default candidate (1 retry), then escalation |
| `OVERFIT` | 1 retry with requirement-level feedback only (requirement ids, no visible-check text); then a fresh candidate from `default_alt` if configured, else the same provider in a new conversation (1 retry); then escalation |
| `TAMPER` | the candidate is discarded, never retried; a fresh candidate (`default_alt` if configured); then escalation |
| `out_of_turns`, `no_progress` | a fresh candidate on the same provider (the context is the problem); then escalation |
| `disputed` | the dispute is filed with `oracle.disputes.open_dispute` (visible checks only) and the task is `AWAITING_DISPUTE`. An unknown or hidden id gets one fixed "Dispute rejected" retry, once; a second rejection counts as `no_progress` |
| `ORACLE_DISPUTED` | `AWAITING_DISPUTE` |
| `UNVERIFIABLE` | `NEEDS_HUMAN` |
| `blocked` (privacy) | stop `BLOCKED_PRIVACY`, unless a local provider exists: one fresh candidate there, and from then on no diagnosis and no escalation |
| `budget_censored` | `BUDGET_CENSORED` (a cap is never a failure) |
| `provider_error` | one move to the next rung (a fresh candidate on `default_alt` if configured, else escalation), then `PROVIDER_ERROR` |
| ladder exhausted | `FAILED` |

Hard limits come from `[recovery]` in hera.toml (`max_retries = 2`, `max_candidates_per_tier = 2`,
`max_turns = 30`, `max_tokens = 4096`); the remaining numbers (retries for the candidate after a
diagnosis: 1, after other fresh default candidates: 1, for the escalated candidate: 0, the progress
rule) are the fields of `policy.Rules`. The per-task dollar cap of the ledger bounds everything.
`decide(state, outcome, caps, limits, rules)` is pure; `tests/test_policy_ladder.py` tests it alone.

## Privacy routing (before any call)

`compute_routing` takes the repository's class from the policy, builds its label and calls
`privacy.gate.permitted_destinations` with the current grants. Per provider role (`default`,
`default_alt`, `strong`, `local`; `select_roles` picks them by name, then by tier): a provider is used
only if its destination is permitted or loopback. If the default provider is not permitted and a
`local`-tier provider exists, the implementer runs there and the diagnosis and escalation rungs that
have no permitted destination are skipped; if neither exists the task ends `BLOCKED_PRIVACY` without
any request. The decision is the first event of the task (`routing`; destination names only). A
personal or proprietary repository has no content hash at this point, so without a grant that names
it routes local-only; the gateway still checks every message again.

## Task events and resume (schema v5, table `task_events`)

`task_events(task_id, seq, at, kind, detail_json)`; kinds: `routing`, `fresh_candidate`, `retry`,
`escalate` (each a plan for the next attempt), `attempt_started`, `attempt_ended`, `verified` (receipt
id, verdict, failing check ids, the feedback that was or would be sent), `diagnosis` (the text, or why
it was skipped), `dispute_filed`, `dispute_rejected`, `stopped` (final status). `tasks.status` follows.

A decision is never stored on its own. `run_task` folds the log: the ladder state comes from
replaying the logged outcomes through `policy.decide`, so the next step is a function of the log. A
crash then continues like this: mid-attempt, `run_attempt` resumes the open attempt (one request per
turn, nothing repeated); after the submit but before the verification, the candidate is verified once;
after the verification, the stored verdict and feedback are used (no second verification) and the
decision is recomputed; after a plan or a diagnosis, the plan is carried out. A task that has
stopped is returned as stored (start a new task id to run it again: a stop for `BUDGET_CENSORED` or
`AWAITING_DISPUTE` is not continued automatically). The window in which a paid call can still be
repeated is the one described above, for the diagnosis call as for a turn.

## Walkthrough: from a spec to a verdict

1. Write a spec (invented example):

   ```toml
   [[requirement]]
   id = "R-1"
   text = "parse_amount('12.50') returns 12.5"

   [[check]]
   id = "chk-r1-basic"
   requirements = ["R-1"]
   visibility = "visible"          # visible | hidden
   file = "test_parse_amount.py"   # relative to the spec file; a test_*.py or *.contract.toml file
   node = "test_basic_amount"
   kind = "new"                    # new | regression (default new)
   runner = "pytest"               # pytest | contract (default pytest)
   ```

2. `hera checks freeze --task T --repo /path/to/repo --spec spec.toml [--base COMMIT] [--allow-unsandboxed]`
   creates the task workspace if needed, runs the checks on the base commit in a scratch store (refusing,
   with exit 3 and per-check health, if any check is flaky or invalid; `--skip-validation` overrides),
   freezes them and prints `check-set hash: <hash>`. The repository must be in the privacy policy with
   its class, or it counts as sensitive and no remote provider will be used.
3. `hera providers check <name>` for each provider in hera.toml (the keys come from the environment).
4. `hera run --task T --checkset HASH --objective "..." [--max-usd X] [--allow-unsandboxed]` prints each
   event as it happens, then the final status, verdict, dollars, and the `in_process_only` and
   unsandboxed notes of the last verification. Exit 0 for `PASSED`, 3 for any other final status, 2 for
   a configuration error (no provider for the default tier, unknown check set, no configuration).
5. `hera task status T` prints the events and the current status.
