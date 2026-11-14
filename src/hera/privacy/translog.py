"""Hash-chained transmission log. Records metadata and a payload hash, never the payload.
The chain makes silent edits or deletions detectable; it is not proof against someone
who controls the machine."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import time

GENESIS = "0" * 64
_FIELDS = ("at", "destination", "purpose", "task_id", "artefacts_json", "bytes",
           "grant_ids_json", "scanner_json", "decision", "reason", "payload_hash")


def _digest(prev_hash: str, rec: dict) -> str:
    body = json.dumps([rec[f] for f in _FIELDS], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


def append(conn: sqlite3.Connection, *, destination: str, purpose: str, decision: str,
           payload: bytes, task_id: str | None = None, artefacts: list | None = None,
           grant_ids: list | None = None, scanner: dict | None = None, reason: str = "",
           at: float | None = None) -> str:
    if decision not in ("sent", "blocked"):
        raise ValueError("decision must be 'sent' or 'blocked'")
    rec = {"at": time.time() if at is None else at, "destination": destination,
           "purpose": purpose, "task_id": task_id,
           "artefacts_json": json.dumps(artefacts or [], sort_keys=True),
           "bytes": len(payload), "grant_ids_json": json.dumps(grant_ids or []),
           "scanner_json": json.dumps(scanner or {}, sort_keys=True),
           "decision": decision, "reason": reason,
           "payload_hash": hashlib.sha256(payload).hexdigest()}
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT this_hash FROM transmissions ORDER BY seq DESC LIMIT 1").fetchone()
        prev = row[0] if row else GENESIS
        this = _digest(prev, rec)
        conn.execute(
            "INSERT INTO transmissions(at,destination,purpose,task_id,artefacts_json,bytes,"
            "grant_ids_json,scanner_json,decision,reason,payload_hash,prev_hash,this_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(rec[f] for f in _FIELDS) + (prev, this))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return this


def verify(conn: sqlite3.Connection) -> tuple[bool, int | None]:
    """Return (ok, first_bad_seq)."""
    prev = GENESIS
    for row in conn.execute("SELECT * FROM transmissions ORDER BY seq"):
        rec = {f: row[f] for f in _FIELDS}
        if row["prev_hash"] != prev or row["this_hash"] != _digest(prev, rec):
            return False, row["seq"]
        prev = row["this_hash"]
    return True, None
