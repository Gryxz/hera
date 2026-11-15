"""Shared provider flow. Adapters build and parse JSON bytes only; every request goes through
`Gateway.send`, so this package imports no network library.

Order of one call: build body -> consistency check -> budget check (nothing sent if it fails)
-> gateway -> status check -> parse -> ledger record.
"""
from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Sequence
from ..config import TierPrice
from ..ledger import Ledger
from ..privacy.gateway import Gateway, Segment
from ..types import ModelReply, Usage


@dataclass(frozen=True)
class Message:
    """One chat message. `artefact_id` must name a labelled artefact whose content is `data`
    (for a local destination the label is not required)."""
    role: str
    artefact_id: str
    data: bytes


class ProviderBlocked(Exception):
    """The gateway refused to send (or could not). Carries the gateway's fixed reason only."""
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ProviderError(Exception):
    """HTTP error status, malformed JSON or missing fields. Never carries payload text."""
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def estimate_usd(price: TierPrice, payload_bytes: int, max_tokens: int) -> float:
    return (payload_bytes / 4 * price.input + max_tokens * price.output) / 1e6


def _error_message(body: bytes) -> str:
    try:
        obj = json.loads(body)
    except Exception:
        return ""
    if not isinstance(obj, dict):
        return ""
    err = obj.get("error")
    msg = err.get("message") if isinstance(err, dict) else obj.get("message")
    return msg[:200] if isinstance(msg, str) else ""


def _body_contains(body_text: str, text: str) -> bool:
    return any(json.dumps(text, ensure_ascii=a)[1:-1] in body_text for a in (False, True))


class BaseProvider:
    def __init__(self, name: str, tier: str, model_id: str, gateway: Gateway, ledger: Ledger, *,
                 destination_name: str | None = None) -> None:
        self.name, self.tier, self.model_id = name, tier, model_id
        self.gateway, self.ledger = gateway, ledger
        self.destination_name = destination_name or name
        dest = gateway.destinations.get(self.destination_name)
        if dest is None:
            raise ValueError(f"unknown destination {self.destination_name!r}")
        # the "local" tier is free and exempt from caps, so it must really be this machine
        if (tier == "local") != bool(dest.local):
            raise ValueError("tier 'local' and a loopback destination must go together")

    # -- subclass hooks
    def _build(self, messages: Sequence[Message], max_tokens: int, tools: Sequence,
               options: dict) -> bytes:
        raise NotImplementedError

    def _parse(self, body: bytes) -> ModelReply:
        raise NotImplementedError

    # -- shared flow
    def complete(self, messages: Sequence[Message], *, max_tokens: int, task_id: str | None = None,
                 purpose: str = "implement", tools: Sequence = (), **options) -> ModelReply:
        messages = list(messages)
        body = self._build(messages, max_tokens, tools, options)
        self._check_body(messages, body)
        price = self.ledger.prices[self.tier]
        self.ledger.check(task_id=task_id, tier=self.tier,
                          estimate_usd=estimate_usd(price, len(body), max_tokens))
        result = self.gateway.send(self.destination_name,
                                   [Segment(m.artefact_id, m.data) for m in messages],
                                   purpose=purpose, task_id=task_id, envelope=lambda segs: body)
        if not result.sent:
            raise ProviderBlocked(result.reason)
        if result.status is None or not 200 <= result.status < 300:
            detail = _error_message(result.body)
            raise ProviderError(f"provider returned HTTP {result.status}" +
                                (f": {detail}" if detail else ""), status=result.status)
        try:
            reply = self._parse(result.body)
        except Exception as e:
            self.ledger.record(tier=self.tier, usage=Usage(), task_id=task_id,
                               purpose=f"{purpose}:unparsed", destination=self.destination_name,
                               model_id=self.model_id)
            if isinstance(e, ProviderError):
                raise
            raise ProviderError("malformed provider response", status=result.status) from None
        if not reply.usage_reported and self.tier != "local":
            # never record a paid call as free: fall back to a size-based estimate and say so
            reply.usage = Usage(input_tokens=len(body) // 4, output_tokens=max(1, len(reply.text) // 4))
            purpose = f"{purpose}:estimated"
        self.ledger.record(tier=self.tier, usage=reply.usage, task_id=task_id, purpose=purpose,
                           destination=self.destination_name,
                           model_id=reply.model_id or self.model_id)
        return reply

    @staticmethod
    def _check_body(messages: Sequence[Message], body: bytes) -> None:
        try:
            text = body.decode("utf-8")
            ok = all(_body_contains(text, m.data.decode("utf-8")) for m in messages)
        except UnicodeError:
            ok = False
        if not ok:
            raise ProviderError("request body does not match inspected segments")
