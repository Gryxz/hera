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
    pr = sub.add_parser("providers", help="list configured model providers or test one")
    psub = pr.add_subparsers(dest="pcmd", required=True)
    psub.add_parser("list", help="name, kind, tier, host and whether the key variable is set")
    pc = psub.add_parser("check", help="send one fixed test prompt to a provider")
    pc.add_argument("name")
    a = ap.parse_args(argv)
    if a.cmd == "verify":
        return _verify(a)
    if a.cmd == "providers":
        return _providers(a)
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


def _providers(a) -> int:
    """`hera providers list` and `hera providers check <name>`. Never prints a key."""
    import os
    from . import config
    from .ledger import Ledger
    from .privacy.gateway import Gateway
    from .privacy.grants import GrantStore
    from .privacy.labels import LabelStore, label_builtin
    from .privacy.policy import load_policy
    from .privacy.scanners import BLOCKING, default_scanners
    from .providers.base import Message, ProviderBlocked, ProviderError
    from .providers.registry import build_providers, effective_url, key_present, url_host
    from .ledger import BudgetExceeded
    cfg_path = paths.config_path()
    if not cfg_path.exists() or not paths.db_path().exists():
        print("not initialised or no hera.toml; run `hera init` and copy config/hera.example.toml "
              f"to {cfg_path}", file=sys.stderr)
        return 2
    try:
        cfg = config.load(cfg_path)
    except Exception as e:
        print(f"error: could not read the configuration: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    if a.pcmd == "list":
        if not cfg.providers:
            print("no providers configured")
            return 0
        for name, spec in sorted(cfg.providers.items()):
            present = key_present(spec, os.environ)
            key = "n/a (local)" if present is None else ("yes" if present else "no")
            print(f"{name}  kind={spec.kind}  tier={spec.tier}  host={url_host(effective_url(spec)) or '-'}  "
                  f"key present: {key}")
        return 0
    conn = db.connect(paths.db_path())
    try:
        policy = load_policy(paths.policy_dir() / "policy.toml")
        labels = LabelStore(conn)
        gateway = Gateway(conn, labels, GrantStore(conn), {}, scanners=default_scanners(policy),
                          blocking=BLOCKING)
        ledger = Ledger(conn, cfg.budget, cfg.prices)
        built = build_providers(cfg, gateway, ledger, os.environ)
        if a.name not in cfg.providers:
            print(f"error: no provider named {a.name!r} in the configuration", file=sys.stderr)
            return 2
        if a.name not in built:
            print(f"error: provider {a.name!r} is not available: {built.absent.get(a.name, 'unknown')}",
                  file=sys.stderr)
            return 2
        prompt = b"Reply with the single word: ready"
        aid = "hera:providers-check:v1"
        label_builtin(labels, artefact_id=aid, data=prompt)
        try:
            reply = built[a.name].complete([Message("user", aid, prompt)], max_tokens=16,
                                           purpose="provider-check")
        except ProviderBlocked as e:
            print(f"error: the request was blocked: {e.reason}", file=sys.stderr)
            return 3
        except BudgetExceeded as e:
            print(f"error: not sent, {e.cap} cap reached", file=sys.stderr)
            return 3
        except ProviderError as e:
            print(f"error: provider failed" + (f" (HTTP {e.status})" if e.status else ""), file=sys.stderr)
            return 3
        row = conn.execute("SELECT usd FROM model_calls ORDER BY id DESC LIMIT 1").fetchone()
        usd = row["usd"] if row else 0.0
        u = reply.usage
        print(f"reply: {reply.text.strip()}")
        print(f"tokens: {u.input_tokens} in, {u.output_tokens} out"
              + ("" if reply.usage_reported else " (estimated: the provider reported no usage)"))
        print(f"cost: ${usd:.6f}")
        return 0
    finally:
        conn.close()


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
