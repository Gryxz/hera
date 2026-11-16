# Implementer loop (S8a)

Code: `src/hera/agent/{tools,prompts,loop}.py`, `src/hera/providers/registry.py`. Tests:
`tests/test_agent_tools.py`, `tests/test_agent_loop.py`, `tests/test_provider_toolcalls.py`,
`tests/test_registry.py` (helper `tests/scripted_llm.py`).

**Nothing here has talked to a real model.** Every test runs against a 127.0.0.1 server that plays a
script of replies in the OpenAI-compatible or the Anthropic wire format. Expect small differences on
the first real call (see "First real call" in the README).

## What one attempt is

`run_attempt(conn, task_id=, attempt_seq=, provider=, toolbox=, messages=, max_turns=30, max_tokens=4096)`
runs a conversation between one model and one candidate worktree. The model replies with tool calls;
HERA runs them in the worktree and sends the results back; this repeats until the model calls
`submit` or `dispute`, or something stops it. The recovery ladder across attempts (retry, diagnosis,
escalation), filing disputes and `hera run` are step S8b; they call `run_attempt` repeatedly with a
new `attempt_seq`.

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
