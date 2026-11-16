# Acceptance oracle, part a (S7a): checks, frozen store, pytest adapter, validation

Code: `src/hera/oracle/model.py`, `checkstore.py`, `adapters/pytest_adapter.py`, `validation.py`.
Tests: `tests/test_checkstore.py`, `tests/test_pytest_adapter.py`, `tests/test_validation.py`, with
the invented toy project in `tests/toyrepo.py`. Verdicts, receipts and audits are S7b; this step
produces the data they are built from.

## Objects

- `Requirement(id "R-n", text)`.
- `DraftCheck`: one check is one pytest test function: `check_id`, `requirement_ids`, `visibility`
  (`visible` | `hidden`), `relpath` (POSIX, relative, `test_*.py`), `node` (function name),
  `author_role`, `author_model`, `kind` (`new`: must fail before the change; `regression`: must pass
  before and after). A file holds checks of one visibility only.
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

## Residual risk, stated plainly

Native pytest checks import the candidate's code into the same process as the runner. Code in the
candidate's own modules (imported by the checks) can therefore monkeypatch pytest and rewrite every
report before it is written, while keeping the sentinel pair and the exit code consistent. Tested and
not caught: patching `TestReport.from_item_and_call` at import time (see
`test_KNOWN_GAP_in_process_monkeypatch_is_not_detected`). Caught: overwriting `junit.xml` at exit with
a forged all-pass file, because pytest's own summary line, already written to the pipe, disagrees. The
hidden check files are also readable by candidate code during a run. Closing this needs the checks to
run out of process from the candidate (for example an HTTP or CLI boundary), a per-run VM or container
(milestone 0.2) or both. The diff audit in S7b will flag candidates that touch pytest internals, but
that is a heuristic.

## Not yet built

Diff audit (declared scope, debris, twin search), receipts, verdict wiring, disputes, stub adversaries
and mutation testing (TODO, milestone 0.2), model-written checks (needs a real provider call),
per-language environments (checks run with the same Python interpreter that runs HERA; building an
environment from the project's lockfile is later), a TypeScript adapter, a per-test timeout (a hang
loses every result of that run).
