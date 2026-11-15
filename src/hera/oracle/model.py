"""Check objects of the acceptance oracle (S7a).

One check is one pytest test function. A draft check belongs to exactly one file of the check
set; a file holds checks of one visibility only (visible files may be shown to the implementer,
hidden files never are). Validation errors name the offending field and never echo file
contents.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..types import CheckResult

_REQ_ID = re.compile(r"R-[0-9]{1,4}")
_CHECK_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_NODE = re.compile(r"test_[A-Za-z0-9_]*")
_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
VISIBILITIES = ("visible", "hidden")
KINDS = ("new", "regression")


def validate_relpath(relpath: str) -> str:
    """Return ``relpath`` if it is a safe POSIX path of a ``test_*.py`` file, else ValueError."""
    if not isinstance(relpath, str) or not relpath or "\\" in relpath or "\0" in relpath:
        raise ValueError("relpath: must be a non-empty POSIX path")
    if relpath.startswith("/") or PurePosixPath(relpath).is_absolute():
        raise ValueError("relpath: must be relative")
    parts = relpath.split("/")
    for p in parts:
        if p in ("", ".", "..") or not _SEGMENT.fullmatch(p):
            raise ValueError("relpath: contains an empty, dot, parent or unsupported segment")
    name = parts[-1]
    if not (name.startswith("test_") and name.endswith(".py") and len(name) > len("test_.py")):
        raise ValueError("relpath: file name must match test_*.py")
    return relpath


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str

    def __post_init__(self):
        if not isinstance(self.id, str) or not _REQ_ID.fullmatch(self.id):
            raise ValueError("Requirement.id: must match R-[0-9]{1,4}")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Requirement.text: must be a non-empty string")


@dataclass(frozen=True)
class DraftCheck:
    check_id: str
    requirement_ids: tuple[str, ...]
    visibility: str
    relpath: str
    node: str
    author_role: str = "visible"
    author_model: str = ""
    kind: str = "new"

    def __post_init__(self):
        if isinstance(self.requirement_ids, (list, set, frozenset)):
            object.__setattr__(self, "requirement_ids", tuple(self.requirement_ids))
        if not isinstance(self.check_id, str) or not _CHECK_ID.fullmatch(self.check_id):
            raise ValueError("DraftCheck.check_id: must match [A-Za-z0-9_.-]{1,80}")
        if not isinstance(self.requirement_ids, tuple) or not self.requirement_ids or \
                not all(isinstance(r, str) and _REQ_ID.fullmatch(r) for r in self.requirement_ids):
            raise ValueError("DraftCheck.requirement_ids: must be a non-empty tuple of R-<n> ids")
        if len(set(self.requirement_ids)) != len(self.requirement_ids):
            raise ValueError("DraftCheck.requirement_ids: duplicate id")
        if self.visibility not in VISIBILITIES:
            raise ValueError("DraftCheck.visibility: must be 'visible' or 'hidden'")
        validate_relpath(self.relpath)
        if not isinstance(self.node, str) or not _NODE.fullmatch(self.node):
            raise ValueError("DraftCheck.node: must be a test function name (test_...)")
        if self.author_role not in VISIBILITIES:
            raise ValueError("DraftCheck.author_role: must be 'visible' or 'hidden'")
        if not isinstance(self.author_model, str):
            raise ValueError("DraftCheck.author_model: must be a string")
        if self.kind not in KINDS:
            raise ValueError("DraftCheck.kind: must be 'new' or 'regression'")


@dataclass(frozen=True)
class CheckSet:
    hash: str
    task_id: str
    requirements: tuple[Requirement, ...]
    checks: tuple[DraftCheck, ...]
    files: dict[str, str]            # relpath -> sha256 of the file
    adapter: str
    root: Path                       # <checks_dir>/<task_id>/<hash>
    uncovered: tuple[str, ...] = ()  # requirement ids no check covers

    @property
    def check_ids(self) -> frozenset[str]:
        return frozenset(c.check_id for c in self.checks)


def validate_draft(requirements, checks) -> None:
    """Cross-object validation shared by freeze and the tests. Raises ValueError (field names
    only, no file contents)."""
    rids = [r.id for r in requirements]
    if len(set(rids)) != len(rids):
        raise ValueError("requirements: duplicate requirement id")
    cids = [c.check_id for c in checks]
    if len(set(cids)) != len(cids):
        raise ValueError("checks: duplicate check_id")
    known = set(rids)
    seen_nodes: set[tuple[str, str]] = set()
    vis_of_file: dict[str, str] = {}
    for c in checks:
        if not set(c.requirement_ids) <= known:
            raise ValueError("checks.requirement_ids: refers to an undefined requirement")
        key = (c.relpath, c.node)
        if key in seen_nodes:
            raise ValueError("checks: two checks name the same function in the same file")
        seen_nodes.add(key)
        if vis_of_file.setdefault(c.relpath, c.visibility) != c.visibility:
            raise ValueError("checks.visibility: a file may not mix visible and hidden checks")
    mods: dict[str, str] = {}
    for rel in vis_of_file:
        m = module_name(rel)
        if mods.setdefault(m, rel) != rel:
            raise ValueError("checks.relpath: two files map to the same module name")


def module_name(relpath: str) -> str:
    """Dotted module name pytest reports for a check file (path without ``.py``)."""
    return relpath[: -len(".py")].replace("/", ".")


@dataclass(frozen=True)
class PrepareReport:
    """What ``prepare`` changed in a candidate checkout. S7b turns these into audit findings.

    restored: protected base paths written back (changed or missing in the candidate).
    deleted: protected-path files the candidate added (not present at base), plus any
    pre-existing ``_hera_checks`` / ``_hera_results``.
    modified_protected: base paths that were present but whose content (or type) differed.
    """
    restored: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    modified_protected: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunReport:
    results: tuple[CheckResult, ...] = ()
    executed_ids: frozenset[str] = frozenset()
    sentinels_ok: bool = False
    exit_code_consistent: bool = False
    returncode: int | None = None
    timed_out: bool = False
    raw_unmatched: tuple[str, ...] = ()
    results_parsed: bool = False
    # check_id -> failure text (capped). Internal: never hand hidden entries to an implementer;
    # use PytestAdapter.feedback_for_implementer.
    failure_texts: dict[str, str] = field(default_factory=dict)
