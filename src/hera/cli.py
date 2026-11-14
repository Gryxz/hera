from __future__ import annotations
import argparse
import sys
from . import __version__, paths
from .privacy import translog
from .store import db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hera")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the local HERA home and state store")
    sub.add_parser("status", help="show spend and transmission-log integrity")
    a = ap.parse_args(argv)
    if a.cmd == "init":
        home = paths.ensure_layout()
        db.connect(paths.db_path()).close()
        print(f"initialised {home}")
        return 0
    if a.cmd == "status":
        if not paths.db_path().exists():
            print("not initialised; run `hera init`", file=sys.stderr)
            return 1
        conn = db.connect(paths.db_path())
        calls, usd = conn.execute("SELECT COUNT(*), COALESCE(SUM(usd),0) FROM model_calls").fetchone()
        sent = conn.execute("SELECT COUNT(*) FROM transmissions WHERE decision='sent'").fetchone()[0]
        blocked = conn.execute("SELECT COUNT(*) FROM transmissions WHERE decision='blocked'").fetchone()[0]
        ok, bad = translog.verify(conn)
        print(f"model calls: {calls}  spend: ${usd:.4f}")
        print(f"transmissions: {sent} sent, {blocked} blocked")
        print("transmission log chain: " + ("intact" if ok else f"BROKEN at seq {bad}"))
        return 0 if ok else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
