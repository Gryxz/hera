"""Deny-list terms from the local policy. Findings carry the term's position and action only;
the term text never appears in a finding, a log row or a block notice.

Scope: v0 scanners see bytes only, so an entry's optional `scope` is not evaluated (the term
applies everywhere, which can only over-block). Unknown actions are treated as "block" and
unknown match modes as "fuzzy", the broadest, so a typo in policy fails closed.
"""
from __future__ import annotations
import re
from typing import Callable
from ._common import to_text

MIN_TERM_LENGTH = 3
ACTIONS = ("block", "review", "signal")
_LEET = {"0": "o", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s",
         "1": "i", "l": "i", "|": "i", "!": "i"}        # 1/l/i are ambiguous: fold all to "i"
_HOMOGLYPHS = {
    # Cyrillic
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j",
    "ѕ": "s", "һ": "h", "к": "k", "м": "m", "т": "t", "в": "b", "н": "h",
    # Greek
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t",
    "υ": "u", "χ": "x", "η": "n", "μ": "u",
}
_FOLD = {ord(k): v for k, v in {**_HOMOGLYPHS, **_LEET}.items()}
_SEP_BETWEEN_LETTERS = re.compile(r"(?<=[a-z])[ .\-_]+(?=[a-z])")


def normalise(text: str) -> str:
    """Fuzzy form: zero-width stripped, lower-cased, homoglyphs and leetspeak folded,
    separators between letters removed."""
    text = to_text(text.encode("utf-8")).lower().translate(_FOLD)
    prev = None
    while prev != text:                       # "a . b" needs more than one pass
        prev, text = text, _SEP_BETWEEN_LETTERS.sub("", text)
    return text


def make_term_scanner(terms: list[dict]) -> Callable[[bytes], list[str]]:
    matchers: list[tuple[str, Callable[[str, str], bool]]] = []     # (finding, test(text, norm))
    skipped: list[int] = []
    for i, entry in enumerate(terms or []):
        term = str(entry.get("term", ""))
        action = str(entry.get("action", "block")).lower()
        action = action if action in ACTIONS else "block"
        mode = str(entry.get("match", "case_insensitive")).lower()
        if len(term.strip()) < MIN_TERM_LENGTH:
            skipped.append(i)
            continue
        name = f"term:{action}#{i}"
        if mode == "exact":
            test = (lambda t, term=term: term in t)
        elif mode == "case_insensitive":
            test = (lambda t, low=term.lower(): low in t.lower())
        elif mode == "word":
            pat = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)
            test = (lambda t, pat=pat: pat.search(t) is not None)
        else:                                  # "fuzzy" and anything unrecognised
            norm = normalise(term)
            if len(norm) < MIN_TERM_LENGTH:
                skipped.append(i)
                continue
            test = (lambda t, norm=norm: norm in normalise(t))
        matchers.append((name, test))

    def scan_terms(data: bytes) -> list[str]:
        text = to_text(data)
        return [name for name, test in matchers if test(text)]

    scan_terms.skipped = skipped                # indexes only, never term text
    return scan_terms
