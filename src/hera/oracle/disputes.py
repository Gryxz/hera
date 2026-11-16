"""Disputes: the check, not the candidate, is what is wrong.

An open dispute pauses verdicts for its task (``ORACLE_DISPUTED``). Only a VISIBLE check may be
disputed, because an implementer never knows hidden check ids; asking about a hidden or unknown
id gives the same error, so the error does not reveal whether the id exists. A dispute is
resolved by someone independent of the check's author and of the implementer.
"""
from __future__ import annotations

import secrets
import sqlite3
import time

from . import checkstore

ARGUMENT_MAX_CHARS = 4000
OUTCOMES = ("upheld", "check_amended", "requirement_amended")
_NOT_DISPUTABLE = "no such visible check in this check set"


def open_dispute(conn: sqlite3.Connection, *, task_id: str, checkset_hash: str, check_id: str,
                 requirement_id: str, argument: str) -> str:
    if not isinstance(argument, str) or not argument.strip():
        raise ValueError("argument: must be a non-empty string")
    row = conn.execute(
        "SELECT c.requirement_id AS reqs FROM checks c JOIN checksets s ON s.hash = c.checkset_hash"
        " WHERE c.id=? AND c.checkset_hash=? AND c.visibility='visible' AND s.task_id=?",
        (check_id, checkset_hash, task_id)).fetchone()
    if row is None or requirement_id not in row["reqs"].split(","):
        raise ValueError(_NOT_DISPUTABLE)          # same text for hidden, unknown and mismatched
    dispute_id = "disp-" + secrets.token_hex(8)
    conn.execute("INSERT INTO disputes(id, task_id, checkset_hash, check_id, requirement_id, argument, status,"
                 " opened_at) VALUES (?,?,?,?,?,?,'open',?)",
                 (dispute_id, task_id, checkset_hash, check_id, requirement_id,
                  argument[:ARGUMENT_MAX_CHARS], time.time()))
    return dispute_id


def has_open_dispute(conn: sqlite3.Connection, task_id: str) -> bool:
    return conn.execute("SELECT 1 FROM disputes WHERE task_id=? AND status='open' LIMIT 1",
                        (task_id,)).fetchone() is not None


def resolve_dispute(conn: sqlite3.Connection, dispute_id: str, *, outcome: str, resolver: str,
                    implementer_model: str, new_checkset_hash: str | None = None) -> None:
    """Close a dispute. ``resolver`` must differ from the check's author model and from
    ``implementer_model`` (the model recorded as implementing the task). ``check_amended``
    needs ``new_checkset_hash`` and records it with ``checkstore.supersede``;
    ``requirement_amended`` needs a human resolver ("human:...")."""
    if outcome not in OUTCOMES:
        raise ValueError("outcome: must be one of " + ", ".join(OUTCOMES))
    if not isinstance(resolver, str) or not resolver.strip():
        raise ValueError("resolver: must be a non-empty string")
    row = conn.execute("SELECT * FROM disputes WHERE id=?", (dispute_id,)).fetchone()
    if row is None:
        raise KeyError("unknown dispute")
    if row["status"] != "open":
        raise ValueError("dispute is already resolved")
    author = conn.execute("SELECT author_model FROM checks WHERE id=? AND checkset_hash=?",
                          (row["check_id"], row["checkset_hash"])).fetchone()
    author_model = (author["author_model"] if author else "") or ""
    norm = resolver.strip().casefold()
    for who in (author_model, implementer_model or ""):
        if who.strip() and norm == who.strip().casefold():
            raise ValueError("resolver: must be independent of the check author and the implementer")
    if outcome == "requirement_amended" and not resolver.startswith("human:"):
        raise ValueError("resolver: a requirement amendment needs a human resolver ('human:...')")
    if outcome == "check_amended" and not new_checkset_hash:
        raise ValueError("new_checkset_hash: required when the check is amended")
    if outcome != "check_amended":
        new_checkset_hash = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        if new_checkset_hash:
            checkstore.supersede(conn, row["checkset_hash"], new_checkset_hash)
        conn.execute("UPDATE disputes SET status='resolved', outcome=?, resolver=?, new_checkset_hash=?,"
                     " resolved_at=? WHERE id=? AND status='open'",
                     (outcome, resolver, new_checkset_hash, time.time(), dispute_id))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
