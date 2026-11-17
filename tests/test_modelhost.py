"""The local model host supervisor (S5b), tested against tests/fake_model_server.py: no Mac and no
model needed. Real child processes, real loopback HTTP, the real provider path (gateway, ledger)."""
import ast
import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

from hera import cli, config, modelhost, sandbox
from hera.agent.orchestrator import TaskSpec, run_task
from hera.config import Budget, Config, ProviderSpec, TierPrice
from hera.ledger import Ledger
from hera.modelhost import (HostBusy, HostSettings, HostStartError, HostUnavailable, Lease,
                            ModelHost, _default_spawn, context_cap, loopback_url, parse_meminfo,
                            parse_vm_stat, scrubbed_env)
from hera.oracle.checkstore import freeze
from hera.privacy.gateway import Destination, Gateway, is_loopback_url
from hera.privacy.grants import GrantStore
from hera.privacy.labels import LabelStore
from hera.privacy.policy import Policy
from hera.providers import Message, OpenAICompatProvider, ProviderError, build_providers
from hera.providers.registry import HOST_ABSENT_KEY, host_attachments
from hera.types import RepoClass
from scripted_llm import PRICES as LLM_PRICES, ScriptedLLM, step
from toyrepo import CHECK_FILES, CHECKS, CORRECT_CALC, REQUIREMENTS

FAKE = Path(__file__).with_name("fake_model_server.py")
GB20 = 20_000_000_000                       # plenty of free memory
PRICES = {"cheap": TierPrice(1.0, 4.0), "default": TierPrice(2.0, 10.0), "local": TierPrice(0.0, 0.0)}


# ---- helpers ------------------------------------------------------------------------------------------------
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Spawner:
    """Starts children like the real host does, and keeps the Popen objects so a test can ask the
    process itself whether it is gone (a pid check would be fooled by zombies)."""
    def __init__(self):
        self.procs = []

    def __call__(self, argv, env, log):
        p = _default_spawn(argv, env, log)
        self.procs.append(p)
        return p


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def group_gone(pgid: int, timeout: float = 5.0) -> bool:
    """No process of the group is left (polls: an orphaned zombie only disappears when its new
    parent reaps it)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def records(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def requests_seen(path: Path) -> list:
    return [r for r in records(path) if "path" in r]


class Rig:
    def __init__(self, tmp_path, monkeypatch=None, *, mode="normal", extra_args=(), memory=lambda: GB20,
                 clock=None, **kw):
        self.port = free_port()
        self.home = tmp_path / "home"
        self.record = tmp_path / "record.jsonl"
        cmd = [sys.executable, "-I", str(FAKE), "--port", "{port}", "--mode", mode, "--record",
               str(self.record), "--context", "{context}", *extra_args]
        base = dict(command=tuple(cmd), port=self.port, startup_timeout_s=20.0, health_timeout_s=2.0)
        base.update(kw)
        self.settings = HostSettings(**base)
        self.spawner = Spawner()
        self.clock = clock or Clock()
        self.host = ModelHost(self.settings, home=self.home, clock=self.clock, memory=memory,
                              spawn=self.spawner)
        self.labels = None

    # the real provider path: gateway, loopback destination, ledger
    def provider(self, conn):
        gw = Gateway(conn, LabelStore(conn), GrantStore(conn),
                     {"local-llm": Destination("local-llm", f"http://127.0.0.1:{self.port}/v1/chat/completions",
                                               {"content-type": "application/json"}, local=True)}, timeout=10)
        ledger = Ledger(conn, Budget(), PRICES)
        self.gw, self.ledger = gw, ledger
        return OpenAICompatProvider("local-llm", "local", "fake-9b", gw, ledger, host=self.host)

    def events(self):
        return records(self.home / "logs" / "modelhost.jsonl")

    def kinds(self):
        return [e["event"] for e in self.events()]


@pytest.fixture
def rig(tmp_path):
    made = []

    def make(**kw):
        r = Rig(tmp_path, **kw)
        made.append(r)
        return r
    yield make
    for r in made:
        try:
            r.host._kill_child()
        except Exception:
            pass
        for p in r.spawner.procs:
            if p.poll() is None:
                p.kill()
            p.wait()


def ask(provider, text="hello", **kw):
    return provider.complete([Message("user", "u1", text.encode())], max_tokens=16, **kw)


# ---- the context cap -------------------------------------------------------------------------------------
def cap(**kw):
    args = dict(limit_gb=9.0, overhead_gb=8.0, kv_kb_per_token=81, available_gb=None, reserve_gb=0.5,
                max_tokens=10 ** 9, min_tokens=2048)
    args.update(kw)
    return context_cap(**args)


@pytest.mark.parametrize("limit, expected", [(8.5, 6172), (9.0, 12345), (9.5, 18518), (10.5, 30864)])
def test_context_cap_reproduces_the_measured_table(limit, expected):
    assert cap(limit_gb=limit) == expected


def test_context_cap_is_clamped_to_the_configured_maximum():
    assert cap(max_tokens=12000) == 12000
    assert cap(max_tokens=5000) == 5000


def test_context_cap_follows_free_memory_when_that_is_lower_than_the_limit():
    assert cap(available_gb=8.7) == 2469            # 8.7 - 0.5 reserve = 8.2 GB effective
    assert cap(available_gb=20.0) == 12345          # plenty free: the limit applies
    assert cap(available_gb=None) == 12345          # unknown: the limit applies


def test_context_cap_is_zero_when_there_is_no_room_to_load():
    assert cap(available_gb=8.6) == 0               # 8.1 GB effective: only 1234 tokens, below the minimum
    assert cap(available_gb=3.0) == 0
    assert cap(limit_gb=8.0) == 0                   # the limit does not even cover the overhead
    assert cap(max_tokens=1000) == 0                # a maximum below the minimum is no room either


def test_context_cap_fails_closed_on_unusable_numbers():
    assert cap(limit_gb=float("nan")) == 0
    assert cap(available_gb=float("inf")) == 0
    assert cap(kv_kb_per_token=0) == 0
    assert cap(kv_kb_per_token=-1) == 0


# ---- free memory --------------------------------------------------------------------------------------------
MEMINFO = """MemTotal:       16384000 kB
MemFree:         1000000 kB
MemAvailable:    8192000 kB
Buffers:          100000 kB
"""
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               12345.
Pages active:                            234567.
Pages inactive:                          223456.
Pages speculative:                         3456.
Pages throttled:                              0.
Pages wired down:                         98765.
Pages purgeable:                           1234.
"Translation faults":                  98765432.
"""


def test_meminfo_parser_reads_memavailable_in_bytes():
    assert parse_meminfo(MEMINFO) == 8192000 * 1024
    assert parse_meminfo("MemTotal: 1 kB\n") is None
    assert parse_meminfo("") is None


def test_vm_stat_parser_adds_free_inactive_and_speculative_pages():
    assert parse_vm_stat(VM_STAT) == (12345 + 223456 + 3456) * 16384
    assert parse_vm_stat(VM_STAT.replace("16384", "4096")) == (12345 + 223456 + 3456) * 4096


def test_vm_stat_parser_returns_none_without_a_page_size_or_required_counts():
    assert parse_vm_stat(VM_STAT.replace("page size of 16384 bytes", "unknown")) is None
    assert parse_vm_stat("Pages free: 5.\n") is None
    no_inactive = "\n".join(l for l in VM_STAT.splitlines() if "inactive" not in l)
    assert parse_vm_stat(no_inactive) is None
    no_spec = "\n".join(l for l in VM_STAT.splitlines() if "speculative" not in l)
    assert parse_vm_stat(no_spec) == (12345 + 223456) * 16384


def test_live_available_memory_is_a_positive_number_or_unknown():
    got = modelhost.available_memory_bytes()
    assert got is None or (isinstance(got, int) and got > 0)


# ---- settings ---------------------------------------------------------------------------------------------------
def test_defaults_match_the_measured_baseline():
    s = HostSettings.from_table({})
    assert (s.host_memory_limit_gb, s.max_context_tokens, s.enable_thinking, s.command, s.port) == \
        (9.0, 12000, False, (), 8091)
    assert (s.fixed_overhead_gb, s.kv_kb_per_token, s.min_context_tokens, s.reserve_gb) == (8.0, 81.0, 2048, 0.5)
    assert (s.idle_evict_seconds, s.evict_before_sandbox, s.startup_timeout_s, s.health_timeout_s) == \
        (60.0, True, 60.0, 2.0)
    assert (s.health_failures_to_restart, s.max_restarts, s.restart_window_s) == (3, 3, 600.0)


def test_settings_parse_every_key_and_fill_in_the_command_placeholders():
    s = HostSettings.from_table({"host_memory_limit_gb": 10, "max_context_tokens": 20000, "enable_thinking": True,
                                 "command": ["serve", "--port", "{port}", "--ctx={context}"], "port": 9000,
                                 "fixed_overhead_gb": 7.5, "kv_kb_per_token": 90, "min_context_tokens": 1000,
                                 "reserve_gb": 1, "idle_evict_seconds": 30, "evict_before_sandbox": False,
                                 "startup_timeout_s": 5, "health_timeout_s": 1, "health_failures_to_restart": 2,
                                 "max_restarts": 0, "restart_window_s": 60})
    assert s.argv(9000, 12345) == ["serve", "--port", "9000", "--ctx=12345"]
    assert s.host_memory_limit_gb == 10.0 and s.enable_thinking is True and s.max_restarts == 0
    assert HostSettings.from_table({"command": []}).command == ()


BAD = [
    ("host_memory_limit_gb", float("nan")), ("host_memory_limit_gb", float("inf")), ("host_memory_limit_gb", True),
    ("host_memory_limit_gb", 0), ("host_memory_limit_gb", -9.0), ("host_memory_limit_gb", "9"),
    ("fixed_overhead_gb", float("nan")), ("fixed_overhead_gb", -1), ("kv_kb_per_token", 0),
    ("kv_kb_per_token", float("inf")), ("reserve_gb", -0.5), ("reserve_gb", True),
    ("idle_evict_seconds", 0), ("idle_evict_seconds", float("nan")), ("startup_timeout_s", -1),
    ("health_timeout_s", float("inf")), ("health_timeout_s", False), ("restart_window_s", 0),
    ("max_context_tokens", 12000.5), ("max_context_tokens", True), ("max_context_tokens", 10),
    ("min_context_tokens", 0), ("min_context_tokens", False), ("port", 80), ("port", 70000), ("port", True),
    ("port", 8091.0), ("health_failures_to_restart", 0), ("health_failures_to_restart", True),
    ("max_restarts", -1), ("max_restarts", 1.5),
    ("enable_thinking", "false"), ("enable_thinking", 0), ("evict_before_sandbox", 1),
    ("command", "serve --port 1"), ("command", ["serve", ""]), ("command", ["serve", 5]), ("command", [None]),
    ("command", {"a": "b"}), ("command", ["a\0b"]),
]


@pytest.mark.parametrize("key, value", BAD, ids=[f"{k}={v!r}" for k, v in BAD])
def test_a_bad_setting_is_refused_and_the_message_names_the_key(key, value):
    with pytest.raises(ValueError, match=key):
        HostSettings.from_table({key: value})


def test_unknown_keys_and_inconsistent_limits_are_refused():
    with pytest.raises(ValueError, match="hostt_memory_limit_gb"):
        HostSettings.from_table({"hostt_memory_limit_gb": 9})
    with pytest.raises(ValueError, match="min_context_tokens"):
        HostSettings.from_table({"min_context_tokens": 5000, "max_context_tokens": 4000})
    with pytest.raises(ValueError, match="local_model"):
        HostSettings.from_table([1])


def test_config_load_fails_at_load_time_on_a_bad_local_model_table(tmp_path):
    p = tmp_path / "hera.toml"
    for body, key in (("host_memory_limit_gb = nan", "host_memory_limit_gb"), ("port = true", "port"),
                      ("enable_thinking = 'no'", "enable_thinking"), ("mystery = 1", "mystery"),
                      ("command = 'serve'", "command"), ("kv_kb_per_token = inf", "kv_kb_per_token")):
        p.write_text("[local_model]\n" + body + "\n")
        with pytest.raises(ValueError, match=key):
            config.load(p)
    p.write_text("[local_model]\nport = 9100\ncommand = ['serve', '{port}']\n")
    cfg = config.load(p)
    assert cfg.local_model["port"] == 9100                     # the dict stays for compatibility
    assert HostSettings.from_config(cfg).command == ("serve", "{port}")


def test_the_example_config_has_no_host_command_and_parses():
    p = Path(__file__).resolve().parents[1] / "config" / "hera.example.toml"
    s = HostSettings.from_config(config.load(p))
    assert s.command == () and s.host_memory_limit_gb == 9.0 and s.enable_thinking is False


# ---- the scrubbed environment ---------------------------------------------------------------------------------
def test_scrubbed_env_keeps_only_a_few_variables_and_adds_port_and_context():
    env = scrubbed_env({"PATH": "/bin", "HOME": "/h", "LANG": "C", "ANTHROPIC_API_KEY": "x", "FOO": "y",
                        "TMPDIR": "/t", "SYSTEMROOT": "C:"}, 8091, 12000)
    assert env == {"PATH": "/bin", "HOME": "/h", "LANG": "C", "TMPDIR": "/t", "SYSTEMROOT": "C:",
                   "HERA_MODELHOST_PORT": "8091", "HERA_MODELHOST_CONTEXT": "12000"}
    assert scrubbed_env({}, 1024, 5) == {"HERA_MODELHOST_PORT": "1024", "HERA_MODELHOST_CONTEXT": "5"}


def test_the_child_sees_no_keys_and_nothing_else_from_the_parent_environment(rig, monkeypatch):
    r = rig()
    for name in ("ANTHROPIC_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY", "HERA_TEST_ARBITRARY_VARIABLE"):
        monkeypatch.setenv(name, "planted-" + name.lower())
    with r.host.lease():
        pass
    env_line = records(r.record)[0]
    names = set(env_line["env"])
    assert not names & {"ANTHROPIC_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY", "HERA_TEST_ARBITRARY_VARIABLE"}
    assert {"HERA_MODELHOST_PORT", "HERA_MODELHOST_CONTEXT"} <= names
    # Python itself may add a locale or macOS variable; nothing else may appear
    assert names <= set(modelhost.ENV_KEEP) | {"HERA_MODELHOST_PORT", "HERA_MODELHOST_CONTEXT", "LC_CTYPE",
                                               "__CF_USER_TEXT_ENCODING"}
    assert env_line["context"] == "12000"                       # {context} was filled in
    assert "planted-" not in (r.home / "logs" / "modelhost.log").read_text()


# ---- loading, generating, evicting: the done-when criterion ------------------------------------------------------
def test_twenty_load_generate_evict_cycles_through_the_real_provider_path(rig, conn):
    r = rig()
    provider = r.provider(conn)
    for i in range(20):
        reply = ask(provider, f"cycle {i}")
        assert reply.text == "fake reply"
        assert r.host.status()["state"] == "ready"
        r.host.evict("cycle done")
        proc = r.spawner.procs[-1]
        assert proc.poll() is not None and group_gone(proc.pid)
        assert r.host.status()["state"] == "evicted" and r.host.status()["pid"] is None
    assert len(r.spawner.procs) == 20
    assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 20
    assert conn.execute("SELECT COUNT(*) FROM transmissions WHERE decision='sent'").fetchone()[0] == 20
    assert conn.execute("SELECT SUM(usd) FROM model_calls").fetchone()[0] == 0
    bodies = [q["body"] for q in requests_seen(r.record)]
    assert len(bodies) == 20
    assert all(b["chat_template_kwargs"] == {"enable_thinking": False} for b in bodies)
    assert r.kinds().count("evict") == 20 and r.kinds().count("ready") == 20


def test_no_grandchild_survives_an_eviction(rig, conn):
    r = rig(mode="spawn_child")
    provider = r.provider(conn)
    for _ in range(3):
        ask(provider)
        start = [x for x in records(r.record) if "grandchild" in x][-1]
        pgid = start["pid"]
        assert pgid == r.host.status()["pid"]
        os.killpg(pgid, 0)                                      # the group is alive
        r.host.evict("test")
        assert r.spawner.procs[-1].poll() is not None
        assert group_gone(pgid), "a grandchild outlived the eviction"


def test_a_child_that_ignores_sigterm_is_killed_after_the_grace_period(rig, monkeypatch):
    monkeypatch.setattr(modelhost, "TERM_GRACE_S", 0.5)
    r = rig(extra_args=["--ignore-term"])
    with r.host.lease():
        pass
    t0 = time.monotonic()
    r.host.evict("test")
    assert time.monotonic() - t0 >= 0.4
    assert r.spawner.procs[0].poll() is not None and group_gone(r.spawner.procs[0].pid)


def test_evict_is_idempotent_and_status_has_the_documented_fields(rig):
    r = rig(memory=lambda: 12_345_000_000)
    r.host.evict("nothing loaded")
    r.host.evict("still nothing")
    st = r.host.status()
    assert set(st) == {"state", "pid", "port", "context_cap", "leased", "restarts_in_window", "last_error",
                       "available_memory_bytes", "port_in_use"}
    assert st["state"] == "evicted" and st["pid"] is None and st["port"] == r.port
    assert st["available_memory_bytes"] == 12_345_000_000 and st["restarts_in_window"] == 0
    assert st["port_in_use"] is False and st["context_cap"] == 0
    with r.host.lease() as lease:
        st = r.host.status()
        assert st["state"] == "ready" and st["leased"] is True and st["pid"] == r.spawner.procs[0].pid
        assert st["context_cap"] == lease.context_cap == 12000
    assert r.kinds() == ["start", "ready"]                      # nothing logged for the no-op evictions


def test_a_lease_reuses_a_healthy_child(rig):
    r = rig()
    with r.host.lease() as a:
        pass
    with r.host.lease() as b:
        pass
    assert len(r.spawner.procs) == 1 and a == b == Lease(loopback_url(r.port), 12000)


# ---- crash, hang, restart budget ---------------------------------------------------------------------------------
def test_a_crashed_child_is_noticed_and_restarted_on_the_next_lease(rig, conn):
    r = rig(mode="crash_after:1")
    provider = r.provider(conn)
    assert ask(provider).text == "fake reply"
    r.spawner.procs[0].wait(timeout=10)
    assert ask(provider).text == "fake reply"                   # the second lease started a new child
    assert len(r.spawner.procs) == 2
    assert "crash" in r.kinds() and "restart" in r.kinds()
    crash = next(e for e in r.events() if e["event"] == "crash")
    assert crash["detail"]["exit_code"] == 3
    assert r.host.status()["restarts_in_window"] == 1


def test_a_hung_child_is_killed_after_n_failed_health_checks_and_restarted(rig, conn):
    r = rig(mode="hang_after:1", health_timeout_s=0.3, health_failures_to_restart=2)
    provider = r.provider(conn)
    assert ask(provider).text == "fake reply"
    first = r.spawner.procs[0]
    assert first.poll() is None                                 # alive, but it no longer answers
    assert ask(provider).text == "fake reply"
    assert first.poll() is not None and group_gone(first.pid)
    assert len(r.spawner.procs) == 2
    kinds = r.kinds()
    assert kinds.index("hang") < kinds.index("restart")


def test_restarts_beyond_the_budget_fail_the_host_until_a_person_resets_it(rig, conn):
    r = rig(mode="crash_after:1", max_restarts=1)
    provider = r.provider(conn)
    ask(provider)
    r.spawner.procs[0].wait(timeout=10)
    ask(provider)                                               # restart 1 of 1: allowed
    r.spawner.procs[1].wait(timeout=10)
    with pytest.raises(ProviderError):                          # the second restart is over budget
        ask(provider)
    assert r.host.status()["state"] == "failed" and "reset" in r.host.status()["last_error"]
    assert len(r.spawner.procs) == 2 and "failed" in r.kinds()
    calls = conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
    with pytest.raises(ProviderError):                          # and it stays failed, nothing is spawned
        ask(provider)
    assert len(r.spawner.procs) == 2 and conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == calls
    with pytest.raises(HostUnavailable):
        with r.host.lease():
            pass
    r.host.reset()
    assert r.host.status()["state"] == "evicted" and r.host.status()["restarts_in_window"] == 0
    assert ask(provider).text == "fake reply" and len(r.spawner.procs) == 3
    assert "reset" in r.kinds()


def test_the_restart_budget_only_counts_restarts_inside_the_window(rig, conn):
    clock = Clock()
    r = rig(mode="crash_after:1", max_restarts=1, restart_window_s=100, clock=clock)
    provider = r.provider(conn)
    ask(provider)
    r.spawner.procs[0].wait(timeout=10)
    ask(provider)                                               # restart at t=1000
    r.spawner.procs[1].wait(timeout=10)
    clock.t += 101                                              # the first restart left the window
    ask(provider)
    assert r.host.status()["state"] == "ready" and len(r.spawner.procs) == 3


def test_the_failed_state_survives_a_new_process_until_reset(rig):
    r = rig(mode="fail_start", max_restarts=0)
    with pytest.raises(HostStartError):
        with r.host.lease():
            pass
    with pytest.raises(HostUnavailable):
        with r.host.lease():
            pass
    again = ModelHost(r.settings, home=r.home, memory=lambda: GB20, spawn=r.spawner)    # "a new hera run"
    assert again.status()["state"] == "failed"
    with pytest.raises(HostUnavailable):
        with again.lease():
            pass
    again.reset()
    assert ModelHost(r.settings, home=r.home).status()["state"] == "evicted"


# ---- start failures --------------------------------------------------------------------------------------------
def test_a_child_that_exits_during_startup_is_a_start_error_and_is_reaped(rig):
    r = rig(mode="fail_start")
    with pytest.raises(HostStartError, match="exited during startup"):
        with r.host.lease():
            pass
    proc = r.spawner.procs[0]
    assert proc.poll() == 1 and group_gone(proc.pid)
    assert r.host.status()["state"] == "evicted" and "exited" in r.host.status()["last_error"]
    assert "failing on purpose" in (r.home / "logs" / "modelhost.log").read_text()   # output goes to the log
    assert "start_failed" in r.kinds()


def test_repeated_start_failures_end_as_failed_not_as_an_endless_loop(rig):
    r = rig(mode="fail_start", max_restarts=1)
    with pytest.raises(HostStartError):
        with r.host.lease():
            pass
    with pytest.raises(HostStartError):
        with r.host.lease():
            pass
    with pytest.raises(HostUnavailable):
        with r.host.lease():
            pass
    assert len(r.spawner.procs) == 2 and r.host.status()["state"] == "failed"


def test_a_child_that_never_listens_is_killed_at_the_startup_timeout(rig):
    r = rig(mode="slow_start", startup_timeout_s=1.0, health_timeout_s=0.3)
    t0 = time.monotonic()
    with pytest.raises(HostStartError, match="did not become healthy"):
        with r.host.lease():
            pass
    assert time.monotonic() - t0 < 15
    proc = r.spawner.procs[0]
    assert proc.poll() is not None and group_gone(proc.pid)
    assert r.host.status()["state"] == "evicted"


def test_a_missing_executable_is_a_start_error(rig):
    r = rig()
    r.host.settings = HostSettings(**{**r.settings.__dict__, "command": ("/nonexistent/definitely-not-here",)})
    with pytest.raises(HostStartError, match="could not start"):
        with r.host.lease():
            pass
    assert r.host.status()["state"] == "evicted"


def test_a_port_that_already_accepts_connections_is_refused_and_nothing_is_spawned(rig):
    r = rig()
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", r.port))
    squatter.listen(5)
    try:
        with pytest.raises(HostStartError, match="already listening"):
            with r.host.lease():
                pass
        assert r.spawner.procs == [] and not r.record.exists()
        assert r.host.status()["port_in_use"] is True
        assert "port_in_use" in r.kinds()
    finally:
        squatter.close()
    with r.host.lease():                                        # once the port is free it works
        pass
    assert len(r.spawner.procs) == 1


def test_no_room_in_memory_means_no_process_and_a_provider_error(rig, conn):
    r = rig(memory=lambda: 8_600_000_000)
    provider = r.provider(conn)
    with pytest.raises(ProviderError):
        ask(provider)
    assert r.spawner.procs == []
    assert r.host.status()["state"] == "no_room" and "no_room" in r.kinds()
    with pytest.raises(HostUnavailable):
        with r.host.lease():
            pass
    assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
    r.host._memory = lambda: GB20                               # memory came back: the next lease loads
    assert ask(provider).text == "fake reply" and r.host.status()["state"] == "ready"


def test_a_memory_probe_that_raises_counts_as_unknown(rig):
    def broken():
        raise RuntimeError("no vm_stat")
    r = rig(memory=broken)
    with r.host.lease() as lease:
        assert lease.context_cap == 12000                       # unknown free memory: the limit applies
    assert r.host.status()["available_memory_bytes"] is None


# ---- the provider rules ------------------------------------------------------------------------------------------
def test_thinking_is_off_by_default_on_when_configured_and_the_caller_can_override(rig, conn):
    r = rig()
    provider = r.provider(conn)
    ask(provider)
    ask(provider, chat_template_kwargs={"enable_thinking": True, "custom": 1})
    r.host.settings = HostSettings(**{**r.settings.__dict__, "enable_thinking": True})
    ask(provider)
    got = [q["body"]["chat_template_kwargs"] for q in requests_seen(r.record)]
    assert got == [{"enable_thinking": False}, {"enable_thinking": True, "custom": 1}, {"enable_thinking": True}]


def test_a_prompt_over_the_lease_cap_is_refused_with_nothing_sent_or_recorded(rig, conn):
    r = rig(memory=lambda: 8_800_000_000)                       # 8.3 GB effective: 3703 tokens
    provider = r.provider(conn)
    assert ask(provider).text == "fake reply"
    sent = len(requests_seen(r.record))
    spent = conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
    logged = conn.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0]
    with pytest.raises(ProviderError, match="does not fit"):
        ask(provider, "x" * 12_000)                             # about 4000 estimated tokens
    assert len(requests_seen(r.record)) == sent
    assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == spent
    assert conn.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0] == logged
    assert ask(provider, "y" * 9_000).text == "fake reply"      # about 3000 estimated tokens: fits


def test_a_prompt_over_the_configured_maximum_is_refused_without_loading_the_model(rig, conn):
    r = rig(max_context_tokens=2048, min_context_tokens=1024)
    provider = r.provider(conn)
    with pytest.raises(ProviderError, match="larger than"):
        ask(provider, "x" * 7_000)
    assert r.spawner.procs == [] and conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0


def test_every_host_failure_reaches_the_orchestrator_as_a_provider_error(rig, conn):
    r = rig()
    provider = r.provider(conn)
    with r.host.lease():
        for _ in range(2):
            with pytest.raises(ProviderError):                  # HostBusy: one generation at a time
                ask(provider)
    fake = rig(mode="fail_start")
    with pytest.raises(ProviderError):                          # HostStartError
        ask(fake.provider(conn))
    full = rig(memory=lambda: 1)
    with pytest.raises(ProviderError):                          # HostUnavailable
        ask(full.provider(conn))


def test_a_host_can_only_be_attached_to_a_local_tier_provider(conn):
    gw = Gateway(conn, LabelStore(conn), GrantStore(conn),
                 {"x": Destination("x", "http://127.0.0.1:9/v1/chat/completions", local=False)})
    with pytest.raises(ValueError):
        OpenAICompatProvider("x", "cheap", "m", gw, Ledger(conn, Budget(), PRICES), host=object())


def test_neither_log_ever_holds_request_or_response_content(rig, conn):
    r = rig()
    provider = r.provider(conn)
    ask(provider, "PRIVATE-REPOSITORY-TEXT-8c1f")
    r.host.evict("done")
    text = (r.home / "logs" / "modelhost.log").read_text() + (r.home / "logs" / "modelhost.jsonl").read_text()
    assert "PRIVATE-REPOSITORY-TEXT-8c1f" not in text and "fake reply" not in text
    for e in r.events():
        assert set(e) == {"time", "event", "pid", "detail"}


# ---- leases, idle eviction, sandbox ----------------------------------------------------------------------------------
def test_a_lease_cannot_be_evicted_reset_or_shared(rig):
    r = rig()
    with r.host.lease():
        with pytest.raises(HostBusy):
            r.host.evict("no")
        with pytest.raises(HostBusy):
            r.host.before_sandbox_job()
        with pytest.raises(HostBusy):
            r.host.reset()
        with pytest.raises(HostBusy):
            with r.host.lease():
                pass
        r.clock.t += 10_000
        r.host.tick()                                           # idle for ages, but leased: untouched
        assert r.spawner.procs[0].poll() is None
    r.host.evict("now it is free")
    assert r.spawner.procs[0].poll() is not None
    with r.host.lease():                                        # and the lease flag was released
        pass


def test_a_lease_is_released_when_the_block_raises(rig):
    r = rig()
    with pytest.raises(RuntimeError):
        with r.host.lease():
            raise RuntimeError("boom")
    r.host.evict("fine")


def test_the_model_is_evicted_after_sixty_idle_seconds_by_the_injected_clock(rig):
    r = rig()
    with r.host.lease():
        pass
    r.clock.t += 59
    r.host.tick()
    assert r.host.status()["state"] == "ready" and r.spawner.procs[0].poll() is None
    r.clock.t += 2
    r.host.tick()
    assert r.host.status()["state"] == "evicted" and r.spawner.procs[0].poll() is not None
    idle = [e for e in r.events() if e["event"] == "evict"]
    assert idle[-1]["detail"]["reason"] == "idle"
    r.clock.t += 500
    r.host.tick()                                               # nothing loaded: nothing happens
    with r.host.lease():
        pass
    r.clock.t += 50
    with r.host.lease():                                        # use resets the idle timer
        pass
    r.clock.t += 50
    r.host.tick()
    assert r.host.status()["state"] == "ready"


def test_tick_records_a_child_that_exited_on_its_own_and_does_not_restart_it(rig, conn):
    r = rig(mode="crash_after:1")
    provider = r.provider(conn)
    ask(provider)
    r.spawner.procs[0].wait(timeout=10)
    r.host.tick()
    assert "crash" in r.kinds() and r.host.status()["state"] == "evicted" and r.host.status()["pid"] is None
    assert len(r.spawner.procs) == 1                            # not restarted until the next lease
    assert "exited" in r.host.status()["last_error"]
    ask(provider)
    assert len(r.spawner.procs) == 2 and "restart" in r.kinds()


def test_before_sandbox_job_evicts_unless_switched_off(rig):
    r = rig()
    with r.host.lease():
        pass
    r.host.before_sandbox_job()
    assert r.spawner.procs[0].poll() is not None and r.host.status()["state"] == "evicted"
    r.host.before_sandbox_job()                                 # nothing loaded: fine
    keep = rig(evict_before_sandbox=False)
    with keep.host.lease():
        pass
    keep.host.before_sandbox_job()
    assert keep.host.status()["state"] == "ready"


def test_the_sandbox_hook_evicts_the_model_before_each_job_and_never_blocks_a_job(rig, tmp_path):
    r = rig()
    job = lambda: sandbox.run([sys.executable, "-c", "pass"], cwd=tmp_path, mechanism="none",
                              allow_unsandboxed=True)
    with sandbox.before_each_job(modelhost.sandbox_hook([r.host])):
        with r.host.lease():
            pass
        assert r.host.status()["state"] == "ready"
        assert job().returncode == 0
        assert r.host.status()["state"] == "evicted" and r.spawner.procs[0].poll() is not None
        with r.host.lease():
            assert job().returncode == 0                        # leased: the job still runs
            assert r.host.status()["state"] == "ready"
    assert "evict_skipped" in r.kinds()
    with r.host.lease():
        pass
    assert job().returncode == 0 and r.host.status()["state"] == "ready"     # the hook is gone again


def test_a_hook_that_raises_cannot_fail_a_sandbox_job(tmp_path):
    def bad():
        raise RuntimeError("boom")
    with sandbox.before_each_job(bad):
        assert sandbox.run([sys.executable, "-c", "pass"], cwd=tmp_path, mechanism="none",
                           allow_unsandboxed=True).returncode == 0


# ---- the orchestrator ---------------------------------------------------------------------------------------------
class StubHost:
    """Looks like a ModelHost to the provider and the orchestrator; records sandbox evictions."""
    def __init__(self):
        self.settings = HostSettings()
        self.sandbox_evictions = 0
        self.leases = 0

    def lease(self):
        import contextlib

        @contextlib.contextmanager
        def cm():
            self.leases += 1
            yield Lease("http://127.0.0.1:1", 10 ** 6)
        return cm()

    def before_sandbox_job(self):
        self.sandbox_evictions += 1

    def _event(self, *a, **k):
        pass


def _task(conn, toy, providers):
    cs = freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES))
    spec = TaskSpec(task_id="toy", repo=toy.repo, base_commit=toy.base, checkset=cs,
                    objective="Add parse_amount to calc.py.", repo_id="toy", allow_unsandboxed=True)
    return run_task(conn, spec, providers=providers, policy=Policy(repos={"toy": (None, RepoClass.PUBLIC)}))


def test_a_run_evicts_every_attached_host_before_each_sandbox_job(conn, toy, hera_home):
    script = [step("w", ("write_file", {"path": "calc.py", "content": CORRECT_CALC})),
              step("s", ("submit", {"summary": "done", "declared_scope": ["calc.py"]}))]
    llm = ScriptedLLM("openai", script)
    try:
        gw = Gateway(conn, LabelStore(conn), GrantStore(conn),
                     {"local-llm": Destination("local-llm", llm.url, {}, local=True)}, timeout=10)
        provider = OpenAICompatProvider("local-llm", "local", "mock", gw, Ledger(conn, Budget(), LLM_PRICES))
        stub = provider.host = StubHost()
        out = _task(conn, toy, {"local-llm": provider})
        assert out.status == "PASSED"
        assert stub.leases == 2 and stub.sandbox_evictions >= 3     # baseline, visible checks, verification
        assert all(b["chat_template_kwargs"] == {"enable_thinking": False} for b in llm.requests)
    finally:
        llm.close()
    assert sandbox._JOB_HOOKS == []                                 # the hook is removed when the run ends


def test_a_run_whose_only_provider_has_a_failing_host_ends_as_provider_error(rig, conn, toy, hera_home):
    r = rig(mode="fail_start", max_restarts=0)
    out = _task(conn, toy, {"local-llm": r.provider(conn)})         # no exception reaches the caller
    assert out.status == "PROVIDER_ERROR"
    assert r.host.status()["state"] == "failed" and sandbox._JOB_HOOKS == []
    assert all(p.poll() is not None for p in r.spawner.procs)


# ---- registry and CLI ---------------------------------------------------------------------------------------------
def cfg_with(local_model, **specs) -> Config:
    return Config(Budget(), PRICES, "", {}, local_model, {n: ProviderSpec(name=n, **kw) for n, kw in specs.items()})


def local_spec(url, **kw):
    return dict(kind="openai_compat", url=url, model="m", local=True, tier="local", **kw)


def test_the_host_attaches_only_to_local_tier_providers_on_exactly_its_address(conn):
    gw = Gateway(conn, LabelStore(conn), GrantStore(conn), {})
    ledger = Ledger(conn, Budget(), PRICES)
    cfg = cfg_with({"command": ["serve"], "port": 9100},
                   a=local_spec("http://127.0.0.1:9100/v1/chat/completions"),
                   other_port=local_spec("http://127.0.0.1:9101/v1/chat/completions"),
                   by_name=local_spec("http://localhost:9100/v1/chat/completions"),
                   other_ip=local_spec("http://127.0.0.2:9100/v1/chat/completions"))
    got = build_providers(cfg, gw, ledger, {})
    assert got.host is not None and got["a"].host is got.host
    assert all(got[n].host is None for n in ("other_port", "by_name", "other_ip"))
    assert got.absent == {} and host_attachments(cfg) == ["a"]


def test_without_a_command_nothing_changes_and_with_a_command_but_no_match_it_is_reported(conn):
    gw = Gateway(conn, LabelStore(conn), GrantStore(conn), {})
    ledger = Ledger(conn, Budget(), PRICES)
    spec = local_spec("http://127.0.0.1:8091/v1/chat/completions")
    plain = build_providers(cfg_with({}, a=spec), gw, ledger, {})
    assert plain.host is None and plain["a"].host is None and plain.absent == {}
    gw2 = Gateway(conn, LabelStore(conn), GrantStore(conn), {})
    lonely = build_providers(cfg_with({"command": ["serve"], "port": 9100}, a=spec), gw2, ledger, {})
    assert "would never be used" in lonely.absent[HOST_ABSENT_KEY] and lonely["a"].host is None


def test_only_the_openai_compatible_kind_carries_a_host(conn):
    gw = Gateway(conn, LabelStore(conn), GrantStore(conn), {})
    got = build_providers(cfg_with({"command": ["serve"], "port": 8091},
                                   a=local_spec("http://127.0.0.1:8091/v1/chat/completions"),
                                   b=dict(kind="anthropic", url="http://127.0.0.1:8091/v1/messages", model="m",
                                          local=True, tier="local")), gw, Ledger(conn, Budget(), PRICES), {})
    assert got["a"].host is got.host and getattr(got["b"], "host", None) is None


@pytest.fixture
def cli_home(hera_home, tmp_path, monkeypatch):
    monkeypatch.setattr(modelhost, "available_memory_bytes", lambda: GB20)
    port = free_port()
    record = tmp_path / "cli-record.jsonl"

    def write(mode="normal", extra="", providers=True):
        cmd = json.dumps([sys.executable, "-I", str(FAKE), "--port", "{port}", "--mode", mode, "--record",
                          str(record)])
        body = (f"[local_model]\ncommand = {cmd}\nport = {port}\nstartup_timeout_s = 20\n{extra}\n")
        if providers:
            body += (f"[providers.local-llm]\nkind = 'openai_compat'\nurl = 'http://127.0.0.1:{port}/v1/chat/completions'\n"
                     "model = 'fake-9b'\nlocal = true\ntier = 'local'\n")
        (hera_home / "hera.toml").write_text(body)
        return port
    return write


def test_cli_host_needs_a_configured_command(hera_home, capsys):
    (hera_home / "hera.toml").write_text("[local_model]\nenable_thinking = false\n")
    for cmd in ("status", "start", "stop", "reset"):
        assert cli.main(["host", cmd]) == 2
    assert "no model host is configured" in capsys.readouterr().err


def test_cli_host_status_start_stop_and_reset(cli_home, capsys):
    port = cli_home()
    assert cli.main(["host", "status"]) == 0
    out = capsys.readouterr().out
    assert "state: evicted" in out and f"port: {port}" in out and "context_cap: 0" in out
    assert cli.main(["host", "start"]) == 0
    out = capsys.readouterr().out
    assert f"url: http://127.0.0.1:{port}" in out and "context cap: 12000 tokens" in out
    assert "evicted again" in out
    assert group_gone_after_cli(port)
    assert cli.main(["host", "stop"]) == 0 and "stopped" in capsys.readouterr().out
    assert cli.main(["host", "reset"]) == 0 and "reset" in capsys.readouterr().out
    assert cli.main(["host", "status"]) == 0 and "state: evicted" in capsys.readouterr().out


def group_gone_after_cli(port) -> bool:
    """After `hera host start` returned, nothing may still listen on the port."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        except OSError:
            return True
        time.sleep(0.05)
    return False


def test_cli_host_start_reports_a_failure_with_exit_3_and_a_squatted_port_is_never_used(cli_home, capsys):
    cli_home(mode="fail_start")
    assert cli.main(["host", "start"]) == 3
    assert "exited during startup" in capsys.readouterr().err
    port = cli_home()
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", port))
    squatter.listen(5)
    try:
        assert cli.main(["host", "start"]) == 3
        assert "already listening" in capsys.readouterr().err
        assert cli.main(["host", "stop"]) == 0
        assert "did not start" in capsys.readouterr().out
    finally:
        squatter.close()


def test_cli_providers_list_shows_what_the_host_attaches_to(cli_home, capsys, hera_home):
    cli_home()
    assert cli.main(["init"]) == 0
    capsys.readouterr()
    assert cli.main(["providers", "list"]) == 0
    out = capsys.readouterr().out
    assert "local-llm" in out and "model host: supervised on 127.0.0.1:" in out and "attached to: local-llm" in out
    cli_home(providers=False)
    assert cli.main(["providers", "list"]) == 0
    out = capsys.readouterr().out
    assert "NOTHING" in out and "no providers configured" in out


def test_cli_providers_check_loads_the_model_and_evicts_it_again(cli_home, capsys, hera_home):
    cli_home()
    assert cli.main(["init"]) == 0
    assert cli.main(["providers", "check", "local-llm"]) == 0
    assert "reply: fake reply" in capsys.readouterr().out
    assert cli.main(["host", "status"]) == 0
    assert "port_in_use: False" in capsys.readouterr().out      # the child is gone when the command ends


# ---- the network rule ------------------------------------------------------------------------------------------------
def test_every_url_the_module_builds_is_on_127_0_0_1():
    for port in (1024, 8091, 65535):
        url = loopback_url(port, "/v1/models")
        assert is_loopback_url(url) and url == f"http://127.0.0.1:{port}/v1/models"
    tree = ast.parse(Path(modelhost.__file__).read_text(encoding="utf-8"))
    inside_fstring = {id(c) for j in ast.walk(tree) if isinstance(j, ast.JoinedStr) for c in ast.walk(j)}
    urls = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value.startswith(("http://", "https://")) and id(n) not in inside_fstring]
    assert urls == []                                           # the only builder is an f-string, checked next
    fstrings = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
                and "://" in "".join(v.value for v in n.values if isinstance(v, ast.Constant))]
    assert len(fstrings) == 1 and "LOOPBACK" in ast.dump(fstrings[0])
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        name = getattr(call.func, "attr", getattr(call.func, "id", ""))
        if name in ("HTTPConnection", "create_connection"):
            first = call.args[0]
            assert (isinstance(first, ast.Name) and first.id == "LOOPBACK") or \
                   (isinstance(first, ast.Tuple) and isinstance(first.elts[0], ast.Name)
                    and first.elts[0].id == "LOOPBACK")
