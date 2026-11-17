"""A fake OpenAI-compatible model server for the model host tests. Standard library only, invented
replies only. Run as:

    python -I fake_model_server.py --port N --mode MODE --record FILE [--context N] [--delay S]
                                   [--ignore-term]

Serves GET /v1/models and POST /v1/chat/completions on 127.0.0.1. Modes:

    normal           answers everything
    slow_start       sleeps --delay seconds (default 30) before listening
    fail_start       exits with status 1 at once
    crash_after:N    answers N completions, then exits (status 3) right after the Nth reply
    hang_after:N     answers N completions, then never answers anything again (stays alive)
    spawn_child      starts a grandchild (same process group) that sleeps, to show that the
                     whole group is killed

--record FILE gets JSON lines: one at startup with the sorted NAMES of the environment variables
(never values), the pid and the --context value, one for a grandchild, and one per request with the
path and the parsed body.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_lock = threading.Lock()
_state = {"completions": 0, "hung": False}


def record(path, obj):
    line = (json.dumps(obj, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def make_handler(args, mode, limit):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, status, obj):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def _maybe_hang(self):
            if _state["hung"]:
                time.sleep(3600)

        def do_GET(self):
            self._maybe_hang()
            if self.path == "/v1/models":
                self._reply(200, {"object": "list", "data": [{"id": "fake-9b", "object": "model"}]})
            else:
                self._reply(404, {"error": {"message": "not found"}})

        def do_POST(self):
            self._maybe_hang()
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                body = json.loads(raw)
            except ValueError:
                body = {"unparsed": True}
            record(args.record, {"path": self.path, "body": body})
            if self.path != "/v1/chat/completions":
                self._reply(404, {"error": {"message": "not found"}})
                return
            self._reply(200, {"id": "fake-1", "object": "chat.completion", "model": "fake-9b",
                              "choices": [{"index": 0, "finish_reason": "stop",
                                           "message": {"role": "assistant", "content": "fake reply"}}],
                              "usage": {"prompt_tokens": max(1, len(raw) // 4), "completion_tokens": 2,
                                        "total_tokens": max(1, len(raw) // 4) + 2}})
            with _lock:
                _state["completions"] += 1
                n = _state["completions"]
            if mode == "crash_after" and n >= limit:
                os._exit(3)
            if mode == "hang_after" and n >= limit:
                _state["hung"] = True
    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--mode", default="normal")
    ap.add_argument("--record", required=True)
    ap.add_argument("--context", default="")
    ap.add_argument("--delay", type=float, default=30.0)
    ap.add_argument("--ignore-term", action="store_true")
    args = ap.parse_args()
    mode, _, num = args.mode.partition(":")
    if mode not in ("normal", "slow_start", "fail_start", "crash_after", "hang_after", "spawn_child"):
        print("unknown mode", file=sys.stderr)
        return 2
    limit = int(num) if num else 0
    if args.ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    record(args.record, {"env": sorted(os.environ), "pid": os.getpid(), "mode": args.mode,
                         "context": args.context})
    if mode == "fail_start":
        print("fake model server: failing on purpose", file=sys.stderr)
        return 1
    if mode == "slow_start":
        time.sleep(args.delay)
    if mode == "spawn_child":
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(600)"])
        record(args.record, {"grandchild": child.pid, "pid": os.getpid()})
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args, mode, limit))
    srv.daemon_threads = True
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
