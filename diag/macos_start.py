"""Throwaway CI diagnostic: why does the fake model server never accept connections on macOS when
started by the model host? Varies environment scrubbing and new session; times getfqdn."""
import os, socket, subprocess, sys, time
from pathlib import Path

FAKE = Path("tests/fake_model_server.py").resolve()
KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait(port, proc, timeout=30):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if proc.poll() is not None:
            return f"exited {proc.returncode}"
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return f"{time.monotonic() - t0:.2f}s"
        except OSError:
            time.sleep(0.05)
    return "NEVER (30s)"


def trial(name, env, new_session):
    port = free_port(); log_path = Path(f"/tmp/log-{name}.txt"); rec = f"/tmp/rec-{name}.jsonl"
    with open(log_path, "wb") as log:
        p = subprocess.Popen([sys.executable, "-I", str(FAKE), "--port", str(port), "--mode", "normal",
                              "--record", rec], stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                             start_new_session=new_session)
        r = wait(port, p)
        p.kill(); p.wait()
    print(f"server  {name:28s} listening: {r}   log tail: {log_path.read_text()[-400:]!r}", flush=True)


def fqdn(name, env, new_session):
    code = "import socket,time;t=time.monotonic();n=socket.getfqdn('127.0.0.1');print(f'{time.monotonic()-t:.2f}s', n)"
    try:
        out = subprocess.run([sys.executable, "-I", "-c", code], env=env, start_new_session=new_session,
                             capture_output=True, text=True, timeout=60)
        print(f"getfqdn {name:28s} {out.stdout.strip()} {out.stderr.strip()[-200:]}", flush=True)
    except subprocess.TimeoutExpired:
        print(f"getfqdn {name:28s} TIMEOUT 60s", flush=True)


print("python", sys.version, sys.executable, flush=True)
t = time.monotonic(); n = socket.getfqdn("127.0.0.1"); print(f"getfqdn in-process {time.monotonic()-t:.2f}s {n}", flush=True)
scrub = {k: os.environ[k] for k in KEEP if k in os.environ}
print("scrubbed env keys:", sorted(scrub), flush=True)
for name, env, ns in [("full_env/same_session", dict(os.environ), False),
                      ("full_env/new_session", dict(os.environ), True),
                      ("scrubbed/same_session", scrub, False),
                      ("scrubbed/new_session", scrub, True)]:
    fqdn(name, env, ns)
    trial(name.replace("/", "_"), env, ns)
