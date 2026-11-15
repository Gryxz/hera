"""Artefact labels: repository class from policy, content hash, and derivation."""
from __future__ import annotations
import hashlib
import json
import sqlite3
from ..types import Label, RepoClass
from .policy import Policy

_STRICTNESS = {RepoClass.PUBLIC: 0, RepoClass.PERSONAL: 1,
               RepoClass.PROPRIETARY: 2, RepoClass.SENSITIVE: 3}


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LabelStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def put(self, label: Label) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO labels(artefact_id,repo_class,content_hash,findings_json,"
            "derived_from_json) VALUES(?,?,?,?,?)",
            (label.artefact_id, label.repo_class.value if label.repo_class else None,
             label.content_hash, json.dumps(list(label.restricted_findings)),
             json.dumps(list(label.derived_from))))

    def get(self, artefact_id: str) -> Label | None:
        row = self.conn.execute("SELECT * FROM labels WHERE artefact_id=?", (artefact_id,)).fetchone()
        if row is None:
            return None
        return Label(artefact_id=row["artefact_id"],
                     repo_class=RepoClass(row["repo_class"]) if row["repo_class"] else None,
                     content_hash=row["content_hash"] or "",
                     restricted_findings=tuple(json.loads(row["findings_json"] or "[]")),
                     derived_from=tuple(json.loads(row["derived_from_json"] or "[]")))


def label_bytes(store: LabelStore, policy: Policy, *, artefact_id: str, repo_id: str,
                data: bytes, findings=()) -> Label:
    label = Label(artefact_id, policy.repo_class(repo_id), content_hash(data), tuple(findings))
    store.put(label)
    return label


def derive(store: LabelStore, *, artefact_id: str, inputs: list[str], data: bytes) -> Label:
    """Strictest input class, union of findings. Unknown or no inputs: unlabelled (blocked)."""
    parents = [store.get(i) for i in inputs]
    if not parents or any(p is None or p.repo_class is None for p in parents):
        cls = None
        findings: tuple[str, ...] = ()
        if parents:
            findings = tuple(sorted({f for p in parents if p for f in p.restricted_findings}))
    else:
        cls = max((p.repo_class for p in parents if p and p.repo_class), key=_STRICTNESS.__getitem__)
        findings = tuple(sorted({f for p in parents if p for f in p.restricted_findings}))
    label = Label(artefact_id, cls, content_hash(data), findings, tuple(inputs))
    store.put(label)
    return label


def label_builtin(store: LabelStore, *, artefact_id: str, data: bytes) -> Label:
    """Label HERA's own static prompt text as public, with no findings.

    Only for prompt templates shipped in this repository. Never use it for anything read from a
    user repository or produced by a model: those must be labelled from policy or derived."""
    label = Label(artefact_id, RepoClass.PUBLIC, content_hash(data), (), ("hera-builtin",))
    store.put(label)
    return label
