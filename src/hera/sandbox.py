"""Run untrusted candidate code with no network, limited resources and a narrow view of disk.

Mechanisms (``detect_mechanism``):

* ``sandbox-exec`` (macOS): a generated Seatbelt profile. Network denied entirely; writes
  denied except cwd, a private temp dir and ``writable``; file contents unreadable under the
  home directory and temp roots except for cwd, ``read_only``, ``writable`` and the Python
  installation; a built-in deny list (``~/.ssh``, ``~/.aws``, ``~/.config``, ``~/.gnupg``,
  ``~/.netrc``, the HERA policy and check directories, plus ``deny_read``) applied last.
* ``bwrap`` (Linux, bubblewrap): all namespaces unshared (so no network), an allow-list
  filesystem (system directories and the Python prefix read-only, cwd writable, tmpfs for
  /tmp and the home directory). Anything not bound does not exist inside.
* ``none``: **provides NO isolation of network or filesystem.** The child can open sockets
  and read or write anything the user can. Only the environment scrub, resource limits,
  timeout and output caps apply. It must be requested with ``allow_unsandboxed=True``.

Known limits are listed in docs/SANDBOX.md. This module deliberately imports no network
library; the network is cut off by the mechanism, not by this code.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Sequence

try:                                     # not available on native Windows (use WSL2)
    import resource
except ImportError:                      # pragma: no cover
    resource = None                      # type: ignore[assignment]

from . import paths

_SAFE_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
_MECHANISMS = ("sandbox-exec", "bwrap", "none")
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"


class SandboxUnavailable(RuntimeError):
    """No sandbox mechanism is available (or the requested one is)."""


@dataclass(frozen=True)
class Limits:
    """wall_s: wall-clock seconds. cpu_s: CPU seconds per process. memory_mb: address space,
    enforced on Linux only (macOS does not enforce RLIMIT_AS reliably, so it is skipped
    there). max_procs: processes the child may start *in addition to* those the user already
    runs (RLIMIT_NPROC counts every process of the user). max_output_bytes: cap per stream.
    max_file_mb: largest file the child may write."""
    wall_s: float = 300
    cpu_s: int = 300
    memory_mb: int | None = 2048
    max_procs: int | None = 256
    max_output_bytes: int = 2_000_000
    max_file_mb: int | None = 512


@dataclass(frozen=True)
class RunResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool
    wall_s: float
    mechanism: str


@lru_cache(maxsize=1)
def detect_mechanism() -> str:
    if sys.platform == "darwin":
        return "sandbox-exec" if os.access(_SANDBOX_EXEC, os.X_OK) else "none"
    if sys.platform.startswith("linux"):
        exe = shutil.which("bwrap")
        if exe:
            try:
                r = subprocess.run([exe, "--unshare-all", "--ro-bind", "/", "/", "true"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=15)
                if r.returncode == 0:
                    return "bwrap"
            except (OSError, subprocess.SubprocessError):
                pass
    return "none"


# ---- environment -----------------------------------------------------------------------
def _scrubbed_env(tmp: str, extra: Mapping[str, str] | None) -> dict[str, str]:
    py_dir = os.path.dirname(sys.executable)
    env = {
        "PATH": os.pathsep.join(dict.fromkeys([py_dir, *_SAFE_PATH])),
        "HOME": tmp,
        "TMPDIR": tmp,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }
    for k, v in (extra or {}).items():
        if not k or "=" in k or "\0" in k or "\0" in str(v):
            raise ValueError(f"invalid environment entry: {k!r}")
        env[k] = str(v)
    return env


# ---- path sets -------------------------------------------------------------------------
def _real(p) -> str:
    return os.path.realpath(os.fspath(p))


def _python_prefixes() -> list[str]:
    cands = [sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix]
    exe = _real(sys.executable)
    cands += [os.path.dirname(exe), os.path.dirname(os.path.dirname(exe))]
    return [c for c in dict.fromkeys(_real(c) for c in cands) if os.path.isdir(c)
            and c not in ("/", "")]


def _home() -> str | None:
    try:
        return _real(Path.home())
    except (RuntimeError, KeyError):
        return None


def _default_deny(extra: Sequence) -> list[str]:
    out: list[str] = []
    home = _home()
    if home:
        for rel in (".ssh", ".aws", ".config", ".gnupg", ".netrc", ".hera/policy", ".hera/checks"):
            out.append(os.path.join(home, rel))
    out += [_real(paths.policy_dir()), _real(paths.checks_dir())]
    out += [_real(p) for p in extra]
    return list(dict.fromkeys(out))


def _is_under(p: str, root: str) -> bool:
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def _drop_allowed(deny: Sequence[str], allowed: Sequence[str]) -> list[str]:
    """An explicitly passed path (read_only/writable/cwd) lifts the built-in deny for itself."""
    return [d for d in deny if d not in allowed]


# ---- macOS: Seatbelt profile ------------------------------------------------------------
def _sb(p: str) -> str:
    if any(ord(c) < 32 for c in p):
        raise ValueError("control characters in path")
    return '"' + p.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sbpl_profile(*, cwd: str, tmp: str, read_only: Sequence[str], writable: Sequence[str],
                  deny_read: Sequence[str]) -> str:
    """Rules are evaluated in order and the LAST matching rule wins, so: broad denies first,
    then the allowed exceptions, then the explicit deny list again at the end."""
    home = _home()
    deny_roots = [r for r in dict.fromkeys(
        [*(h for h in (home,) if h), "/Users", "/private/tmp", "/private/var/folders",
         "/private/var/tmp", "/Volumes"]) if r]
    tools = _python_prefixes()
    readable = [cwd, tmp, *read_only, *writable, *tools]
    write_ok = [cwd, tmp, *writable]
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny file-write*)",
        "(allow file-write* " + " ".join(f"(subpath {_sb(p)})" for p in write_ok) + ")",
        '(allow file-write* (literal "/dev/null") (literal "/dev/zero") '
        '(literal "/dev/dtracehelper"))',
        "(deny file-read-data " + " ".join(f"(subpath {_sb(r)})" for r in deny_roots) + ")",
        "(allow file-read* " + " ".join(f"(subpath {_sb(p)})" for p in readable) + ")",
    ]
    final_deny = _drop_allowed(_default_deny(deny_read), [*read_only, *writable, cwd, tmp])
    if final_deny:
        lines.append("(deny file-read-data " + " ".join(f"(subpath {_sb(p)})" for p in final_deny)
                     + ")")
    return "\n".join(lines) + "\n"


# ---- Linux: bubblewrap ------------------------------------------------------------------
def _bwrap_command(*, cwd: str, tmp: str, read_only: Sequence[str], writable: Sequence[str],
                   deny_read: Sequence[str], argv: Sequence[str]) -> list[str]:
    cmd = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session"]
    system = [d for d in ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
              if os.path.exists(d)]
    for d in system:
        cmd += ["--ro-bind", d, d]
    for f in ("alternatives", "ld.so.cache", "ld.so.conf", "ld.so.conf.d", "localtime",
              "passwd", "group"):
        p = os.path.join("/etc", f)
        if os.path.exists(p):
            cmd += ["--ro-bind", p, p]
    cmd += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    home = _home()
    if home and home not in ("/", "/tmp"):
        cmd += ["--tmpfs", home]
    for pfx in _python_prefixes():
        if not any(_is_under(pfx, s) for s in system):
            cmd += ["--ro-bind", pfx, pfx]
    for p in read_only:
        cmd += ["--ro-bind", p, p]
    cmd += ["--bind", tmp, tmp, "--bind", cwd, cwd]
    for p in writable:
        cmd += ["--bind", p, p]
    for d in _drop_allowed(_default_deny(deny_read), [*read_only, *writable, cwd, tmp]):
        if os.path.isdir(d):
            cmd += ["--tmpfs", d]
        elif os.path.exists(d):
            cmd += ["--ro-bind", os.devnull, d]
    cmd += ["--chdir", cwd, "--", *argv]
    return cmd


# ---- limits -----------------------------------------------------------------------------
def _count_user_procs() -> int | None:
    uid = os.getuid()
    try:
        if os.path.isdir("/proc/self"):
            n = 0
            with os.scandir("/proc") as it:
                for e in it:
                    if e.name.isdigit():
                        try:
                            if e.stat().st_uid == uid:
                                n += 1
                        except OSError:
                            pass
            return n
        r = subprocess.run(["/bin/ps", "-U", str(uid), "-o", "pid="], capture_output=True,
                           timeout=10, text=True)
        return len(r.stdout.split()) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _make_preexec(limits: Limits):
    nproc = None
    if limits.max_procs is not None:
        existing = _count_user_procs()
        if existing is not None:
            nproc = existing + limits.max_procs
    linux = sys.platform.startswith("linux")

    def setl(res: int, soft: int, hard: int | None = None, strict: bool = True) -> None:
        try:
            _s, cur_hard = resource.getrlimit(res)
            hard = soft if hard is None else hard
            if cur_hard != resource.RLIM_INFINITY:
                soft, hard = min(soft, cur_hard), min(hard, cur_hard)
            resource.setrlimit(res, (soft, hard))
        except (ValueError, OSError):
            if strict:
                raise

    def apply() -> None:               # runs in the child between fork and exec
        setl(resource.RLIMIT_CORE, 0)
        setl(resource.RLIMIT_CPU, int(limits.cpu_s), int(limits.cpu_s) + 1)
        if limits.max_file_mb is not None:
            setl(resource.RLIMIT_FSIZE, limits.max_file_mb * 1024 * 1024)
        if nproc is not None:
            setl(resource.RLIMIT_NPROC, nproc, strict=False)
        # RLIMIT_AS is Linux-only: macOS accepts the call but does not enforce it, and large
        # virtual reservations by system libraries make it fail spuriously.
        if linux and limits.memory_mb is not None:
            setl(resource.RLIMIT_AS, limits.memory_mb * 1024 * 1024, strict=False)
    return apply


# ---- running ----------------------------------------------------------------------------
def _drain(stream, cap: int, buf: bytearray, flag: list) -> None:
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            room = cap - len(buf)
            if room > 0:
                buf += chunk[:room]
            if len(chunk) > room:
                flag[0] = True         # keep draining (and discarding) so the child never blocks
    except (OSError, ValueError):
        pass


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run(argv: Sequence[str], *, cwd, limits: Limits = Limits(), read_only: Sequence = (),
        writable: Sequence = (), env: Mapping[str, str] | None = None,
        mechanism: str | None = None, allow_unsandboxed: bool = False,
        deny_read: Sequence = ()) -> RunResult:
    """Run ``argv`` in ``cwd`` under the chosen mechanism.

    ``cwd`` is the only writable project location (plus a private temp dir exported as
    TMPDIR and HOME, and any ``writable`` paths). ``read_only`` paths are readable.
    ``deny_read`` paths are added to the built-in deny list (sandbox-exec and bwrap only).

    mechanism "none" provides NO isolation of network or filesystem; it still scrubs the
    environment and applies rlimits, the wall-clock timeout and output caps. It raises
    SandboxUnavailable unless ``allow_unsandboxed=True``.
    """
    if mechanism is None:
        mechanism = detect_mechanism()
    if mechanism not in _MECHANISMS:
        raise ValueError(f"unknown mechanism: {mechanism}")
    if mechanism == "none" and not allow_unsandboxed:
        raise SandboxUnavailable("no sandbox mechanism available; pass allow_unsandboxed=True "
                                 "to run with no network or filesystem isolation")
    if mechanism == "sandbox-exec" and not (sys.platform == "darwin" and os.access(_SANDBOX_EXEC, os.X_OK)):
        raise SandboxUnavailable("sandbox-exec is not available on this system")
    if mechanism == "bwrap" and not shutil.which("bwrap"):
        raise SandboxUnavailable("bwrap is not available on this system")
    if resource is None:
        raise SandboxUnavailable("resource limits are not available on this platform")
    argv = [str(a) for a in argv]
    if not argv or argv[0].startswith("-"):
        raise ValueError("argv must start with a program name")
    cwd_p = _real(cwd)
    if not os.path.isdir(cwd_p):
        raise ValueError("cwd is not a directory")
    ro = [_real(p) for p in read_only]
    wr = [_real(p) for p in writable]
    for p in (*ro, *wr):
        if not os.path.exists(p):
            raise ValueError(f"path does not exist: {p}")

    tmp = _real(tempfile.mkdtemp(prefix="hera-sbx-"))
    try:
        child_env = _scrubbed_env(tmp, env)
        deny = [_real(p) for p in deny_read]
        if mechanism == "sandbox-exec":
            prof = _sbpl_profile(cwd=cwd_p, tmp=tmp, read_only=ro, writable=wr, deny_read=deny)
            cmd = [_SANDBOX_EXEC, "-p", prof, *argv]
        elif mechanism == "bwrap":
            cmd = _bwrap_command(cwd=cwd_p, tmp=tmp, read_only=ro, writable=wr, deny_read=deny,
                                 argv=argv)
            cmd[0] = shutil.which("bwrap") or "bwrap"
        else:
            cmd = argv
        preexec = _make_preexec(limits)
        t0 = time.monotonic()
        proc = subprocess.Popen(cmd, cwd=cwd_p, env=child_env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, preexec_fn=preexec)
        out, err = bytearray(), bytearray()
        trunc = [False]
        threads = [threading.Thread(target=_drain, args=(proc.stdout, limits.max_output_bytes, out, trunc), daemon=True),
                   threading.Thread(target=_drain, args=(proc.stderr, limits.max_output_bytes, err, trunc), daemon=True)]
        for t in threads:
            t.start()
        timed_out = False
        try:
            proc.wait(timeout=limits.wall_s)
        except subprocess.TimeoutExpired:
            timed_out = True
        _kill_group(proc.pid)          # also reaps stragglers left behind by a normal exit
        proc.wait()
        for t in threads:
            t.join(timeout=5)
        for s in (proc.stdout, proc.stderr):
            try:
                s.close()
            except OSError:
                pass
        return RunResult(None if timed_out else proc.returncode, bytes(out), bytes(err),
                         timed_out, trunc[0], time.monotonic() - t0, mechanism)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
