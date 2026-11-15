"""xAI Grok adapter. Grok's chat-completions endpoint is OpenAI-compatible and stateless, so this
is the OpenAI-compatible adapter with three differences:

- `max_completion_tokens` is always sent (xAI documents `max_tokens` as deprecated);
- `stop` is dropped (not supported by Grok reasoning models);
- reasoning tokens are counted as output tokens. Grok models reason by default and may report
  reasoning tokens outside `completion_tokens`. `total_tokens` decides which case applies; when
  it cannot, reasoning tokens are added, so cost is over- rather than under-counted.

Destination: URL `https://api.x.ai/v1/chat/completions`, header `Authorization: Bearer <key>`,
both set in local configuration. Tested against a mock server only.
"""
from __future__ import annotations
import json
from typing import Sequence
from ..types import ModelReply
from .base import Message
from .openai_compat import OpenAICompatProvider

GROK_CHAT_URL = "https://api.x.ai/v1/chat/completions"


class GrokProvider(OpenAICompatProvider):
    def _build(self, messages: Sequence[Message], max_tokens: int, tools: Sequence,
               options: dict) -> bytes:
        options = {k: v for k, v in options.items() if k != "stop"}
        options.setdefault("max_completion_tokens", max_tokens)
        if options["max_completion_tokens"] is None:
            options["max_completion_tokens"] = max_tokens
        return super()._build(messages, max_tokens, tools, options)

    def _parse(self, body: bytes) -> ModelReply:
        reply = super()._parse(body)
        u = json.loads(body).get("usage")
        if isinstance(u, dict):
            details = u.get("completion_tokens_details")
            reasoning = int((details if isinstance(details, dict) else {}).get("reasoning_tokens") or 0)
            if reasoning:
                prompt = int(u.get("prompt_tokens") or 0)
                completion = int(u.get("completion_tokens") or 0)
                total = u.get("total_tokens")
                already_included = isinstance(total, int) and total == prompt + completion
                if not already_included:
                    reply.usage.output_tokens = completion + reasoning
        return reply
