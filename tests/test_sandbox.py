"""Sandbox: scrubbed env, limits and timeouts everywhere; isolation where a mechanism exists."""
import os
import signal
import socket
import sys
import time
from pathlib import Path

import pytest

from hera import paths, sandbox
from hera.sandbox import Limits, SandboxUnavailable, detect_mechanism, run
from hera.workspace import TaskRepo

PY = sys.executable
NONE = dict(mechanism="none", allow_unsandboxed=True)
MECH = detect_mechanism()
needs_mech = pytest.mark.skipif(
    MECH == "none", reason="no sandbox mechanism on this machine (needs sandbox-exec on macOS or a working bwrap on Linux)")


def py(code, cwd, **kw):
    return run([PY, "-c", code], cwd=cwd, **kw)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


# ---- mechanism independent ---------------------------------------------------------------
def test_detect_returns_known_value():
    assert detect_mechanism() in ("sandbox-exec", "bwrap", "none")


def test_none_requires_opt_in(tmp_path):
    with pytest.raises(SandboxUnavailable):
        run([PY, "-c", "pass"], cwd=tmp_path, mechanism="none")
    with pytest.raises(ValueError):
        run([PY, "-c", "pass"], cwd=tmp_path, mechanism="docker")


def test_returncode_and_streams(tmp_path):
    r = py("import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)", tmp_path, **NONE)
    assert (r.returncode, r.stdout.strip(), r.stderr.strip()) == (7, b"out", b"err")
    assert not r.timed_out and not r.truncated and r.mechanism == "none" and r.wall_s >= 0


def test_env_scrub(tmp_path, monkeypatch):
    monkeypatch.setenv("HERA_FAKE_TOKEN", "t")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("MY_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_FOO", "a")
    monkeypatch.setenv("UNRELATED", "u")
    code = "import os,json; print(json.dumps(dict(os.environ))); print(os.path.isdir(os.environ['HOME']))"
    r = py(code, tmp_path, env={"EXPLICIT": "yes"}, **NONE)
    import json
    lines = r.stdout.decode().splitlines()
    env = json.loads(lines[0])
    assert lines[1] == "True"
    for k in ("HERA_FAKE_TOKEN", "AWS_SECRET_ACCESS_KEY", "MY_API_KEY", "ANTHROPIC_FOO", "UNRELATED"):
        assert k not in env
    assert env["HOME"] == env["TMPDIR"] and env["EXPLICIT"] == "yes"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1" and env["PYTHONHASHSEED"] == "0"
    assert env["LC_ALL"] == "C.UTF-8" and os.path.dirname(PY) in env["PATH"].split(os.pathsep)
    assert set(env) <= {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE",
                        "PYTHONHASHSEED", "EXPLICIT", "LC_CTYPE",
                        "__CF_USER_TEXT_ENCODING"}      # macOS adds this one itself
    assert not os.path.exists(env["HOME"])                   # private temp dir is removed afterwards


def test_timeout_kills_process_group(tmp_path):
    code = ("import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "open('gc.pid', 'w').write(str(p.pid))\n"
            "time.sleep(60)\n")
    t0 = time.monotonic()
    r = py(code, tmp_path, limits=Limits(wall_s=2), **NONE)
    assert r.timed_out and r.returncode is None and time.monotonic() - t0 < 20
    pid = int((tmp_path / "gc.pid").read_text())
    for _ in range(40):
        if not alive(pid):
            break
        time.sleep(0.1)
    assert not alive(pid)


def test_straggler_killed_after_normal_exit(tmp_path):
    code = ("import subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "open('gc.pid', 'w').write(str(p.pid))\n")
    r = py(code, tmp_path, limits=Limits(wall_s=20), **NONE)
    assert r.returncode == 0 and not r.timed_out
    pid = int((tmp_path / "gc.pid").read_text())
    for _ in range(40):
        if not alive(pid):
            break
        time.sleep(0.1)
    assert not alive(pid)


def test_output_cap(tmp_path):
    code = "import sys; sys.stdout.write('x' * 10_000_000); sys.stderr.write('e' * 500)"
    r = py(code, tmp_path, limits=Limits(max_output_bytes=100_000), **NONE)
    assert r.returncode == 0 and r.truncated
    assert len(r.stdout) == 100_000 and r.stderr == b"e" * 500


@pytest.mark.skipif(not hasattr(signal, "SIGXCPU"), reason="no RLIMIT_CPU on this platform")
def test_cpu_limit(tmp_path):
    t0 = time.monotonic()
    r = py("while True: pass", tmp_path, limits=Limits(cpu_s=1, wall_s=30), **NONE)
    assert not r.timed_out and r.returncode in (-signal.SIGXCPU, -signal.SIGKILL)
    assert time.monotonic() - t0 < 15


def test_file_size_limit(tmp_path):
    code = "open('big', 'wb').write(b'x' * 3_000_000)"
    r = py(code, tmp_path, limits=Limits(max_file_mb=1), **NONE)
    assert r.returncode != 0
    assert (tmp_path / "big").stat().st_size <= 1024 * 1024


def test_rejects_bad_arguments(tmp_path):
    with pytest.raises(ValueError):
        run(["-bad"], cwd=tmp_path, **NONE)
    with pytest.raises(ValueError):
        run([PY], cwd=tmp_path / "missing", **NONE)
    with pytest.raises(ValueError):
        run([PY], cwd=tmp_path, read_only=[tmp_path / "missing"], **NONE)
    with pytest.raises(ValueError):
        run([PY], cwd=tmp_path, env={"A=B": "x"}, **NONE)


# ---- generated policy (pure functions, checked on every platform) --------------------------
def test_sbpl_profile_shape(tmp_path):
    prof = sandbox._sbpl_profile(cwd="/w/cwd", tmp="/w/tmp", read_only=['/w/ro"x'], writable=["/w/wr"],
                                 deny_read=["/w/secret"])
    lines = prof.splitlines()
    assert "(deny network*)" in lines and "(deny file-write*)" in lines
    assert '(subpath "/w/ro\\"x")' in prof                      # quoting is escaped
    first_deny_read = next(i for i, l in enumerate(lines) if l.startswith("(deny file-read-data"))
    allow_read = next(i for i, l in enumerate(lines) if l.startswith("(allow file-read*"))
    last = lines[-1]
    assert first_deny_read < allow_read and last.startswith("(deny file-read-data") and '"/w/secret"' in last
    assert ".ssh" in last and ".aws" in last and ".gnupg" in last and ".netrc" in last
    allow_write = next(l for l in lines if l.startswith("(allow file-write*") and "/w/cwd" in l)
    assert '"/w/tmp"' in allow_write and '"/w/wr"' in allow_write and "/w/ro" not in allow_write
    with pytest.raises(ValueError):
        sandbox._sbpl_profile(cwd="/a\nb", tmp="/t", read_only=[], writable=[], deny_read=[])


def test_bwrap_command_shape(tmp_path):
    cwd, tmp, ro = str(tmp_path / "c"), str(tmp_path / "t"), str(tmp_path / "r")
    cmd = sandbox._bwrap_command(cwd=cwd, tmp=tmp, read_only=[ro], writable=[], deny_read=[],
                                 argv=["prog", "arg"])
    assert cmd[:4] == ["bwrap", "--unshare-all", "--die-with-parent", "--new-session"]
    assert "--share-net" not in cmd and cmd[-3:] == ["--", "prog", "arg"]
    def triple(flag, path):
        return any(cmd[i:i + 3] == [flag, path, path] for i in range(len(cmd)))
    assert triple("--ro-bind", ro) and triple("--bind", cwd) and triple("--bind", tmp)
    assert ["--proc", "/proc"] == cmd[cmd.index("--proc"):cmd.index("--proc") + 2]
    assert ["--tmpfs", "/tmp"] == cmd[cmd.index("--tmpfs"):cmd.index("--tmpfs") + 2]
    assert cmd.index("--tmpfs") < cmd.index(cwd)               # mounts under /tmp come after the tmpfs
    assert ["--chdir", cwd] == cmd[cmd.index("--chdir"):cmd.index("--chdir") + 2]


def test_explicit_read_only_lifts_builtin_deny(tmp_path, monkeypatch):
    home = tmp_path / "h"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    ssh = os.path.realpath(home / ".ssh")
    prof = sandbox._sbpl_profile(cwd="/c", tmp="/t", read_only=[ssh], writable=[], deny_read=[])
    assert f'(subpath "{ssh}")' not in prof.splitlines()[-1]
    prof2 = sandbox._sbpl_profile(cwd="/c", tmp="/t", read_only=[], writable=[], deny_read=[])
    assert f'(subpath "{ssh}")' in prof2.splitlines()[-1]


# ---- real isolation (skipped when no mechanism is available) ---------------------------------
@needs_mech
def test_mechanism_runs_python(tmp_path):
    r = py("print('hi')", tmp_path, mechanism=MECH)
    assert r.returncode == 0 and r.stdout.strip() == b"hi" and r.mechanism == MECH


@needs_mech
def test_no_tcp_connection(tmp_path):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    code = ("import socket\n"
            "s = socket.socket(); s.settimeout(3)\n"
            f"try:\n    s.connect(('127.0.0.1', {port})); print('CONNECTED')\n"
            "except OSError:\n    print('BLOCKED')\n")
    try:
        r = py(code, tmp_path, mechanism=MECH)
    finally:
        srv.close()
    assert r.stdout.strip() == b"BLOCKED", r.stderr


@needs_mech
def test_cannot_write_outside_cwd(tmp_path, monkeypatch):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    cwd, sibling = tmp_path / "cwd", tmp_path / "sibling"
    cwd.mkdir()
    sibling.mkdir()
    code = (f"for p in ({str(sibling / 'w.txt')!r}, {str(fake_home / 'w.txt')!r}, 'inside.txt'):\n"
            "    try:\n        open(p, 'w').write('x')\n    except OSError:\n        pass\n")
    r = py(code, cwd, mechanism=MECH)
    assert r.returncode == 0, r.stderr
    assert (cwd / "inside.txt").read_text() == "x"            # cwd is writable
    assert not (sibling / "w.txt").exists() and not (fake_home / "w.txt").exists()


@needs_mech
def test_deny_read_and_read_only(tmp_path):
    cwd = tmp_path / "cwd"
    secret = cwd / "secret"
    ro = tmp_path / "ro"
    for d in (cwd, secret, ro):
        d.mkdir()
    (secret / "cred.txt").write_text("hunter2")
    (cwd / "ok.txt").write_text("fine")
    (ro / "data.txt").write_text("readable")
    code = (
        "def rd(p):\n"
        "    try:\n        return open(p).read()\n    except OSError:\n        return 'DENIED'\n"
        f"print(rd({str(secret / 'cred.txt')!r}))\nprint(rd('ok.txt'))\nprint(rd({str(ro / 'data.txt')!r}))\n"
        f"try:\n    open({str(ro / 'new.txt')!r}, 'w').write('x')\n    print('WROTE')\nexcept OSError:\n    print('RO')\n")
    r = py(code, cwd, mechanism=MECH, read_only=[ro], deny_read=[secret])
    assert r.stdout.decode().split() == ["DENIED", "fine", "readable", "RO"], r.stderr


@needs_mech
def test_cannot_read_sibling_worktree(tmp_path):
    cwd, sib = tmp_path / "wt-a", tmp_path / "wt-b"
    cwd.mkdir()
    sib.mkdir()
    (sib / "secret.txt").write_text("other candidate")
    code = (f"try:\n    print(open({str(sib / 'secret.txt')!r}).read())\n"
            "except OSError:\n    print('DENIED')\n")
    r = py(code, cwd, mechanism=MECH)
    assert r.stdout.strip() == b"DENIED", r.stderr


@needs_mech
def test_cannot_read_hera_check_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERA_HOME", str(tmp_path / "hera"))
    paths.ensure_layout()
    (paths.checks_dir() / "frozen.py").write_text("hidden check")
    cwd = tmp_path / "wt"
    cwd.mkdir()
    code = (f"try:\n    print(open({str(paths.checks_dir() / 'frozen.py')!r}).read())\n"
            "except OSError:\n    print('DENIED')\n")
    assert py(code, cwd, mechanism=MECH).stdout.strip() == b"DENIED"


# ---- integration ---------------------------------------------------------------------------
def test_checkout_runs_in_sandbox(tmp_path, monkeypatch):
    import subprocess
    from hera.workspace import _git_env
    monkeypatch.setenv("HERA_HOME", str(tmp_path / "hera"))
    paths.ensure_layout()
    src = tmp_path / "src"
    src.mkdir()
    env = _git_env()
    base = ["git", "-c", "user.name=T", "-c", "user.email=t@e.invalid"]
    subprocess.run([*base, "init", "-q", "-b", "main"], cwd=src, env=env, check=True)
    (src / "seed.txt").write_text("seed\n")
    subprocess.run([*base, "add", "-A"], cwd=src, env=env, check=True)
    subprocess.run([*base, "commit", "-q", "-m", "i"], cwd=src, env=env, check=True)
    repo = TaskRepo.create("int1", src)
    cand = repo.new_candidate("c1")
    (cand.path / "answer.txt").write_text("42\n")
    commit = cand.commit_all("answer")
    checkout = repo.verification_checkout(commit)
    r = run([PY, "-c", "import sys; sys.exit(0 if open('answer.txt').read() == '42\\n' else 3)"],
            cwd=checkout, allow_unsandboxed=True)
    assert r.returncode == 0, r.stderr
    assert r.mechanism == detect_mechanism()
    repo.cleanup_verification(checkout)
