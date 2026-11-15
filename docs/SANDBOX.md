# Workspaces and sandbox (S6)

Code: `src/hera/workspace.py`, `src/hera/sandbox.py`. Tests: `tests/test_workspace.py`, `tests/test_sandbox.py`.

## The two guarantees

1. **Nothing from the implementer's workspace reaches verification except the commit.** A verification
   checkout is a new directory under `<HERA_HOME>/work/<task>/verify/`, built from the commit's tree
   objects fetched by full commit hash from the task's bare repository. It has no `.git`, so
   candidate-controlled git config and hooks cannot run there. Untracked files, ignored files,
   virtualenvs and caches are absent. Commits that are not descendants of the task's base commit,
   short hashes, unknown hashes, `.git` entries and symlinks that resolve outside the checkout are
   rejected. Submodules are never initialised (a gitlink becomes an empty directory).
2. **Candidate code runs with no network, limited resources, and no view of the check store, other
   worktrees, the home directory or credentials.**

Git itself is also hardened: every call uses `core.hooksPath=/dev/null`, `core.fsmonitor=false`,
`protocol.file.allow=user`, a scrubbed environment (no system or global config) and an explicit
`--git-dir`/`--work-tree` (the `.git` file inside a worktree is never trusted). Config keys that can
run programs (filters, diff/merge drivers, aliases, `core.sshCommand`, …) are removed from the shared
repository config before each operation on a candidate worktree.

## What each mechanism enforces

| | sandbox-exec (macOS) | bwrap (Linux) | none |
|---|---|---|---|
| Network | all `network*` denied | all namespaces unshared | **not isolated** |
| File writes | only cwd, private temp dir, `writable` | only cwd, private temp dir, `writable` (everything else is read-only or absent) | **not isolated** |
| File reads | contents unreadable under the home directory and temp roots, except cwd, `read_only`, `writable` and the Python installation; deny list applied last | allow-list: system directories, Python prefix, binds you pass; everything else does not exist; home and `/tmp` are empty tmpfs | **not isolated** |
| Deny list | `~/.ssh ~/.aws ~/.config ~/.gnupg ~/.netrc ~/.hera/policy ~/.hera/checks`, the HERA policy and checks directories, plus `deny_read` | same, hidden by tmpfs or `/dev/null` binds | not applied |
| Environment, rlimits, wall timeout, output cap | yes | yes | yes |

`none` means exactly that: it **isolates nothing**. The child can open sockets and read or write
anything the user can. It is available only with `allow_unsandboxed=True`, for tests and development.
The verifier must not use it for real runs.

Common to all mechanisms: only `PATH`, `HOME` (a private temp dir), `TMPDIR`, `LANG`/`LC_ALL`,
`PYTHONDONTWRITEBYTECODE`, `PYTHONHASHSEED` and keys passed in `env` reach the child. Limits are
CPU seconds, file size, process count, address space (Linux only), wall-clock time (the whole
process group is killed with SIGKILL) and a per-stream output cap.

## Known limits

- `sandbox-exec` is deprecated by Apple but present on current macOS. The profile uses
  `file-read-data` denies, so file metadata (existence, size) remains visible; broad temp roots are
  denied rather than every possible location. The macOS path has not been run by its author (see the
  pull request); CI is the first real test.
- Unprivileged user namespaces are disabled on some Linux systems (and restricted by default on
  recent Ubuntu releases), so `bwrap` can be installed yet unusable. `detect_mechanism()` probes it
  and returns `none` then; tests that need isolation skip.
- Resource limits are not a memory guarantee on macOS: `RLIMIT_AS` is skipped there. CPU and file
  limits are per process, so many children can add up; the wall-clock kill is the backstop.
  `max_procs` is relative to the processes the user already runs.
- Signals: the macOS profile does not restrict signalling other processes of the same user.
- A kernel or sandbox escape is out of scope.
- `commit_all` and diff helpers run git in the coordinator against a repository the implementer can
  write to. The hardening above removes the known execution paths, but a new git feature could open
  another one.
- For milestone 0.2 the verifier should move to a per-run VM or container.
