"""Tools the implementer model can call, working inside one candidate worktree.

Rules that hold for every tool:

- Errors come back as results (text starting "error:"), never as exceptions to the loop, and an
  error text never contains exception messages, paths the model did not already give, or any
  repository content: fixed sentences only.
- Every result is returned as bytes plus a privacy label. Results that carry repository content
  (file text, command output, check feedback) are labelled by the repository's class, so a
  sensitive repository's results can never be sent to a remote provider. Fixed error and
  acknowledgement sentences are HERA's own text and get the built-in label.
- File tools take relative POSIX paths only. ``..``, absolute paths, and any component named
  ``.git`` (case-insensitive), or starting ``_hera_`` (``_hera_checks``, ``_hera_results``,
  ``_hera_project`` and anything else HERA reserves), are rejected, also after following symlinks.
  Symlinks that leave the worktree are rejected; writes never follow a symlink at the target.
"""
from __future__ import annotations

import dataclasses
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..privacy.labels import LabelStore, label_builtin, label_bytes
from ..privacy.policy import Policy
from ..sandbox import Limits, SandboxUnavailable, run as sandbox_run
from ..types import Label, ToolCall

LIST_MAX_ENTRIES = 500
READ_MAX_BYTES = 64 * 1024
READ_MAX_LINES = 400
SEARCH_MAX_HITS = 200
SEARCH_EXCERPT = 200
WRITE_MAX_BYTES = 256 * 1024
EDIT_MAX_FILE_BYTES = 1024 * 1024
RUN_MAX_TIMEOUT_S = 120
RUN_OUTPUT_TAIL = 16 * 1024
VISIBLE_FEEDBACK_MAX = 32 * 1024
_SEARCH_MAX_FILE = 1024 * 1024
_SEARCH_MAX_FILES = 20000
_SEARCH_MAX_TOTAL = 64 * 1024 * 1024
_SUMMARY_MAX = 4000
_REQ_ID = re.compile(r"R-[0-9]{1,4}")
_CHECK_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}")

TOOL_SPECS: tuple[dict, ...] = (
    {"name": "list_files",
     "description": "List the entries of a directory in the workspace (not recursive, at most 500). "
                    "Directories end with '/'.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "relative directory, default '.'"}}}},
    {"name": "read_file",
     "description": "Read a text file (at most 400 lines and 64 KB per call). Use start_line to continue.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"},
         "start_line": {"type": "integer", "description": "first line, 1-based, default 1"},
         "max_lines": {"type": "integer", "description": "default and maximum 400"}},
         "required": ["path"]}},
    {"name": "search",
     "description": "Literal substring search (no regular expressions) over text files under a path; "
                    "at most 200 hits as path:line: excerpt.",
     "parameters": {"type": "object", "properties": {
         "text": {"type": "string"}, "path": {"type": "string", "description": "default '.'"}},
         "required": ["text"]}},
    {"name": "write_file",
     "description": "Create or overwrite a text file (at most 256 KB). Parent directories are created.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["path", "content"]}},
    {"name": "replace_in_file",
     "description": "Replace one exact piece of text in a file. 'old' must occur exactly once.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
         "required": ["path", "old", "new"]}},
    {"name": "run_command",
     "description": "Run a program in the workspace with no network (argv list, no shell). Use "
                    "'{python}' as the first element for the Python interpreter. Returns the exit code "
                    "and the last 16 KB of output. At most 120 seconds.",
     "parameters": {"type": "object", "properties": {
         "argv": {"type": "array", "items": {"type": "string"}},
         "timeout_s": {"type": "integer", "description": "default 60, maximum 120"}},
         "required": ["argv"]}},
    {"name": "run_visible_checks",
     "description": "Save your work and run the visible checks. Returns which ones pass or fail. "
                    "Hidden checks are not run here.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "read_visible_check",
     "description": "Read a visible check file (read-only). A directory path lists its entries.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]}},
    {"name": "submit",
     "description": "Finish: you are done. Give a short summary, the files you expect to have changed "
                    "(declared_scope) and, if you copied an existing pattern, a twin_pattern.",
     "parameters": {"type": "object", "properties": {
         "summary": {"type": "string"},
         "declared_scope": {"type": "array", "items": {"type": "string"}},
         "twin_pattern": {"type": "string"}},
         "required": ["summary", "declared_scope"]}},
    {"name": "dispute",
     "description": "Finish by contesting a visible check you believe is wrong, instead of working "
                    "around it. Name the check, the requirement and give your argument.",
     "parameters": {"type": "object", "properties": {
         "check_id": {"type": "string"}, "requirement_id": {"type": "string"},
         "argument": {"type": "string"}},
         "required": ["check_id", "requirement_id", "argument"]}},
)
TOOL_NAMES = frozenset(t["name"] for t in TOOL_SPECS)


class _ToolError(Exception):
    """Raised inside a tool; the message is a FIXED sentence (never repository or exception text)."""


@dataclass(frozen=True)
class ToolOutput:
    data: bytes
    label: Label
    artefact_id: str
    builtin: bool                  # True: a fixed HERA sentence (built-in label); False: repository-derived
    ended: str | None = None       # "submitted" | "disputed" when the call ends the attempt
    payload: dict | None = None    # submit/dispute arguments, validated


@dataclass
class _R:
    text: str
    builtin: bool = False
    ended: str | None = None
    payload: dict | None = None


def _err(msg: str) -> _R:
    return _R("error: " + msg, builtin=True)


def _reserved(component: str) -> bool:
    c = component.casefold().rstrip(" .")
    return c == ".git" or c.startswith("_hera_")


def _split(rel) -> list[str]:
    """Validate a relative POSIX path and return its components ("." and "" dropped)."""
    if not isinstance(rel, str) or not rel:
        raise _ToolError("path must be a non-empty string")
    if "\0" in rel or "\\" in rel:
        raise _ToolError("path contains a character that is not allowed")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise _ToolError("absolute paths are not allowed; use a path relative to the workspace")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    for p in parts:
        if p == "..":
            raise _ToolError("'..' is not allowed in a path")
        if _reserved(p):
            raise _ToolError("that path is reserved and cannot be used")
    return parts


def _inside(base_real: str, real: str) -> bool:
    return real == base_real or real.startswith(base_real + os.sep)


def _check_resolved(base_real: str, real: str) -> None:
    if not _inside(base_real, real):
        raise _ToolError("path leaves the workspace")
    rel = os.path.relpath(real, base_real)
    if rel != "." and any(_reserved(p) for p in rel.split(os.sep)):
        raise _ToolError("that path is reserved and cannot be used")


def _locate(base: str, rel, *, parent_only: bool = False) -> tuple[str, str]:
    """Resolve ``rel`` under ``base``. Returns (real path, final component name).

    For ``parent_only`` the last component is not resolved (the caller decides about symlinks at
    the target); otherwise every symlink on the way is resolved and the result must stay inside
    ``base`` and must not land on a reserved name."""
    base_real = os.path.realpath(base)
    parts = _split(rel)
    if parent_only:
        if not parts:
            raise _ToolError("path must name a file")
        parent_real = os.path.realpath(os.path.join(base_real, *parts[:-1]))
        _check_resolved(base_real, parent_real)
        return os.path.join(parent_real, parts[-1]), parts[-1]
    real = os.path.realpath(os.path.join(base_real, *parts)) if parts else base_real
    _check_resolved(base_real, real)
    return real, (parts[-1] if parts else ".")


def _types(args: dict, schema: dict) -> None:
    unknown = set(args) - set(schema)
    if unknown:
        raise _ToolError("unexpected argument; see the tool description")
    for key, (typ, required) in schema.items():
        if key not in args:
            if required:
                raise _ToolError("a required argument is missing; see the tool description")
            continue
        v = args[key]
        if typ is int:
            ok = isinstance(v, int) and not isinstance(v, bool)
        elif typ == "strlist":
            ok = isinstance(v, list) and all(isinstance(x, str) for x in v)
        else:
            ok = isinstance(v, typ)
        if not ok:
            raise _ToolError("an argument has the wrong type; see the tool description")


class ToolBox:
    """The tools for one candidate worktree.

    ``worktree`` is a ``workspace.Worktree`` (it is committed by ``run_visible_checks`` and by the
    loop's checkpoints). ``verify_visible(commit) -> str`` is injected by the caller and must run
    visible checks only; its text is returned to the model, so it can never contain hidden-check
    information as long as hidden checks are not run there."""

    def __init__(self, worktree, *, repo_id: str, label_store: LabelStore, policy: Policy,
                 sandbox_limits: Limits = Limits(), allow_unsandboxed: bool = False,
                 visible_checks_dir=None, verify_visible: Callable[[str], str] | None = None) -> None:
        self.worktree = worktree
        self.path = Path(worktree.path)
        self.repo_id = repo_id
        self.label_store = label_store
        self.policy = policy
        self.sandbox_limits = sandbox_limits
        self.allow_unsandboxed = allow_unsandboxed
        self.visible_checks_dir = Path(visible_checks_dir) if visible_checks_dir is not None else None
        self.verify_visible = verify_visible
        self._table = {
            "list_files": (self._list_files, {"path": (str, False)}),
            "read_file": (self._read_file, {"path": (str, True), "start_line": (int, False),
                                            "max_lines": (int, False)}),
            "search": (self._search, {"text": (str, True), "path": (str, False)}),
            "write_file": (self._write_file, {"path": (str, True), "content": (str, True)}),
            "replace_in_file": (self._replace, {"path": (str, True), "old": (str, True),
                                                "new": (str, True)}),
            "run_command": (self._run_command, {"argv": ("strlist", True), "timeout_s": (int, False)}),
            "run_visible_checks": (self._run_visible, {}),
            "read_visible_check": (self._read_visible, {"path": (str, True)}),
            "submit": (self._submit, {"summary": (str, True), "declared_scope": ("strlist", True),
                                      "twin_pattern": (str, False)}),
            "dispute": (self._dispute, {"check_id": (str, True), "requirement_id": (str, True),
                                        "argument": (str, True)}),
        }

    # ---- entry point ------------------------------------------------------------------
    def execute(self, call: ToolCall, *, artefact_id: str) -> ToolOutput:
        """Run one call. Never raises for tool problems (only BaseException passes through)."""
        try:
            if call.name == "__invalid__":
                r = _err("the tool arguments were not valid JSON; send a JSON object")
            elif call.name not in self._table:
                r = _err("unknown tool; use one of the listed tools")
            else:
                fn, schema = self._table[call.name]
                try:
                    if not isinstance(call.arguments, dict):
                        raise _ToolError("arguments must be an object")
                    _types(call.arguments, schema)
                    r = fn(**call.arguments)
                except _ToolError as e:
                    r = _err(str(e))
        except Exception:
            r = _err("the tool failed unexpectedly")
        return self.make_output(r.text.encode("utf-8", "replace"), artefact_id, r.builtin,
                                r.ended, r.payload)

    def make_output(self, data: bytes, artefact_id: str, builtin: bool, ended: str | None = None,
                    payload: dict | None = None) -> ToolOutput:
        """Label ``data``. Also used by the loop to restore a stored result on resume."""
        if builtin:
            label = label_builtin(self.label_store, artefact_id=artefact_id, data=data)
        else:
            label = label_bytes(self.label_store, self.policy, artefact_id=artefact_id,
                                repo_id=self.repo_id, data=data)
        return ToolOutput(data, label, artefact_id, builtin, ended, payload)

    # ---- file tools -------------------------------------------------------------------
    def _list_files(self, path: str = ".") -> _R:
        real, _ = _locate(str(self.path), path)
        return self._list_dir(real)

    @staticmethod
    def _list_dir(real: str) -> _R:
        try:
            if not stat.S_ISDIR(os.lstat(real).st_mode):
                return _err("that path is not a directory")
            names = sorted(n for n in os.listdir(real) if not _reserved(n))
        except OSError:
            return _err("that directory cannot be listed")
        lines = []
        for n in names[:LIST_MAX_ENTRIES]:
            try:
                mode = os.lstat(os.path.join(real, n)).st_mode
            except OSError:
                continue
            lines.append(n + ("/" if stat.S_ISDIR(mode) else "@" if stat.S_ISLNK(mode) else ""))
        if len(names) > LIST_MAX_ENTRIES:
            lines.append(f"[{len(names) - LIST_MAX_ENTRIES} more entries not shown]")
        return _R("\n".join(lines) if lines else "(empty directory)")

    def _read_text(self, real: str, start_line: int, max_lines: int) -> _R:
        if start_line < 1 or max_lines < 1:
            return _err("start_line and max_lines must be at least 1")
        max_lines = min(max_lines, READ_MAX_LINES)
        try:
            st = os.lstat(real)
        except OSError:
            return _err("no such file")
        if not stat.S_ISREG(st.st_mode):
            return _err("that path is not a regular file")
        out: list[str] = []
        size = 0
        more = False
        lineno = 0
        try:
            fd = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as f:
                head = f.peek(8192)[:8192] if hasattr(f, "peek") else b""
                if b"\0" in head:
                    return _err("binary files cannot be read")
                while True:
                    raw = f.readline(READ_MAX_BYTES + 1)
                    if not raw:
                        break
                    lineno += 1
                    if lineno < start_line:
                        continue
                    text = raw.decode("utf-8", "replace")
                    if len(out) >= max_lines:
                        more = True
                        break
                    if size + len(raw) > READ_MAX_BYTES - 200:
                        if not out:                       # one huge line: return its start
                            out.append(text[:READ_MAX_BYTES - 400] + "\n")
                        more = True
                        break
                    out.append(text)
                    size += len(raw)
        except OSError:
            return _err("that file cannot be read")
        if not out and not more:
            return _R("(no lines in that range)" if lineno else "(empty file)")
        body = "".join(out)
        if len(body.encode("utf-8")) > READ_MAX_BYTES - 200:       # replacement characters can widen text
            body = body.encode("utf-8")[:READ_MAX_BYTES - 200].decode("utf-8", "ignore")
            more = True
        if more:
            if not body.endswith("\n"):
                body += "\n"
            body += f"[truncated; continue with start_line={start_line + len(out)}]"
        return _R(body)

    def _read_file(self, path: str, start_line: int = 1, max_lines: int = READ_MAX_LINES) -> _R:
        real, _ = _locate(str(self.path), path)
        return self._read_text(real, start_line, max_lines)

    def _search(self, text: str, path: str = ".") -> _R:
        if not text or len(text) > 500 or "\n" in text or "\r" in text:
            return _err("search text must be one non-empty line of at most 500 characters")
        needle = text
        real, _ = _locate(str(self.path), path)
        try:
            st = os.lstat(real)
        except OSError:
            return _err("no such path")
        base_real = os.path.realpath(self.path)
        files: list[str] = []
        if stat.S_ISREG(st.st_mode):
            files = [real]
        elif stat.S_ISDIR(st.st_mode):
            for dirpath, dirnames, filenames in os.walk(real, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames
                                     if not _reserved(d) and not os.path.islink(os.path.join(dirpath, d)))
                for fn in sorted(filenames):
                    full = os.path.join(dirpath, fn)
                    if not _reserved(fn) and not os.path.islink(full):
                        files.append(full)
                if len(files) > _SEARCH_MAX_FILES:
                    break
        else:
            return _err("that path cannot be searched")
        hits: list[str] = []
        total = 0
        for full in files[:_SEARCH_MAX_FILES]:
            try:
                st = os.lstat(full)
                if not stat.S_ISREG(st.st_mode) or st.st_size > _SEARCH_MAX_FILE:
                    continue
                total += st.st_size
                if total > _SEARCH_MAX_TOTAL:
                    break
                fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                with os.fdopen(fd, "rb") as f:
                    data = f.read(_SEARCH_MAX_FILE + 1)
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            rel = os.path.relpath(full, base_real).replace(os.sep, "/")
            for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if needle in line:
                    ex = line.strip()
                    if len(ex) > SEARCH_EXCERPT:
                        ex = ex[:SEARCH_EXCERPT]
                    hits.append(f"{rel}:{n}: {ex}")
                    if len(hits) >= SEARCH_MAX_HITS:
                        break
            if len(hits) >= SEARCH_MAX_HITS:
                break
        if not hits:
            return _R("no matches")
        if len(hits) >= SEARCH_MAX_HITS:
            hits.append(f"[stopped after {SEARCH_MAX_HITS} hits]")
        return _R("\n".join(hits))

    def _write_target(self, path: str) -> str:
        """Real path of a file to create or overwrite; never a symlink, parents inside the worktree."""
        target, _name = _locate(str(self.path), path, parent_only=True)
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            return target
        except OSError:
            raise _ToolError("that path cannot be written") from None
        if stat.S_ISLNK(st.st_mode):
            raise _ToolError("refusing to write through a symlink")
        if not stat.S_ISREG(st.st_mode):
            raise _ToolError("that path is not a regular file")
        return target

    @staticmethod
    def _write_bytes(target: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(data)

    def _write_file(self, path: str, content: str) -> _R:
        try:
            data = content.encode("utf-8")
        except UnicodeError:
            return _err("content is not valid text")
        if len(data) > WRITE_MAX_BYTES:
            return _err("content is larger than the 256 KB limit")
        target = self._write_target(path)
        try:
            self._write_bytes(target, data)
        except OSError:
            return _err("that file cannot be written")
        return _R(f"ok: wrote {len(data)} bytes", builtin=True)

    def _replace(self, path: str, old: str, new: str) -> _R:
        if not old:
            return _err("old must not be empty")
        target = self._write_target(path)
        try:
            st = os.lstat(target)
            if st.st_size > EDIT_MAX_FILE_BYTES:
                return _err("that file is too large to edit this way")
            fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as f:
                raw = f.read()
            text = raw.decode("utf-8")
        except FileNotFoundError:
            return _err("no such file")
        except (OSError, UnicodeError):
            return _err("that file cannot be edited as text")
        n = text.count(old)
        if n == 0:
            return _err("old text was not found")
        if n > 1:
            return _err(f"old text occurs {n} times; it must occur exactly once")
        data = text.replace(old, new, 1).encode("utf-8")
        if len(data) > WRITE_MAX_BYTES:
            return _err("the result would be larger than the 256 KB limit")
        try:
            self._write_bytes(target, data)
        except OSError:
            return _err("that file cannot be written")
        return _R("ok: replaced 1 occurrence", builtin=True)

    # ---- commands and checks ----------------------------------------------------------
    def _run_command(self, argv: list, timeout_s: int = 60) -> _R:
        if not argv or len(argv) > 100 or any(not isinstance(a, str) or len(a) > 4096 or "\0" in a
                                              for a in argv):
            return _err("argv must be a non-empty list of at most 100 strings")
        if not argv[0] or argv[0].startswith("-"):
            return _err("argv must start with a program name")
        timeout = max(1, min(int(timeout_s), RUN_MAX_TIMEOUT_S))
        argv = [sys.executable if argv[0] == "{python}" else argv[0], *argv[1:]]
        limits = dataclasses.replace(self.sandbox_limits, wall_s=timeout)
        try:
            r = sandbox_run(argv, cwd=self.path, limits=limits, allow_unsandboxed=self.allow_unsandboxed,
                            env={"PYTEST_ADDOPTS": "-p no:cacheprovider"})
        except SandboxUnavailable:
            return _err("no sandbox is available, so commands cannot be run")
        except (ValueError, OSError):
            return _err("the command could not be started")
        combined = r.stdout + (b"\n" if r.stdout and r.stderr else b"") + r.stderr
        omitted = len(combined) > RUN_OUTPUT_TAIL
        tail = combined[-RUN_OUTPUT_TAIL:].decode("utf-8", "replace")
        head = (f"exit code: {r.returncode}" if r.returncode is not None
                else f"timed out after {timeout}s; the process was killed")
        notes = []
        if omitted:
            notes.append("earlier output omitted")
        if r.truncated:
            notes.append("output was cut by the sandbox limit")
        suffix = f" [{'; '.join(notes)}]" if notes else ""
        return _R(f"{head}\n--- output{suffix} ---\n{tail}")

    def _run_visible(self) -> _R:
        if self.verify_visible is None:
            return _err("visible checks are not available for this task")
        try:
            commit = self.worktree.commit_all("hera: before visible checks")
            feedback = self.verify_visible(commit)
        except Exception:
            return _err("the visible checks could not be run")
        text = feedback if isinstance(feedback, str) else str(feedback)
        if len(text.encode("utf-8")) > VISIBLE_FEEDBACK_MAX:
            text = text.encode("utf-8")[:VISIBLE_FEEDBACK_MAX].decode("utf-8", "ignore") + "\n[truncated]"
        return _R(text or "(no feedback)")

    def _read_visible(self, path: str) -> _R:
        if self.visible_checks_dir is None:
            return _err("there are no visible checks for this task")
        real, _ = _locate(str(self.visible_checks_dir), path)
        try:
            is_dir = stat.S_ISDIR(os.lstat(real).st_mode)
        except OSError:
            return _err("no such file")
        if is_dir:
            return self._list_dir(real)
        return self._read_text(real, 1, READ_MAX_LINES)

    # ---- ending the attempt -----------------------------------------------------------
    def _submit(self, summary: str, declared_scope: list, twin_pattern: str = "") -> _R:
        if not summary.strip() or len(summary) > _SUMMARY_MAX:
            return _err("summary must be 1 to 4000 characters")
        if len(declared_scope) > 100:
            return _err("declared_scope has too many entries")
        scope = []
        for s in declared_scope:
            parts = s.split("/")
            if (not s or len(s) > 300 or "\0" in s or "\\" in s or s.startswith("/")
                    or ".." in parts or any(_reserved(p) for p in parts)):
                return _err("declared_scope entries must be relative paths or globs without '..'")
            scope.append(s)
        if len(twin_pattern) > 300:
            return _err("twin_pattern must be at most 300 characters")
        return _R("ok: submitted; the attempt is over", builtin=True, ended="submitted",
                  payload={"summary": summary, "declared_scope": scope, "twin_pattern": twin_pattern})

    def _dispute(self, check_id: str, requirement_id: str, argument: str) -> _R:
        if not _CHECK_ID.fullmatch(check_id) or not _REQ_ID.fullmatch(requirement_id):
            return _err("check_id or requirement_id is not in the expected format")
        if not argument.strip() or len(argument) > _SUMMARY_MAX:
            return _err("argument must be 1 to 4000 characters")
        return _R("ok: dispute recorded; the attempt is over", builtin=True, ended="disputed",
                  payload={"check_id": check_id, "requirement_id": requirement_id,
                           "argument": argument})
