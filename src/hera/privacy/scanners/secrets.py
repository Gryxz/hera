"""Secret scanner: known token formats, generic assignments, high-entropy tokens, one decode level.

Findings are names only ("secret:aws_access_key"); matched text is never returned.
Honest limits: secrets that are short, reversed, split across string pieces, ROT13'd, hex-encoded
or compressed are not found; random hex secrets of 32/40/64 characters look like hashes and pass.
"""
from __future__ import annotations
import base64
import binascii
import math
import re
from collections import Counter
from ._common import to_text, unique

_FORMATS: tuple[tuple[str, re.Pattern], ...] = tuple((n, re.compile(p)) for n, p in (
    ("aws_access_key", r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    ("github_token", r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    ("slack_token", r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("stripe_key", r"(?<![A-Za-z0-9_])[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("google_api_key", r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}"),
    ("anthropic_key", r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{20,}"),
    ("openai_key", r"(?<![A-Za-z0-9])sk-(?!ant-)[A-Za-z0-9_-]{20,}"),
    ("jwt", r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{10,}"),
    ("private_key", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
))

_ASSIGN = re.compile(
    r"""(?i)(?:password|passwd|secret|token|api[_-]?key)[\w-]*["']?\s*[:=]\s*["']?([^\s"']{8,})""")
_PLACEHOLDER_PREFIXES = ("your_", "your-", "xxx", "<", "${", "{{", "changeme", "example")
_ACCESSORS = ("self.", "os.environ", "process.env", "os.getenv", "getenv", "config.", "settings.")
_DOTTED_IDENT = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+$")

_INTEGRITY = re.compile(r"\bsha(?:256|384|512)-[A-Za-z0-9+/]+={0,2}")
_ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9+/_-]{32,}={0,2}")      # "=" only as padding
_HASH_HEX = re.compile(r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{56}|[0-9a-fA-F]{64}"
                       r"|[0-9a-fA-F]{96}|[0-9a-fA-F]{128})$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ENTROPY_MIN_BITS = 4.0

_B64_TOKEN = re.compile(r"[A-Za-z0-9+/_-]{24,}={0,2}")
_PCT = re.compile(r"%([0-9A-Fa-f]{2})")
_UESC = re.compile(r"\\u([0-9A-Fa-f]{4})")
_XESC = re.compile(r"\\x([0-9A-Fa-f]{2})")


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _is_placeholder(value: str) -> bool:
    v = value.strip().strip(",;)").lower()
    return (not v or len(set(v)) == 1 or v.startswith(_PLACEHOLDER_PREFIXES))


def _scan_text(text: str) -> list[str]:
    found: list[str] = []
    for name, pat in _FORMATS:
        if pat.search(text):
            found.append(f"secret:{name}")
    for m in _ASSIGN.finditer(text):
        value = m.group(1).rstrip(",;)")
        if len(value) < 8 or _is_placeholder(value):
            continue
        if "(" in value or value.startswith(_ACCESSORS) or _DOTTED_IDENT.match(value):
            continue                      # code reading a value, not a literal secret
        found.append("secret:generic_assignment")
        break
    for tok in _ENTROPY_TOKEN.findall(_INTEGRITY.sub(" ", text)):
        if _HASH_HEX.match(tok) or _UUID.match(tok):
            continue
        if not (any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok)):
            continue
        if shannon_entropy(tok) >= ENTROPY_MIN_BITS:
            found.append("secret:high_entropy")
            break
    return found


def _printable_ratio(b: bytes) -> float:
    if not b:
        return 0.0
    return sum(1 for x in b if 32 <= x < 127 or x in (9, 10, 13)) / len(b)


def _base64_decoded(text: str) -> list[str]:
    out = []
    for m in _B64_TOKEN.finditer(text):
        tok = m.group(0)
        if len(tok) % 4:
            continue
        try:
            raw = (base64.urlsafe_b64decode(tok) if ("-" in tok or "_" in tok)
                   else base64.b64decode(tok, validate=True))
        except (binascii.Error, ValueError):
            continue
        if _printable_ratio(raw) >= 0.8:
            out.append(raw.decode("ascii", errors="replace"))
    return out


def _unescaped(text: str) -> str:
    text = _PCT.sub(lambda m: chr(int(m.group(1), 16)), text)
    text = _UESC.sub(lambda m: chr(int(m.group(1), 16)), text)
    return _XESC.sub(lambda m: chr(int(m.group(1), 16)), text)


def scan_secrets(data: bytes) -> list[str]:
    text = to_text(data)
    found = _scan_text(text)
    layers = _base64_decoded(text)
    unescaped = _unescaped(text)
    if unescaped != text:
        layers.append(unescaped)
    for layer in layers:
        found.extend(f"{n} (decoded)" for n in _scan_text(layer.translate(
            {ord(c): None for c in "​‌‍﻿"})))
    return unique(found)
