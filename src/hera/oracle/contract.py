"""Contract checks: declarative, out-of-process checks (S7c).

A contract file (``*.contract.toml``) holds checks that say: run this command with this input in
a copy of the candidate checkout, then expect this exit code, this output, this file. The
command runs as a separate sandboxed subprocess (``contract_runner``); the comparison of its
output with the expectations happens HERE, in the oracle's own process, which never imports,
executes or evaluates anything from the checkout. A candidate can do whatever it likes inside
its own process; it cannot change what the oracle compares and it never sees the expected
values of a hidden check.

This module is a parser and a pure evaluator. It uses no regular expressions on purpose: an
expression written by a model and evaluated in the coordinator against candidate-controlled
output is a denial-of-service risk. Expectations are exact text, substrings, exact JSON and
exact files only.

Format (see docs/ORACLE.md, "Contract checks")::

    [[check]]
    node = "parse_twelve_fifty"               # [A-Za-z_][A-Za-z0-9_]{0,63}, unique in the file
    argv = ["{python}", "-c", "...", "12.50"]
    stdin = ""                                # optional, at most 64 KB
    timeout_s = 10                            # optional, default 10, at most 120
    env = { LANG = "C.UTF-8" }                # optional
    files = { "input.json" = "{}" }           # optional, written into the run directory first

    [check.expect]                            # at least one expectation
    exit_code = 0
    stdout_equals = "12.5\\n"
    stdout_contains = ["12.5"]
    stderr_contains = ["ValueError"]
    stdout_json = { value = 12.5 }
    stdout_json_subset = { ok = true }
    files_equal = { "out.txt" = "done\\n" }
    files_contain = { "log.txt" = ["started"] }

Error messages name the offending key or field, never the content of the file.
"""
from __future__ import annotations

import json
import math
import os
import re

import stat
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from ..types import CheckStatus
from .model import validate_relpath

MAX_FILE_BYTES = 256 * 1024
MAX_CHECKS_PER_FILE = 200
MAX_STDIN_BYTES = 64 * 1024
MAX_INPUT_FILE_BYTES = 64 * 1024
MAX_INPUT_FILES = 32
MAX_ENV_VARS = 32
MAX_ARGV = 64
MAX_ARG_CHARS = 4096
DEFAULT_TIMEOUT_S = 10
MAX_TIMEOUT_S = 120
MAX_EXPECTED_FILE_BYTES = 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 64
PYTHON_PLACEHOLDER = "{python}"
OWNED_DIRS = ("_hera_checks", "_hera_results", "_hera_project")

_NODE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_ENV_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")
_FORBIDDEN_ENV = frozenset({"PATH", "HOME", "TMPDIR", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"})
_FORBIDDEN_ENV_PREFIX = ("LD_", "DYLD_", "HERA_")
_CHECK_KEYS = frozenset({"node", "argv", "stdin", "timeout_s", "env", "files", "expect"})
_EXPECT_KEYS = frozenset({"exit_code", "stdout_equals", "stdout_contains", "stderr_contains",
                          "stdout_json", "stdout_json_subset", "files_equal", "files_contain"})


@dataclass(frozen=True)
class ContractCheck:
    node: str
    argv: tuple[str, ...]
    relpath: str = ""
    stdin: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    env: tuple[tuple[str, str], ...] = ()
    files: tuple[tuple[str, str], ...] = ()
    exit_code: int | None = None
    stdout_equals: str | None = None
    stdout_contains: tuple[str, ...] = ()
    stderr_contains: tuple[str, ...] = ()
    has_stdout_json: bool = False
    stdout_json: Any = None
    stdout_json_subset: dict | None = None
    files_equal: tuple[tuple[str, str], ...] = ()
    files_contain: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def resolved_argv(self, python: str) -> list[str]:
        """``argv`` with every element that is exactly ``{python}`` replaced by ``python``."""
        return [python if a == PYTHON_PLACEHOLDER else a for a in self.argv]


# ---- parsing -----------------------------------------------------------------------------
def _k(key) -> str:
    return repr(str(key)[:40])


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _str(v, where: str, *, max_bytes: int | None = None, nonempty: bool = False) -> str:
    if not isinstance(v, str) or "\0" in v:
        raise ValueError(f"{where}: must be a string without NUL")
    if nonempty and not v:
        raise ValueError(f"{where}: must not be empty")
    if max_bytes is not None and len(v.encode("utf-8", "replace")) > max_bytes:
        raise ValueError(f"{where}: larger than {max_bytes} bytes")
    return v


def _table(v, where: str) -> dict:
    if not isinstance(v, dict):
        raise ValueError(f"{where}: must be a table")
    return v


def _no_unknown(d: dict, allowed, where: str) -> None:
    for key in d:
        if key not in allowed:
            raise ValueError(f"{where}: unknown key {_k(key)}")


def _rel_path(v, where: str) -> str:
    _str(v, where, nonempty=True)
    if len(v) > 200 or "\\" in v or v.startswith("/") or PurePosixPath(v).is_absolute():
        raise ValueError(f"{where}: must be a short relative POSIX path")
    parts = v.split("/")
    for p in parts:
        if p in ("", ".", "..") or not _PATH_SEGMENT.fullmatch(p):
            raise ValueError(f"{where}: contains an empty, dot, parent or unsupported segment")
    if parts[0] in OWNED_DIRS:
        raise ValueError(f"{where}: refers to a directory owned by the oracle")
    return v


def _json_value(v, where: str, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError(f"{where}: nested deeper than {MAX_JSON_DEPTH}")
    if isinstance(v, bool) or isinstance(v, str) or _is_int(v):
        return
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError(f"{where}: NaN and infinity are not JSON values")
        return
    if isinstance(v, list):
        for x in v:
            _json_value(x, where, depth + 1)
        return
    if isinstance(v, dict):
        for x in v.values():
            _json_value(x, where, depth + 1)
        return
    raise ValueError(f"{where}: contains a value with no JSON equivalent (such as a date)")


def _str_list(v, where: str) -> tuple[str, ...]:
    if not isinstance(v, list) or not v:
        raise ValueError(f"{where}: must be a non-empty list of strings")
    return tuple(_str(x, where, nonempty=True, max_bytes=MAX_INPUT_FILE_BYTES) for x in v)


def _parse_argv(v, where: str) -> tuple[str, ...]:
    if not isinstance(v, list) or not v or len(v) > MAX_ARGV:
        raise ValueError(f"{where}: must be a non-empty list of at most {MAX_ARGV} strings")
    out = tuple(_str(a, where) for a in v)
    if any(len(a) > MAX_ARG_CHARS for a in out):
        raise ValueError(f"{where}: an element is longer than {MAX_ARG_CHARS} characters")
    prog = out[0]
    if prog != PYTHON_PLACEHOLDER:
        if not prog or prog.startswith("-") or "\\" in prog or prog.startswith("/") \
                or PurePosixPath(prog).is_absolute() or re.match(r"[A-Za-z]:", prog):
            raise ValueError(f"{where}: argv[0] must be a bare program name or a relative path "
                             "inside the checkout, never an absolute path")
        if any(p in ("", "..") for p in prog.split("/")[:-1]) or prog.endswith("/") \
                or prog.split("/")[-1] in ("", ".", ".."):
            raise ValueError(f"{where}: argv[0] must not contain empty or parent segments")
    return out


def _parse_env(v, where: str) -> tuple[tuple[str, str], ...]:
    t = _table(v, where)
    if len(t) > MAX_ENV_VARS:
        raise ValueError(f"{where}: more than {MAX_ENV_VARS} variables")
    out = []
    for key, val in t.items():
        if not isinstance(key, str) or not _ENV_KEY.fullmatch(key):
            raise ValueError(f"{where}: variable name {_k(key)} must match [A-Z_][A-Z0-9_]*")
        if key in _FORBIDDEN_ENV or key.startswith(_FORBIDDEN_ENV_PREFIX):
            raise ValueError(f"{where}: variable {_k(key)} is not allowed")
        out.append((key, _str(val, f"{where}.{key}", max_bytes=MAX_ARG_CHARS)))
    return tuple(sorted(out))


def _parse_files(v, where: str) -> tuple[tuple[str, str], ...]:
    t = _table(v, where)
    if len(t) > MAX_INPUT_FILES:
        raise ValueError(f"{where}: more than {MAX_INPUT_FILES} files")
    out = []
    for key, val in t.items():
        _rel_path(key, f"{where} key {_k(key)}")
        out.append((key, _str(val, f"{where}.{key}", max_bytes=MAX_INPUT_FILE_BYTES)))
    return tuple(sorted(out))


def _parse_expect(t: dict, where: str) -> dict:
    t = _table(t, where)
    _no_unknown(t, _EXPECT_KEYS, where)
    out: dict[str, Any] = {}
    if "exit_code" in t:
        ec = t["exit_code"]
        if not _is_int(ec) or not -128 <= ec <= 255:
            raise ValueError(f"{where}.exit_code: must be an integer from -128 to 255")
        out["exit_code"] = ec
    if "stdout_equals" in t:
        out["stdout_equals"] = _str(t["stdout_equals"], f"{where}.stdout_equals",
                                    max_bytes=MAX_FILE_BYTES)
    for key in ("stdout_contains", "stderr_contains"):
        if key in t:
            out[key] = _str_list(t[key], f"{where}.{key}")
    if "stdout_json" in t:
        _json_value(t["stdout_json"], f"{where}.stdout_json")
        out["has_stdout_json"], out["stdout_json"] = True, t["stdout_json"]
    if "stdout_json_subset" in t:
        sub = t["stdout_json_subset"]
        if not isinstance(sub, dict) or not sub:
            raise ValueError(f"{where}.stdout_json_subset: must be a non-empty table")
        _json_value(sub, f"{where}.stdout_json_subset")
        out["stdout_json_subset"] = sub
    if "files_equal" in t:
        fe = _table(t["files_equal"], f"{where}.files_equal")
        if not fe or len(fe) > MAX_INPUT_FILES:
            raise ValueError(f"{where}.files_equal: must hold 1 to {MAX_INPUT_FILES} files")
        out["files_equal"] = tuple(sorted(
            (_rel_path(k, f"{where}.files_equal key {_k(k)}"),
             _str(v, f"{where}.files_equal.{k}", max_bytes=MAX_INPUT_FILE_BYTES)) for k, v in fe.items()))
    if "files_contain" in t:
        fc = _table(t["files_contain"], f"{where}.files_contain")
        if not fc or len(fc) > MAX_INPUT_FILES:
            raise ValueError(f"{where}.files_contain: must hold 1 to {MAX_INPUT_FILES} files")
        out["files_contain"] = tuple(sorted(
            (_rel_path(k, f"{where}.files_contain key {_k(k)}"),
             _str_list(v, f"{where}.files_contain.{k}")) for k, v in fc.items()))
    if not out:
        raise ValueError(f"{where}: at least one expectation is required")
    return out


def _parse_check(t, index: int, relpath: str) -> ContractCheck:
    where = f"check[{index}]"
    t = _table(t, where)
    _no_unknown(t, _CHECK_KEYS, where)
    for required in ("node", "argv", "expect"):
        if required not in t:
            raise ValueError(f"{where}: missing key '{required}'")
    node = t["node"]
    if not isinstance(node, str) or not _NODE.fullmatch(node):
        raise ValueError(f"{where}.node: must match [A-Za-z_][A-Za-z0-9_]{{0,63}}")
    argv = _parse_argv(t["argv"], f"{where}.argv")
    stdin = _str(t.get("stdin", ""), f"{where}.stdin", max_bytes=MAX_STDIN_BYTES)
    timeout = t.get("timeout_s", DEFAULT_TIMEOUT_S)
    if not _is_num(timeout) or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_S:
        raise ValueError(f"{where}.timeout_s: must be a number above 0 and at most {MAX_TIMEOUT_S}")
    env = _parse_env(t["env"], f"{where}.env") if "env" in t else ()
    files = _parse_files(t["files"], f"{where}.files") if "files" in t else ()
    expect = _parse_expect(t["expect"], f"{where}.expect")
    return ContractCheck(node=node, argv=argv, relpath=relpath, stdin=stdin, timeout_s=timeout,
                         env=env, files=files, **expect)


def load_contract_file(data: bytes, relpath: str) -> tuple[ContractCheck, ...]:
    """Parse one ``*.contract.toml`` file. Raises ValueError naming the offending key or field
    (never the file's content) on any violation of the format or its limits."""
    validate_relpath(relpath, "contract")
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("contract file: must be bytes")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"contract file: larger than {MAX_FILE_BYTES} bytes")
    try:
        doc = tomllib.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError, ValueError):
        raise ValueError("contract file: not valid UTF-8 TOML") from None
    _no_unknown(doc, {"check"}, "contract file")
    checks = doc.get("check")
    if not isinstance(checks, list) or not checks:
        raise ValueError("contract file: needs at least one [[check]] table")
    if len(checks) > MAX_CHECKS_PER_FILE:
        raise ValueError(f"contract file: more than {MAX_CHECKS_PER_FILE} checks")
    out: list[ContractCheck] = []
    seen: set[str] = set()
    for i, t in enumerate(checks):
        c = _parse_check(t, i, relpath)
        if c.node in seen:
            raise ValueError(f"contract file: duplicate node {c.node!r}")
        seen.add(c.node)
        out.append(c)
    return tuple(out)


# ---- evaluation --------------------------------------------------------------------------
def _norm(text: str) -> str:
    return text.replace("\r\n", "\n")


def _decode(b: bytes) -> str:
    return _norm(b.decode("utf-8", "replace"))


def _short(s: str, n: int = 60) -> str:
    s = s if len(s) <= n else s[:n] + "..."
    return repr(s)


def _json_eq(actual, expected, depth: int = 0) -> bool:
    """Strict JSON equality: a boolean is never a number, numbers compare by value."""
    if depth > MAX_JSON_DEPTH:
        return False
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual is expected
    if _is_num(expected):
        return _is_num(actual) and actual == expected
    if isinstance(expected, str):
        return isinstance(actual, str) and actual == expected
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(_json_eq(a, e, depth + 1) for a, e in zip(actual, expected)))
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and set(actual) == set(expected)
                and all(_json_eq(actual[k], v, depth + 1) for k, v in expected.items()))
    return False


def _json_subset(actual, expected: dict, depth: int = 0) -> bool:
    if depth > MAX_JSON_DEPTH or not isinstance(actual, dict):
        return False
    for k, v in expected.items():
        if k not in actual:
            return False
        ok = _json_subset(actual[k], v, depth + 1) if isinstance(v, dict) else _json_eq(actual[k], v, depth + 1)
        if not ok:
            return False
    return True


def _reject_constant(name):
    raise ValueError("NaN and infinity are not JSON")


def _parse_stdout_json(stdout: bytes):
    """Returns (ok, value). Never raises: oversized, invalid or too deeply nested output is a
    failed expectation, not an oracle crash."""
    if len(stdout) > MAX_JSON_BYTES:
        return False, "stdout is larger than 1 MB"
    try:
        return True, json.loads(stdout.decode("utf-8"), parse_constant=_reject_constant)
    except RecursionError:
        return False, "stdout is nested too deeply"
    except (ValueError, OverflowError, MemoryError):          # UnicodeDecodeError is a ValueError
        return False, "stdout is not valid JSON"


def _read_expected_file(run_dir, rel: str):
    """Returns (text or None, problem). Symbolic links (in any component below ``run_dir``) are
    never followed; only regular files up to 1 MB are read."""
    cur = os.fspath(run_dir)
    parts = rel.split("/")
    for i, part in enumerate(parts):
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except OSError:
            return None, "does not exist"
        if stat.S_ISLNK(st.st_mode):
            return None, "is (or lies behind) a symbolic link"
        if i < len(parts) - 1:
            if not stat.S_ISDIR(st.st_mode):
                return None, "has a non-directory in its path"
        else:
            if not stat.S_ISREG(st.st_mode):
                return None, "is not a regular file"
            if st.st_size > MAX_EXPECTED_FILE_BYTES:
                return None, "is larger than 1 MB"
    try:
        fd = os.open(cur, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as f:
            data = f.read(MAX_EXPECTED_FILE_BYTES + 1)
    except OSError:
        return None, "cannot be read"
    if len(data) > MAX_EXPECTED_FILE_BYTES:
        return None, "is larger than 1 MB"
    return _decode(data), ""


def evaluate(check: ContractCheck, run_result, run_dir) -> tuple[CheckStatus, list[str]]:
    """Compare what the program did with what the check expects.

    ``run_result`` needs ``returncode``, ``stdout``, ``stderr``, ``timed_out`` and (optionally)
    ``truncated`` (``hera.sandbox.RunResult`` has them). A timeout is TIMEOUT; a program that
    could not be started (no return code and no timeout) is ERROR; otherwise PASS when every
    expectation holds and FAIL with the list of the ones that do not. Pure apart from reading
    expected files from ``run_dir`` (regular files only, symlinks not followed, 1 MB each)."""
    if run_result.timed_out:
        return CheckStatus.TIMEOUT, ["timed out"]
    if run_result.returncode is None:
        return CheckStatus.ERROR, ["the program could not be started"]
    failed: list[str] = []
    if getattr(run_result, "truncated", False):
        failed.append("output: exceeded the capture limit")
    out_text, err_text = _decode(run_result.stdout), _decode(run_result.stderr)
    if check.exit_code is not None and run_result.returncode != check.exit_code:
        failed.append(f"exit_code: expected {check.exit_code}, got {run_result.returncode}")
    if check.stdout_equals is not None and out_text != _norm(check.stdout_equals):
        failed.append("stdout_equals: standard output differs from the expected text")
    for label, text, needles in (("stdout_contains", out_text, check.stdout_contains),
                                 ("stderr_contains", err_text, check.stderr_contains)):
        for n in needles:
            if _norm(n) not in text:
                failed.append(f"{label}: missing {_short(n)}")
    if check.has_stdout_json or check.stdout_json_subset is not None:
        ok, value = _parse_stdout_json(run_result.stdout)
        if check.has_stdout_json:
            try:
                good = ok and _json_eq(value, check.stdout_json)
            except RecursionError:
                good = False
            if not good:
                failed.append("stdout_json: " + (value if not ok else "parsed JSON differs from the expected value"))
        if check.stdout_json_subset is not None:
            try:
                good = ok and _json_subset(value, check.stdout_json_subset)
            except RecursionError:
                good = False
            if not good:
                failed.append("stdout_json_subset: " + (value if not ok else
                                                        "parsed JSON lacks an expected key or value"))
    for rel, expected in check.files_equal:
        text, why = _read_expected_file(run_dir, rel)
        if text is None:
            failed.append(f"files_equal: {rel} {why}")
        elif text != _norm(expected):
            failed.append(f"files_equal: {rel} differs from the expected text")
    for rel, needles in check.files_contain:
        text, why = _read_expected_file(run_dir, rel)
        if text is None:
            failed.append(f"files_contain: {rel} {why}")
            continue
        for n in needles:
            if _norm(n) not in text:
                failed.append(f"files_contain: {rel} is missing {_short(n)}")
    return (CheckStatus.FAIL if failed else CheckStatus.PASS), failed
