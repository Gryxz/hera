"""Replaceable boundaries. One in-process implementation of each is enough for milestone 0.1;
network implementations come later without changing callers."""
from __future__ import annotations
from typing import Iterable, Protocol, Sequence
from .oracle.model import CheckSet, PrepareReport, RunReport
from .types import Grant, Label, ModelReply, Receipt, Verdict


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
    """One per ecosystem (pytest first, then vitest/jest). See oracle/adapters/."""
    name: str

    def detect(self, repo_root) -> float: ...
    def protected_globs(self) -> tuple[str, ...]: ...
    def is_protected(self, path: str) -> bool: ...
    def prepare(self, checkout, checkset: CheckSet, *, base_tree: dict[str, bytes],
                include_hidden: bool) -> PrepareReport: ...
    def command(self, checkout) -> list[str]: ...
    def run(self, checkout, checkset: CheckSet, *, include_hidden: bool = True, limits=None,
            allow_unsandboxed: bool = False) -> RunReport: ...
    def feedback_for_implementer(self, report: RunReport, checkset: CheckSet) -> list[dict]: ...


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
