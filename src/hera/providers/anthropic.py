"""Anthropic Messages API adapter (stateless).

The Destination URL is the full endpoint URL (e.g. https://.../v1/messages). The required
headers (`x-api-key`, `anthropic-version`, `content-type`) live in the Destination's headers,
loaded from local config; this module never sees a key.

Tested against a mock server only; it has not been exercised against the real service.
"""
from __future__ import annotations
import json
from typing import Sequence
from ..types import ModelReply, Usage
from .base import BaseProvider, Message, ProviderError

_ALLOWED = ("temperature", "top_p", "stop_sequences", "tool_choice", "thinking")


class AnthropicProvider(BaseProvider):
    def _build(self, messages: Sequence[Message], max_tokens: int, tools: Sequence,
               options: dict) -> bytes:
        system = [m.data.decode("utf-8") for m in messages if m.role == "system"]
        body: dict = {"model": self.model_id, "max_tokens": max_tokens,
                      "messages": [{"role": m.role, "content": m.data.decode("utf-8")}
                                   for m in messages if m.role != "system"]}
        if system:
            body["system"] = "\n\n".join(system)
        if tools:
            body["tools"] = list(tools)
        body.update({k: v for k, v in options.items() if k in _ALLOWED and v is not None})
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

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
