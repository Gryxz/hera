"""The task orchestrator: runs attempts, verifies each submitted candidate with the oracle and decides
what to do next with the recovery ladder (``policy.py``), until the task passes or a stopping
condition is reached.

Resume. Every step is a row in ``task_events``. ``run_task`` rebuilds everything from that log: the
ladder state by replaying the logged outcomes through ``policy.decide`` (a pure function), the open
attempt through ``loop.run_attempt`` (which resumes without repeating a paid call), the stored
receipt of a candidate that was already verified. A decision is never stored on its own: it is
recomputed from the outcomes, and only what it caused (a plan, a diagnosis, a stop) is logged. So a
crash at any point is followed by the same decision. The one window in which a paid call can be
repeated is the one in docs/AGENT.md (between a provider's answer and the row that stores it), for
the diagnosis call as for a turn.

Privacy first. Before any call, the destinations permitted for the repository are computed from its
label and the current grants (``privacy.gate.permitted_destinations``); a rung whose provider is not
permitted is not used. Nothing here imports a network library: every request goes through a provider
and the gateway.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping

from ..config import RecoveryLimits
from ..ledger import BudgetExceeded, Ledger
from ..oracle import checkstore
from ..oracle.adapters.pytest_adapter import PytestAdapter
from ..oracle.disputes import open_dispute
from ..oracle.model import CheckSet
from ..oracle.verify import render_feedback, run_baseline, verify
from ..privacy.gate import permitted_destinations
from ..privacy.labels import derive, label_builtin, label_bytes
from ..privacy.policy import Policy
from ..providers.base import BaseProvider, Message, ProviderBlocked, ProviderError
from ..types import CheckHealth, CheckStatus, Label
from ..workspace import TaskRepo
from . import policy as P
from .loop import attempt_id_for, replay_messages, run_attempt
from .policy import (ALT, DEFAULT, LOCAL, STRONG, Action, Caps, LadderState, Outcome, Rules)
from .prompts import initial_messages
from .tools import ToolBox

DIAGNOSIS_HEADING = "Diagnosis from a reviewer (advice, may be wrong)"
FEEDBACK_HEADING = "Verification feedback (derived from the repository; it may quote code)"
DISPUTE_REJECTED_MESSAGE = ("## Dispute rejected\nYour dispute was rejected: it must name a visible check of this "
                            "task and a requirement that check covers. Do not work around the check; keep "
                            "working on the task and call submit when you are done.")
DIAGNOSIS_SYSTEM = """You are a reviewer in a verified coding workflow. An implementer model worked on a repository and its result did not pass the checks. You get the requirements, the visible check files, the implementer's diff against the base commit and the verifier's feedback. You have no tools and cannot see the repository beyond that.

Say in plain words, in at most 300 words, why the attempt most likely failed and what a fresh attempt should do differently. Be concrete about the approach, not about edits. Do not write the full solution. Hidden checks exist that you cannot see: aim at the requirements, not at the visible examples only."""
DIAGNOSIS_SYSTEM_ID = "hera:system:diagnosis:v1"
_DIFF_MAX = 32 * 1024


class TaskConfigError(ValueError):
    """The providers or the task cannot be used at all (a configuration problem, not a verdict)."""


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    repo: TaskRepo
    base_commit: str
    checkset: CheckSet
    objective: str
    repo_id: str
    allowed_protected: tuple = ()
    limits: RecoveryLimits = RecoveryLimits()
    rules: Rules = Rules()
    allow_unsandboxed: bool = False


@dataclass(frozen=True)
class TaskOutcome:
    status: str
    attempts: int
    final_commit: str | None
    receipt_id: str | None
    verdict: str | None
    usd: float
    events: tuple = ()


# ---- the event log ---------------------------------------------------------------------------------
def _append(conn: sqlite3.Connection, task_id: str, kind: str, detail: dict) -> dict:
    """Append one event with the next sequence number (one statement, so it cannot interleave)."""
    now = time.time()
    conn.execute("INSERT INTO task_events(task_id, seq, at, kind, detail_json) "
                 "SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ? FROM task_events WHERE task_id=?",
                 (task_id, now, kind, json.dumps(detail, sort_keys=True, ensure_ascii=False), task_id))
    row = conn.execute("SELECT seq FROM task_events WHERE task_id=? ORDER BY seq DESC LIMIT 1",
                       (task_id,)).fetchone()
    return {"seq": row["seq"], "at": now, "kind": kind, "detail": detail}


def load_events(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    return [{"seq": r["seq"], "at": r["at"], "kind": r["kind"],
             "detail": json.loads(r["detail_json"] or "{}")}
            for r in conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY seq", (task_id,))]


@dataclass
class View:
    """The state of a task as the log describes it."""
    caps: Caps = field(default_factory=Caps)
    state: LadderState = field(default_factory=LadderState)
    pending: Action | None = None          # decided, not yet carried out
    plan: dict | None = None               # planned attempt that has not started
    open_attempt: dict | None = None       # started, not ended
    unverified: dict | None = None         # ended "submitted", not verified
    dispute: dict | None = None            # ended "disputed", not filed or rejected
    stopped: dict | None = None
    attempts: int = 0
    last_ended: dict | None = None
    last_verified: dict | None = None
    diagnosis_seq: int | None = None
    plans: dict = field(default_factory=dict)      # attempt number -> plan detail
    routing: dict | None = None


def fold(events, limits: RecoveryLimits, rules: Rules) -> View:
    v = View()
    for ev in events:
        k, d = ev["kind"], ev["detail"]
        if k == "routing":
            v.routing = d
            v.caps = Caps(**d["caps"])
            if d.get("blocked"):
                v.pending = Action("stop", status=P.BLOCKED_PRIVACY, why="no permitted destination")
            else:
                v.pending, v.state = P.start(v.caps, limits, rules)
        elif k == "attempt_started":
            v.open_attempt, v.plan = d, None
            v.attempts = max(v.attempts, d["attempt"])
        elif k == "attempt_ended":
            v.open_attempt, v.last_ended = None, d
            if d["status"] == "submitted":
                v.unverified = d
            elif d["status"] == "disputed":
                v.dispute = d
            else:
                v.pending, v.state = P.decide(v.state, Outcome(d["status"]), v.caps, limits, rules)
        elif k == "verified":
            v.unverified, v.last_verified = None, d
            v.pending, v.state = P.decide(v.state, Outcome(d["verdict"], tuple(d["failing"])), v.caps,
                                          limits, rules)
        elif k in ("dispute_filed", "dispute_rejected"):
            v.dispute = None
            v.pending, v.state = P.decide(v.state, Outcome(k), v.caps, limits, rules)
        elif k == "diagnosis":
            if v.pending is not None and v.pending.kind == "diagnose":
                if d.get("skipped"):
                    v.pending, v.state = P.after_diagnosis(v.state, v.pending, d["skipped"])
                else:
                    v.pending, v.state = P.after_diagnosis(v.state, v.pending, "ok")
                    v.diagnosis_seq = ev["seq"]
        elif k in ("retry", "fresh_candidate", "escalate"):
            v.pending, v.plan = None, d
            v.plans[d["attempt"]] = d
        elif k == "stopped":
            v.stopped = d
    return v


# ---- provider roles and privacy routing -----------------------------------------------------------------
def select_roles(providers: Mapping[str, BaseProvider]) -> dict[str, BaseProvider]:
    """Role -> provider. ``default`` is the provider named so, else the first (by name) of tier
    ``default``; ``default_alt`` only by that name; ``strong`` and ``local`` by name, else the first
    provider of that tier."""
    names = sorted(providers)

    def pick(role: str, tier: str, exclude=()):
        if role in providers:
            return providers[role]
        return next((providers[n] for n in names if providers[n].tier == tier and n not in exclude), None)
    roles = {DEFAULT: pick(DEFAULT, "default", exclude=(ALT,)), ALT: providers.get(ALT),
             STRONG: pick(STRONG, "strong"), LOCAL: pick(LOCAL, "local")}
    return {k: p for k, p in roles.items() if p is not None}


def compute_routing(roles: Mapping[str, BaseProvider], policy: Policy, repo_id: str, *,
                    now: float | None = None) -> dict:
    """The routing decision (destination names only) for a repository, before any call."""
    if DEFAULT not in roles and LOCAL not in roles:
        raise TaskConfigError("no provider for the default tier")
    gw = next(iter(roles.values())).gateway
    repo_class = policy.repo_class(repo_id)
    label = Label(f"hera:repo:{repo_id}", repo_class, "", ())
    permitted = permitted_destinations([label], gw.grants.all(), set(gw.destinations),
                                       time.time() if now is None else now)

    def ok(p: BaseProvider) -> bool:
        return bool(gw.destinations[p.destination_name].local) or p.destination_name in permitted

    default = roles.get(DEFAULT)
    if default is not None and ok(default):
        impl = DEFAULT
    elif LOCAL in roles:
        impl = LOCAL
    else:
        impl = None
    alt = ALT in roles and impl == DEFAULT and ok(roles[ALT])
    strong = STRONG in roles and ok(roles[STRONG])
    skipped = [] if strong else ["diagnosis", "escalation"]
    caps = Caps(impl_key=impl or DEFAULT, alt=alt, strong=strong, local=LOCAL in roles)
    return {"repo_class": repo_class.value, "permitted": sorted(permitted),
            "providers": {k: p.destination_name for k, p in sorted(roles.items())},
            "impl": impl, "caps": dataclasses.asdict(caps), "skipped_rungs": skipped,
            "blocked": impl is None}


# ---- the orchestrator -------------------------------------------------------------------------------------
class _Run:
    def __init__(self, conn, spec: TaskSpec, providers, policy: Policy, on_event) -> None:
        if spec.base_commit != spec.repo.base_commit:
            raise TaskConfigError("base_commit is not the base commit of the task repository")
        if spec.checkset.task_id != spec.task_id or spec.repo.task_id != spec.task_id:
            raise TaskConfigError("the check set or repository belongs to a different task")
        self.conn, self.spec, self.policy, self.on_event = conn, spec, policy, on_event
        self.roles = select_roles(providers)
        if not self.roles:
            raise TaskConfigError("no provider for the default tier")
        first = next(iter(self.roles.values()))
        self.labels = first.gateway.labels
        self.ledger: Ledger = first.ledger
        self.adapter = PytestAdapter()
        self._baseline = None
        self._visible: dict[str, bytes] | None = None

    # -- small helpers
    def _emit(self, kind: str, detail: dict) -> dict:
        ev = _append(self.conn, self.spec.task_id, kind, detail)
        self._notify(ev)
        return ev

    def _notify(self, ev: dict) -> None:
        if self.on_event is not None:
            self.on_event(ev)

    def _set_status(self, status: str) -> None:
        self.conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                          (status, time.time(), self.spec.task_id))

    def _baseline_run(self):
        if self._baseline is None:
            self._baseline = run_baseline(self.spec.repo, self.adapter,
                                          allow_unsandboxed=self.spec.allow_unsandboxed)
        return self._baseline

    def _visible_files(self) -> dict[str, bytes]:
        if self._visible is None:
            cs = self.spec.checkset
            root = checkstore.visible_dir(cs)
            self._visible = {rel: (root / rel).read_bytes()
                             for rel in sorted({c.relpath for c in cs.checks if c.visibility == "visible"})}
        return self._visible

    def _verify_visible(self, commit: str) -> str:
        s = self.spec
        out = verify(self.conn, s.repo, s.checkset, commit, adapter=self.adapter, include_hidden=False,
                     allow_unsandboxed=s.allow_unsandboxed, allowed_protected=s.allowed_protected,
                     baseline=self._baseline_run())
        return render_feedback(out)

    def _toolbox(self, cand: int) -> ToolBox:
        s = self.spec
        wt = s.repo.candidate(f"{s.task_id}-c{cand}")
        return ToolBox(wt, repo_id=s.repo_id, label_store=self.labels, policy=self.policy,
                       allow_unsandboxed=s.allow_unsandboxed,
                       visible_checks_dir=checkstore.visible_dir(s.checkset),
                       verify_visible=self._verify_visible)

    # -- conversations
    def _initial(self, diagnosis: str | None) -> list[Message]:
        s = self.spec
        extra = [(DIAGNOSIS_HEADING, diagnosis)] if diagnosis else []
        return initial_messages(task_objective=s.objective, requirements=s.checkset.requirements,
                                visible_check_files=self._visible_files(),
                                repo_file_list=s.repo.list_files(s.base_commit),
                                label_store=self.labels, policy=self.policy, repo_id=s.repo_id,
                                extra_sections=extra)

    def _feedback_message(self, plan: dict) -> Message:
        if plan["feedback_kind"] == P.FEEDBACK_DISPUTE_REJECTED:
            data = DISPUTE_REJECTED_MESSAGE.encode("utf-8")
            aid = "hera:feedback:dispute-rejected:v1"
            label_builtin(self.labels, artefact_id=aid, data=data)
        else:
            text = f"## {FEEDBACK_HEADING}\n{plan['feedback']}\n\nFix the problems and call submit again."
            data = text.encode("utf-8")
            aid = "hera:feedback:" + hashlib.sha256(data).hexdigest()[:16]
            label_bytes(self.labels, self.policy, artefact_id=aid, repo_id=self.spec.repo_id, data=data)
        return Message("user", aid, data)

    def _start_messages(self, plans: dict, events: list, n: int) -> list[Message]:
        plan = plans[n]
        if plan["kind"] == "retry":
            return self._history(plans, events, plan["parent"]) + [self._feedback_message(plan)]
        diagnosis = None
        if plan.get("diagnosis_seq") is not None:
            diagnosis = next(e["detail"]["text"] for e in events if e["seq"] == plan["diagnosis_seq"])
        return self._initial(diagnosis)

    def _history(self, plans: dict, events: list, n: int) -> list[Message]:
        cand = plans[n]["candidate"]
        return replay_messages(self.conn, attempt_id_for(self.spec.task_id, n), self._toolbox(cand),
                               self._start_messages(plans, events, n))

    # -- steps
    def route(self) -> dict:
        routing = compute_routing(self.roles, self.policy, self.spec.repo_id)
        self._emit("routing", routing)
        return routing

    def enact(self, v: View, events: list) -> None:
        act = v.pending
        if act.kind == "stop":
            self._emit("stopped", {"status": act.status, "reason": act.why})
            self._set_status(act.status)
        elif act.kind == "diagnose":
            self._diagnose(v, events)
        else:
            n = v.attempts + 1
            if act.kind == "retry":
                kind, parent = "retry", v.attempts
                fb = (v.last_verified or {}).get("feedback", "") \
                    if act.feedback != P.FEEDBACK_DISPUTE_REJECTED else ""
                plan = {"attempt": n, "candidate": v.state.candidate, "kind": "retry", "parent": parent,
                        "rung": 2, "provider_key": v.state.provider_key, "why": act.why,
                        "feedback_kind": act.feedback, "feedback": fb, "diagnosis_seq": None}
            else:
                kind = "escalate" if act.role == "strong" else "fresh_candidate"
                plan = {"attempt": n, "candidate": v.state.candidate, "kind": "fresh", "parent": None,
                        "rung": act.rung, "provider_key": act.provider_key, "why": act.why,
                        "feedback_kind": "", "feedback": "",
                        "diagnosis_seq": v.diagnosis_seq if act.with_diagnosis else None}
            self._emit(kind, plan)

    def start_attempt(self, v: View) -> None:
        plan = v.plan
        wt_box = self._toolbox(plan["candidate"])          # creates or re-opens the worktree
        provider = self.roles[plan["provider_key"]]
        self._emit("attempt_started", {"attempt": plan["attempt"], "candidate": plan["candidate"],
                                       "worktree": wt_box.worktree.name, "provider_key": plan["provider_key"],
                                       "destination": provider.destination_name, "tier": provider.tier,
                                       "kind": plan["kind"]})

    def run_one_attempt(self, v: View, events: list) -> None:
        d = v.open_attempt
        n = d["attempt"]
        plan = v.plans[n]
        provider = self.roles[plan["provider_key"]]
        toolbox = self._toolbox(plan["candidate"])
        lim = self.spec.limits
        purpose = ("implement:retry" if plan["kind"] == "retry"
                   else "implement:escalate" if plan["rung"] == 4 else "implement")
        res = run_attempt(self.conn, task_id=self.spec.task_id, attempt_seq=n, provider=provider,
                          toolbox=toolbox, messages=self._start_messages(v.plans, events, n),
                          max_turns=lim.max_turns, max_tokens=lim.max_tokens, purpose=purpose)
        self._emit("attempt_ended", {"attempt": n, "status": res.status, "final_commit": res.final_commit,
                                     "turns": res.turns, "usd": res.usd, "reason": res.reason,
                                     "summary": res.summary, "declared_scope": list(res.declared_scope),
                                     "twin_pattern": res.twin_pattern, "dispute": res.dispute})

    def verify_attempt(self, v: View) -> None:
        d = v.unverified
        s = self.spec
        scope = tuple(d["declared_scope"]) or None
        out = verify(self.conn, s.repo, s.checkset, d["final_commit"], adapter=self.adapter,
                     include_hidden=True, allow_unsandboxed=s.allow_unsandboxed, declared_scope=scope,
                     allowed_protected=s.allowed_protected, twin_pattern=d["twin_pattern"] or None,
                     baseline=self._baseline_run())
        verdict = out.verdict.value
        if verdict == "OVERFIT":            # requirement-level feedback only
            out_fb = dataclasses.replace(out, feedback=tuple(f for f in out.feedback if "check_id" not in f))
            feedback = render_feedback(out_fb)
        else:
            feedback = render_feedback(out)
        counted = (CheckHealth.VALID, CheckHealth.WEAK)
        failing = sorted(r.check_id for r in out.receipt.results
                         if r.status is not CheckStatus.PASS and r.health in counted)
        failing += sorted("regression:" + t for t in out.receipt.regressions)
        self._emit("verified", {
            "attempt": d["attempt"], "commit": d["final_commit"], "receipt_id": out.receipt_id,
            "verdict": verdict, "failing": failing, "reasons": list(out.reasons), "feedback": feedback,
            "attribution": out.attribution.label.value if out.attribution else None,
            "in_process_only": list(out.in_process_only), "notes": list(out.notes),
            "mechanism": out.mechanism})

    def file_dispute(self, v: View) -> None:
        d = v.dispute
        att = d["dispute"] or {}
        s = self.spec
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            try:
                dispute_id = open_dispute(self.conn, task_id=s.task_id, checkset_hash=s.checkset.hash,
                                          check_id=att.get("check_id", ""),
                                          requirement_id=att.get("requirement_id", ""),
                                          argument=att.get("argument", ""))
                ev = _append(self.conn, s.task_id, "dispute_filed",
                             {"attempt": d["attempt"], "dispute_id": dispute_id,
                              "check_id": att.get("check_id"), "requirement_id": att.get("requirement_id")})
            except ValueError:               # unknown or hidden check id: one fixed answer for both
                ev = _append(self.conn, s.task_id, "dispute_rejected", {"attempt": d["attempt"]})
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self._notify(ev)

    def _diagnose(self, v: View, events: list) -> None:
        s = self.spec
        strong = self.roles[STRONG]
        last = v.last_ended
        parts = ["## Requirements"]
        parts += [f"{r.id}: {r.text}" for r in s.checkset.requirements]
        parts += ["", "## Visible check files"]
        for name, data in self._visible_files().items():
            parts += [f"### {name}", data.decode("utf-8", "replace").rstrip("\n"), ""]
        parts += ["## The implementer's diff against the base commit",
                  s.repo.diff_text(s.base_commit, last["final_commit"], _DIFF_MAX), "",
                  "## The verifier's feedback", (v.last_verified or {}).get("feedback", "(none)")]
        user = "\n".join(parts).encode("utf-8")
        system = DIAGNOSIS_SYSTEM.encode("utf-8")
        label_builtin(self.labels, artefact_id=DIAGNOSIS_SYSTEM_ID, data=system)
        uid = "hera:diagnosis-input:" + hashlib.sha256(user).hexdigest()[:16]
        label_bytes(self.labels, self.policy, artefact_id=uid, repo_id=s.repo_id, data=user)
        try:
            reply = strong.complete([Message("system", DIAGNOSIS_SYSTEM_ID, system), Message("user", uid, user)],
                                    max_tokens=s.rules.diagnosis_max_tokens, task_id=s.task_id, purpose="diagnose")
        except ProviderBlocked as e:
            self._emit("diagnosis", {"skipped": "blocked", "reason": e.reason})
            return
        except BudgetExceeded as e:
            self._emit("diagnosis", {"skipped": "budget_censored", "reason": f"{e.cap} cap reached"})
            return
        except ProviderError as e:
            self._emit("diagnosis", {"skipped": "provider_error",
                                     "reason": f"HTTP {e.status}" if e.status else "provider error"})
            return
        text = reply.text.strip()
        did = "hera:diagnosis:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        derive(self.labels, artefact_id=did, inputs=[uid], data=text.encode("utf-8"))
        self._emit("diagnosis", {"text": text, "attempt": last["attempt"], "usd": self.ledger.spent_task(s.task_id)})

    # -- the loop
    def run(self) -> TaskOutcome:
        s = self.spec
        checkstore.verify_integrity(s.checkset)
        self.conn.execute("INSERT OR IGNORE INTO tasks(id, repo_id, objective, status, created_at, updated_at) "
                          "VALUES(?,?,?,?,?,?)", (s.task_id, s.repo_id, s.objective, "new", time.time(), time.time()))
        row = self.conn.execute("SELECT objective, repo_id FROM tasks WHERE id=?", (s.task_id,)).fetchone()
        if row["objective"] and row["objective"] != s.objective:
            raise TaskConfigError("this task already exists with a different objective")
        if not row["objective"] or not row["repo_id"]:
            self.conn.execute("UPDATE tasks SET objective=?, repo_id=? WHERE id=?",
                              (s.objective, row["repo_id"] or s.repo_id, s.task_id))
        events = load_events(self.conn, s.task_id)
        if not any(e["kind"] == "routing" for e in events):
            self.route()
        if not events or events[-1]["kind"] != "stopped":
            self._set_status("running")
        while True:
            events = load_events(self.conn, s.task_id)
            v = fold(events, s.limits, s.rules)
            if v.stopped:
                return self._outcome(events, v)
            if v.open_attempt:
                self.run_one_attempt(v, events)
            elif v.unverified:
                self.verify_attempt(v)
            elif v.dispute:
                self.file_dispute(v)
            elif v.pending:
                self.enact(v, events)
            elif v.plan:
                self.start_attempt(v)
            else:
                raise RuntimeError("the task log is in a state the orchestrator does not recognise")

    def _outcome(self, events: list, v: View) -> TaskOutcome:
        status = v.stopped["status"]
        lv = v.last_verified
        last = v.last_ended
        if status == P.PASSED and lv:
            commit = lv["commit"]
        else:
            commit = last["final_commit"] if last else None
        return TaskOutcome(status=status, attempts=v.attempts, final_commit=commit,
                           receipt_id=lv["receipt_id"] if lv else None,
                           verdict=lv["verdict"] if lv else None,
                           usd=self.ledger.spent_task(self.spec.task_id), events=tuple(events))


def run_task(conn: sqlite3.Connection, task: TaskSpec, *, providers: Mapping[str, BaseProvider],
             policy: Policy, on_event: Callable[[dict], None] | None = None) -> TaskOutcome:
    """Run (or resume) ``task`` until it ends. ``providers`` maps a role to a provider: ``default``,
    optionally ``default_alt``, ``strong`` and ``local`` (see ``select_roles``). ``on_event`` is
    called with every event as it is logged. A task that has already stopped is returned as stored,
    without any call; start a new task id to run again."""
    return _Run(conn, task, providers, policy, on_event).run()
