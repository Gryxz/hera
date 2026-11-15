"""Replaceable boundaries. One in-process implementation of each is enough for milestone 0.1;
network implementations come later without changing callers."""
from __future__ import annotations
from typing import Iterable, Protocol, Sequence
from .types import CheckResult, Grant, Label, ModelReply, Receipt, Verdict


class ModelProvider(Protocol):
    """A local model, a remote API, a friend's endpoint, or a model pooled across machines."""
    name: str            # destination name used by the privacy gate, e.g. "local", "provider-a"
    tier: str            # "local" | "cheap" | "default" | "strong"

    def complete(self, messages: Sequence[dict], *, max_tokens: int, tools: Sequence[dict] = (),
                 **options) -> ModelReply: ...


class CapabilityProvider(Protocol):
    def probe(self) -> dict: ...            # measured: OS, arch, memory, GPU, runtimes


class ArtifactStore(Protocol):
    def put(self, data: bytes) -> str: ...  # returns content hash
    def get(self, content_hash: str) -> bytes: ...


class WorkerRuntime(Protocol):
    def run(self, argv: Sequence[str], *, cwd_artifact: str, limits: dict) -> dict: ...


class VerificationAdapter(Protocol):
    """One per ecosystem (pytest first, then vitest/jest)."""
    name: str

    def detect(self, repo_root) -> float: ...
    def protected_paths(self, repo_root) -> list[str]: ...
    def collect(self, checkset_dir) -> list[str]: ...
    def run(self, checkset_dir, worktree, limits: dict) -> list[CheckResult]: ...


class Oracle(Protocol):
    def verify(self, task_id: str, candidate_commit: str) -> Receipt: ...
    def verdict(self, receipt: Receipt) -> Verdict: ...


class PrivacyGate(Protocol):
    """Runs before the router. Decides from labels only; no model is in this path."""
    def permitted_destinations(self, labels: Iterable[Label], grants: Iterable[Grant],
                               destinations: Iterable[str], now: float) -> set[str]: ...


class EgressGateway(Protocol):
    """The only component allowed to open a network connection or hold an API key."""
    def send(self, destination_name: str, segments: Sequence, *, purpose: str,
             task_id: str | None = None, envelope=None): ...
