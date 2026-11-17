# Local model host supervisor (S5b)

Code: `src/hera/modelhost.py`. Tests: `tests/test_modelhost.py`, with the fake model process
`tests/fake_model_server.py`. CLI: `hera host status | start | stop | reset`.

The supervisor starts, health-checks, leases, evicts, reloads and restarts **a child process that
speaks the OpenAI-compatible HTTP protocol on 127.0.0.1**. It knows nothing about the model. The real
MLX worker and the Mac memory measurement are S5. Everything here is tested against the fake server,
on any machine.

## What it does

- **Start.** Computes the context cap first (no room: state `no_room`, nothing is started). Refuses
  if anything already accepts connections on 127.0.0.1:`port` (it never adopts a process HERA did not
  start: content sent to a local destination skips the privacy gate, so a squatter on the port would
  receive private repository text). Spawns the command in its own session with stdin closed, stdout
  and stderr appended to `<home>/logs/modelhost.log` (never a pipe) and a scrubbed environment, then
  polls `GET /v1/models` on 127.0.0.1 until 200. A child that exits during startup, or is not healthy
  within `startup_timeout_s`, is killed and reaped (`HostStartError`).
- **Lease.** `lease()` is how a generation gets the model: it loads or reloads it if needed, replaces
  a dead or hung child (subject to the restart budget), marks the host busy so nothing can evict it,
  and yields the URL and the context cap. One lease at a time: a second one raises `HostBusy`
  at once (it never waits). `evict`, `before_sandbox_job` and `reset` raise `HostBusy` during a lease.
- **Evict.** SIGTERM to the child's whole process group, `5 s` of grace, then SIGKILL; the leader is
  reaped and the group is swept once more so no grandchild outlives it. Idempotent when nothing is loaded.
- **Strict alternation.** Reload takes about 3 s, so the model is evicted before every sandbox job
  (`before_sandbox_job`) and after `idle_evict_seconds` without use (`tick()`), and only one local
  generation runs at a time.
- **Health and restarts.** During a lease a loaded child gets `GET /v1/models` with `health_timeout_s`;
  `health_failures_to_restart` failures in a row kill it (event `hang`). A child that exited (event
  `crash`) is noticed by the next lease or by `tick()`; `tick()` only records it, the restart waits for
  the next lease. More than `max_restarts` restarts (after a crash, a hang or a failed start) within
  `restart_window_s` put the host in state `failed`: every lease raises `HostUnavailable` until a person
  runs `hera host reset`. The `failed` state is kept in `<home>/logs/modelhost.failed`, so it also holds
  for the next `hera run`.
- **No background threads.** `tick()` is called by whoever drives the host; tests drive it with a fake clock.

States: `evicted`, `starting`, `ready`, `failed`, `no_room`.

## How HERA uses it

- `[local_model].command` non-empty: `build_providers` builds one `ModelHost` and attaches it to every
  provider of tier `local`, kind `openai_compat`, whose URL is exactly `http://127.0.0.1:<port>` (scheme
  http, host literally `127.0.0.1`, the same port). A local provider pointing at that port without a
  command is unchanged. A command with no matching provider is reported in `built.absent` (key
  `[local_model].command`) and by `hera providers list`.
- A provider with a host takes a lease around each `complete()`, refuses (`ProviderError`, nothing sent,
  nothing recorded) a prompt whose estimated tokens (request bytes / 3, which over-counts) exceed the
  lease's context cap, always sends `chat_template_kwargs: {"enable_thinking": <setting>}` unless the
  caller passed its own, and turns `HostUnavailable`, `HostStartError` and `HostBusy` into `ProviderError`,
  so the orchestrator's provider-error handling applies and the coordinator never crashes because the host did.
- `sandbox.run` calls `before_each_job` hooks. `run_task` and `continue_task` install one that evicts every
  attached host before each sandbox job of the run: the verifications, the baseline run and the agent's
  `run_command` tool. A hook that fails is ignored; a job never fails because of the host. If a lease is
  active when a job starts (a second thread), the host is left alone and `evict_skipped` is logged.
  Not covered: sandbox jobs of commands that never go through `run_task` / `continue_task`
  (`hera verify`, `hera checks freeze`). No host is loaded in those processes.
- `hera run`, `hera task continue` and `hera providers check` evict the host in a `finally` block.

## Settings (`[local_model]` in hera.toml)

Every value is validated when the configuration is loaded: numbers must be finite and in range (a bool is
not a number), unknown keys are refused, and the error names the key.

| Key | Default | Meaning |
|---|---|---|
| `host_memory_limit_gb` | 9.0 | memory limit for the host (0 to 1024) |
| `max_context_tokens` | 12000 | upper bound on the context cap |
| `enable_thinking` | false | sent as `chat_template_kwargs.enable_thinking` (must be a bool) |
| `command` | `[]` | argv of the child; `{port}` and `{context}` are replaced. Empty: no supervised host |
| `port` | 8091 | 1024 to 65535 |
| `fixed_overhead_gb` | 8.0 | memory the model needs regardless of context |
| `kv_kb_per_token` | 81 | memory per context token, in KB |
| `min_context_tokens` | 2048 | below this many tokens the host does not load (`no_room`) |
| `reserve_gb` | 0.5 | free memory kept back when free memory is lower than the limit |
| `idle_evict_seconds` | 60 | idle time before eviction |
| `evict_before_sandbox` | true | evict before each sandbox job |
| `startup_timeout_s` | 60 | how long to wait for the first healthy answer |
| `health_timeout_s` | 2 | timeout of one health check |
| `health_failures_to_restart` | 3 | failed checks in a row before a kill and restart |
| `max_restarts` | 3 | restarts allowed within the window |
| `restart_window_s` | 600 | the window |

The child gets only `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR` and `SYSTEMROOT` (when set), plus
`HERA_MODELHOST_PORT` and `HERA_MODELHOST_CONTEXT`. No API key and nothing else from the parent environment.
If the real worker needs another variable (for example a model cache directory) that is a setting to add in S5.

## The context cap

Peak memory of the 9B model is `8.0 GB + 81 KB x context tokens` (decimal units: 1 GB = 1e9 bytes, 1 KB = 1e3
bytes). With the effective limit `min(host limit, free memory - reserve)` (just the host limit when free memory
is unknown):

    cap = floor((effective limit - overhead) x 1e9 / (kv x 1e3)), at most max_context_tokens;
    0 if that is below min_context_tokens  (no room: nothing is started)

| Limit | Cap |
|---|---|
| 8.5 GB | 6172 |
| 9.0 GB | 12345 (12000 with the default `max_context_tokens`) |
| 9.5 GB | 18518 |
| 10.5 GB | 30864 |

Free memory: Linux reads `MemAvailable` from `/proc/meminfo`; macOS sums free, inactive and speculative
pages from `vm_stat` (page size from its header line); anything else, or any failure, is "unknown".

## Files

- `<home>/logs/modelhost.log`: what the child prints (stdout and stderr). HERA writes nothing else there.
- `<home>/logs/modelhost.jsonl`: one JSON line per supervisor event: `{"time", "event", "pid", "detail"}`.
  Events: `start`, `ready`, `evict` (with the reason: `idle`, `before sandbox job`, `run ended`, ...),
  `crash`, `hang`, `restart`, `failed`, `no_room`, `port_in_use`, `start_failed`, `reset`, `evict_skipped`,
  `evict_failed`. Details are small facts (ports, exit codes, reasons). **No request or response content
  ever goes in either log**, and a test checks it.
- `<home>/logs/modelhost.failed`: exists while the host is `failed`.

## The CLI

`hera host status` prints the state, pid, port, context cap, free memory, restarts in the window, whether
something is already listening on the port, and the last error. `start` loads the model once, prints the
URL and the context cap, and evicts it again. `stop` evicts. `reset` clears `failed`. Exit status 2 when no
command is configured, 3 when the host cannot start.

The supervisor lives inside one process and does not adopt anything, so there is no state to share between
commands: `start` is a load check, `stop` only affects a host loaded by the same process, and `hera run`
loads the model by itself when a local provider needs it.

## Known limits

- **The model server is not sandboxed in this step.** It has network access and sees the repository text sent
  to it, so it must be trusted software. Sandboxing it with loopback-only networking is future work:
  Seatbelt can allow a local bind, but bwrap's network namespace would cut the coordinator off from it.
- A process that takes the port **between** HERA's check and the child's bind is not detected (the health
  check could then be answered by the other process while the child fails to bind).
- The memory limit is **not enforced** on the child by the supervisor. It only decides the context cap and the
  `HERA_MODELHOST_CONTEXT` value; the MLX worker has to set its own limit (S5).
- **No Mac measurement was made.** The numbers are the baseline's; the fake server uses no memory.
- If the coordinator is killed hard (SIGKILL), the child is orphaned and keeps the port. The next start is
  refused as `port_in_use` and the child has to be stopped by hand. A normal exit, an exception or Ctrl-C
  evicts it (a `finally` block and an `atexit` hook).
- The context cap counts the prompt only (estimated as request bytes / 3), not the tokens still to be
  generated.
- The health check proves the process answers `GET /v1/models`, not that it can generate.
