"""Content sniffing for data files, independent of file name or extension."""
from __future__ import annotations
import csv
import io
import json
import re
from ._common import unique

PERSONAL_KEYS = ("name", "first_name", "last_name", "email", "phone", "address", "city",
                 "postal", "zip", "order", "customer")
_INSERT = re.compile(r"(?im)^\s*INSERT\s+INTO\b")
_CREATE = re.compile(r"(?im)^\s*CREATE\s+TABLE\b")
_DUMP_HEADER = re.compile(r"(?im)^--\s*(?:PostgreSQL database dump|MySQL dump|MariaDB dump|"
                          r"Dumping data for table)")
_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Z][A-Z0-9_]*)=\S.*$")
_ENV_SENSITIVE = ("KEY", "SECRET", "TOKEN", "PASSWORD", "DATABASE_URL")


def _personal(keys) -> bool:
    lowered = [str(k).lower() for k in keys]
    return sum(1 for w in PERSONAL_KEYS if any(w in k for k in lowered)) >= 2


def _tabular_personal(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped:
        return False
    try:
        if stripped[0] == "[":
            rows = json.loads(stripped)
            if isinstance(rows, list):
                return any(isinstance(r, dict) and _personal(r) for r in rows[:5])
        elif stripped[0] == "{":
            lines = [l for l in stripped.splitlines() if l.strip()][:5]
            parsed = [json.loads(l) for l in lines]
            if parsed and all(isinstance(p, dict) for p in parsed):
                return any(_personal(p) for p in parsed)
            return False
    except (ValueError, RecursionError):
        pass
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2 or len(lines[0]) > 1000:
        return False
    try:
        dialect = csv.Sniffer().sniff(lines[0], delimiters=",;\t|")
        header = next(csv.reader(io.StringIO(lines[0]), dialect))
    except csv.Error:
        header = lines[0].split(",")
    return len(header) >= 2 and _personal(h.strip().strip('"') for h in header)


def scan_structured(data: bytes) -> list[str]:
    found: list[str] = []
    text = data[:2_000_000].decode("utf-8", errors="replace")
    if data.startswith(b"SQLite format 3\x00"):
        found.append("data:sqlite")
    inserts = len(_INSERT.findall(text))
    if inserts >= 3 or (inserts >= 1 and _CREATE.search(text)) or _DUMP_HEADER.search(text):
        found.append("data:sql_dump")
    if data.startswith(b"PK\x03\x04") or data.startswith(b"\x1f\x8b") or data[257:262] == b"ustar":
        found.append("data:archive")
    env = [m.group(1) for l in text.splitlines() if (m := _ENV_LINE.match(l.strip()))]
    if len(env) >= 3 and any(w in k for k in env for w in _ENV_SENSITIVE):
        found.append("data:env_file")
    if _tabular_personal(text):
        found.append("data:tabular_personal")
    return unique(found)
