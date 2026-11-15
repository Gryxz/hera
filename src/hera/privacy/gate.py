"""Privacy gate: default-deny, decided from labels and grants only, before any routing.

Rules (architecture §P):
- "local" is always permitted.
- An unlabelled artefact, or one with any restricted-data finding, permits nothing remote.
- public        → every configured destination.
- personal / proprietary → only destinations named by an unexpired grant bound to the
                           artefact's exact content hash.
- sensitive     → nothing remote, whatever ordinary grants exist. Only a grant marked
                  `declassified` (the user read and approved that exact sanitized text)
                  can release a sensitive-origin artefact, and only that exact content.
- A payload's permitted set is the intersection over all its artefacts.
"""
from __future__ import annotations
from typing import Iterable
from ..types import Grant, Label, RepoClass

LOCAL = "local"


def _for_label(label: Label, grants: list[Grant], destinations: set[str], now: float) -> set[str]:
    if label.repo_class is None or label.restricted_findings:
        return set()
    if label.repo_class is RepoClass.PUBLIC:
        return set(destinations)
    allowed: set[str] = set()
    sensitive = label.repo_class is RepoClass.SENSITIVE
    if label.content_hash:
        for g in grants:
            if sensitive and not g.declassified:
                continue                 # an ordinary grant never releases sensitive-origin content
            if g.expires_at > now and label.content_hash in g.content_hashes:
                allowed |= set(g.destinations)
    return allowed & destinations


def permitted_destinations(labels: Iterable[Label], grants: Iterable[Grant],
                           destinations: Iterable[str], now: float) -> set[str]:
    labels = list(labels)
    remote = set(destinations) - {LOCAL}
    if not labels:                       # nothing labelled → nothing may leave
        return {LOCAL}
    grants = list(grants)
    result = set(remote)
    for label in labels:
        result &= _for_label(label, grants, remote, now)
        if not result:
            break
    return result | {LOCAL}
