"""Anthropic Messages API adapter (stateless).

The Destination URL is the full endpoint URL (e.g. https://.../v1/messages). The required
headers (`x-api-key`, `anthropic-version`, `content-type`) live in the Destination's headers,
loaded from local config; this module never sees a key.

Tested against a mock server only; it has not been exercised against the real service.
"""
from __future__ import annotations
import json
from typing import Sequence
from ..types import ModelReply, ToolCall, Usage
from .base import BaseProvider, Message, ProviderError, args_text, make_tool_call

_ALLOWED = ("temperature", "top_p", "stop_sequences", "tool_choice", "thinking")


class AnthropicProvider(BaseProvider):
    def _build(self, messages: Sequence[Message], max_tokens: int, tools: Sequence,
               options: dict) -> bytes:
        system = [m.data.decode("utf-8") for m in messages if m.role == "system"]
        body: dict = {"model": self.model_id, "max_tokens": max_tokens,
                      "messages": self._wire_messages([m for m in messages if m.role != "system"])}
        if system:
            body["system"] = "\n\n".join(system)
        if tools:
            body["tools"] = list(tools)
        body.update({k: v for k, v in options.items() if k in _ALLOWED and v is not None})
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _wire_messages(messages: Sequence[Message]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            text = m.data.decode("utf-8")
            if m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": text}
                last = out[-1] if out else None
                if last and last["role"] == "user" and isinstance(last["content"], list) \
                        and last["content"] and last["content"][-1].get("type") == "tool_result":
                    last["content"].append(block)       # results of one assistant turn share a message
                else:
                    out.append({"role": "user", "content": [block]})
            elif m.role == "assistant" and m.tool_calls:
                blocks = ([{"type": "text", "text": text}] if text else [])
                blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                           for c in m.tool_calls]
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": m.role, "content": text})
        return out

    def render_tools(self, specs: Sequence[dict]) -> list[dict]:
        return [{"name": t["name"], "description": t.get("description", ""),
                 "input_schema": t["parameters"]} for t in specs]

    def normalise_tool_calls(self, reply: ModelReply) -> tuple[ToolCall, ...]:
        out: list[ToolCall] = []
        seen: set[str] = set()
        for i, raw in enumerate(reply.tool_calls):
            raw = raw if isinstance(raw, dict) else {}
            out.append(make_tool_call(i, raw.get("id"), raw.get("name"), raw.get("input"), seen))
        return tuple(out)

    def _args_in_body(self, call: ToolCall, body_text: str) -> bool:
        # the arguments travel as a nested JSON object, so they appear unescaped
        return any(json.dumps(call.arguments, ensure_ascii=a) in body_text for a in (False, True))

    def _parse(self, body: bytes) -> ModelReply:
        try:
            obj = json.loads(body)
            blocks = obj["content"]
            if not isinstance(blocks, list):
                raise TypeError
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            calls = tuple(b for b in blocks if b.get("type") == "tool_use")
            u = obj.get("usage")
            usage, reported = Usage(), False
            if isinstance(u, dict):
                usage = Usage(input_tokens=int(u.get("input_tokens") or 0),
                              output_tokens=int(u.get("output_tokens") or 0),
                              cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
                              cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0))
                reported = True
            model = obj.get("model")
            return ModelReply(text=text, usage=usage, tool_calls=calls,
                              model_id=model if isinstance(model, str) else "",
                              usage_reported=reported)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ProviderError("malformed provider response") from None
