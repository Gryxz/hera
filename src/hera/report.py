"""The learning ledger and the report built from it (step S9).

``attempt_records`` joins what HERA already stores about every FINISHED attempt (``attempts``,
``turns``, ``task_events``, ``receipts``, ``verdicts``, ``attributions``, ``model_calls``) into one
record per attempt. Nothing here reads model text: a record holds identifiers, statuses, counts,
verdicts and dollars that HERA's own code produced. There is no table of its own; the records are
recomputed from the stored data every time, so they cannot drift from it.

``summary`` turns records into numbers and ``render_summary`` into text. Every rate carries its
counts ("3/7") and anything with fewer than ``SMALL_N`` observations is marked "(n<5)": a rate over a
handful of attempts says almost nothing. See docs/AGENT.md, "Reading the report".

Dollars: an attempt's own dollars are the sum of its turns (``turns.usd``, priced when the reply was
stored); a diagnosis call is attributed to the attempt it followed. A task's total comes from the
cost ledger (``model_calls``), so it also holds any call that no attempt row accounts for.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, replace

SMALL_N = 5
PASS = "PASS"
_RETRY_EVENTS = ("retry", "fresh_candidate", "escalate")


@dataclass(frozen=True)
class AttemptRecord:
    task_id: str
    attempt_seq: int
    attempt_id: str
    candidate: str
    tier: str
    provider: str
    model_id: str
    status: str                       # the attempt's end status (submitted, disputed, budget_censored, ...)
    turns: int
    usd: float                        # this attempt's turns
    diagnosis_usd: float              # a diagnosis call that followed this attempt
    diagnosis_calls: int
    task_usd: float                   # the whole task, from the cost ledger (the same on every record of a task)
    plan: str                         # "fresh" | "retry" | "escalate" | "" (no orchestrator log)
    rung: int | None
    feedback_kind: str
    diagnosis_seq: int | None         # a fresh candidate that was told a diagnosis: that event's seq
    receipt_id: str | None            # the verification that followed (full checks), if any
    verdict: str | None
    attribution: str | None           # the verdict's attribution; a PASS has none
    in_process_only: tuple            # requirement ids that passed on in-process checks alone
    unsandboxed: bool | None          # the verdict was reached with no sandbox (None: not verified)
    declared_scope: tuple
    out_of_scope: int | None          # changed paths outside the declared scope, from the stored audit
    started_at: float
    ended_at: float
    later_attempt_passed: bool = False

    @property
    def passed(self) -> bool:
        return self.verdict == PASS

    def to_dict(self) -> dict:
        d = asdict(self)
        d["in_process_only"] = list(self.in_process_only)
        d["declared_scope"] = list(self.declared_scope)
        return d


# ---- reading the store -----------------------------------------------------------------------
def _task_events(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    return [{"seq": r["seq"], "kind": r["kind"], "detail": json.loads(r["detail_json"] or "{}")}
            for r in conn.execute("SELECT seq, kind, detail_json FROM task_events WHERE task_id=? ORDER BY seq",
                                  (task_id,))]


def _receipt_facts(conn: sqlite3.Connection, receipt_id: str | None) -> dict | None:
    if not receipt_id:
        return None
    row = conn.execute("SELECT receipt_json FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if row is None:
        return None
    doc = json.loads(row["receipt_json"])
    ev = doc.get("evidence", {})
    flags = ev.get("audit", {}).get("flags", [])
    verdict = conn.execute("SELECT verdict FROM verdicts WHERE receipt_id=?", (receipt_id,)).fetchone()
    attr = conn.execute("SELECT label FROM attributions WHERE receipt_id=? AND superseded_by IS NULL "
                        "AND rule != 'weak_check_counted' ORDER BY id LIMIT 1", (receipt_id,)).fetchone()
    return {"verdict": verdict["verdict"] if verdict else None,
            "attribution": attr["label"] if attr else None,
            "in_process_only": tuple(ev.get("in_process_only", [])),
            "unsandboxed": ev.get("mechanism") == "none",
            "out_of_scope": sum(1 for f in flags if f and f[0] == "out_of_declared_scope"),
            "include_hidden": bool(ev.get("include_hidden", True))}


def _fallback_receipt(conn: sqlite3.Connection, task_id: str, commit: str | None) -> str | None:
    """An attempt with no orchestrator log: the latest full verification of its final commit."""
    if not commit:
        return None
    for r in conn.execute("SELECT id FROM receipts WHERE task_id=? AND candidate_commit=? ORDER BY created_at DESC",
                          (task_id, commit)):
        facts = _receipt_facts(conn, r["id"])
        if facts and facts["include_hidden"]:
            return r["id"]
    return None


def attempt_records(conn: sqlite3.Connection, *, since: float | None = None,
                    task_id: str | None = None) -> list[AttemptRecord]:
    """One record per finished attempt, ordered by task then attempt number. ``since`` is a Unix time:
    only attempts started at or after it. A running attempt has no record yet."""
    where, args = ["status != 'running'"], []
    if since is not None:
        where.append("started_at >= ?")
        args.append(since)
    if task_id is not None:
        where.append("task_id = ?")
        args.append(task_id)
    rows = conn.execute(f"SELECT * FROM attempts WHERE {' AND '.join(where)} ORDER BY task_id, seq", args).fetchall()
    out: list[AttemptRecord] = []
    by_task: dict[str, list] = {}
    for r in rows:
        by_task.setdefault(r["task_id"], []).append(r)
    for tid, attempts in by_task.items():
        events = _task_events(conn, tid)
        plans: dict[int, tuple[str, dict]] = {}
        verified: dict[int, dict] = {}
        diagnoses: list[dict] = []
        for e in events:
            d = e["detail"]
            if e["kind"] in _RETRY_EVENTS and "attempt" in d:
                plans[d["attempt"]] = (e["kind"], d)
            elif e["kind"] == "verified":
                verified[d["attempt"]] = d                     # the latest one wins (a re-verification)
            elif e["kind"] == "diagnosis" and not d.get("skipped"):
                diagnoses.append(d)
        diag_usd = [r["usd"] for r in conn.execute(
            "SELECT usd FROM model_calls WHERE task_id=? AND purpose LIKE 'diagnose%' ORDER BY id", (tid,))]
        diag_for: dict[int, tuple[float, int]] = {}
        for i, d in enumerate(diagnoses):
            usd, n = diag_for.get(d.get("attempt"), (0.0, 0))
            diag_for[d.get("attempt")] = (usd + (diag_usd[i] if i < len(diag_usd) else 0.0), n + 1)
        task_usd = conn.execute("SELECT COALESCE(SUM(usd),0) FROM model_calls WHERE task_id=?", (tid,)).fetchone()[0]
        recs: list[AttemptRecord] = []
        for a in attempts:
            n, usd = conn.execute("SELECT COUNT(*), COALESCE(SUM(usd),0) FROM turns WHERE attempt_id=?",
                                  (a["id"],)).fetchone()
            ev_kind, plan = plans.get(a["seq"], ("", {}))
            receipt_id = (verified.get(a["seq"]) or {}).get("receipt_id")
            if receipt_id is None and not events:
                receipt_id = _fallback_receipt(conn, tid, a["final_commit"])
            facts = _receipt_facts(conn, receipt_id)
            if receipt_id and facts is None:                   # the receipt row is gone: keep what the event says
                v = verified[a["seq"]]
                facts = {"verdict": v.get("verdict"), "attribution": v.get("attribution"),
                         "in_process_only": tuple(v.get("in_process_only", [])), "unsandboxed": None,
                         "out_of_scope": None}
            d_usd, d_n = diag_for.get(a["seq"], (0.0, 0))
            recs.append(AttemptRecord(
                task_id=tid, attempt_seq=a["seq"], attempt_id=a["id"], candidate=a["candidate"] or "",
                tier=a["tier"] or "", provider=a["provider"] or "", model_id=a["model_id"] or "",
                status=a["status"], turns=n, usd=usd, diagnosis_usd=d_usd, diagnosis_calls=d_n,
                task_usd=task_usd,
                plan="escalate" if ev_kind == "escalate" else plan.get("kind", ""),
                rung=plan.get("rung"), feedback_kind=plan.get("feedback_kind", ""),
                diagnosis_seq=plan.get("diagnosis_seq"),
                receipt_id=receipt_id if facts else None,
                verdict=facts["verdict"] if facts else None,
                attribution=facts["attribution"] if facts else None,
                in_process_only=facts["in_process_only"] if facts else (),
                unsandboxed=facts["unsandboxed"] if facts else None,
                declared_scope=tuple(json.loads(a["declared_scope_json"] or "[]")),
                out_of_scope=facts["out_of_scope"] if facts else None,
                started_at=a["started_at"] or 0.0, ended_at=a["ended_at"] or 0.0))
        for i, r in enumerate(recs):
            later = any(o.passed for o in recs[i + 1:])
            out.append(replace(r, later_attempt_passed=later))
    return out


# ---- the numbers -------------------------------------------------------------------------------
def rate(num: int, den: int) -> dict:
    """A rate with its counts. ``small`` is true below SMALL_N observations."""
    return {"num": num, "den": den, "rate": (num / den) if den else None, "small": den < SMALL_N}


def fmt_rate(r: dict) -> str:
    if r["den"] == 0:
        text = "0/0"
    else:
        text = f"{r['num']}/{r['den']} ({100 * r['num'] / r['den']:.0f}%)"
    return text + (" (n<5)" if r["small"] else "")


def _count(items) -> dict:
    out: dict[str, int] = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items()))


def _pass_rates(records, key) -> dict:
    groups: dict[str, list] = {}
    for r in records:
        groups.setdefault(key(r), []).append(r)
    return {k: rate(sum(1 for r in v if r.passed), len(v)) for k, v in sorted(groups.items())}


def _candidate_outcomes(records, select) -> tuple[int, int]:
    """(evaluated, passed) over the candidates of the records ``select`` picks: a candidate is
    evaluated once any of its attempts was verified, and passed if any of them passed."""
    evaluated = passed = 0
    for r in records:
        if not select(r):
            continue
        same = [o for o in records if o.task_id == r.task_id and o.candidate == r.candidate]
        if any(o.verdict is not None for o in same):
            evaluated += 1
            passed += any(o.passed for o in same)
    return evaluated, passed


def summary(records) -> dict:
    """Numbers over ``records``. Plain dicts, lists, numbers and strings only (JSON-ready)."""
    records = list(records)
    tasks = sorted({r.task_id for r in records})
    verified = [r for r in records if r.verdict is not None]
    passes = [r for r in verified if r.passed]
    firsts = [r for r in records if r.attempt_seq == 1]
    attributed = [r for r in verified if r.attribution is not None]

    task_usd = {r.task_id: r.task_usd for r in records}
    total = sum(task_usd.values())
    verified_tasks = sorted({r.task_id for r in passes if r.unsandboxed is False})

    retries = [r for r in records if r.plan == "retry" and r.feedback_kind != "continued"]
    r_ok = sum(1 for r in retries if r.passed)
    r_bad = sum(1 for r in retries if r.verdict is not None and not r.passed)
    diag_calls = sum(r.diagnosis_calls for r in records)
    d_eval, d_pass = _candidate_outcomes(records, lambda r: r.diagnosis_seq is not None
                                         and r.plan == "fresh")
    e_eval, e_pass = _candidate_outcomes(records, lambda r: r.plan == "escalate")
    not_passed = [r for r in verified if not r.passed]
    return {
        "attempts": len(records),
        "tasks": len(tasks),
        "verified_attempts": len(verified),
        "pass_rate_by_tier": _pass_rates(records, lambda r: r.tier),
        "pass_rate_by_model": _pass_rates(records, lambda r: r.model_id),
        "first_attempt_pass_rate": {
            "overall": rate(sum(1 for r in firsts if r.passed), len(firsts)),
            "by_tier": _pass_rates(firsts, lambda r: r.tier),
            "by_model": _pass_rates(firsts, lambda r: r.model_id)},
        "verdicts": {"counts": _count(r.verdict for r in verified),
                     "not_verified": _count(r.status for r in records if r.verdict is None)},
        "attribution": {
            "counts": _count(r.attribution for r in attributed),
            "attributed": len(attributed),
            "unknown": rate(sum(1 for r in attributed if r.attribution == "UNKNOWN"), len(attributed)),
            "not_attributed": {"PASS": len(passes),
                               "not verified": len(records) - len(verified)}},
        "dollars": {
            "total": total, "tasks": len(tasks),
            "per_task": (total / len(tasks)) if tasks else None,
            "verified_tasks": len(verified_tasks),
            "per_verified_task": (total / len(verified_tasks)) if verified_tasks else None},
        "passes_on_in_process_checks_only": rate(sum(1 for r in passes if r.in_process_only), len(passes)),
        "verdicts_without_sandbox": rate(sum(1 for r in verified if r.unsandboxed), len(verified)),
        "attempts_with_out_of_scope_changes": rate(
            sum(1 for r in verified if r.out_of_scope), sum(1 for r in verified if r.out_of_scope is not None)),
        "retries": {"succeeded": r_ok, "failed": r_bad, "not_verified": len(retries) - r_ok - r_bad,
                    "success_rate": rate(r_ok, r_ok + r_bad)},
        "diagnosis": {"calls": diag_calls, "usd": sum(r.diagnosis_usd for r in records),
                      "next_candidate_evaluated": d_eval, "next_candidate_passed": rate(d_pass, d_eval)},
        "escalations": {"attempts": sum(1 for r in records if r.plan == "escalate"),
                        "passed": rate(e_pass, e_eval)},
        "later_attempt_passed": rate(sum(1 for r in not_passed if r.later_attempt_passed), len(not_passed)),
    }


def _usd(x) -> str:
    return "n/a" if x is None else f"${x:.4f}"


def _rates_block(title: str, rates: dict) -> list[str]:
    return [f"{title}:"] + ([f"  {k or '(none)'}: {fmt_rate(r)}" for k, r in rates.items()] or ["  (no attempts)"])


def render_summary(s: dict) -> str:
    L: list[str] = []
    L.append(f"attempts: {s['attempts']} over {s['tasks']} task(s); {s['verified_attempts']} reached a verdict")
    L.append("rates are shown as passed/total (percent); (n<5) marks fewer than 5 observations")
    L.append("")
    L += _rates_block("pass rate per tier (all finished attempts)", s["pass_rate_by_tier"])
    L += _rates_block("pass rate per model", s["pass_rate_by_model"])
    fa = s["first_attempt_pass_rate"]
    L.append(f"first-attempt pass rate: {fmt_rate(fa['overall'])}")
    L += [f"  tier {k or '(none)'}: {fmt_rate(r)}" for k, r in fa["by_tier"].items()]
    L += [f"  model {k or '(none)'}: {fmt_rate(r)}" for k, r in fa["by_model"].items()]
    L.append("")
    v = s["verdicts"]
    L.append("verdicts: " + (", ".join(f"{k} {n}" for k, n in v["counts"].items()) or "none"))
    if v["not_verified"]:
        L.append("attempts that ended without a verification: "
                 + ", ".join(f"{k} {n}" for k, n in v["not_verified"].items()))
    a = s["attribution"]
    L.append("attribution (verdicts other than PASS): "
             + (", ".join(f"{k} {n}" for k, n in a["counts"].items()) or "none"))
    L.append(f"UNKNOWN attribution rate: {fmt_rate(a['unknown'])}")
    na = a["not_attributed"]
    L.append(f"not attributed: {na['PASS']} PASS, {na['not verified']} not verified")
    L.append("")
    d = s["dollars"]
    L.append(f"dollars, all tasks: {_usd(d['total'])} over {d['tasks']} task(s)" + (" (n<5)" if d["tasks"] < SMALL_N else ""))
    L.append(f"dollars per task: {_usd(d['per_task'])}" + (" (n<5)" if d["tasks"] < SMALL_N else ""))
    L.append(f"dollars per verified task: {_usd(d['per_verified_task'])} over {d['verified_tasks']} verified task(s)"
             + (" (n<5)" if d["verified_tasks"] < SMALL_N else "")
             + "  [verified = PASS reached with a sandbox; the dollars of every task are counted]")
    L.append("")
    L.append(f"passes resting only on in-process checks: {fmt_rate(s['passes_on_in_process_checks_only'])}")
    L.append(f"verdicts reached without a sandbox: {fmt_rate(s['verdicts_without_sandbox'])}")
    L.append(f"verified attempts with changes outside the declared scope: "
             f"{fmt_rate(s['attempts_with_out_of_scope_changes'])}")
    L.append("")
    rt = s["retries"]
    L.append(f"retries that succeeded: {fmt_rate(rt['success_rate'])}; failed {rt['failed']}; "
             f"not verified {rt['not_verified']}")
    dg = s["diagnosis"]
    L.append(f"diagnosis calls: {dg['calls']} ({_usd(dg['usd'])}); next candidate passed: "
             f"{fmt_rate(dg['next_candidate_passed'])}")
    es = s["escalations"]
    L.append(f"escalations: {es['attempts']}; escalated candidate passed: {fmt_rate(es['passed'])}")
    L.append(f"attempts that did not pass but a later attempt of the task did: {fmt_rate(s['later_attempt_passed'])}")
    return "\n".join(L)
