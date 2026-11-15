"""Personal-data shape scanner: email, phone, postal address, payment card, tracking and order ids.

Pattern shapes only: it cannot tell a real person's data from look-alikes, and it does not
read names. Findings are names ("pii:email"); matched text is never returned.
"""
from __future__ import annotations
import re
from ._common import to_text, unique

_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
_FILE_TLDS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "css", "js"}
_RESERVED_TLDS = {"invalid", "test", "example", "localhost"}


def _real_email_domain(domain: str) -> bool:
    d = domain.lower()
    labels = d.split(".")
    if labels[-1] in _FILE_TLDS or labels[-1] in _RESERVED_TLDS:
        return False
    if d in ("example.com", "example.org", "example.net") or d.endswith(
            (".example.com", ".example.org", ".example.net")):
        return False
    if "example" in labels or labels[0] == "test" or "localhost" in labels:
        return False
    return True


_PHONE_INTL = re.compile(r"(?<![\w+])\+\d[\d\s().-]{6,18}\d")
_PHONE_NA = re.compile(r"(?<![\d.+-])(?:\+?1[\s.-]+)?\(?[2-9]\d{2}\)?[\s.-]+\d{3}[\s.-]\d{4}(?![\d-])")


def _is_doc_phone(digits: str) -> bool:
    return len(digits) >= 7 and digits[-7:-4] == "555" and digits[-4:-2] == "01"


def _has_phone(text: str) -> bool:
    for m in _PHONE_INTL.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 8 <= len(digits) <= 15 and not _is_doc_phone(digits):
            return True
    for m in _PHONE_NA.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if not _is_doc_phone(digits):
            return True
    return False


_STREET_TYPES = (r"St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Way|"
                 r"Ct|Court|Crescent|Cres|Terrace|Place")
_STREET = re.compile(r"(?<![\w.])\d{1,6}[A-Za-z]?\s+(?:(?:[A-Z][A-Za-z.'-]*|\d+(?:st|nd|rd|th))\s+){1,3}"
                     rf"(?i:{_STREET_TYPES})\b")
_POSTAL = re.compile(r"(?<![A-Za-z0-9])(?:[ABCEGHJ-NPRSTVXY]\d[A-Z][ -]?\d[A-Z]\d"          # Canada
                     r"|[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}"                                   # UK
                     r"|\d{5}(?:-\d{4})?)(?![A-Za-z0-9])")                                  # US
_POSTAL_CONTEXT = re.compile(r"(?i)\b(?:address|ship(?:ping|ped|\s+to)?|postal|post\s*code|zip)\b")


def _has_address(text: str) -> bool:
    if _STREET.search(text):
        return True
    for m in _POSTAL.finditer(text):
        window = text[max(0, m.start() - 40):m.start()] + " " + text[m.end():m.end() + 40]
        if _POSTAL_CONTEXT.search(window):
            return True
    return False


_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_CARD_PREFIX = re.compile(r"4|5[1-5]|2[2-7]|3\d|6\d")


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _has_card(text: str) -> bool:
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if (13 <= len(digits) <= 19 and len(set(digits)) > 1 and _CARD_PREFIX.match(digits)
                and luhn_ok(digits)):
            return True
    return False


_UPS = re.compile(r"(?<![A-Za-z0-9])1Z[0-9A-Z]{16}(?![A-Za-z0-9])")
_USPS = re.compile(r"(?<!\d)9[2-5]\d{18,20}(?!\d)")
_FEDEX = re.compile(r"(?<!\d)(?:\d{15}|\d{12})(?!\d)")
_TRACK_WORD = re.compile(r"(?i)tracking|fedex|shipment")


def _has_tracking(text: str) -> bool:
    if _UPS.search(text) or _USPS.search(text):
        return True
    for m in _FEDEX.finditer(text):
        window = text[max(0, m.start() - 40):m.start()] + " " + text[m.end():m.end() + 40]
        if _TRACK_WORD.search(window):
            return True
    return False


_ORDER = re.compile(r"(?i)\b(?:order[ _]id|order\s+no\.?|order\s*#|order|invoice)(?![A-Za-z])"
                    r"[^A-Za-z0-9]{0,3}([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)")


def _has_order_id(text: str) -> bool:
    for m in _ORDER.finditer(text):
        ident = m.group(1)
        if len(re.sub(r"[^A-Za-z0-9]", "", ident)) >= 5 and any(c.isdigit() for c in ident):
            return True
    return False


def scan_pii(data: bytes) -> list[str]:
    text = to_text(data)
    found: list[str] = []
    if any(_real_email_domain(m.group(1)) for m in _EMAIL.finditer(text)):
        found.append("pii:email")
    if _has_phone(text):
        found.append("pii:phone")
    if _has_address(text):
        found.append("pii:postal_address")
    if _has_card(text):
        found.append("pii:payment_card")
    if _has_tracking(text):
        found.append("pii:tracking_number")
    if _has_order_id(text):
        found.append("pii:order_id")
    return unique(found)


def field_combination(findings: list[str]) -> list[str]:
    """Several weak personal-data signals together become one more finding."""
    kinds = {f for f in findings if f.startswith("pii:") and f != "pii:combination"}
    return ["pii:combination"] if len(kinds) >= 2 else []


def scan_pii_combined(data: bytes) -> list[str]:
    found = scan_pii(data)
    return found + field_combination(found)
