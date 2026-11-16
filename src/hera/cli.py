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
    v = sub.add_parser("verify", help="verify a candidate commit against a frozen check set")
    v.add_argument("--task", required=True)
    v.add_argument("--checkset", required=True)
    v.add_argument("--commit", required=True)
    v.add_argument("--no-hidden", action="store_true", help="run visible checks only")
    v.add_argument("--allow-unsandboxed", action="store_true",
                   help="run with NO network or filesystem isolation if no sandbox mechanism exists")
    a = ap.parse_args(argv)
    if a.cmd == "verify":
        return _verify(a)
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


def _verify(a) -> int:
    """Exit 0 for PASS, 3 for any other verdict, 2 for an environment or store error."""
    from .oracle import checkstore
    from .oracle.verify import render_feedback, verify
    from .workspace import TaskRepo
    if not paths.db_path().exists():
        print("not initialised; run `hera init`", file=sys.stderr)
        return 2
    conn = db.connect(paths.db_path())
    try:
        repo = TaskRepo.open(a.task)
        cs = checkstore.load(conn, a.checkset)
        out = verify(conn, repo, cs, a.commit, include_hidden=not a.no_hidden,
                     allow_unsandboxed=a.allow_unsandboxed)
    except Exception as e:                      # store, workspace, sandbox or argument problems
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(f"verdict: {out.verdict.value}")
    for r in out.reasons:
        print(f"  reason: {r}")
    if out.attribution:
        print(f"attribution: {out.attribution.label.value} (confidence {out.attribution.confidence}, "
              f"rule {out.attribution.rule})")
    else:
        print("attribution: none")
    for rec in out.advisories:
        print(f"advisory: {rec.label.value} (rule {rec.rule})")
    for f in out.audit.block:
        print(f"audit block: {f.rule}: {f.path}")
    for f in out.audit.flags:
        print(f"audit flag: {f.rule}: {f.path}")
    for n in out.notes:
        print(f"note: {n}")
    print(f"receipt: {out.receipt_id}")
    print("--- feedback for the implementer ---")
    print(render_feedback(out))
    return 0 if out.verdict.value == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
