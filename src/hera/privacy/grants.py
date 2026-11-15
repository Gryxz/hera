"""Approval store. Grants are created only by a local user command, never from a model tool call:
nothing in this module may be exposed as, or called from, a tool available to a model."""
from __future__ import annotations
import json
import sqlite3
import time
import uuid
from ..types import Grant


class GrantStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(self, *, content_hashes, destinations, purpose: str, expires_at: float,
               grant_id: str | None = None, declassified: bool = False) -> Grant:
        grant = Grant(grant_id or str(uuid.uuid4()), frozenset(content_hashes),
                      frozenset(destinations), purpose, float(expires_at), bool(declassified))
        self.conn.execute(
            "INSERT INTO grants(id,content_hashes_json,destinations_json,purpose,expires_at,created_at,"
            "declassified) VALUES(?,?,?,?,?,?,?)",
            (grant.grant_id, json.dumps(sorted(grant.content_hashes)),
             json.dumps(sorted(grant.destinations)), purpose, grant.expires_at, time.time(),
             int(grant.declassified)))
        return grant

    def all(self) -> list[Grant]:
        rows = self.conn.execute("SELECT * FROM grants ORDER BY created_at, id").fetchall()
        return [Grant(r["id"], frozenset(json.loads(r["content_hashes_json"])),
                      frozenset(json.loads(r["destinations_json"])), r["purpose"], r["expires_at"],
                      bool(r["declassified"]))
                for r in rows]

    def revoke(self, grant_id: str) -> None:
        self.conn.execute("DELETE FROM grants WHERE id=?", (grant_id,))
