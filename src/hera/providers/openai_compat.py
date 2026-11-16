"""OpenAI-compatible chat-completions adapter (llama-server, a friend's endpoint, a pooled model,
or a vendor). Stateless: the full conversation is sent every call.

The Destination URL is the full endpoint URL (e.g. http://host:port/v1/chat/completions); any key
lives in the Destination's headers, never here.
"""
from __future__ import annotations
import json
from typing import Sequence
from ..types import ModelReply, ToolCall, Usage
from .base import BaseProvider, Message, ProviderError, args_text, make_tool_call

_ALLOWED = ("temperature", "top_p", "stop", "tool_choice", "chat_template_kwargs",
            "reasoning_effort", "max_completion_tokens")


class OpenAICompatProvider(BaseProvider):
    def __init__(self, *args, send_store_false: bool = False, **kw) -> None:
        super().__init__(*args, **kw)
        self.send_store_false = send_store_false

    def _build(self, messages: Sequence[Message], max_tokens: int, tools: Sequence,
               options: dict) -> bytes:
        opts = {k: v for k, v in options.items() if k in _ALLOWED and v is not None}
        body: dict = {"model": self.model_id,
                      "messages": [self._wire_message(m) for m in messages]}
        if "max_completion_tokens" in opts:
            body["max_completion_tokens"] = opts.pop("max_completion_tokens")
        else:
            body["max_tokens"] = max_tokens
        body["stream"] = False
        if tools:
            body["tools"] = list(tools)
        if self.send_store_false:
            body["store"] = False
        body.update(opts)
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _wire_message(m: Message) -> dict:
        text = m.data.decode("utf-8")
        if m.role == "tool":
            return {"role": "tool", "tool_call_id": m.tool_call_id, "content": text}
        if m.role == "assistant" and m.tool_calls:
            return {"role": "assistant", "content": text,
                    "tool_calls": [{"id": c.id, "type": "function",
                                    "function": {"name": c.name, "arguments": args_text(c)}}
                                   for c in m.tool_calls]}
        return {"role": m.role, "content": text}

    def render_tools(self, specs: Sequence[dict]) -> list[dict]:
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t.get("description", ""),
                              "parameters": t["parameters"]}} for t in specs]

    def normalise_tool_calls(self, reply: ModelReply) -> tuple[ToolCall, ...]:
        out: list[ToolCall] = []
        seen: set[str] = set()
        for i, raw in enumerate(reply.tool_calls):
            fn = raw.get("function") if isinstance(raw, dict) else None
            fn = fn if isinstance(fn, dict) else {}
            out.append(make_tool_call(i, raw.get("id") if isinstance(raw, dict) else None,
                                      fn.get("name"), fn.get("arguments"), seen))
        return tuple(out)

    def _parse(self, body: bytes) -> ModelReply:
        try:
            obj = json.loads(body)
            msg = obj["choices"][0]["message"]
            text = msg.get("content")
            if text is None:
                text = ""
            if not isinstance(text, str):
                raise TypeError
            calls = msg.get("tool_calls") or []
            u = obj.get("usage")
            usage, reported = Usage(), False
            if isinstance(u, dict):
                cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                prompt = int(u.get("prompt_tokens") or 0)
                usage = Usage(input_tokens=max(prompt - cached, 0),
                              output_tokens=int(u.get("completion_tokens") or 0),
                              cache_read_tokens=cached)
                reported = True
            model = obj.get("model")
            return ModelReply(text=text, usage=usage, tool_calls=tuple(calls),
                              model_id=model if isinstance(model, str) else "",
                              usage_reported=reported)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ProviderError("malformed provider response") from None
