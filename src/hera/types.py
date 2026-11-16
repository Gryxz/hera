from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    OVERFIT = "OVERFIT"
    REGRESSION = "REGRESSION"
    TAMPER = "TAMPER"
    UNVERIFIABLE = "UNVERIFIABLE"
    ORACLE_DISPUTED = "ORACLE_DISPUTED"


class Attribution(str, Enum):
    IMPL_WRONG = "IMPL_WRONG"
    IMPL_OVERFIT = "IMPL_OVERFIT"
    IMPL_TAMPER = "IMPL_TAMPER"
    CHECK_FLAKY = "CHECK_FLAKY"
    CHECK_INVALID = "CHECK_INVALID"
    CHECK_WEAK = "CHECK_WEAK"
    SPEC_AMBIGUOUS = "SPEC_AMBIGUOUS"
    ENV_FAILURE = "ENV_FAILURE"
    BUDGET_CENSORED = "BUDGET_CENSORED"
    UNKNOWN = "UNKNOWN"


class RepoClass(str, Enum):
    PUBLIC = "public"
    PERSONAL = "personal"
    PROPRIETARY = "proprietary"
    SENSITIVE = "sensitive"


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"
    TIMEOUT = "timeout"


class CheckHealth(str, Enum):
    VALID = "valid"
    FLAKY = "flaky"
    INVALID = "invalid"
    WEAK = "weak"


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    requirement_ids: tuple[str, ...]
    status: CheckStatus
    visibility: str = "visible"            # "visible" | "hidden"
    health: CheckHealth = CheckHealth.VALID
    duration_s: float = 0.0
    output_digest: str = ""


@dataclass(frozen=True)
class Receipt:
    """Evidence of one verification run. The verdict is a pure function of this."""
    task_id: str
    candidate_commit: str
    checkset_hash: str
    results: tuple[CheckResult, ...]
    manifest_ids: frozenset[str]
    audit_block_hits: tuple[str, ...] = ()
    audit_flags: tuple[str, ...] = ()
    sentinels_ok: bool = True
    exit_code_consistent: bool = True
    regressions: tuple[str, ...] = ()
    uncovered_requirements: tuple[str, ...] = ()
    dispute_open: bool = False


@dataclass(frozen=True)
class Label:
    """Sensitivity label of one artefact."""
    artefact_id: str
    repo_class: RepoClass | None            # None = unlabelled → blocked
    content_hash: str = ""
    restricted_findings: tuple[str, ...] = ()   # secrets, PII, dumps … (data-class scanners)
    derived_from: tuple[str, ...] = ()


@dataclass(frozen=True)
class Grant:
    """Explicit approval: scoped, hash-bound, destination-bound, expiring."""
    grant_id: str
    content_hashes: frozenset[str]
    destinations: frozenset[str]
    purpose: str
    expires_at: float                       # unix seconds
    declassified: bool = False              # user read and approved this exact text as sanitized


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class ModelReply:
    text: str
    usage: Usage = field(default_factory=Usage)
    model_id: str = ""
    tool_calls: tuple = ()
    usage_reported: bool = True


@dataclass(frozen=True)
class ToolCall:
    """A tool call the model asked for, normalised across wire formats. ``name`` is
    "__invalid__" when the model's arguments could not be parsed (raw text in arguments["raw"])."""
    id: str
    name: str
    arguments: dict
