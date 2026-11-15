"""Scanners v0: a second line of defence behind default-deny classification.

A scanner is a callable `bytes -> list[str]` that returns finding names only. See docs/SCANNERS.md
for what each one detects and what it is known to miss.
"""
from __future__ import annotations
from .pii import field_combination, scan_pii, scan_pii_combined
from .secrets import scan_secrets
from .structured import scan_structured
from .terms import make_term_scanner

__all__ = ["BLOCKING", "default_scanners", "field_combination", "make_term_scanner",
           "scan_pii", "scan_pii_combined", "scan_secrets", "scan_structured"]


def default_scanners(policy=None) -> tuple:
    scanners = [scan_secrets, scan_pii_combined, scan_structured]
    terms = getattr(policy, "terms", None)
    if terms:
        scanners.append(make_term_scanner(terms))
    return tuple(scanners)


def BLOCKING(findings: list[str]) -> list[str]:
    """Which of one payload's findings block. Pass as Gateway(blocking=BLOCKING).

    Everything blocks (including "term:review", which the gateway reports as held for user
    review) except "term:signal" on its own; a signal together with any personal-data finding
    blocks too."""
    findings = list(findings)
    if any(f.startswith("pii:") for f in findings):
        return findings
    return [f for f in findings if not f.startswith("term:signal")]
