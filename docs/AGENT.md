# Implementer loop (S8a) and task orchestrator (S8b)

Code: `src/hera/agent/{tools,prompts,loop,policy,orchestrator}.py`, `src/hera/providers/registry.py`,
`src/hera/report.py`, `src/hera/cli.py`. Tests: `tests/test_agent_tools.py`, `tests/test_agent_loop.py`,
`tests/test_provider_toolcalls.py`, `tests/test_registry.py`, `tests/test_policy_ladder.py`,
`tests/test_orchestrator.py`, `tests/test_cli_run.py`, `tests/test_continue.py`,
`tests/test_cli_continue.py`, `tests/test_dispute_cli.py`, `tests/test_report.py` (helper
`tests/scripted_llm.py`).

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
it was skipped), `dispute_filed`, `dispute_rejected`, `stopped` (final status), `continued` (see
"Continuing a stopped task"). `tasks.status` follows.

A decision is never stored on its own. `run_task` folds the log: the ladder state comes from
replaying the logged outcomes through `policy.decide`, so the next step is a function of the log. A
crash then continues like this: mid-attempt, `run_attempt` resumes the open attempt (one request per
turn, nothing repeated); after the submit but before the verification, the candidate is verified once;
after the verification, the stored verdict and feedback are used (no second verification) and the
decision is recomputed; after a plan or a diagnosis, the plan is carried out. A task that has
stopped is returned as stored by `run_task` and `hera run`; only `continue_task` / `hera task continue`
goes on after a stop, and only for the stops listed below. The window in which a paid call can still be
repeated is the one described above, for the diagnosis call as for a turn.

## Privacy of personal and proprietary repositories: local-only by design

In `hera run`, a repository whose class is personal or proprietary is routed to a local provider only.
This is a design decision, not a missing feature. The user's privacy rule binds an approval to the
exact content hash it was given for, a destination and an expiry. An implementer changes the content
on every turn (its own edits, tool results that quote them, the model's replies), so no approval
given beforehand can cover an agent loop: the text that would be sent does not exist yet when the
approval would have to be made. Without a local provider such a task ends `BLOCKED_PRIVACY` before any
request. A remote model can help on such a repository only through descriptions that a person has read,
sanitised and approved as `declassified` (the mechanism exists in the gateway); building that path into
the orchestrator is not done yet.

## Continuing a stopped task

`continue_task(conn, task_id, spec=, providers=, policy=, max_usd=None, on_event=None)` and
`hera task continue ID [--max-usd X] [--allow-unsandboxed]` go on after a stop that a person or the
clock can have changed. Every other stop (`PASSED`, `FAILED`, `NEEDS_HUMAN`, `BLOCKED_PRIVACY`) is
refused with a message and exit 3; nothing is logged and nothing is sent. So is a stop that still has
no room (below), and a task that has not stopped (`hera run` resumes those). The continuation is one
`continued` event appended after the `stopped` event: the log is append-only, nothing is rewritten.
`run_task` folds the log as before, so a continuation resumes like any run; continuing, crashing and
continuing again repeats no request (`tests/test_continue.py` checks the request bodies).

Caps fail closed. `--max-usd` must be a finite amount above 0, and budget caps and prices in
`hera.toml` must be finite and at least 0 (fractions between 0 and 1): every comparison with NaN is
false, so `nan` (which TOML accepts) would otherwise switch a cap off. The ledger also refuses a NaN
or negative call estimate. Every provider of a task must share one ledger and one gateway, because
a continuation's cap is applied to the ledger the task uses. `--max-usd` given to a continuation is
recorded in the `continued` event whatever the stop was, so an interrupted continuation resumes
under the same cap.

| Stopped as | When it can continue | What happens |
|---|---|---|
| `BUDGET_CENSORED` | The ledger would allow a call now (checked before anything is logged; if not, the message names the cap and the exit is 3). `--max-usd X` replaces the per-task cap for this continuation only and is recorded in the `continued` event; a continuation that is interrupted resumes under that cap, then it is gone. | An attempt that was cut off goes on as a new attempt on the same candidate, in the same conversation, with a fixed "continued" message (no message if the cut-off attempt had never been answered). It does not use up a retry. If the diagnosis call was what the cap refused, the diagnosis is asked again. |
| `AWAITING_DISPUTE` | Only once the dispute is resolved (`hera dispute resolve`); an open dispute refuses. A resolved dispute is used once. | `upheld`: the candidate that disputed gets one retry, told "your dispute was reviewed and the check stands" and to keep working; the ladder then goes on as usual. `check_amended`: the task's check set becomes the new one recorded on the dispute (named in the `continued` event), the last candidate is verified again against it and the ladder goes on from that verdict. `requirement_amended`: the task stops again as `NEEDS_HUMAN`, reason "requirement changed: start a new task". A dispute that was opened outside the run (the verdict was `ORACLE_DISPUTED`, no candidate filed it) is handled like `check_amended` with the same check set: the candidate is verified again. |
| `PROVIDER_ERROR` | A provider for the next rung exists and its destination is permitted for the repository now. | The ladder moves to the next rung it would have used (a fresh candidate on `default_alt`, else the escalation). If no rung is left it refuses. A diagnosis call that failed twice goes on without the diagnosis. |

The routing decision is not recomputed: a provider added after the stop is used only for the rung
the logged routing already allows.

## Disputes

An implementer can dispute a VISIBLE check (`dispute` tool); the orchestrator files it and the task
waits as `AWAITING_DISPUTE`. A person resolves it:

- `hera dispute list [--open]`: id, task, check id, requirement, status, opened time (UTC) and the first
  200 characters of the argument on a line of its own (the argument is model text: shown as one line of
  printable characters). Resolved disputes also show the outcome and the resolver.
- `hera dispute resolve ID --outcome upheld|check_amended|requirement_amended --resolver human:<name>
  [--new-checkset HASH]`. From the command line the resolver must be a person (`human:` and a name).
  `oracle.disputes.resolve_dispute` still applies its independence rule: the resolver may not be the
  check's author model or any model that implemented the task (the models in the task's `attempts`
  rows are all passed). `check_amended` needs the hash of a check set already frozen
  (`hera checks freeze`) for the same task and different from the disputed one; the new set is
  recorded as superseding the old. Refusals (not a person, unknown dispute, already resolved, unknown
  or foreign hash, a hash with an outcome other than `check_amended`) exit 2 and change nothing.

Then `hera task continue TASK` (above).

## Reading the report

`hera report [--since YYYY-MM-DD] [--task ID] [--json]` prints the learning ledger: one record per
finished attempt (`report.attempt_records`), joined from `attempts`, `turns`, `task_events`,
`receipts`, `verdicts`, `attributions` and `model_calls`; nothing in a record is model text. `--json`
prints the summary and every record. Nothing is stored separately: it is recomputed on every call.

- **Rates** are `passed/total (percent)`, always with the counts, and marked `(n<5)` when the total
  is below five. A rate over three attempts is an anecdote; the mark is there so nobody reads it as a
  measurement. Dollar averages carry the mark when they average fewer than five tasks.
- **Pass rate per tier and per model** counts every finished attempt; an attempt that never reached a
  verification (cap, privacy block, provider error, out of turns) counts as not passed. The
  **first-attempt pass rate** looks at attempt 1 of each task only, which is what a user of a single
  model would have got.
- **Verdicts** are the oracle's; attempts that ended without one are listed by their end status.
- **Attribution** covers verdicts other than `PASS` (a pass has none). The **`UNKNOWN` rate** is the
  share of those the deterministic rules could not explain; it is the number to watch, because every
  `UNKNOWN` is a failure HERA cannot learn from. Attempts that never reached a verification have no
  attribution at all and are counted apart.
- **Dollars**: the total is the sum of the cost ledger over the tasks in the report; per task is that
  divided by the number of tasks; **per verified task** divides the same total by the tasks that have a
  `PASS` reached with a sandbox, so the cost of everything that failed is paid for by the passes. A
  `PASS` without a sandbox is not "verified": a hostile candidate could have tampered with it.
- **Passes resting only on in-process checks**: a pytest-only check set can be forged by a candidate
  that patches pytest, so a pass whose requirements rest on such checks alone is weaker. **Verdicts
  reached without a sandbox** is the same warning for isolation.
- **Changes outside the declared scope** counts verified attempts whose stored audit flagged a changed
  path outside the scope the implementer declared.
- **Retries** are attempts that continued a candidate with feedback; "succeeded" passed, "failed"
  reached a verdict that was not a pass. **Diagnosis**: calls, their dollars and how often the next
  candidate (told the diagnosis) passed. **Escalations**: strong-tier candidates and how often they
  passed. The last line says how often an attempt that did not pass was followed by a pass on the same
  task.

All of these are small numbers until the pilot (S10): read them as a check that the ledger works, not
as conclusions.

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
6. `hera task continue T [--max-usd X]` after a stop that can continue; `hera dispute list|resolve`
   for disputes; `hera report` for the ledger (all described above).
