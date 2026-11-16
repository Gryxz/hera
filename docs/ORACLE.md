# Acceptance oracle (S7a: checks, frozen store, pytest adapter, validation; S7b: audit, verify, verdicts, disputes; S7c: contract checks)

Code: `src/hera/oracle/model.py`, `checkstore.py`, `adapters/pytest_adapter.py`, `validation.py`
(S7a); `audit.py`, `verify.py`, `disputes.py` (S7b); `contract.py`, `contract_runner.py` (S7c).
Tests: `tests/test_checkstore.py`, `tests/test_pytest_adapter.py`, `tests/test_validation.py`
(S7a); `tests/test_audit.py`, `tests/test_verify_e2e.py`, `tests/test_disputes.py` (S7b);
`tests/test_contract.py`, `tests/test_contract_runner.py` (S7c), with
the invented toy project in `tests/toyrepo.py`.

## Objects

- `Requirement(id "R-n", text)`.
- `DraftCheck`: one check is one pytest test function or one contract check: `check_id`,
  `requirement_ids`, `visibility` (`visible` | `hidden`), `relpath` (POSIX, relative; `test_*.py` for
  runner `pytest`, `*.contract.toml` for runner `contract`), `node` (function name, or the contract
  node), `author_role`, `author_model`, `kind` (`new`: must fail before the change; `regression`: must
  pass before and after), `runner` (`pytest`, the default, or `contract`). A file holds checks of one
  visibility and one runner only. `checks_by_runner(checkset, runner)` selects them.
- `CheckSet`: hash, task id, requirements, checks, `files` (relpath to sha256), adapter, root path,
  `uncovered` (requirements with no check; reported, not an error).
- Validation errors name the field, never file contents.

## Freezing, hash and layout

`checkstore.freeze` hashes a canonical JSON manifest (sorted keys, compact separators) of the task id,
adapter, requirement ids with the sha256 of each text, all check fields, file hashes and the sha256 of
the summary text. Same inputs give the same hash (inputs are sorted, so order does not matter); one
changed byte anywhere changes it.

```
<HERA_HOME>/checks/<task_id>/<hash>/visible/<relpath>   files with visible checks
                                  /hidden/<relpath>     files with hidden checks
                                  /manifest.json        manifest plus requirement and summary text
```

Files are mode 0444 and directories 0555 (this deters accidents; against a hostile process the
protection is the sandbox's deny list for the check store). Re-freezing identical content is a no-op;
a hash directory whose content differs raises `CheckStoreTampered`. `verify_integrity` re-derives the
hash from the manifest and re-hashes every file; a modified, missing or extra file, directory or
symlink raises. Rows go to `requirements`, `checksets`, `checks` with no schema change, which forces
two conventions: `requirements.id` is `<task_id>:<R-n>` (the column is a global primary key) and
`checks.requirement_id` holds the check's requirement ids joined by commas. `supersede` sets
`checksets.supersedes` for later disputes.

The `runner` of every check is part of the manifest, so it is part of the hash. At freeze time every
contract file is parsed with `contract.load_contract_file`; each contract `DraftCheck` must name a node
that exists in its file and every node of the file must be named by exactly one `DraftCheck` (no silent
extra checks).

Only `visible_dir` may ever be handed to an implementer workspace. `hidden_dir` is for oracle runs.

## What `prepare` does to a verification checkout, and why

The checkout is a fresh copy of the candidate commit. A candidate that edits the things that decide
what runs and how results are judged would win without doing the work, so `prepare`:

0. deletes any `_hera_checks` and `_hera_results` the candidate committed (they are the oracle's names);
1. restores every protected path that existed at base to its base content, and deletes protected files
   the candidate added (new `conftest.py`, `pytest.ini`, `sitecustomize.py`, `pytest_*.py`, tests ...).
   Symlinks are never followed and are replaced; the report lists restored, deleted and
   modified-protected paths for the diff audit in S7b. Protected globs: `conftest.py`, `**/conftest.py`,
   `pytest.ini`, `tox.ini`, `setup.cfg`, `tests/**`, `test/**`, `**/test_*.py`, `**/*_test.py`,
   `.github/**`, `sitecustomize.py`, `usercustomize.py` (also nested), top-level `pytest_*.py`.
   Matching is case-insensitive;
2. verifies the frozen files against their hashes and copies them to `_hera_checks/visible` (and
   `hidden` when `include_hidden`);
3. writes the sentinel pair `_hera_checks/_sentinel/test_hera_sentinel.py`;
4. writes an oracle-owned `_hera_checks/pytest.ini`. The project's configuration is never read.

The run is `python -P -m pytest -c _hera_checks/pytest.ini --rootdir . --confcutdir _hera_checks ...`
with plugin autoload off. `-P` keeps the working directory off `sys.path` at startup, so candidate files
cannot shadow pytest or the standard library; project code is found through the oracle ini
(`pythonpath` for the checkout root and `src/`). `--confcutdir` keeps any `conftest.py` outside
`_hera_checks` from loading at all, which makes restoring it defence in depth.

## Result integrity

`RunReport` carries, besides per-check results: `executed_ids` (a manifest check that never produced a
testcase is absent, and S7b treats that as tampering; on a timeout unseen checks are `TIMEOUT` and
included), `sentinels_ok` (`test_hera_must_pass` passed exactly once and `test_hera_must_fail` failed
exactly once), `exit_code_consistent`, `raw_unmatched` (testcases that match no check) and the
per-check `output_digest` (sha256 of failure text; empty for passes). `exit_code_consistent` requires
exit code 1 (0 when nothing failed; 2 to 5 never), AND that the pass, fail-or-error and skip counts on
pytest's final summary line equal the counts parsed from the JUnit file. The JUnit file is refused if
missing, over 5 MB, containing NUL bytes, a DOCTYPE or an ENTITY, or not parseable; the report then has
no results and both flags false. Parametrized checks pass only if every case passes.

Known limit of the summary check: a teardown error after a passing call is counted differently by
pytest and in the XML and would read as inconsistent.

## Feedback redaction

`feedback_for_implementer`: failing visible checks give check id, requirement ids and the first 2,000
characters of failure text; failing hidden checks give requirement ids only, de-duplicated, with no
check id, file name or text. Tested with hidden names, file names and assertion values.

## Validation on the base commit

`validate_on_base` runs the full set `runs` times (default 3) in fresh base checkouts. Per check:
status differs between runs: FLAKY. kind `new`: passes every run: WEAK ("passes before the change");
fails: VALID; errors with a final exception of ImportError, ModuleNotFoundError, AttributeError or
NameError (never SyntaxError) because the feature is missing: VALID (rule `missing_feature`); any other
error, skip or timeout: INVALID. kind `regression`: passes every run: VALID; anything else on base:
INVALID. A sentinel or integrity failure, an unparsable result, a timeout or an unexecuted check sets
`environment_broken`. `ok` means no flaky or invalid checks and a sound environment. Every report
entry names the rule that fired.

## Verification sequence (`verify.verify`)

`verify(conn, repo, checkset, commit, ...)` returns a `VerificationOutcome`:

1. `checkstore.verify_integrity`. A tampered store raises `CheckStoreTampered`; that is not a verdict
   about the candidate and nothing is persisted.
2. A fresh verification checkout of the commit; `prepare` (base protected paths restored);
   `run` in the sandbox (mechanism auto-detected; `allow_unsandboxed=True` is needed where there is none).
   The pytest adapter runs only if the check set has pytest checks (otherwise the sentinel and exit-code
   flags are True: there is nothing in-process to forge). If it has contract checks, a second fresh
   checkout is prepared and the `ContractRunner` runs them (see "Contract checks"); results, executed ids
   and failure texts of both runners are merged and the manifest ids are all included checks of both.
3. Diff audit (below), given the `PrepareReport` so the two views cannot disagree silently.
4. Regressions: the project's PRE-EXISTING tests (the `test_*.py` / `*_test.py` files of the base tree)
   run in a SECOND fresh checkout of the commit (protected files restored by `prepare`, hidden checks
   not copied, `_hera_checks` removed before the run), under the sandbox, with an oracle-owned ini and
   the project's own `conftest.py` files loaded (`--confcutdir` at the checkout root). The result is
   compared with `run_baseline` (the same run on the base commit, twice): a test that passed in both
   baseline runs and does not pass now (including a test that no longer exists or a module that no longer
   imports) is a regression. Tests with differing baseline results are known-flaky and excluded; tests
   that did not pass at base are ignored (`baseline_not_passing`). With no pre-existing tests the
   receipt says so. If the baseline run itself produces no usable results, regression detection is
   unavailable and the receipt notes that (it does not block the verdict). If the candidate's project
   run produces no results, every baseline-passing test counts as not passing. The project's
   third-party pytest plugins are not loaded (autoload is off), same as for the checks.
5. A `hera.types.Receipt` from results (with per-check health read from `checks.health`),
   audit hits and flags, regressions, requirements without a counted check (when hidden checks are
   excluded, requirements only they cover count as uncovered), and `dispute_open`
   (the argument or any open dispute of the task). Check durations are not part of the receipt.
6. `verdict(receipt)` (pure) and attribution.
7. Persistence: `receipts` (`id` is the sha256 of the canonical receipt plus evidence JSON, so the
   same content is the same row; `receipt_json` holds the receipt, the audit findings, `timed_out`,
   the sandbox mechanism, notes, baseline summary and prepare lists, and timings that are not hashed),
   `verdicts`, `attributions`. `env_hash` is the sha256 of interpreter version, adapter name and sandbox
   mechanism. `load_receipt` rebuilds the `Receipt`, so `verdict(load_receipt(...))` reproduces the stored verdict.
8. The checkouts are removed unless `cleanup=False`.

## Audit rules (`audit.audit`)

Pure git/diff analysis; findings carry a rule, a path and a short fixed description, never file content.
BLOCK findings go to `Receipt.audit_block_hits` (verdict TAMPER); flags go to `audit_flags` and never change a verdict.

| Rule | Kind | Fires when |
|---|---|---|
| `protected_path_modified` | BLOCK | a path matching `adapter.is_protected` existed at base and its content differs at the commit (mode-only changes are ignored), unless matched by `allowed_protected` |
| `protected_path_deleted` | BLOCK | a protected base path is deleted, unless allowed |
| `protected_path_added` | BLOCK | a candidate-ADDED `conftest.py`, `sitecustomize.py` or `usercustomize.py` (any depth), top-level `pytest.ini`, `tox.ini`, `setup.cfg`, top-level `pytest_*.py`, or anything under `_hera_checks/` or `_hera_results/`. `allowed_protected` does NOT apply |
| `prepare_restored` / `prepare_deleted` | BLOCK | `prepare` restored or deleted a path that no diff rule reported (allowed paths excluded for restores) |
| `test_file_added` | flag | another added file under a protected pattern (a new test file) |
| `skip_marker_added` | flag | added lines in a `.py` file containing `pytest.mark.skip` (also `skipif`), `pytest.mark.xfail`, `pytest.skip(`, `unittest.skip` or `@skip` |
| `out_of_declared_scope` | flag | with `declared_scope` globs, every changed path matching none of them |
| `debris` | flag | added `*.orig *.rej *.bak *.tmp *.log *.swp`, `.DS_Store`, names starting `scratch`, `debug`, `tmp_`; added `.py` lines with `breakpoint()`, `import pdb`, `pdb.set_trace()`; added lines starting `print(` in files that are neither protected (tests) nor `__main__.py` |
| `dependency_manifest_changed` | flag | `requirements*.txt`, `pyproject.toml`, `setup.py`, `setup.cfg`, `Pipfile*`, `poetry.lock`, `uv.lock`, `package.json` and common lockfiles |
| `twin_remaining` | flag | with a literal `twin_pattern`, non-protected text files at the commit that still contain it (line counts only) |

Added lines come from `TaskRepo.diff_added_lines` (NUL-separated paths, literal pathspecs, hardened git,
binary files skipped). `allowed_protected` only exempts the audit: the verification checkout still
restores those paths to base content, so an allowed edit to a test file has no effect on the run.

## The timeout rule

A run that hits the wall limit cannot produce a JUnit file, so the adapter reports `sentinels_ok` and
`exit_code_consistent` false. That is not tampering. When the check run timed out the receipt is built
with both true; the TIMEOUT statuses (which count as not passing) produce FAIL or OVERFIT through the
verdict function, and the outcome records `timed_out=True`. The raw adapter values stay in the stored
evidence. A timeout never becomes TAMPER unless the audit blocks. The rule is per runner: a timeout of the
pytest run excuses only the pytest integrity flags; a timeout in a contract check produces `TIMEOUT`
statuses and excuses nothing else, so it cannot hide a failed sentinel or an inconsistent pytest summary.

## Attribution (deterministic; first matching rule wins; never defaults to IMPL_WRONG)

| Condition | Label (confidence, rule) |
|---|---|
| no results parsed, not timed out, no audit block | `UNKNOWN` (low, `no_results_parsed`), `environment_suspect=True`, verdict left as computed — a broken environment and a candidate that kills the runner look the same here, so neither is blamed |
| TAMPER | `IMPL_TAMPER` (high `audit_block`; medium `result_integrity`) |
| OVERFIT | `IMPL_OVERFIT` (high, `unseen_case_failed`) |
| FAIL | `IMPL_WRONG` (high, `visible_check_failed`; medium when timed out) |
| REGRESSION | `IMPL_WRONG` (high, `baseline_test_regressed`) |
| ORACLE_DISPUTED | `UNKNOWN` (`dispute_open`) |
| UNVERIFIABLE | `UNKNOWN` (`no executable checks`) |
| PASS | none; plus an advisory `CHECK_WEAK` row when a counted check is WEAK |
| anything else | `UNKNOWN` |

Flaky and invalid checks are excluded by the verdict function, so they can never produce an `IMPL_*` label.

## Disputes (`disputes.py`)

Schema version 3 adds the `disputes` table. `open_dispute` accepts only a VISIBLE check of the task's
check set (a hidden, unknown or mismatched id raises the same `ValueError`, so it does not confirm that an id
exists); the argument is capped at 4,000 characters. An open dispute makes verdicts `ORACLE_DISPUTED`
(`has_open_dispute`; `verify` looks it up itself). `resolve_dispute` takes `upheld`, `check_amended`
(needs `new_checkset_hash`, recorded with `checkstore.supersede`) or `requirement_amended` (resolver must
start with `human:`); the resolver may not equal the check's author model or the implementer model
(passed in), compared case-insensitively.

## What the implementer is told

`verify` builds feedback from `feedback_for_implementer` plus the regressed test ids and the BLOCK
findings (rule and path; the candidate did those). `render_feedback` turns it into plain text of at most
6,000 characters: verdict, reasons, blocked changes, failing visible checks with up to 2,000 characters of
failure text, requirement ids only for unseen failures, regressed tests, audit flags. It contains no hidden
check id, file name, function name, input or expected value (tested on the hard-coded candidate); the
oracle's checkout location is replaced by `<checkout>`; sentinel and exit-code mechanics are reported
only as "the integrity checks on the verification run failed".

## Contract checks (S7c)

Native pytest checks run candidate code in the runner's own process, which is why they can be forged
(below). A contract check is declarative: the candidate's code runs as a SEPARATE sandboxed subprocess
that is given the check's inputs, and the comparison of its output with the expectations happens in the
oracle (the HERA coordinator process), which never imports, executes or evaluates anything from the
checkout. A candidate can do anything inside its own process; it cannot change what the oracle compares,
and it never sees the expected values of a hidden check.

### File format (`*.contract.toml`, parsed with `tomllib`)

```toml
[[check]]
node = "parse_twelve_fifty"          # [A-Za-z_][A-Za-z0-9_]{0,63}, unique within the file
argv = ["{python}", "-c", "import calc, sys; print(calc.parse_amount(sys.argv[1]))", "12.50"]
stdin = ""                           # optional, at most 64 KB
timeout_s = 10                       # optional, default 10, above 0 and at most 120
env = { LANG = "C.UTF-8" }           # optional
files = { "input.json" = "{...}" }   # optional; written into the run directory first (relative paths, at most 64 KB each)

[check.expect]                       # at least one expectation
exit_code = 0
stdout_equals = "12.5\n"             # exact match after normalising \r\n to \n
stdout_contains = ["12.5"]
stderr_contains = ["ValueError"]
stdout_json = { value = 12.5 }       # stdout parsed as JSON must EQUAL this (any TOML value)
stdout_json_subset = { ok = true }   # every key/value must appear in the parsed JSON object (recursive for tables)
files_equal = { "out.txt" = "done\n" }       # file in the run directory after the run
files_contain = { "log.txt" = ["started"] }
```

- `{python}` is replaced by the interpreter the oracle chose (`sys.executable` for now) in every argv
  element that is exactly `{python}`. There are no other placeholders (a `{python}` inside a longer
  string is left alone). `argv[0]` otherwise must be a bare program name (resolved on the sandbox `PATH`)
  or a relative path inside the checkout; absolute paths, `..` segments and a leading `-` are rejected at
  load time.
- `env` keys match `[A-Z_][A-Z0-9_]*` and may not be `PATH`, `HOME`, `TMPDIR`, `PYTHONPATH`,
  `PYTHONHOME`, `PYTHONSTARTUP` or start with `LD_`, `DYLD_`, `HERA_`. The oracle sets `PYTHONPATH` itself
  to the run directory and its `src/`, so flat and src layouts both import.
- Limits: file at most 256 KB; at most 200 checks per file; at most 64 argv elements; at most 32 input
  files, 32 env variables and 32 expected files per check. Unknown keys anywhere raise ValueError naming
  the key (never the file's content). Expectation lists and tables must not be empty (no vacuous checks);
  `stdout_json` values must be JSON-representable (no dates, NaN or infinity) and at most 64 levels deep.
- **No regular expressions in v0.** A pattern written by a model and evaluated in the coordinator against
  candidate-controlled output is a denial-of-service risk. Expectations are exact text, substrings, exact
  JSON and exact files.
- Comparison rules: stdout, stderr and expected files are decoded as UTF-8 (invalid bytes replaced) and
  `\r\n` is normalised to `\n` on both sides. JSON equality is strict (a boolean is never a number;
  numbers compare by value); `stdout_json_subset` recurses into tables only (arrays must match exactly).
  JSON parsing of stdout is capped at 1 MB and catches invalid input, NaN/Infinity, over-deep nesting and
  recursion errors: each is a failed expectation, never an oracle crash. File expectations read regular
  files only, at most 1 MB, and never follow symbolic links (a symlink in any component fails the
  expectation). Truncated output (above the sandbox capture cap) never passes. A timeout is `TIMEOUT`; a
  program that could not be started is `ERROR`; otherwise `PASS` or `FAIL` with the failed expectations.

### Execution model

`ContractRunner.run(checkout, checkset, include_hidden, limits, allow_unsandboxed)` returns a
`ContractRunReport` (results, executed ids, failure texts for visible checks, `timed_out_any`, mechanism).
For EACH contract check it: copies the verification checkout into a fresh run directory next to it (so
inside the task's verify area, which the sandbox may write to; the oracle's own `_hera_checks`,
`_hera_results` and `_hera_project` are left out and symlinks that point out of the copy are removed);
writes the check's `files` without following links; runs `argv` under `hera.sandbox` with `cwd` the run
directory, the check's timeout as the wall limit and a scrubbed environment; evaluates in this process;
deletes the run directory. A fresh copy per check means a candidate cannot carry state from one check to
the next or learn one check's inputs from an earlier one. Contract files are read from the frozen store
(hash-checked on every read) by the oracle and are NEVER copied into any checkout or run directory;
`PytestAdapter.prepare` skips them too. Visible contract files may be shown to an implementer like visible
pytest files. The inputs of a hidden check (argv, stdin, env, input files) are necessarily visible to the
candidate while it runs; the expectations are not.

Feedback: a failing visible check gives its id, requirement ids, the failed expectations and the first
2,000 characters of stdout and stderr (the run directory is shown as `<checkout>`). A failing hidden check
gives requirement ids only (same redaction as pytest). `CheckResult.output_digest` is the sha256 of
stdout + stderr for any non-passing check. Health comes from the stored `checks.health`, as for pytest
checks. Validation on the base commit (`validate_on_base`) runs contract checks through the same runner,
with the same fail-before, pass-before and 3x stability rules; `kind "new"` checks that fail on base
are VALID (`fails_before_change`).

`VerificationOutcome.in_process_only` lists the requirement ids that PASSED where every passing counted
check is a native pytest check. When it is not empty `outcome.notes` carries "PASS for <ids> rests only on
in-process checks, which a candidate can forge" and `hera verify` prints it. It is never part of the
implementer's feedback. The receipt, the verdict function and the schema are unchanged.

### What this closes and what stays open

CLOSED for contract checks: a candidate that monkeypatches pytest, rewrites result files, patches the
oracle's modules inside its own process or writes `_hera_results/junit.xml` has no effect, because
nothing the oracle compares was produced inside that process. Tested:
`test_in_process_forgery_is_caught_by_contract_checks` (the forging candidate is OVERFIT when the set has a
hidden contract check) and `test_monkeypatching_pytest_and_forging_result_files_has_no_effect`.

STILL OPEN:

- Native pytest checks are unchanged and forgeable in-process (the two KNOWN_GAP tests document this for
  pytest-only sets and stay as they are). So is the regression run of the project's own tests.
- A contract check can only look at an external surface (command line, files, later HTTP). Author hidden
  checks as contract checks wherever the task has one; `in_process_only` shows what still rests on pytest.
- A candidate that detects it is being checked can still behave differently under check than in
  production; hidden inputs and varied cases are what catch that (OVERFIT), not the runner.
- With sandbox mechanism `none` (tests and development only) nothing isolates processes or the file
  system: a candidate can read the frozen store from disk and a process that escapes the process group
  (setsid) can tamper with a later check's run directory. Both were tried in S7c and succeed under
  `none`. The real mechanisms are built to stop them (the check store is on the deny list; bwrap's pid
  namespace dies with the run) and two tests assert it, but those tests skip where there is no mechanism
  and had not been run by their author when this was written; CI is their first run. The verifier must not use `none` for real runs.
- A straggler that escaped the process group can hold the output pipes open; `sandbox.run` now stops
  waiting for the readers after five seconds in total instead of blocking until the straggler exits.

## Residual risk, stated plainly

Native pytest checks import the candidate's code into the same process as the runner. Code in the
candidate's own modules (imported by the checks) can therefore monkeypatch pytest and rewrite every
report before it is written, while keeping the sentinel pair and the exit code consistent. Tested and
not caught: patching `TestReport.from_item_and_call` at import time (see
`test_KNOWN_GAP_in_process_monkeypatch_is_not_detected`). Caught: overwriting `junit.xml` at exit with
a forged all-pass file, because pytest's own summary line, already written to the pipe, disagrees. The
hidden check files are also readable by candidate code during a run. The same in-process route applies
to the run of the project's own tests (regressions could be hidden); the summary cross-check is applied
there too and folded into `exit_code_consistent`. Closing this needs the checks to run out of process
from the candidate: contract checks (S7c, above) close it for checks that have an external surface; a
per-run VM or container is milestone 0.2. The audit does NOT flag candidates that touch pytest internals.

**Status: CLOSED for contract checks, OPEN for native pytest checks.** The e2e tests
`test_KNOWN_GAP_in_process_monkeypatch_yields_false_pass` and
`test_KNOWN_GAP_in_process_monkeypatch_is_not_detected` still assert the false PASS: they document that a
pytest-only check set remains forgeable, and they must keep passing until pytest checks themselves move
out of process.

Other limits of S7b: the project's third-party pytest plugins are not loaded; git attributes in the
shared bare repository's `info/attributes` (writable from an implementer worktree) could make git treat
a file as binary and hide its added lines from the line rules; a candidate that kills the runner is
attributed `UNKNOWN` (with `environment_suspect`) although its verdict is TAMPER.

## Not yet built

Requirements summary and approval screen, two blind check
authors, persisting validation results into `checks.health`, stub adversaries
and mutation testing (TODO, milestone 0.2), model-written checks (needs a real provider call),
per-language environments (checks run with the same Python interpreter that runs HERA; building an
environment from the project's lockfile is later), a TypeScript adapter, a per-test timeout (a hang
loses every result of that run).

## Verification without a sandbox

`allow_unsandboxed=True` (CLI `--allow-unsandboxed`) exists so the test suite runs on machines with no sandbox mechanism. Under mechanism `none` nothing is isolated: a candidate can read the frozen store, including hidden contract checks, and a background process it leaves behind can tamper with later checks. Every outcome verified this way carries the note "verified WITHOUT sandbox isolation", and such a verdict must not be relied on against a hostile candidate.

