from __future__ import annotations
import argparse
import sys
from . import __version__, paths
from .privacy import translog
from .store import db


def _usd(text: str) -> float:
    """A dollar cap from the command line: finite and above 0 (`nan` would switch the cap off)."""
    import math
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError(f"must be a finite amount above 0, got {text!r}")
    return v


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
    ck = sub.add_parser("checks", help="author and freeze check sets")
    cksub = ck.add_subparsers(dest="ccmd", required=True)
    cf = cksub.add_parser("freeze", help="validate a hand-written check set on the base commit and freeze it")
    cf.add_argument("--task", required=True)
    cf.add_argument("--repo", required=True, help="path of the source repository (a local directory)")
    cf.add_argument("--spec", required=True, help="SPEC.toml with [[requirement]] and [[check]] tables")
    cf.add_argument("--base", help="base commit (full hash); default: the repository's HEAD")
    cf.add_argument("--skip-validation", action="store_true", help="freeze without validating on the base commit")
    cf.add_argument("--allow-unsandboxed", action="store_true",
                    help="run with NO network or filesystem isolation if no sandbox mechanism exists")
    rn = sub.add_parser("run", help="run a task from objective to verdict with the recovery ladder")
    rn.add_argument("--task", required=True)
    rn.add_argument("--checkset", required=True, help="check-set hash printed by `hera checks freeze`")
    rn.add_argument("--objective", required=True)
    rn.add_argument("--max-usd", type=_usd, help="per-task dollar cap (default: the budget in hera.toml)")
    rn.add_argument("--allow-unsandboxed", action="store_true",
                    help="run with NO network or filesystem isolation if no sandbox mechanism exists")
    tk = sub.add_parser("task", help="inspect a task")
    tksub = tk.add_subparsers(dest="tcmd", required=True)
    ts = tksub.add_parser("status", help="show a task's events and its current status")
    ts.add_argument("id")
    tc = tksub.add_parser("continue", help="continue a task that stopped as BUDGET_CENSORED, PROVIDER_ERROR "
                                           "or (once resolved) AWAITING_DISPUTE")
    tc.add_argument("id")
    tc.add_argument("--max-usd", type=_usd,
                    help="per-task dollar cap for this continuation only (default: the budget in hera.toml)")
    tc.add_argument("--allow-unsandboxed", action="store_true",
                    help="run with NO network or filesystem isolation if no sandbox mechanism exists")
    dp = sub.add_parser("dispute", help="list and resolve disputes about visible checks")
    dpsub = dp.add_subparsers(dest="dcmd", required=True)
    dl = dpsub.add_parser("list", help="id, task, check, requirement, status, opened time and argument")
    dl.add_argument("--open", action="store_true", help="only disputes that are still open")
    dr = dpsub.add_parser("resolve", help="close a dispute (a human decides)")
    dr.add_argument("id")
    dr.add_argument("--outcome", required=True, choices=["upheld", "check_amended", "requirement_amended"])
    dr.add_argument("--resolver", required=True, help="who decides: human:<name>")
    dr.add_argument("--new-checkset", help="check_amended only: hash of the frozen replacement check set")
    rp = sub.add_parser("report", help="the learning ledger: pass rates, UNKNOWN rate, dollars per verified task")
    rp.add_argument("--since", help="only attempts started on or after this date (YYYY-MM-DD, local time)")
    rp.add_argument("--task", help="only this task")
    rp.add_argument("--json", action="store_true", help="print the summary and every record as JSON")
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
    if a.cmd == "checks":
        return _checks_freeze(a)
    if a.cmd == "run":
        return _run(a)
    if a.cmd == "task":
        return _task_continue(a) if a.tcmd == "continue" else _task_status(a)
    if a.cmd == "dispute":
        return _dispute_list(a) if a.dcmd == "list" else _dispute_resolve(a)
    if a.cmd == "report":
        return _report(a)
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


def format_event(ev: dict) -> str:
    """One line for a task event (never prints check ids of hidden checks: events hold none in text)."""
    k, d = ev["kind"], ev["detail"]
    if k == "routing":
        if d.get("blocked"):
            return "routing: no provider is permitted for this repository; nothing was sent"
        extra = f", skipped rungs: {', '.join(d['skipped_rungs'])}" if d.get("skipped_rungs") else ""
        return f"routing: implementer runs on `{d['impl']}` (repository class {d['repo_class']}){extra}"
    if k == "attempt_started":
        return (f"attempt {d['attempt']} started: candidate {d['candidate']} on `{d['provider_key']}` "
                f"({d['kind']})")
    if k == "attempt_ended":
        return f"attempt {d['attempt']} ended: {d['status']} after {d['turns']} turn(s), ${d['usd']:.4f}"
    if k == "verified":
        return f"attempt {d['attempt']} verified: {d['verdict']} (receipt {d['receipt_id'][:12]})"
    if k == "retry":
        return f"retry: attempt {d['attempt']} continues candidate {d['candidate']} ({d['why']})"
    if k == "fresh_candidate":
        return f"fresh candidate {d['candidate']} for attempt {d['attempt']} on `{d['provider_key']}` ({d['why']})"
    if k == "escalate":
        return f"escalate: strong candidate {d['candidate']} for attempt {d['attempt']}"
    if k == "diagnosis":
        return "diagnosis: " + (f"skipped ({d['skipped']})" if d.get("skipped") else "received")
    if k == "dispute_filed":
        return f"dispute filed for attempt {d['attempt']} ({d['dispute_id']})"
    if k == "dispute_rejected":
        return f"dispute from attempt {d['attempt']} rejected"
    if k == "continued":
        extra = []
        if d.get("max_usd") is not None:
            extra.append(f"cap ${d['max_usd']:.4f} for this continuation")
        if d.get("checkset_hash"):
            extra.append(f"check set {d['checkset_hash'][:12]}")
        if d.get("dispute_id"):
            extra.append(f"dispute {d['dispute_id']}: {d.get('outcome')}")
        return (f"continued after {d.get('from_status')} ({d.get('reason')})"
                + (": " + ", ".join(extra) if extra else ""))
    if k == "stopped":
        return f"stopped: {d['status']}" + (f" ({d['reason']})" if d.get("reason") else "")
    return k


def _open_db():
    if not paths.db_path().exists():
        print("not initialised; run `hera init`", file=sys.stderr)
        return None
    return db.connect(paths.db_path())


def _read_spec(spec_path):
    """SPEC.toml -> (requirements, checks, files). ValueError names the problem, never file content."""
    import tomllib
    from pathlib import Path
    from .oracle.model import DraftCheck, Requirement
    sp = Path(spec_path)
    with sp.open("rb") as fh:
        raw = tomllib.load(fh)
    reqs = [Requirement(str(r["id"]), str(r["text"])) for r in raw.get("requirement", [])]
    base = sp.resolve().parent
    checks, files = [], {}
    for c in raw.get("check", []):
        try:
            rel = str(c["file"])
            vis = str(c["visibility"])
            checks.append(DraftCheck(str(c["id"]), tuple(str(x) for x in c["requirements"]), vis, rel,
                                     str(c["node"]), author_role=str(c.get("author_role", vis)),
                                     author_model=str(c.get("author_model", "human")),
                                     kind=str(c.get("kind", "new")), runner=str(c.get("runner", "pytest"))))
        except KeyError as e:
            raise ValueError(f"check: missing key {e.args[0]!r}") from None
        target = (base / rel).resolve()
        if base not in target.parents or not target.is_file():
            raise ValueError("check.file: must name a file inside the spec's directory")
        files[rel] = target.read_bytes()
    return reqs, checks, files


def _checks_freeze(a) -> int:
    """Exit 0 when frozen, 3 when validation refused the check set, 2 for an error."""
    import os
    import tempfile
    import time
    from pathlib import Path
    from .oracle import checkstore
    from .oracle.adapters.pytest_adapter import PytestAdapter
    from .oracle.validation import validate_on_base
    from .privacy.policy import load_policy
    from .workspace import TaskRepo, _rm_tree
    conn = _open_db()
    if conn is None:
        return 2
    scratch_home = None
    old_home = os.environ.get("HERA_HOME")
    checkouts = []
    repo = None
    try:
        try:
            reqs, checks, files = _read_spec(a.spec)
            try:
                repo = TaskRepo.open(a.task)
                if a.base and a.base.lower() != repo.base_commit:
                    raise ValueError("the task already exists with a different base commit")
            except FileNotFoundError:
                repo = TaskRepo.create(a.task, a.repo, a.base)
            adapter = PytestAdapter()
            if not a.skip_validation:
                scratch_home = tempfile.mkdtemp(prefix="hera-validate-")
                os.environ["HERA_HOME"] = scratch_home          # the draft is frozen in a scratch store
                scratch = db.connect(":memory:")
                draft = checkstore.freeze(scratch, task_id=a.task, requirements=reqs, checks=checks, files=files)

                def factory():
                    co = repo.verification_checkout(repo.base_commit)
                    checkouts.append(co)
                    return co, repo.read_tree(repo.base_commit, adapter.is_protected)
                report = validate_on_base(adapter, factory, draft, runs=3, allow_unsandboxed=a.allow_unsandboxed)
                for v in report.checks:
                    print(f"check {v.check_id}: {v.health.value} ({v.rule}: {v.reason})")
                if report.environment_broken:
                    print("the validation environment is broken (a sentinel, timeout or result-file problem)")
                if not report.ok:
                    print("not frozen: the check set did not validate on the base commit "
                          "(use --skip-validation to freeze anyway)")
                    return 3
        finally:
            if old_home is None:
                os.environ.pop("HERA_HOME", None)
            else:
                os.environ["HERA_HOME"] = old_home
            for co in checkouts:
                try:
                    repo.cleanup_verification(co)
                except Exception:
                    pass
            if scratch_home:
                _rm_tree(Path(scratch_home))
        cs = checkstore.freeze(conn, task_id=a.task, requirements=reqs, checks=checks, files=files)
        policy = load_policy(paths.policy_dir() / "policy.toml")
        repo_id = policy.repo_for_path(a.repo) or ""
        conn.execute("INSERT OR IGNORE INTO tasks(id, repo_id, objective, status, created_at, updated_at) "
                     "VALUES(?,?,NULL,'new',?,?)", (a.task, repo_id, time.time(), time.time()))
        conn.execute("UPDATE tasks SET repo_id=? WHERE id=? AND (repo_id IS NULL OR repo_id='')",
                     (repo_id, a.task))
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    if cs.uncovered:
        print("note: requirement(s) with no check: " + ", ".join(cs.uncovered))
    if not repo_id:
        print("note: the repository is not in the privacy policy, so it counts as sensitive and "
              "no remote provider will be used for it")
    print(f"check-set hash: {cs.hash}")
    return 0


def _run(a) -> int:
    """Exit 0 for PASSED, 3 for any other final status, 2 for a configuration error."""
    import dataclasses
    import os
    from . import config
    from .agent.orchestrator import TaskConfigError, TaskSpec, run_task, select_roles
    from .ledger import Ledger
    from .oracle import checkstore
    from .privacy.gateway import Gateway
    from .privacy.grants import GrantStore
    from .privacy.labels import LabelStore
    from .privacy.policy import load_policy
    from .privacy.scanners import BLOCKING, default_scanners
    from .providers.registry import build_providers
    from .workspace import TaskRepo
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
    conn = db.connect(paths.db_path())
    try:
        try:
            cs = checkstore.load(conn, a.checkset)
        except KeyError:
            print("error: unknown check set; run `hera checks freeze` first", file=sys.stderr)
            return 2
        if cs.task_id != a.task:
            print("error: that check set belongs to a different task", file=sys.stderr)
            return 2
        row = conn.execute("SELECT repo_id FROM tasks WHERE id=?", (a.task,)).fetchone()
        repo_id = (row["repo_id"] if row else "") or ""
        policy = load_policy(paths.policy_dir() / "policy.toml")
        labels = LabelStore(conn)
        gateway = Gateway(conn, labels, GrantStore(conn), {}, scanners=default_scanners(policy), blocking=BLOCKING)
        budget = cfg.budget if a.max_usd is None else dataclasses.replace(cfg.budget, per_task_usd=a.max_usd)
        ledger = Ledger(conn, budget, cfg.prices)
        built = build_providers(cfg, gateway, ledger, os.environ)
        roles = select_roles(built)
        if "default" not in roles and "local" not in roles:
            why = "; ".join(f"{n}: {r}" for n, r in sorted(built.absent.items())) or "none configured"
            print(f"error: no provider for the default tier ({why})", file=sys.stderr)
            return 2
        repo = TaskRepo.open(a.task)
        spec = TaskSpec(task_id=a.task, repo=repo, base_commit=repo.base_commit, checkset=cs,
                        objective=a.objective, repo_id=repo_id, limits=cfg.recovery,
                        allow_unsandboxed=a.allow_unsandboxed)
        out = run_task(conn, spec, providers=built, policy=policy,
                       on_event=lambda ev: print(f"[{ev['seq']}] {format_event(ev)}", flush=True))
    except (TaskConfigError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:                      # store, workspace, sandbox or resume problems
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return _print_outcome(out)


def _print_outcome(out) -> int:
    lv = next((e["detail"] for e in reversed(out.events) if e["kind"] == "verified"), None)
    print(f"status: {out.status}")
    print(f"verdict: {out.verdict or 'none'}")
    print(f"attempts: {out.attempts}")
    print(f"spent: ${out.usd:.4f}")
    if lv:
        if lv["in_process_only"]:
            print("in_process_only: " + ", ".join(lv["in_process_only"])
                  + " (these requirements rest only on in-process checks, which a candidate can forge)")
        for n in lv["notes"]:
            if "WITHOUT sandbox" in n:
                print(f"note: {n}")
        print(f"receipt: {out.receipt_id}")
    if out.final_commit:
        print(f"final commit: {out.final_commit}")
    return 0 if out.status == "PASSED" else 3


def _task_continue(a) -> int:
    """Exit 0 for PASSED, 3 for any other final status or a refusal (nothing is logged then), 2 for a
    configuration error."""
    import os
    from . import config
    from .agent.orchestrator import (ContinueRefused, TaskConfigError, TaskSpec, continue_task, select_roles,
                                     task_checkset_hash)
    from .ledger import Ledger
    from .oracle import checkstore
    from .privacy.gateway import Gateway
    from .privacy.grants import GrantStore
    from .privacy.labels import LabelStore
    from .privacy.policy import load_policy
    from .privacy.scanners import BLOCKING, default_scanners
    from .providers.registry import build_providers
    from .workspace import TaskRepo
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
    conn = db.connect(paths.db_path())
    try:
        row = conn.execute("SELECT objective, repo_id FROM tasks WHERE id=?", (a.id,)).fetchone()
        if row is None or not row["objective"]:
            print(f"error: no such task: {a.id}", file=sys.stderr)
            return 2
        h = task_checkset_hash(conn, a.id)
        if h is None:
            print("error: cannot tell which check set the task uses", file=sys.stderr)
            return 2
        cs = checkstore.load(conn, h)
        policy = load_policy(paths.policy_dir() / "policy.toml")
        labels = LabelStore(conn)
        gateway = Gateway(conn, labels, GrantStore(conn), {}, scanners=default_scanners(policy), blocking=BLOCKING)
        ledger = Ledger(conn, cfg.budget, cfg.prices)
        built = build_providers(cfg, gateway, ledger, os.environ)
        roles = select_roles(built)
        if "default" not in roles and "local" not in roles:
            why = "; ".join(f"{n}: {r}" for n, r in sorted(built.absent.items())) or "none configured"
            print(f"error: no provider for the default tier ({why})", file=sys.stderr)
            return 2
        repo = TaskRepo.open(a.id)
        spec = TaskSpec(task_id=a.id, repo=repo, base_commit=repo.base_commit, checkset=cs,
                        objective=row["objective"], repo_id=row["repo_id"] or "", limits=cfg.recovery,
                        allow_unsandboxed=a.allow_unsandboxed)
        try:
            out = continue_task(conn, a.id, spec=spec, providers=built, policy=policy, max_usd=a.max_usd,
                                on_event=lambda ev: print(f"[{ev['seq']}] {format_event(ev)}", flush=True))
        except ContinueRefused as e:
            print(f"not continued: {e}", file=sys.stderr)
            return 3
    except (TaskConfigError, FileNotFoundError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return _print_outcome(out)


def _clean(text: str, limit: int) -> str:
    """One line of printable text (a dispute argument is written by a model)."""
    flat = " ".join(str(text or "").split())
    return "".join(ch if ch.isprintable() else "?" for ch in flat)[:limit]


def _utc(ts) -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(ts)) if ts else "-"


def _dispute_list(a) -> int:
    conn = _open_db()
    if conn is None:
        return 2
    try:
        rows = conn.execute("SELECT * FROM disputes" + (" WHERE status='open'" if a.open else "")
                            + " ORDER BY opened_at, id").fetchall()
    finally:
        conn.close()
    if not rows:
        print("no open disputes" if a.open else "no disputes")
        return 0
    for r in rows:
        line = (f"{r['id']}  task={r['task_id']}  check={r['check_id']}  requirement={r['requirement_id']}  "
                f"status={r['status']}  opened={_utc(r['opened_at'])}")
        if r["status"] != "open":
            line += f"  outcome={r['outcome']}  resolver={_clean(r['resolver'], 80)}"
        print(line)
        print(f"  argument: {_clean(r['argument'], 200)}")
    return 0


def _dispute_resolve(a) -> int:
    """Exit 0 when resolved, 2 for a refusal or an error (the dispute is left as it was)."""
    from .oracle.disputes import resolve_dispute
    who = a.resolver.strip()
    if not who.startswith("human:") or not who[len("human:"):].strip():
        print("error: from the command line the resolver must be a person: --resolver human:<name>", file=sys.stderr)
        return 2
    if a.new_checkset and a.outcome != "check_amended":
        print("error: --new-checkset only goes with --outcome check_amended", file=sys.stderr)
        return 2
    if a.outcome == "check_amended" and not a.new_checkset:
        print("error: --outcome check_amended needs --new-checkset HASH (freeze the amended check set first)",
              file=sys.stderr)
        return 2
    conn = _open_db()
    if conn is None:
        return 2
    try:
        row = conn.execute("SELECT * FROM disputes WHERE id=?", (a.id,)).fetchone()
        if row is None:
            print(f"error: no such dispute: {a.id}", file=sys.stderr)
            return 2
        if row["status"] != "open":
            print("error: dispute is already resolved", file=sys.stderr)
            return 2
        if a.outcome == "check_amended":
            cs = conn.execute("SELECT task_id FROM checksets WHERE hash=?", (a.new_checkset,)).fetchone()
            if cs is None:
                print("error: unknown check set: freeze the amended check set first", file=sys.stderr)
                return 2
            if cs["task_id"] != row["task_id"]:
                print("error: that check set belongs to a different task", file=sys.stderr)
                return 2
        models = [r["model_id"] for r in conn.execute(
            "SELECT DISTINCT model_id FROM attempts WHERE task_id=? AND model_id IS NOT NULL AND model_id != ''",
            (row["task_id"],))]
        try:
            resolve_dispute(conn, a.id, outcome=a.outcome, resolver=who, implementer_model=models,
                            new_checkset_hash=a.new_checkset)
        except (ValueError, KeyError) as e:
            print(f"error: {e.args[0] if e.args else e}", file=sys.stderr)
            return 2
        task = row["task_id"]
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(f"dispute {a.id} resolved: {a.outcome} by {who}")
    print(f"the task can now be continued: hera task continue {task}")
    return 0


def _report(a) -> int:
    import json
    import time
    from . import report
    since = None
    if a.since:
        try:
            since = time.mktime(time.strptime(a.since, "%Y-%m-%d"))
        except ValueError:
            print("error: --since must be a date like 2026-11-01", file=sys.stderr)
            return 2
    conn = _open_db()
    if conn is None:
        return 2
    try:
        recs = report.attempt_records(conn, since=since, task_id=a.task)
    finally:
        conn.close()
    summ = report.summary(recs)
    if a.json:
        print(json.dumps({"summary": summ, "attempts": [r.to_dict() for r in recs]}, indent=2, sort_keys=True))
    else:
        print(report.render_summary(summ))
    return 0


def _task_status(a) -> int:
    from .agent.orchestrator import load_events
    conn = _open_db()
    if conn is None:
        return 2
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (a.id,)).fetchone()
        events = load_events(conn, a.id)
    finally:
        conn.close()
    if row is None and not events:
        print(f"error: no such task: {a.id}", file=sys.stderr)
        return 2
    for ev in events:
        print(f"[{ev['seq']}] {format_event(ev)}")
    print(f"status: {row['status'] if row else 'unknown'}")
    return 0


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
