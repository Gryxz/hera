"""Verification adapters: one per ecosystem (pytest first, then node:test)."""
from __future__ import annotations

ADAPTER_NAMES = ("pytest", "node")


def get_adapter(name: str):
    """A new adapter object for a check set's ``adapter`` field. ValueError for an unknown name."""
    if name == "pytest":
        from .pytest_adapter import PytestAdapter
        return PytestAdapter()
    if name == "node":
        from .node_adapter import NodeAdapter
        return NodeAdapter()
    raise ValueError(f"adapter: unknown adapter {name!r} (known: {', '.join(ADAPTER_NAMES)})")
