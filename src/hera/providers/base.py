"""Shared provider flow. Adapters build and parse JSON bytes only; every request goes through
`Gateway.send`, so this package imports no network library.

Order of one call: build body -> consistency check -> budget check (nothing sent if it fails)
-> gateway -> status check -> parse -> ledger record.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from typing import Sequence
from ..config import TierPrice
from ..ledger import Ledger
from ..privacy.gateway import Gateway, Segment
from ..types import ModelReply, ToolCall, Usage

_ID_OK = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
INVALID_TOOL = "__invalid__"
CALLS_SUFFIX = "#calls"
_RAW_CAP = 4000


@dataclass(frozen=True)
class Message:
    """One chat message. `artefact_id` must name a labelled artefact whose content is `data`
    (for a local destination the label is not required).

    ``tool_call_id`` is set on role "tool" messages (the result of one tool call).
    ``tool_calls`` (tuple of ToolCall) is set on role "assistant" messages that asked for tools; the
    calls are HERA's rendering of what the model sent and travel as a second labelled artefact,
    ``calls_artefact_id(message)``, whose content is ``render_calls_artefact(message.tool_calls)``."""
    role: str
    artefact_id: str
    data: bytes
    tool_call_id: str = ""
    tool_calls: tuple = ()


def calls_artefact_id(m: Message) -> str:
    return m.artefact_id + CALLS_SUFFIX


def args_text(call: ToolCall) -> str:
    """The exact JSON text of a call's arguments as every adapter encodes it."""
    return json.dumps(call.arguments, ensure_ascii=False)


def _string_leaves(obj, out: list[str]) -> None:
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _string_leaves(k, out)
            _string_leaves(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _string_leaves(v, out)


def render_calls_artefact(calls: Sequence[ToolCall]) -> bytes:
    """Content of the labelled artefact that stands for an assistant message's tool calls: one JSON
    line per call (what the wire carries), then every string inside the arguments as raw text, so
    scanners see line starts and quotes exactly as written rather than JSON-escaped."""
    lines = [json.dumps({"id": c.id, "name": c.name, "arguments": c.arguments}, ensure_ascii=False)
             for c in calls]
    leaves: list[str] = []
    for c in calls:
        _string_leaves(c.arguments, leaves)
    return "\n".join(lines + leaves).encode("utf-8")


def make_tool_call(index: int, call_id, name, arguments, seen: set[str]) -> ToolCall:
    """Normalise one call. Odd ids become ``call_<n>``; unparseable arguments or an odd name become
    a ToolCall named "__invalid__" whose arguments hold the raw text (capped)."""
    cid = call_id if isinstance(call_id, str) and _ID_OK.fullmatch(call_id) and call_id not in seen \
        else f"call_{index}"
    while cid in seen:
        cid += "_"
    seen.add(cid)
    if not (isinstance(name, str) and _ID_OK.fullmatch(name)) or name == INVALID_TOOL:
        raw = arguments if isinstance(arguments, str) else json.dumps(arguments, default=str)
        return ToolCall(cid, INVALID_TOOL, {"raw": str(raw)[:_RAW_CAP]})
    if isinstance(arguments, str):
        if not arguments.strip():
            parsed = {}
        else:
            try:
                parsed = json.loads(arguments)
            except ValueError:
                return ToolCall(cid, INVALID_TOOL, {"raw": arguments[:_RAW_CAP]})
    else:
        parsed = arguments
    if not isinstance(parsed, dict):
        return ToolCall(cid, INVALID_TOOL, {"raw": json.dumps(parsed, default=str)[:_RAW_CAP]})
    return ToolCall(cid, name, parsed)


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

    def render_tools(self, specs: Sequence[dict]) -> list[dict]:
        """Neutral specs {"name","description","parameters"} -> this provider's ``tools`` shape."""
        raise NotImplementedError

    def normalise_tool_calls(self, reply: ModelReply) -> tuple[ToolCall, ...]:
        """The reply's tool calls as ToolCall objects (see ``make_tool_call``)."""
        raise NotImplementedError

    def _args_in_body(self, call: ToolCall, body_text: str) -> bool:
        """Is the call's argument text JSON-encoded in the request body? Default: the arguments
        travel as a JSON *string* (OpenAI style), so they appear escaped."""
        return _body_contains(body_text, args_text(call))

    # -- shared flow
    def complete(self, messages: Sequence[Message], *, max_tokens: int, task_id: str | None = None,
                 purpose: str = "implement", tools: Sequence = (), **options) -> ModelReply:
        messages = list(messages)
        self._validate(messages)
        body = self._build(messages, max_tokens, tools, options)
        self._check_body(messages, body)
        price = self.ledger.prices[self.tier]
        self.ledger.check(task_id=task_id, tier=self.tier,
                          estimate_usd=estimate_usd(price, len(body), max_tokens))
        segments: list[Segment] = []
        for m in messages:
            segments.append(Segment(m.artefact_id, m.data))
            if m.tool_calls:
                segments.append(Segment(calls_artefact_id(m), render_calls_artefact(m.tool_calls)))
        result = self.gateway.send(self.destination_name, segments,
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
    def _validate(messages: Sequence[Message]) -> None:
        for m in messages:
            if m.tool_calls and m.role != "assistant":
                raise ProviderError("tool calls on a message that is not from the assistant")
            if m.role == "tool" and not _ID_OK.fullmatch(m.tool_call_id or ""):
                raise ProviderError("tool message without a valid tool call id")
            if m.role != "tool" and m.tool_call_id:
                raise ProviderError("tool call id on a message that is not a tool result")

    def _check_body(self, messages: Sequence[Message], body: bytes) -> None:
        try:
            text = body.decode("utf-8")
            ok = True
            for m in messages:
                ok = ok and _body_contains(text, m.data.decode("utf-8"))
                if m.tool_call_id:
                    ok = ok and _body_contains(text, m.tool_call_id)
                for c in m.tool_calls:
                    ok = ok and (_body_contains(text, c.id) and _body_contains(text, c.name)
                                 and self._args_in_body(c, text))
        except UnicodeError:
            ok = False
        if not ok:
            raise ProviderError("request body does not match inspected segments")
