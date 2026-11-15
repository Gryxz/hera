"""Helpers shared by the scanners. Standard library only; no network imports."""
from __future__ import annotations

ZERO_WIDTH = "​‌‍﻿"
_ZW_TABLE = {ord(c): None for c in ZERO_WIDTH}


def to_text(data: bytes) -> str:
    """Decode leniently and strip zero-width characters, which are used to split matches."""
    return data.decode("utf-8", errors="replace").translate(_ZW_TABLE)


def unique(items) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return list(seen)
