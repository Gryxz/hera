"""Tool calls through the provider adapters: wire formats, normalisation, and the rule that every
byte the model gets back (including its own tool-call arguments) is a labelled, inspected segment."""
import json

import pytest

from hera.privacy.labels import label_bytes
from hera.privacy.scanners import BLOCKING, default_scanners
from hera.providers import (AnthropicProvider, GrokProvider, Message, OpenAICompatProvider, ProviderBlocked,
                            ProviderError)
from hera.providers.base import calls_artefact_id, render_calls_artefact
from hera.types import ModelReply, ToolCall
from test_providers import Env as PEnv, Mock

SPECS = [{"name": "read_file", "description": "Read a file.",
          "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                         "required": ["path"]}}]
OK_OPENAI = {"model": "m", "choices": [{"message": {"content": "ok"}}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
OK_ANTHROPIC = {"model": "m", "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "output_tokens": 5}}


@pytest.fixture
def mock():
    m = Mock()
    yield m
    m.close()


@pytest.fixture
def env(conn, mock):
    return PEnv(conn, mock)


def convo(env):
    """system, user, assistant (text + two tool calls), two tool results."""
    calls = (ToolCall("call_1", "read_file", {"path": "calc.py"}),
             ToolCall("call_2", "read_file", {"path": "é.txt"}))
    asst = env.msg("assistant", "a1", "Let me look.")
    for aid, data in ((calls_artefact_id(asst), render_calls_artefact(calls)),):
        label_bytes(env.labels, env.policy, artefact_id=aid, repo_id="pub", data=data)
    asst = Message("assistant", "a1", asst.data, tool_calls=calls)
    t1 = env.msg("tool", "t1", "def add(a, b): ...")
    t2 = env.msg("tool", "t2", "no such file")
    return [env.msg("system", "s", "rules"), env.msg("user", "u", "go"), asst,
            Message("tool", "t1", t1.data, tool_call_id="call_1"),
            Message("tool", "t2", t2.data, tool_call_id="call_2")]


def test_openai_wire_format(env, mock):
    mock.json(**OK_OPENAI)
    p = env.openai()
    p.complete(convo(env), max_tokens=50, tools=p.render_tools(SPECS))
    assert json.loads(mock.bodies[0]) == {
        "model": "model-x", "max_tokens": 50, "stream": False,
        "messages": [
            {"role": "system", "content": "rules"}, {"role": "user", "content": "go"},
            {"role": "assistant", "content": "Let me look.", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path": "calc.py"}'}},
                {"id": "call_2", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path": "é.txt"}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "def add(a, b): ..."},
            {"role": "tool", "tool_call_id": "call_2", "content": "no such file"}],
        "tools": [{"type": "function", "function": {
            "name": "read_file", "description": "Read a file.", "parameters": SPECS[0]["parameters"]}}]}


def test_anthropic_wire_format_merges_tool_results(env, mock):
    mock.json(**OK_ANTHROPIC)
    p = env.anthropic()
    p.complete(convo(env), max_tokens=50, tools=p.render_tools(SPECS))
    assert json.loads(mock.bodies[0]) == {
        "model": "model-y", "max_tokens": 50, "system": "rules",
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "calc.py"}},
                {"type": "tool_use", "id": "call_2", "name": "read_file", "input": {"path": "é.txt"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "def add(a, b): ..."},
                {"type": "tool_result", "tool_use_id": "call_2", "content": "no such file"}]}],
        "tools": [{"name": "read_file", "description": "Read a file.",
                   "input_schema": SPECS[0]["parameters"]}]}


def test_anthropic_assistant_without_text_has_only_tool_use(env, mock):
    mock.json(**OK_ANTHROPIC)
    calls = (ToolCall("c1", "read_file", {"path": "x"}),)
    asst = env.msg("assistant", "a", "")
    label_bytes(env.labels, env.policy, artefact_id=calls_artefact_id(asst), repo_id="pub",
                data=render_calls_artefact(calls))
    tool = env.msg("tool", "t", "r")
    env.anthropic().complete([env.msg("user", "u", "go"), Message("assistant", "a", b"", tool_calls=calls),
                              Message("tool", "t", tool.data, tool_call_id="c1")], max_tokens=5)
    msgs = json.loads(mock.bodies[0])["messages"]
    assert msgs[1]["content"] == [{"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "x"}}]


def test_grok_inherits_openai_tool_format(conn, mock):
    e = PEnv(conn, mock)
    p = GrokProvider("prov", "cheap", "grok-x", e.gw, e.ledger)
    assert p.render_tools(SPECS)[0]["type"] == "function"


# -- normalisation ------------------------------------------------------------------------------
def oa_reply(*calls):
    return ModelReply(text="", tool_calls=tuple(calls))


def test_openai_normalise(env):
    p = env.openai()
    got = p.normalise_tool_calls(oa_reply(
        {"id": "call_a", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a"}'}},
        {"id": "call_b", "function": {"name": "read_file", "arguments": "{not json"}},
        {"id": "call_c", "function": {"name": "read_file", "arguments": "[1, 2]"}},
        {"id": "call_d", "function": {"name": "list_files", "arguments": ""}},
        {"id": "bad id!", "function": {"name": "list_files", "arguments": "{}"}},
        {"id": "call_a", "function": {"name": "list_files", "arguments": {"path": "."}}},
        {"function": {"name": "bad name!", "arguments": "{}"}},
        {"id": "call_g"}))
    assert got[0] == ToolCall("call_a", "read_file", {"path": "a"})
    assert got[1] == ToolCall("call_b", "__invalid__", {"raw": "{not json"})
    assert got[2] == ToolCall("call_c", "__invalid__", {"raw": "[1, 2]"})
    assert got[3] == ToolCall("call_d", "list_files", {})
    assert got[4].id == "call_4" and got[4].name == "list_files"            # odd id replaced
    assert got[5].id == "call_5"                                            # duplicate id replaced
    assert got[5].arguments == {"path": "."}                                # arguments already an object
    assert got[6].name == "__invalid__" and got[7].name == "__invalid__"
    assert len({c.id for c in got}) == len(got)


def test_invalid_raw_text_is_capped(env):
    big = "x" * 50_000
    (c,) = env.openai().normalise_tool_calls(oa_reply(
        {"id": "c", "function": {"name": "read_file", "arguments": big}}))
    assert c.name == "__invalid__" and len(c.arguments["raw"]) == 4000


def test_anthropic_normalise(env):
    p = env.anthropic()
    got = p.normalise_tool_calls(ModelReply(text="", tool_calls=(
        {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a"}},
        {"type": "tool_use", "id": "toolu_2", "name": "read_file", "input": "oops"},
        {"type": "tool_use", "id": "toolu_3", "name": "list_files"})))
    assert got[0] == ToolCall("toolu_1", "read_file", {"path": "a"})
    assert got[1].name == "__invalid__" and got[1].arguments == {"raw": "oops"}
    assert got[2].name == "__invalid__"


def test_parsed_reply_round_trips_into_normalised_calls(env, mock):
    mock.json(model="m", choices=[{"message": {"content": None, "tool_calls": [
        {"id": "call_z", "type": "function",
         "function": {"name": "list_files", "arguments": '{"path": "src"}'}}]}}],
              usage={"prompt_tokens": 1, "completion_tokens": 1})
    p = env.openai()
    reply = p.complete([env.msg("user", "u", "go")], max_tokens=5)
    assert p.normalise_tool_calls(reply) == (ToolCall("call_z", "list_files", {"path": "src"}),)
    mock.json(model="m", content=[{"type": "tool_use", "id": "tu", "name": "list_files", "input": {"path": "."}}],
              usage={"input_tokens": 1, "output_tokens": 1})
    p = env.anthropic()
    reply = p.complete([env.msg("user", "u2", "go")], max_tokens=5)
    assert p.normalise_tool_calls(reply) == (ToolCall("tu", "list_files", {"path": "."}),)


# -- the arguments are an inspected, labelled artefact ----------------------------------------------
def test_unlabelled_tool_call_artefact_is_blocked(env, mock):
    mock.json(**OK_OPENAI)
    calls = (ToolCall("c1", "write_file", {"path": "a.py", "content": "x"}),)
    msgs = [env.msg("user", "u", "go"), Message("assistant", "a", b"", tool_calls=calls)]
    env.msg("assistant", "a", "")                                        # the text is labelled, the calls are not
    with pytest.raises(ProviderBlocked, match="unlabelled"):
        env.openai().complete(msgs, max_tokens=5)
    assert mock.bodies == []


def test_secret_inside_tool_call_arguments_is_blocked_even_at_a_line_start(conn, mock):
    key = "AKIA" + "TESTFAKE0000KEY1"
    e = PEnv(conn, mock, scanners=default_scanners(), blocking=BLOCKING)
    mock.json(**OK_OPENAI)
    calls = (ToolCall("c1", "write_file", {"path": "a.py", "content": "# config\n" + key + "\n"}),)
    asst = e.msg("assistant", "a", "")
    label_bytes(e.labels, e.policy, artefact_id=calls_artefact_id(asst), repo_id="pub",
                data=render_calls_artefact(calls))
    with pytest.raises(ProviderBlocked, match="restricted"):
        e.openai().complete([e.msg("user", "u", "go"), Message("assistant", "a", b"", tool_calls=calls)],
                            max_tokens=5)
    assert mock.bodies == []
    # the raw rendering carries the value unescaped, which is what lets a scanner anchor on line starts
    assert b"\n" + key.encode() in render_calls_artefact(calls)


def test_tool_call_artefact_class_follows_the_repository(conn, mock):
    e = PEnv(conn, mock)
    mock.json(**OK_OPENAI)
    calls = (ToolCall("c1", "write_file", {"path": "a.py", "content": "x"}),)
    asst = e.msg("assistant", "a", "", repo="prop")
    label_bytes(e.labels, e.policy, artefact_id=calls_artefact_id(asst), repo_id="prop",
                data=render_calls_artefact(calls))
    with pytest.raises(ProviderBlocked, match="not permitted"):
        e.openai().complete([e.msg("user", "u", "go", repo="prop"),
                             Message("assistant", "a", b"", tool_calls=calls)], max_tokens=5)


def test_body_must_contain_the_arguments(conn, mock):
    class Liar(OpenAICompatProvider):
        def _build(self, messages, max_tokens, tools, options):
            body = json.loads(super()._build(messages, max_tokens, tools, options))
            for m in body["messages"]:
                for c in m.get("tool_calls", []):
                    c["function"]["arguments"] = '{"path": "something else"}'
            return json.dumps(body).encode()
    e = PEnv(conn, mock)
    calls = (ToolCall("c1", "read_file", {"path": "calc.py"}),)
    asst = e.msg("assistant", "a", "")
    label_bytes(e.labels, e.policy, artefact_id=calls_artefact_id(asst), repo_id="pub",
                data=render_calls_artefact(calls))
    p = Liar("prov", "cheap", "m", e.gw, e.ledger)
    with pytest.raises(ProviderError, match="does not match"):
        p.complete([e.msg("user", "u", "go"), Message("assistant", "a", b"", tool_calls=calls)], max_tokens=5)
    assert mock.bodies == []


def test_message_shape_is_validated(env, mock):
    p = env.openai()
    with pytest.raises(ProviderError):
        p.complete([env.msg("tool", "t", "r")], max_tokens=5)                       # no tool_call_id
    t = env.msg("user", "u", "x")
    with pytest.raises(ProviderError):
        p.complete([Message("user", "u", t.data, tool_call_id="c1")], max_tokens=5)  # id on a user message
    with pytest.raises(ProviderError):
        p.complete([Message("user", "u", t.data, tool_calls=(ToolCall("c", "n", {}),))], max_tokens=5)
    with pytest.raises(ProviderError):
        p.complete([Message("tool", "u", t.data, tool_call_id="has space!")], max_tokens=5)
    assert mock.bodies == []
