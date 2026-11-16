"""A 127.0.0.1 HTTP server that plays a script of model replies, plus a small harness that wires a
provider, gateway, ledger and toolbox around it. Invented data only."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

from hera.agent.tools import ToolBox
from hera.config import Budget, TierPrice
from hera.ledger import Ledger
from hera.privacy.gateway import Destination, Gateway
from hera.privacy.grants import GrantStore
from hera.privacy.labels import LabelStore
from hera.privacy.policy import Policy
from hera.privacy.scanners import BLOCKING, default_scanners
from hera.providers import AnthropicProvider, OpenAICompatProvider
from hera.types import RepoClass

KEY = "invented-key-for-agent-tests-0001"
PRICES = {"cheap": TierPrice(1.0, 4.0, 0.1, 1.25), "default": TierPrice(2.0, 10.0, 0.2, 2.5),
          "local": TierPrice(0.0, 0.0)}
MODEL_ID = "mock-model-2026-11-01"


@dataclass(frozen=True)
class Step:
    """One scripted model reply: text, tool calls [(name, arguments)], and token usage."""
    text: str = ""
    calls: tuple = ()
    usage: tuple = (1000, 100)      # (input tokens, output tokens)


def step(text: str = "", *calls, usage=(1000, 100)) -> Step:
    return Step(text, tuple(calls), usage)


def _openai(s: Step, n: int) -> dict:
    msg: dict = {"content": s.text or None}
    if s.calls:
        msg["tool_calls"] = [
            {"id": f"call_{n}_{i}", "type": "function",
             "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}
            for i, (name, args) in enumerate(s.calls)]
    return {"model": MODEL_ID, "choices": [{"message": msg}],
            "usage": {"prompt_tokens": s.usage[0], "completion_tokens": s.usage[1]}}


def _anthropic(s: Step, n: int) -> dict:
    blocks: list = [{"type": "text", "text": s.text}] if s.text else []
    blocks += [{"type": "tool_use", "id": f"toolu_{n}_{i}", "name": name, "input": args}
               for i, (name, args) in enumerate(s.calls)]
    return {"model": MODEL_ID, "content": blocks, "stop_reason": "tool_use" if s.calls else "end_turn",
            "usage": {"input_tokens": s.usage[0], "output_tokens": s.usage[1]}}


class ScriptedLLM:
    """Serves one scripted reply per successful request, in order. ``fail_on`` maps a request number
    (1-based, counting every request) to an HTTP status to answer instead; a failed request does not
    consume a scripted reply. After the script is exhausted every request gets HTTP 500."""

    def __init__(self, kind: str = "openai", script=(), fail_on: dict | None = None) -> None:
        assert kind in ("openai", "anthropic")
        self.kind, self.script, self.fail_on = kind, list(script), dict(fail_on or {})
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.served = 0
        self._lock = threading.Lock()
        me = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                with me._lock:
                    me.requests.append(json.loads(raw))
                    me.headers.append({k.lower(): v for k, v in self.headers.items()})
                    n = len(me.requests)
                    status, body = 200, b""
                    if n in me.fail_on:
                        status, body = me.fail_on[n], b'{"error": {"message": "scripted failure"}}'
                    elif me.served >= len(me.script):
                        status, body = 500, b'{"error": {"message": "script exhausted"}}'
                    else:
                        s = me.script[me.served]
                        me.served += 1
                        body = json.dumps((_openai if me.kind == "openai" else _anthropic)(s, n)).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        path = "/v1/chat/completions" if kind == "openai" else "/v1/messages"
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}{path}"

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


class Crash(BaseException):
    """Stands for a process dying: derives from BaseException so no handler in the loop eats it."""


class Env:
    """Gateway, ledger, provider and toolbox around a ScriptedLLM, for one toy candidate worktree."""

    def __init__(self, conn, llm: ScriptedLLM, wt, *, repo_class=RepoClass.PUBLIC, budget=None,
                 tier: str = "default", scanners: bool = False, verify_visible=None,
                 visible_checks_dir=None, repo_id: str = "toy") -> None:
        self.conn, self.llm, self.wt, self.repo_id = conn, llm, wt, repo_id
        self.labels = LabelStore(conn)
        self.policy = Policy(repos={repo_id: (None, repo_class)})
        headers = {"content-type": "application/json",
                   ("x-api-key" if llm.kind == "anthropic" else "Authorization"):
                       (KEY if llm.kind == "anthropic" else "Bearer " + KEY)}
        extra = dict(scanners=default_scanners(self.policy), blocking=BLOCKING) if scanners else {}
        self.gw = Gateway(conn, self.labels, GrantStore(conn), {"mock": Destination("mock", llm.url, headers)},
                          timeout=10, **extra)
        self.ledger = Ledger(conn, budget or Budget(), PRICES)
        cls = AnthropicProvider if llm.kind == "anthropic" else OpenAICompatProvider
        self.provider = cls("mock", tier, "mock-model", self.gw, self.ledger)
        self.toolbox = ToolBox(wt, repo_id=repo_id, label_store=self.labels, policy=self.policy,
                               allow_unsandboxed=True, verify_visible=verify_visible,
                               visible_checks_dir=visible_checks_dir)

    def messages(self, objective: str = "Add parse_amount to calc.py."):
        from hera.agent.prompts import initial_messages
        return initial_messages(task_objective=objective, requirements=[("R-1", "parse_amount works")],
                                visible_check_files={}, repo_file_list=["calc.py"],
                                label_store=self.labels, policy=self.policy, repo_id=self.repo_id)

    def run(self, **kw):
        from hera.agent.loop import run_attempt
        kw.setdefault("messages", self.messages())
        kw.setdefault("task_id", "toy")
        kw.setdefault("attempt_seq", 1)
        return run_attempt(self.conn, provider=self.provider, toolbox=self.toolbox, **kw)

    def transmissions(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM transmissions ORDER BY seq")]

    def calls(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM model_calls ORDER BY id")]
