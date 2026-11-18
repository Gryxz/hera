import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest
from hera.config import Budget, TierPrice
from hera.ledger import BudgetExceeded, Ledger
from hera.privacy import translog
from hera.privacy.gateway import Destination, Gateway
from hera.privacy.grants import GrantStore
from hera.privacy.labels import LabelStore, content_hash, label_bytes, label_builtin
from hera.privacy.policy import Policy
from hera.privacy.scanners import BLOCKING, default_scanners
from hera.providers import (AnthropicProvider, Message, OpenAICompatProvider, ProviderBlocked,
                            ProviderError, estimate_usd)
from hera.types import RepoClass

KEY = "invented-key-for-tests-0000"
PRICES = {"cheap": TierPrice(input=1.0, output=4.0, cache_read=0.1, cache_write=1.25),
          "local": TierPrice(0.0, 0.0)}


class Mock:
    def __init__(self):
        self.bodies: list[bytes] = []
        self.headers: list = []
        self.status = 200
        self.reply = b"{}"
        mock = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                mock.bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
                mock.headers.append({k.lower(): v for k, v in self.headers.items()})
                self.send_response(mock.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(mock.reply)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1/chat"

    def json(self, status=200, **obj):
        self.status, self.reply = status, json.dumps(obj).encode()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class SilentServer:
    """A plain socket listener: reads the whole request, then closes without replying (or, with
    hang=True, keeps the connection open and silent until close())."""
    def __init__(self, hang=False):
        self.hang = hang
        self.requests = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/v1/chat"
        self._held = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                head, _, rest = data.partition(b"\r\n\r\n")
                want = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        want = int(line.split(b":")[1])
                while len(rest) < want:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    rest += chunk
                self.requests += 1
            except OSError:
                pass
            if self.hang:
                self._held.append(conn)
            else:
                conn.close()

    def close(self):
        self.sock.close()
        for c in self._held:
            c.close()


@pytest.fixture
def mock():
    m = Mock()
    yield m
    m.close()


class Env:
    def __init__(self, conn, mock, *, budget=None, local=False, **gw_kw):
        self.conn, self.mock = conn, mock
        self.labels, self.grants = LabelStore(conn), GrantStore(conn)
        self.policy = Policy(repos={"pub": (None, RepoClass.PUBLIC),
                                    "prop": (None, RepoClass.PROPRIETARY)})
        dest = Destination("prov", mock.url, {"x-api-key": KEY, "Authorization": "Bearer " + KEY},
                           local=local)
        self.gw = Gateway(conn, self.labels, self.grants, {"prov": dest}, timeout=5, **gw_kw)
        self.ledger = Ledger(conn, budget or Budget(), PRICES)

    def msg(self, role, aid, text, repo="pub", label=True):
        data = text.encode()
        if label:
            label_bytes(self.labels, self.policy, artefact_id=aid, repo_id=repo, data=data)
        return Message(role, aid, data)

    def openai(self, tier="cheap", **kw):
        return OpenAICompatProvider("prov", tier, "model-x", self.gw, self.ledger, **kw)

    def anthropic(self, tier="cheap"):
        return AnthropicProvider("prov", tier, "model-y", self.gw, self.ledger)

    def rows(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM transmissions ORDER BY seq")]

    def calls(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM model_calls ORDER BY rowid")]


@pytest.fixture
def env(conn, mock):
    return Env(conn, mock)


OK = {"model": "model-x-2026", "choices": [{"message": {"content": "hi there"}}],
      "usage": {"prompt_tokens": 1000, "completion_tokens": 200,
                "prompt_tokens_details": {"cached_tokens": 400}}}


# -- OpenAI-compatible -------------------------------------------------------------------------
def test_openai_body_shape_and_parse(env, mock):
    mock.json(**OK)
    msgs = [env.msg("system", "s", "be brief"), env.msg("user", "u", "say hi é")]
    reply = env.openai().complete(msgs, max_tokens=300, task_id="t1")
    assert json.loads(mock.bodies[0]) == {
        "model": "model-x",
        "messages": [{"role": "system", "content": "be brief"},
                     {"role": "user", "content": "say hi é"}],
        "max_tokens": 300, "stream": False}
    assert reply.text == "hi there" and reply.model_id == "model-x-2026"
    assert reply.usage_reported and reply.tool_calls == ()
    u = reply.usage
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens) == (600, 200, 400)
    (row,) = env.calls()
    assert row["tier"] == "cheap" and row["task_id"] == "t1" and row["purpose"] == "implement"
    assert row["destination"] == "prov" and row["model_id"] == "model-x-2026"
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (600, 200, 400)
    assert row["usd"] == pytest.approx((600 * 1.0 + 200 * 4.0 + 400 * 0.1) / 1e6)
    assert mock.headers[0]["x-api-key"] == KEY            # key reached the server via headers only


def test_openai_store_false_and_max_completion_tokens(env, mock):
    mock.json(**OK)
    p = env.openai(send_store_false=True)
    p.complete([env.msg("user", "u", "q")], max_tokens=50, max_completion_tokens=77)
    body = json.loads(mock.bodies[0])
    assert body["store"] is False and body["max_completion_tokens"] == 77 and "max_tokens" not in body


def test_openai_options_allowlist_and_tools(env, mock):
    mock.json(**OK)
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    env.openai().complete([env.msg("user", "u", "q")], max_tokens=10, tools=tools, temperature=0.2,
                          top_p=0.9, stop=["x"], tool_choice="auto", reasoning_effort="low",
                          chat_template_kwargs={"enable_thinking": False},
                          user="someone", logit_bias={"1": 1}, store=True, stream=True)
    body = json.loads(mock.bodies[0])
    assert body["tools"] == tools and body["temperature"] == 0.2 and body["top_p"] == 0.9
    assert body["stop"] == ["x"] and body["tool_choice"] == "auto" and body["reasoning_effort"] == "low"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["stream"] is False
    for dropped in ("user", "logit_bias", "store"):
        assert dropped not in body


def test_openai_tool_calls_and_null_content(env, mock):
    calls = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    mock.json(choices=[{"message": {"content": None, "tool_calls": calls}}],
              usage={"prompt_tokens": 5, "completion_tokens": 1})
    reply = env.openai().complete([env.msg("user", "u", "q")], max_tokens=10)
    assert reply.text == "" and reply.tool_calls == tuple(calls)


def test_openai_missing_usage(env, mock):
    mock.json(choices=[{"message": {"content": "x"}}])
    reply = env.openai().complete([env.msg("user", "u", "q")], max_tokens=10)
    # a paid call is never recorded as free: size-based estimate, marked as such
    assert reply.usage_reported is False and reply.usage.input_tokens > 0
    row = env.calls()[0]
    assert row["usd"] > 0 and row["purpose"].endswith(":estimated")


def test_builtin_label_is_public(env, mock):
    mock.json(**OK)
    data = b"static prompt template"
    label_builtin(env.labels, artefact_id="tpl", data=data)
    env.openai().complete([Message("system", "tpl", data)], max_tokens=10)
    assert env.labels.get("tpl").derived_from == ("hera-builtin",) and len(mock.bodies) == 1


# -- Anthropic ---------------------------------------------------------------------------------
ANTH = {"model": "model-y-2026", "content": [
    {"type": "text", "text": "Hello, "}, {"type": "tool_use", "id": "tu1", "name": "f", "input": {"a": 1}},
    {"type": "text", "text": "world"}],
    "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 1000,
              "cache_creation_input_tokens": 200}}


def test_anthropic_request_and_parse(env, mock):
    mock.json(**ANTH)
    msgs = [env.msg("system", "s1", "rule one"), env.msg("user", "u", "go"),
            env.msg("system", "s2", "rule two")]
    tools = [{"name": "f", "input_schema": {"type": "object"}}]
    reply = env.anthropic().complete(msgs, max_tokens=64, tools=tools, temperature=0.1,
                                     thinking={"type": "disabled"}, top_k=5, stop_sequences=["z"],
                                     tool_choice={"type": "auto"}, top_p=0.5, junk=1, task_id="t9")
    body = json.loads(mock.bodies[0])
    assert body == {"model": "model-y", "max_tokens": 64, "system": "rule one\n\nrule two",
                    "messages": [{"role": "user", "content": "go"}], "tools": tools,
                    "temperature": 0.1, "thinking": {"type": "disabled"},
                    "stop_sequences": ["z"], "tool_choice": {"type": "auto"}, "top_p": 0.5}
    assert reply.text == "Hello, world" and reply.model_id == "model-y-2026"
    assert reply.tool_calls == (ANTH["content"][1],)
    u = reply.usage
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens) == (100, 50, 1000, 200)
    (row,) = env.calls()
    assert row["usd"] == pytest.approx((100 * 1.0 + 50 * 4.0 + 1000 * 0.1 + 200 * 1.25) / 1e6)


def test_anthropic_no_system_omits_key(env, mock):
    mock.json(**ANTH)
    env.anthropic().complete([env.msg("user", "u", "go")], max_tokens=5)
    assert "system" not in json.loads(mock.bodies[0])


def test_anthropic_malformed_content(env, mock):
    mock.json(model="m", content="not a list")
    with pytest.raises(ProviderError):
        env.anthropic().complete([env.msg("user", "u", "go")], max_tokens=5)
    assert env.calls()[0]["purpose"] == "implement:unparsed"


# -- privacy -----------------------------------------------------------------------------------
def test_unlabelled_blocked(env, mock):
    mock.json(**OK)
    with pytest.raises(ProviderBlocked) as ei:
        env.openai().complete([env.msg("user", "u", "q", label=False)], max_tokens=10)
    assert ei.value.reason == "unlabelled segment"
    assert mock.bodies == [] and env.calls() == []
    assert [r["decision"] for r in env.rows()] == ["blocked"]


def test_planted_key_blocked(conn, mock):
    e = Env(conn, mock, scanners=default_scanners(), blocking=BLOCKING)
    planted = "AKIA" + "TESTFAKE0000KEY1"
    with pytest.raises(ProviderBlocked) as ei:
        e.openai().complete([e.msg("user", "u", f"note {planted}")], max_tokens=10)
    assert ei.value.reason == "restricted data finding" and planted not in str(ei.value)
    assert mock.bodies == [] and e.calls() == []


def test_proprietary_needs_grant(env, mock):
    mock.json(**OK)
    m = env.msg("user", "p", "proprietary code", repo="prop")
    with pytest.raises(ProviderBlocked):
        env.openai().complete([m], max_tokens=10)
    assert mock.bodies == []
    env.grants.create(content_hashes=[content_hash(m.data)], destinations=["prov"], purpose="implement",
                      expires_at=4e9)
    assert env.openai().complete([m], max_tokens=10).text == "hi there"
    assert len(mock.bodies) == 1


def test_body_must_match_inspected_segments(env, mock):
    class Sneaky(OpenAICompatProvider):
        def _build(self, messages, max_tokens, tools, options):
            return json.dumps({"messages": [{"role": "user", "content": "something else"}]}).encode()

    with pytest.raises(ProviderError, match="does not match inspected segments"):
        Sneaky("prov", "cheap", "m", env.gw, env.ledger).complete(
            [env.msg("user", "u", "real content")], max_tokens=10)
    assert mock.bodies == [] and env.rows() == [] and env.calls() == []


# -- budget ------------------------------------------------------------------------------------
def test_budget_blocks_before_sending(conn, mock):
    e = Env(conn, mock, budget=Budget(per_task_usd=0.0001))
    mock.json(**OK)
    with pytest.raises(BudgetExceeded):
        e.openai().complete([e.msg("user", "u", "q")], max_tokens=10_000, task_id="t1")
    assert mock.bodies == [] and e.rows() == [] and e.calls() == []


def test_estimate_usd():
    assert estimate_usd(TierPrice(2.0, 10.0), 300, 1000) == pytest.approx((100 * 2.0 + 1000 * 10.0) / 1e6)


# -- errors ------------------------------------------------------------------------------------
@pytest.mark.parametrize("status", [500, 429])
def test_http_errors(env, mock, status):
    mock.json(status=status, error={"message": "slow down " + "x" * 300})
    with pytest.raises(ProviderError) as ei:
        env.openai().complete([env.msg("user", "u", "secret question")], max_tokens=10)
    assert ei.value.status == status and "secret question" not in str(ei.value)
    assert len(str(ei.value)) < 260 and env.calls() == []


def test_http_error_non_json_body_has_no_text(env, mock):
    mock.status, mock.reply = 502, b"<html>upstream said: secret question</html>"
    with pytest.raises(ProviderError) as ei:
        env.openai().complete([env.msg("user", "u", "q")], max_tokens=10)
    assert ei.value.status == 502 and "upstream" not in str(ei.value)


def test_non_json_200_records_unparsed(env, mock):
    mock.status, mock.reply = 200, b"not json"
    with pytest.raises(ProviderError):
        env.openai().complete([env.msg("user", "u", "q")], max_tokens=10, task_id="t2")
    (row,) = env.calls()
    assert row["purpose"] == "implement:unparsed" and row["usd"] == 0 and row["input_tokens"] == 0
    assert row["task_id"] == "t2"


def test_transport_failure_is_a_provider_error_not_a_privacy_block(conn, mock):
    # a timeout or refused connection must reach the ladder as a provider error (continuable), not as
    # a privacy refusal (which would move the task to local-only or stop it as BLOCKED_PRIVACY)
    e = Env(conn, mock)
    mock.close()
    with pytest.raises(ProviderError, match="could not be reached") as ei:
        e.openai().complete([e.msg("user", "u", "q")], max_tokens=10)
    assert not isinstance(ei.value, ProviderBlocked) and ei.value.status is None
    (row,) = conn.execute("SELECT decision FROM transmissions").fetchall()
    assert row["decision"] == "sent"                  # the log says it was allowed out (it may have arrived)



def test_refused_connection_records_nothing(conn, mock):
    e = Env(conn, mock)
    mock.close()
    with pytest.raises(ProviderError, match="could not be reached"):
        e.openai().complete([e.msg("user", "u", "q")], max_tokens=1000, task_id="t")
    assert e.calls() == []


def test_closed_without_reply_records_the_estimate(conn, mock):
    srv = SilentServer()
    try:
        e = Env(conn, srv)
        p = e.openai()
        m = e.msg("user", "u", "q")
        body = p._build([m], 1000, (), {})
        with pytest.raises(ProviderError, match="could not be reached") as ei:
            p.complete([m], max_tokens=1000, task_id="t", purpose="implement")
        assert not isinstance(ei.value, ProviderBlocked)
        (row,) = e.calls()
        assert row["purpose"] == "implement:transport_error:estimated"
        assert row["task_id"] == "t" and row["tier"] == "cheap"
        assert row["destination"] == "prov" and row["model_id"] == "model-x"
        assert row["output_tokens"] == 1000
        assert row["usd"] == pytest.approx(estimate_usd(PRICES["cheap"], len(body), 1000))
        assert e.ledger.spent_task("t") == pytest.approx(row["usd"])
        assert srv.requests == 1
    finally:
        srv.close()


def test_recorded_estimate_counts_against_the_next_cap_check(conn):
    srv = SilentServer()
    try:
        # one call's estimate (about $0.004) fits under the task cap, two do not
        e = Env(conn, srv, budget=Budget(per_task_usd=0.005, per_day_usd=10, per_month_usd=100))
        p = e.openai()
        m = e.msg("user", "u", "q")
        with pytest.raises(ProviderError):
            p.complete([m], max_tokens=1000, task_id="t")
        with pytest.raises(BudgetExceeded) as ei:
            p.complete([m], max_tokens=1000, task_id="t")
        assert ei.value.cap == "per-task"
        assert len(e.calls()) == 1 and srv.requests == 1       # nothing was sent the second time
    finally:
        srv.close()


def test_read_timeout_records_the_estimate(conn):
    srv = SilentServer(hang=True)
    try:
        e = Env(conn, srv)
        e.gw.timeout = 0.3
        with pytest.raises(ProviderError, match="could not be reached"):
            e.anthropic().complete([e.msg("user", "u", "q")], max_tokens=500, task_id="t")
        (row,) = e.calls()
        assert row["purpose"] == "implement:transport_error:estimated" and row["usd"] > 0
    finally:
        srv.close()


def test_local_tier_transport_failure_records_nothing(conn):
    srv = SilentServer()
    try:
        e = Env(conn, srv, local=True)
        with pytest.raises(ProviderError, match="could not be reached"):
            e.openai(tier="local").complete([e.msg("user", "u", "q", label=False)], max_tokens=1000)
        assert e.calls() == []
    finally:
        srv.close()


# -- local tier --------------------------------------------------------------------------------
def test_local_tier_unlabelled_free_uncapped(conn, mock):
    e = Env(conn, mock, local=True, budget=Budget(per_task_usd=0.0, per_day_usd=0.0, per_month_usd=0.0))
    mock.json(**OK)
    reply = e.openai(tier="local").complete(
        [e.msg("user", "raw", "unlabelled sensitive text", label=False)], max_tokens=10_000, task_id="t")
    assert reply.text == "hi there" and len(mock.bodies) == 1
    (row,) = e.calls()
    assert row["tier"] == "local" and row["usd"] == 0
    assert e.rows()[-1]["reason"] == "loopback"
    assert translog.verify(conn) == (True, None)


# -- secrets never recorded --------------------------------------------------------------------
def test_api_key_never_reaches_logs_ledger_or_errors(conn, mock):
    e = Env(conn, mock)
    mock.json(**OK)
    p = e.openai()
    p.complete([e.msg("user", "u", "q")], max_tokens=10)
    mock.json(status=500, error={"message": "boom"})
    with pytest.raises(ProviderError) as e1:
        p.complete([e.msg("user", "u", "q")], max_tokens=10)
    mock.status, mock.reply = 200, b"junk"
    with pytest.raises(ProviderError) as e2:
        p.complete([e.msg("user", "u", "q")], max_tokens=10)
    with pytest.raises(ProviderBlocked) as e3:
        p.complete([e.msg("user", "x", "q", label=False)], max_tokens=10)
    dump = json.dumps([e.rows(), e.calls()]) + repr(e.gw.destinations)
    for exc in (e1.value, e2.value, e3.value):
        dump += str(exc) + repr(exc)
    assert KEY not in dump and "invented-key" not in dump
    assert KEY in repr(mock.headers)          # sanity: the key really was sent to the server


def test_local_tier_requires_loopback_destination_and_vice_versa(conn, mock):
    remote = Env(conn, mock)                       # destination not marked local
    with pytest.raises(ValueError):
        remote.openai(tier="local")                # would be free and uncapped while leaving the machine
    local = Env(conn, mock, local=True)
    with pytest.raises(ValueError):
        local.openai(tier="cheap")
    local.openai(tier="local")                     # the matching pair is accepted
    with pytest.raises(ValueError):
        OpenAICompatProvider("nowhere", "cheap", "m", remote.gw, remote.ledger)


def test_remote_destination_must_use_https(conn):
    labels, grants = LabelStore(conn), GrantStore(conn)
    for url in ("http://api.provider.example/v1/chat", "http://192.168.1.20:8080/v1/chat"):
        with pytest.raises(ValueError):
            Gateway(conn, labels, grants, {"p": Destination("p", url, {})})
    Gateway(conn, labels, grants, {"p": Destination("p", "https://api.provider.example/v1/chat", {})})
    Gateway(conn, labels, grants, {"p": Destination("p", "http://127.0.0.1:9/v1/chat", {})})


# ---- Grok (xAI): OpenAI-compatible with three differences

def _grok(env):
    from hera.providers import GrokProvider
    return GrokProvider("prov", "cheap", "grok-test", env.gw, env.ledger)


def test_grok_request_shape(env, mock):
    mock.json(choices=[{"message": {"content": "ok"}}],
              usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
    _grok(env).complete([env.msg("system", "s", "be brief"), env.msg("user", "u", "hi")],
                        max_tokens=64, stop=["END"], reasoning_effort="low")
    body = json.loads(mock.bodies[0])
    assert body["max_completion_tokens"] == 64 and "max_tokens" not in body
    assert "stop" not in body and body["reasoning_effort"] == "low" and body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_grok_reasoning_tokens_outside_completion_are_billed(env, mock):
    # reasoning reported separately: total = prompt + completion + reasoning
    mock.json(choices=[{"message": {"content": "303"}}],
              usage={"prompt_tokens": 100, "completion_tokens": 14, "total_tokens": 424,
                     "completion_tokens_details": {"reasoning_tokens": 310}})
    reply = _grok(env).complete([env.msg("user", "u", "q")], max_tokens=500)
    assert reply.usage.output_tokens == 324
    assert env.calls()[0]["usd"] == pytest.approx((100 * 1.0 + 324 * 4.0) / 1e6)


def test_grok_reasoning_tokens_already_included_are_not_double_counted(env, mock):
    mock.json(choices=[{"message": {"content": "x"}}],
              usage={"prompt_tokens": 100, "completion_tokens": 324, "total_tokens": 424,
                     "completion_tokens_details": {"reasoning_tokens": 310}})
    assert _grok(env).complete([env.msg("user", "u", "q")], max_tokens=500).usage.output_tokens == 324


def test_grok_ambiguous_total_overcounts_rather_than_undercounts(env, mock):
    mock.json(choices=[{"message": {"content": "x"}}],
              usage={"prompt_tokens": 100, "completion_tokens": 14,
                     "completion_tokens_details": {"reasoning_tokens": 310}})
    assert _grok(env).complete([env.msg("user", "u", "q")], max_tokens=500).usage.output_tokens == 324


def test_grok_goes_through_the_same_privacy_and_cache_rules(env, mock):
    mock.json(choices=[{"message": {"content": "x"}}],
              usage={"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55,
                     "prompt_tokens_details": {"cached_tokens": 40}})
    reply = _grok(env).complete([env.msg("user", "u", "q")], max_tokens=10)
    assert reply.usage.cache_read_tokens == 40 and reply.usage.input_tokens == 10
    with pytest.raises(ProviderBlocked):
        _grok(env).complete([env.msg("user", "nolabel", "secret-ish", label=False)], max_tokens=10)
    assert len(mock.bodies) == 1                      # the blocked call never reached the server


def test_grok_url_constant_is_https():
    from hera.providers import GROK_CHAT_URL
    assert GROK_CHAT_URL == "https://api.x.ai/v1/chat/completions"
