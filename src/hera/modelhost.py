"""Supervisor for the local model host (S5b): a child process that speaks the OpenAI-compatible
HTTP protocol on 127.0.0.1. This module starts, health-checks, leases, evicts, reloads and restarts
that child. It knows nothing about the model: the real MLX worker is S5.

Rules (from the measured Mac baseline, docs/MODELHOST.md):

- peak memory = fixed overhead + kilobytes per context token, so the context cap follows from the
  memory limit (`context_cap`);
- reloading takes about three seconds, so the model is evicted before every sandbox job and after
  60 seconds idle, and only one local generation runs at a time (`ModelHost.lease`);
- HERA never adopts a process it did not start: content sent to a local destination skips the
  privacy gate, so whatever listens on the port must be a child HERA spawned itself.

Standard library only. The only network use is `http.client` and `socket` connecting to 127.0.0.1
(health checks and the "is the port free" probe); `loopback_url` is the one place a URL is built.
No request or response content is ever logged here.
"""
from __future__ import annotations

import atexit
import contextlib
import http.client
import json
import math
import os
import re
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

LOOPBACK = "127.0.0.1"
EVICTED, STARTING, READY, FAILED, NO_ROOM = "evicted", "starting", "ready", "failed", "no_room"
STATES = (EVICTED, STARTING, READY, FAILED, NO_ROOM)
TERM_GRACE_S = 5.0                 # SIGTERM, then this long, then SIGKILL
ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
_POLL_S = 0.05


class HostError(Exception):
    """Base of every refusal the model host can give."""


class HostUnavailable(HostError):
    """No model can be loaded now: no room in memory, or the restart budget is used up."""


class HostStartError(HostError):
    """The child could not be started or did not become healthy; it has been reaped."""


class HostBusy(HostError):
    """A lease is active: a second lease, an eviction or a reset has to wait."""


# ---- settings ---------------------------------------------------------------------------------------
_KEYS = ("host_memory_limit_gb", "max_context_tokens", "enable_thinking", "command", "port",
         "fixed_overhead_gb", "kv_kb_per_token", "min_context_tokens", "reserve_gb",
         "idle_evict_seconds", "evict_before_sandbox", "startup_timeout_s", "health_timeout_s",
         "health_failures_to_restart", "max_restarts", "restart_window_s")


def _number(key: str, v, lo: float, hi: float, *, lo_open: bool = False) -> float:
    ok = (not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
          and (v > lo if lo_open else v >= lo) and v <= hi)
    if not ok:
        raise ValueError(f"local_model.{key}: must be a finite number {'above' if lo_open else 'from'} "
                         f"{lo:g} {'up to' if lo_open else 'to'} {hi:g}, got {v!r}")
    return float(v)


def _integer(key: str, v, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"local_model.{key}: must be an integer from {lo} to {hi}, got {v!r}")
    return v


@dataclass(frozen=True)
class HostSettings:
    """The ``[local_model]`` table of hera.toml, validated. An empty ``command`` means there is no
    supervised host (a local provider then simply points at whatever the user runs)."""
    host_memory_limit_gb: float = 9.0
    max_context_tokens: int = 12000
    enable_thinking: bool = False
    command: tuple = ()
    port: int = 8091
    fixed_overhead_gb: float = 8.0
    kv_kb_per_token: float = 81.0
    min_context_tokens: int = 2048
    reserve_gb: float = 0.5
    idle_evict_seconds: float = 60.0
    evict_before_sandbox: bool = True
    startup_timeout_s: float = 60.0
    health_timeout_s: float = 2.0
    health_failures_to_restart: int = 3
    max_restarts: int = 3
    restart_window_s: float = 600.0

    @classmethod
    def from_table(cls, table) -> "HostSettings":
        """Parse and validate a ``[local_model]`` table. ValueError names the offending key."""
        if not isinstance(table, Mapping):
            raise ValueError("local_model: must be a table")
        unknown = set(table) - set(_KEYS)
        if unknown:
            raise ValueError(f"local_model: unknown setting(s): {', '.join(sorted(map(str, unknown)))}")
        d = cls()
        t = dict(table)
        out: dict = {}
        num = (("host_memory_limit_gb", 0, 1024, True), ("fixed_overhead_gb", 0, 1024, False),
               ("kv_kb_per_token", 0, 1e6, True), ("reserve_gb", 0, 1024, False),
               ("idle_evict_seconds", 0, 86400, True), ("startup_timeout_s", 0, 3600, True),
               ("health_timeout_s", 0, 60, True), ("restart_window_s", 0, 86400, True))
        for key, lo, hi, lo_open in num:
            out[key] = _number(key, t[key], lo, hi, lo_open=lo_open) if key in t else getattr(d, key)
        ints = (("max_context_tokens", 256, 10_000_000), ("min_context_tokens", 1, 10_000_000),
                ("port", 1024, 65535), ("health_failures_to_restart", 1, 100), ("max_restarts", 0, 100))
        for key, lo, hi in ints:
            out[key] = _integer(key, t[key], lo, hi) if key in t else getattr(d, key)
        for key in ("enable_thinking", "evict_before_sandbox"):
            if key in t and not isinstance(t[key], bool):
                raise ValueError(f"local_model.{key}: must be true or false, got {t[key]!r}")
            out[key] = t.get(key, getattr(d, key))
        cmd = t.get("command", ())
        if (not isinstance(cmd, (list, tuple)) or not all(isinstance(c, str) and c and "\0" not in c
                                                          for c in cmd)):
            raise ValueError("local_model.command: must be a list of non-empty strings")
        out["command"] = tuple(cmd)
        if out["min_context_tokens"] > out["max_context_tokens"]:
            raise ValueError("local_model.min_context_tokens: must not exceed max_context_tokens")
        return cls(**out)

    @classmethod
    def from_config(cls, cfg) -> "HostSettings":
        """The one parser of ``cfg.local_model`` (``config.load`` calls it, so a bad table fails at load)."""
        return cls.from_table(cfg.local_model)

    def argv(self, port: int, context: int) -> list[str]:
        """The command with ``{port}`` and ``{context}`` filled in (plain replacement, no formatting)."""
        return [c.replace("{port}", str(port)).replace("{context}", str(context)) for c in self.command]


# ---- the context cap ------------------------------------------------------------------------------------
def context_cap(*, limit_gb: float, overhead_gb: float, kv_kb_per_token: float,
                available_gb: float | None, reserve_gb: float, max_tokens: int, min_tokens: int) -> int:
    """How many context tokens fit: peak memory = overhead + kv x tokens, decimal units (1 GB = 1e9
    bytes, 1 KB = 1e3 bytes). The effective limit is the host limit, or free memory minus a reserve
    when that is lower (None = free memory unknown: use the limit). Clamped to ``max_tokens``;
    0 means no room to load (fewer than ``min_tokens`` fit, or any input is not a usable number)."""
    values = (limit_gb, overhead_gb, kv_kb_per_token, reserve_gb)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values) or kv_kb_per_token <= 0:
        return 0
    eff = limit_gb
    if available_gb is not None:
        if not isinstance(available_gb, (int, float)) or not math.isfinite(available_gb):
            return 0
        eff = min(limit_gb, available_gb - reserve_gb)
    room = eff - overhead_gb
    if room <= 0:
        return 0
    cap = min(math.floor(room * 1e9 / (kv_kb_per_token * 1e3)), max_tokens)
    return cap if cap >= min_tokens else 0


# ---- free memory ---------------------------------------------------------------------------------------
def parse_meminfo(text: str) -> int | None:
    """MemAvailable from /proc/meminfo text, in bytes (the file's "kB" are 1024 bytes); None if absent."""
    m = re.search(r"^MemAvailable:\s+(\d+)\s*kB\s*$", text, re.MULTILINE)
    return int(m.group(1)) * 1024 if m else None


def parse_vm_stat(text: str) -> int | None:
    """Free + inactive + speculative pages x page size from macOS `vm_stat` output, in bytes. The
    page size comes from the header line. A missing page size, free or inactive count gives None;
    a missing speculative count counts as zero (the smaller, safer number)."""
    size = re.search(r"page size of (\d+) bytes", text)
    if not size:
        return None
    pages: dict[str, int] = {}
    for name in ("free", "inactive", "speculative"):
        m = re.search(rf"^Pages {name}:\s+(\d+)\.?\s*$", text, re.MULTILINE)
        if m:
            pages[name] = int(m.group(1))
    if "free" not in pages or "inactive" not in pages:
        return None
    return (pages["free"] + pages["inactive"] + pages.get("speculative", 0)) * int(size.group(1))


def available_memory_bytes() -> int | None:
    """Memory that could be used without swapping, or None when it cannot be told (any other
    platform, any failure)."""
    try:
        if os.name == "posix" and os.uname().sysname == "Linux":
            with open("/proc/meminfo", encoding="ascii", errors="replace") as fh:
                return parse_meminfo(fh.read())
        if os.name == "posix" and os.uname().sysname == "Darwin":
            r = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, timeout=5, check=False,
                               env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
            return parse_vm_stat(r.stdout.decode("ascii", "replace")) if r.returncode == 0 else None
    except Exception:
        return None
    return None


# ---- small helpers ---------------------------------------------------------------------------------------
def loopback_url(port: int, path: str = "") -> str:
    """The only place this module builds a URL: always http://127.0.0.1:<port>."""
    return f"http://{LOOPBACK}:{int(port)}{path}"


def scrubbed_env(environ: Mapping[str, str], port: int, context: int) -> dict[str, str]:
    """The child's environment: a few harmless variables plus the port and context it must use.
    No API keys and nothing else from the parent."""
    env = {k: environ[k] for k in ENV_KEEP if k in environ}
    env["HERA_MODELHOST_PORT"] = str(port)
    env["HERA_MODELHOST_CONTEXT"] = str(context)
    return env


def _default_spawn(argv: Sequence[str], env: dict, log) -> subprocess.Popen:
    """Own session (so the whole group can be killed), no stdin, output appended to the log file
    (never a pipe: an unread pipe can block the child)."""
    return subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                            start_new_session=True)


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


@dataclass(frozen=True)
class Lease:
    """What a lease gives: the base URL of the running host and the context cap in force."""
    url: str
    context_cap: int


# ---- the supervisor ------------------------------------------------------------------------------------
class ModelHost:
    """Supervises one child process. No background threads: ``tick()`` is driven by the caller.

    ``clock`` drives idle eviction and the restart window (inject a fake one in tests); startup and
    health waits use real time. ``spawn(argv, env, log_file) -> Popen`` can be replaced in tests."""

    def __init__(self, settings: HostSettings, *, home, clock: Callable[[], float] = time.monotonic,
                 memory: Callable[[], int | None] | None = None,
                 spawn: Callable | None = None) -> None:
        self.settings = settings
        self.home = Path(home)
        self._clock = clock
        self._memory = memory if memory is not None else available_memory_bytes
        self._spawn = spawn or _default_spawn
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._state = EVICTED
        self._leased = False
        self._cap = 0
        self._last_used = 0.0
        self._needs_restart = ""           # why the next start replaces a failed child ("" = first start)
        self._restarts: list[float] = []
        self._last_error = ""
        self._failed_marker = self.home / "logs" / "modelhost.failed"
        if self._failed_marker.exists():     # a person has to run `hera host reset`
            self._state = FAILED
            self._last_error = "restart budget used up in an earlier run; run `hera host reset`"

    # -- paths and events
    @property
    def _log_path(self) -> Path:
        return self.home / "logs" / "modelhost.log"

    def _event(self, kind: str, **detail) -> None:
        """One JSON line per event. Details are small facts (ports, exit codes, reasons), never
        request or response content. Best effort: a log that cannot be written never stops the host."""
        rec = {"time": time.time(), "event": kind, "pid": self._proc.pid if self._proc else None,
               "detail": detail}
        try:
            path = self.home / "logs" / "modelhost.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
        except OSError:
            pass

    # -- small queries
    def _recent_restarts(self) -> int:
        now = self._clock()
        self._restarts = [t for t in self._restarts if now - t < self.settings.restart_window_s]
        return len(self._restarts)

    def _available_gb(self) -> float | None:
        try:
            b = self._memory()
        except Exception:
            return None
        return b / 1e9 if isinstance(b, (int, float)) and math.isfinite(b) and b >= 0 else None

    def _compute_cap(self) -> int:
        s = self.settings
        return context_cap(limit_gb=s.host_memory_limit_gb, overhead_gb=s.fixed_overhead_gb,
                           kv_kb_per_token=s.kv_kb_per_token, available_gb=self._available_gb(),
                           reserve_gb=s.reserve_gb, max_tokens=s.max_context_tokens,
                           min_tokens=s.min_context_tokens)

    @staticmethod
    def _port_in_use_probe(port: int) -> bool:
        """True unless a connection to the port is cleanly refused (fail closed: anything else
        means something may be listening)."""
        try:
            with socket.create_connection((LOOPBACK, port), timeout=1.0):
                return True
        except ConnectionRefusedError:
            return False
        except OSError:
            return True

    def _probe(self, timeout: float) -> bool:
        """GET /v1/models on 127.0.0.1 answers 200."""
        conn = http.client.HTTPConnection(LOOPBACK, self.settings.port, timeout=timeout)
        try:
            conn.request("GET", "/v1/models")
            resp = conn.getresponse()
            resp.read()
            return resp.status == 200
        except (OSError, http.client.HTTPException):
            return False
        finally:
            conn.close()

    def status(self) -> dict:
        """State, pid, port, context cap, free memory (bytes, None if unknown), restarts inside the
        window, whether something is already listening on the port, and the last error."""
        with self._lock:
            pid = self._proc.pid if self._proc is not None else None
            snap = {"state": self._state, "pid": pid, "port": self.settings.port,
                    "context_cap": self._cap if self._proc is not None else 0,
                    "leased": self._leased, "restarts_in_window": self._recent_restarts(),
                    "last_error": self._last_error}
        try:
            mem = self._memory()
        except Exception:
            mem = None
        snap["available_memory_bytes"] = mem if isinstance(mem, int) and not isinstance(mem, bool) else None
        snap["port_in_use"] = pid is None and self._port_in_use_probe(self.settings.port)
        return snap

    # -- killing
    def _kill_child(self) -> None:
        """SIGTERM the whole process group, SIGKILL after the grace period, reap the leader, then
        sweep the group once more so no grandchild outlives it. Never raises."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        atexit.unregister(self._kill_at_exit)
        pgid = proc.pid                      # start_new_session makes the child its own group leader
        try:
            if proc.poll() is None:
                _killpg(pgid, signal.SIGTERM)
                try:
                    proc.wait(timeout=TERM_GRACE_S)
                except subprocess.TimeoutExpired:
                    _killpg(pgid, signal.SIGKILL)
                    proc.wait(timeout=10)
            _killpg(pgid, signal.SIGKILL)    # stragglers (also after a crash of the leader)
        except Exception:
            _killpg(pgid, signal.SIGKILL)
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        self._cap = 0

    def _kill_at_exit(self) -> None:
        try:
            self._kill_child()
        except Exception:
            pass

    def _note_exit(self, why: str) -> None:
        """The child is gone or unusable: record it, reap it, and make the next start a restart."""
        code = self._proc.poll() if self._proc is not None else None
        self._event(why, exit_code=code)
        self._last_error = (f"the model host process exited with code {code}" if why == "crash"
                            else "the model host stopped answering health checks")
        self._kill_child()
        self._needs_restart = why
        self._state = EVICTED

    def _enter_failed(self, reason: str) -> None:
        self._kill_child()
        self._state = FAILED
        self._last_error = reason
        self._event("failed", reason=reason)
        try:
            self._failed_marker.parent.mkdir(parents=True, exist_ok=True)
            self._failed_marker.write_text(json.dumps({"time": time.time(), "reason": reason}) + "\n",
                                           encoding="utf-8")
        except OSError:
            pass

    # -- starting
    def _start(self) -> None:
        cap = self._compute_cap()
        if cap == 0:
            self._state = NO_ROOM
            self._last_error = "not enough free memory to load the model"
            self._event("no_room", available_gb=self._available_gb())
            raise HostUnavailable(self._last_error)
        port = self.settings.port
        if self._port_in_use_probe(port):
            # never adopt a process HERA did not start: local destinations skip the privacy gate
            self._state = EVICTED
            self._last_error = f"something is already listening on 127.0.0.1:{port}; HERA will not use it"
            self._event("port_in_use", port=port)
            raise HostStartError(self._last_error)
        if self._needs_restart:
            if self._recent_restarts() >= self.settings.max_restarts:
                self._enter_failed(f"the model host failed {self.settings.max_restarts + 1} times within "
                                   f"{self.settings.restart_window_s:g} s; run `hera host reset`")
                raise HostUnavailable(self._last_error)
            self._restarts.append(self._clock())
            self._event("restart", after=self._needs_restart)
        argv = self.settings.argv(port, cap)
        env = scrubbed_env(os.environ, port, cap)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._state = STARTING
        try:
            with open(self._log_path, "ab") as log:
                self._proc = self._spawn(argv, env, log)
        except (OSError, ValueError, TypeError) as e:
            self._start_failed(f"could not start the model host process ({type(e).__name__})")
        atexit.register(self._kill_at_exit)
        self._event("start", port=port, context_cap=cap)
        t0 = time.monotonic()
        deadline = t0 + self.settings.startup_timeout_s
        while True:
            if self._proc.poll() is not None:
                self._start_failed(f"the model host process exited during startup "
                                   f"(code {self._proc.poll()})")
            if self._probe(self.settings.health_timeout_s):
                break
            if time.monotonic() >= deadline:
                self._start_failed(f"the model host did not become healthy within "
                                   f"{self.settings.startup_timeout_s:g} s")
            time.sleep(_POLL_S)
        self._cap = cap
        self._state = READY
        self._needs_restart = ""
        self._last_error = ""
        self._last_used = self._clock()
        self._event("ready", port=port, context_cap=cap, startup_s=round(time.monotonic() - t0, 3))

    def _start_failed(self, message: str) -> None:
        self._event("start_failed", reason=message)
        self._kill_child()
        self._state = EVICTED
        self._last_error = message
        self._needs_restart = self._needs_restart or "start_failure"
        raise HostStartError(message)

    def _ensure_loaded(self) -> None:
        """Make sure a healthy child is running: keep a good one, replace a dead or hung one."""
        if self._proc is not None:
            if self._proc.poll() is not None:
                self._note_exit("crash")
            else:
                s = self.settings
                for _ in range(s.health_failures_to_restart):
                    if self._probe(s.health_timeout_s):
                        return
                    if self._proc.poll() is not None:
                        break
                    time.sleep(_POLL_S)
                self._note_exit("crash" if self._proc.poll() is not None else "hang")
        self._start()

    # -- the public operations
    @contextlib.contextmanager
    def lease(self) -> Iterator[Lease]:
        """Exclusive use of the loaded model for one generation. Loads (or reloads) it if needed,
        refuses with HostBusy if another lease is active (it never waits), and cannot be evicted
        until the block ends."""
        with self._lock:
            if self._leased:
                raise HostBusy("the local model is in use by another request")
            if self._state == FAILED:
                raise HostUnavailable(self._last_error or "the model host has failed; run `hera host reset`")
            self._leased = True
        try:
            self._ensure_loaded()
            try:
                yield Lease(loopback_url(self.settings.port), self._cap)
            finally:
                self._last_used = self._clock()
                if self._proc is not None and self._proc.poll() is not None:
                    self._note_exit("crash")
        finally:
            with self._lock:
                self._leased = False

    def evict(self, reason: str) -> None:
        """Stop the child (whole process group) and reap it. HostBusy while leased; does nothing
        when nothing is loaded."""
        with self._lock:
            if self._leased:
                raise HostBusy("cannot evict the model while it is in use")
            self._evict_locked(reason)

    def _evict_locked(self, reason: str) -> None:
        if self._proc is None:
            return
        self._event("evict", reason=reason)
        self._kill_child()
        if self._state in (READY, STARTING):
            self._state = EVICTED

    def before_sandbox_job(self) -> None:
        """Strict alternation: the model is out of memory before a sandbox job runs."""
        if self.settings.evict_before_sandbox:
            self.evict("before sandbox job")

    def tick(self) -> None:
        """Housekeeping, called by whoever drives the host: evict after ``idle_evict_seconds`` without
        use; notice a child that exited on its own (recorded; it is restarted by the next lease)."""
        with self._lock:
            if self._leased or self._proc is None:
                return
            if self._proc.poll() is not None:
                self._note_exit("crash")
            elif self._clock() - self._last_used >= self.settings.idle_evict_seconds:
                self._evict_locked("idle")

    def reset(self) -> None:
        """A person clears the `failed` state (and the restart history) so the host can start again."""
        with self._lock:
            if self._leased:
                raise HostBusy("cannot reset while the model is in use")
            self._kill_child()
            self._restarts.clear()
            self._needs_restart = ""
            self._last_error = ""
            self._state = EVICTED
            try:
                self._failed_marker.unlink()
            except FileNotFoundError:
                pass
            self._event("reset")


# ---- the sandbox hook ---------------------------------------------------------------------------------
def sandbox_hook(hosts: Sequence[ModelHost]) -> Callable[[], None]:
    """A callback for `sandbox.before_each_job`: evict every host before a sandbox job. It never
    raises (a busy or failing host is logged and the job goes on), so a model host problem cannot
    crash the coordinator."""
    def hook() -> None:
        for h in hosts:
            try:
                h.before_sandbox_job()
            except HostBusy:
                h._event("evict_skipped", reason="in use")
            except Exception:
                h._event("evict_failed", reason="unexpected error")
    return hook
