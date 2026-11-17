"""The implementer loop: one attempt = a conversation with one model, working through tools.

Resume without repeating a paid call. Each model turn is a row in ``turns`` keyed by
(attempt, seq). The row holds a hash of the request that was sent, the model's reply (stored BEFORE
any tool runs), then the tool results and a checkpoint commit of the worktree. Calling
``run_attempt`` again for the same task and attempt number replays the stored turns: the request for
turn N is rebuilt from the stored history; if the hash differs from the stored one the run stops with
``ResumeMismatch`` (it never silently diverges), otherwise the stored reply is used and the provider
is not called. A turn that has a reply but no stored tool results re-runs its tools after the
worktree is reset to the previous checkpoint. The only window in which a paid call can be repeated is
between the provider's response arriving and the turn row being written (a few statements in this
process); see docs/AGENT.md.

Nothing here imports a network library: every request goes through the provider and the gateway.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass

from ..ledger import BudgetExceeded, cost_usd
from ..privacy.labels import label_builtin, label_bytes
from ..providers.base import (BaseProvider, Message, ProviderBlocked, ProviderError,
                              calls_artefact_id, render_calls_artefact)
from ..types import ModelReply, ToolCall, Usage
from .tools import TOOL_SPECS, ToolBox

MAX_NUDGES = 2
MAX_TOOL_RESULT_BYTES = 2_000_000
PROVIDER_FAILURES = 2
NUDGE_TEXT = "Use the tools. Call submit when you are done."
EMPTY_REPLY_TEXT = "(no reply)"
STATUSES = ("submitted", "disputed", "out_of_turns", "no_progress", "blocked", "budget_censored",
            "provider_error")


class ResumeMismatch(Exception):
    """The request rebuilt for a stored turn differs from what was sent the first time."""


@dataclass(frozen=True)
class AttemptResult:
    status: str                       # one of STATUSES ("running" is never returned)
    final_commit: str                 # checkpoint at the end; only a "submitted" one is to be verified
    summary: str = ""                 # the model's summary (submitted)
    declared_scope: tuple = ()
    twin_pattern: str = ""
    dispute: dict | None = None       # {"check_id", "requirement_id", "argument"} when disputed
    turns: int = 0
    usd: float = 0.0
    reason: str = ""                  # fixed reason for blocked / budget_censored / provider_error / ...


def attempt_id_for(task_id: str, attempt_seq: int) -> str:
    return f"{task_id}-a{attempt_seq}"


def request_sha256(provider: BaseProvider, messages) -> str:
    doc = {"provider": provider.name, "model": provider.model_id,
           "messages": [[m.role, hashlib.sha256(m.data).hexdigest(), m.tool_call_id,
                         [[c.id, c.name, c.arguments] for c in m.tool_calls]] for m in messages]}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


# ---- (de)serialisation of stored turns --------------------------------------------------
def _reply_json(reply: ModelReply, calls: tuple[ToolCall, ...]) -> str:
    u = reply.usage
    return json.dumps({"text": reply.text, "model_id": reply.model_id, "usage_reported": reply.usage_reported,
                       "usage": {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                                 "cache_read_tokens": u.cache_read_tokens,
                                 "cache_write_tokens": u.cache_write_tokens},
                       "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments}
                                      for c in calls]}, ensure_ascii=False)


def _reply_from_json(text: str) -> tuple[ModelReply, tuple[ToolCall, ...]]:
    d = json.loads(text)
    calls = tuple(ToolCall(c["id"], c["name"], c["arguments"]) for c in d["tool_calls"])
    return (ModelReply(text=d["text"], usage=Usage(**d["usage"]), model_id=d.get("model_id", ""),
                       tool_calls=calls, usage_reported=d.get("usage_reported", True)), calls)


def _results_json(outputs) -> str:
    return json.dumps([{"artefact_id": o.artefact_id, "builtin": o.builtin,
                        "data_b64": base64.b64encode(o.data).decode("ascii"),
                        "ended": o.ended, "payload": o.payload} for o in outputs])


def _results_from_json(toolbox: ToolBox, text: str):
    out = []
    for d in json.loads(text):
        out.append(toolbox.make_output(base64.b64decode(d["data_b64"]), d["artefact_id"], d["builtin"],
                                       d.get("ended"), d.get("payload")))
    return out


# ---- attempt rows ------------------------------------------------------------------------
def _result_from_row(conn: sqlite3.Connection, row) -> AttemptResult:
    n, usd = conn.execute("SELECT COUNT(*), COALESCE(SUM(usd),0) FROM turns WHERE attempt_id=?",
                          (row["id"],)).fetchone()
    status, text = row["status"], row["summary"] or ""
    dispute = json.loads(text) if status == "disputed" and text else None
    return AttemptResult(
        status=status, final_commit=row["final_commit"] or "",
        summary=text if status == "submitted" else "",
        declared_scope=tuple(json.loads(row["declared_scope_json"] or "[]")),
        twin_pattern=row["twin_pattern"] or "", dispute=dispute, turns=n, usd=usd,
        reason=text if status not in ("submitted", "disputed") else "")


def _finish(conn: sqlite3.Connection, attempt_id: str, status: str, final_commit: str, *,
            summary: str = "", declared_scope=(), twin_pattern: str = "") -> AttemptResult:
    conn.execute("UPDATE attempts SET status=?, final_commit=?, summary=?, declared_scope_json=?, "
                 "twin_pattern=?, ended_at=? WHERE id=?",
                 (status, final_commit, summary, json.dumps(list(declared_scope)), twin_pattern,
                  time.time(), attempt_id))
    return _result_from_row(conn, conn.execute("SELECT * FROM attempts WHERE id=?",
                                               (attempt_id,)).fetchone())


def run_attempt(conn: sqlite3.Connection, *, task_id: str, attempt_seq: int, provider: BaseProvider,
                toolbox: ToolBox, messages, max_turns: int = 30, max_tokens: int = 4096,
                purpose: str = "implement") -> AttemptResult:
    """Run (or resume) attempt ``attempt_seq`` of ``task_id``. Returns when the attempt reaches an
    end status; a crash leaves it "running" and the next call resumes it. A finished attempt is
    returned as stored, without any call. Raises ResumeMismatch if ``messages`` or the stored history
    no longer reproduce the requests that were sent."""
    attempt_id = attempt_id_for(task_id, attempt_seq)
    wt = toolbox.worktree
    row = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if row is None:
        # a checkpoint of whatever the worktree holds now, so a resume can always reset to it
        base = wt.commit_all(f"hera attempt {attempt_id} start")
        conn.execute("INSERT INTO attempts(id,task_id,seq,tier,provider,model_id,candidate,status,"
                     "base_commit,started_at) VALUES(?,?,?,?,?,?,?,'running',?,?)",
                     (attempt_id, task_id, attempt_seq, provider.tier, provider.name, provider.model_id,
                      getattr(wt, "name", ""), base, time.time()))
        row = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    elif row["status"] != "running":
        return _result_from_row(conn, row)

    tools = provider.render_tools(TOOL_SPECS)
    history: list[Message] = list(messages)
    checkpoint = row["base_commit"]
    nudges = 0
    tool_bytes = 0

    for seq in range(1, max_turns + 1):
        sha = request_sha256(provider, history)
        turn = conn.execute("SELECT * FROM turns WHERE attempt_id=? AND seq=?", (attempt_id, seq)).fetchone()
        replayed = turn is not None and turn["reply_json"] is not None
        if replayed:
            if turn["request_sha256"] != sha:
                raise ResumeMismatch(f"turn {seq} of {attempt_id}: the request no longer matches the stored one")
            reply, calls = _reply_from_json(turn["reply_json"])
        else:
            try:
                reply = _complete(provider, history, tools, max_tokens, task_id, purpose)
            except ProviderBlocked as e:
                return _finish(conn, attempt_id, "blocked", checkpoint, summary=e.reason)
            except BudgetExceeded as e:
                return _finish(conn, attempt_id, "budget_censored", checkpoint,
                               summary=f"{e.cap} cap reached")
            except ProviderError as e:
                return _finish(conn, attempt_id, "provider_error", checkpoint,
                               summary=f"provider error (HTTP {e.status})" if e.status else "provider error")
            try:
                calls = provider.normalise_tool_calls(reply)
            except Exception:
                calls = ()
            usd = cost_usd(provider.ledger.prices[provider.tier], reply.usage)
            conn.execute("INSERT OR REPLACE INTO turns(attempt_id,seq,request_sha256,reply_json,usd,created_at) "
                         "VALUES(?,?,?,?,?,?)", (attempt_id, seq, sha, _reply_json(reply, calls), usd, time.time()))
            turn = conn.execute("SELECT * FROM turns WHERE attempt_id=? AND seq=?", (attempt_id, seq)).fetchone()

        text = reply.text if (reply.text or calls) else EMPTY_REPLY_TEXT
        assistant = Message("assistant", f"{attempt_id}:{seq}:assistant", text.encode("utf-8"),
                            tool_calls=calls)
        label_bytes(toolbox.label_store, toolbox.policy, artefact_id=assistant.artefact_id,
                    repo_id=toolbox.repo_id, data=assistant.data)
        if calls:
            label_bytes(toolbox.label_store, toolbox.policy, artefact_id=calls_artefact_id(assistant),
                        repo_id=toolbox.repo_id, data=render_calls_artefact(calls))
        history.append(assistant)

        if not calls:
            if turn["tool_results_json"] is None:
                conn.execute("UPDATE turns SET tool_results_json='[]', checkpoint_commit=? "
                             "WHERE attempt_id=? AND seq=?", (checkpoint, attempt_id, seq))
            nudges += 1
            if nudges > MAX_NUDGES:
                return _finish(conn, attempt_id, "no_progress", checkpoint, summary="no tool calls")
            nudge = NUDGE_TEXT.encode("utf-8")
            nid = "hera:nudge:v1"
            label_builtin(toolbox.label_store, artefact_id=nid, data=nudge)
            history.append(Message("user", nid, nudge))
            continue
        nudges = 0

        if turn["tool_results_json"] is not None:
            outputs = _results_from_json(toolbox, turn["tool_results_json"])
            checkpoint = turn["checkpoint_commit"] or checkpoint
        else:
            if replayed:        # the reply is stored but the tools did not finish: undo partial effects
                wt.reset_to(checkpoint)
            outputs = []
            for i, call in enumerate(calls):
                out = toolbox.execute(call, artefact_id=f"{attempt_id}:{seq}:{i}:result")
                outputs.append(out)
                if out.ended:
                    break
            checkpoint = wt.commit_all(f"hera checkpoint {attempt_id}/{seq}")
            conn.execute("UPDATE turns SET tool_results_json=?, checkpoint_commit=? "
                         "WHERE attempt_id=? AND seq=?", (_results_json(outputs), checkpoint, attempt_id, seq))

        for call, out in zip(calls, outputs):
            history.append(Message("tool", out.artefact_id, out.data, tool_call_id=call.id))
            tool_bytes += len(out.data)
        ended = next((o for o in outputs if o.ended), None)
        if ended is not None:
            p = ended.payload or {}
            if ended.ended == "submitted":
                return _finish(conn, attempt_id, "submitted", checkpoint, summary=p.get("summary", ""),
                               declared_scope=p.get("declared_scope", ()),
                               twin_pattern=p.get("twin_pattern", ""))
            return _finish(conn, attempt_id, "disputed", checkpoint, summary=json.dumps(p, ensure_ascii=False))
        if tool_bytes > MAX_TOOL_RESULT_BYTES:
            return _finish(conn, attempt_id, "out_of_turns", checkpoint, summary="context budget")

    return _finish(conn, attempt_id, "out_of_turns", checkpoint, summary="turn limit")


NOT_RUN_TEXT = "not run: the attempt ended before this call"


def replay_messages(conn: sqlite3.Connection, attempt_id: str, toolbox: ToolBox, start_messages) -> list[Message]:
    """The conversation of a FINISHED attempt as the loop held it: ``start_messages`` then, per stored
    turn, the model's message and its tool results (a nudge after a reply without calls). Calls that
    never ran because ``submit``/``dispute`` ended the reply get a fixed "not run" result, so every
    tool call in the history has a result, as both wire formats require when a conversation goes on.
    Used by the orchestrator to continue the same conversation in a retry. Labels are (re)written
    exactly as the loop writes them."""
    history: list[Message] = list(start_messages)
    rows = conn.execute("SELECT * FROM turns WHERE attempt_id=? ORDER BY seq", (attempt_id,)).fetchall()
    for i, turn in enumerate(rows):
        if turn["reply_json"] is None:
            break
        reply, calls = _reply_from_json(turn["reply_json"])
        text = reply.text if (reply.text or calls) else EMPTY_REPLY_TEXT
        assistant = Message("assistant", f"{attempt_id}:{turn['seq']}:assistant", text.encode("utf-8"),
                            tool_calls=calls)
        label_bytes(toolbox.label_store, toolbox.policy, artefact_id=assistant.artefact_id,
                    repo_id=toolbox.repo_id, data=assistant.data)
        if calls:
            label_bytes(toolbox.label_store, toolbox.policy, artefact_id=calls_artefact_id(assistant),
                        repo_id=toolbox.repo_id, data=render_calls_artefact(calls))
        history.append(assistant)
        if not calls:
            if i + 1 < len(rows):
                nudge = NUDGE_TEXT.encode("utf-8")
                label_builtin(toolbox.label_store, artefact_id="hera:nudge:v1", data=nudge)
                history.append(Message("user", "hera:nudge:v1", nudge))
            continue
        if turn["tool_results_json"] is None:
            raise ValueError("the attempt has a turn without tool results; it is not finished")
        outputs = _results_from_json(toolbox, turn["tool_results_json"])
        for j, call in enumerate(calls):
            if j < len(outputs):
                out = outputs[j]
                history.append(Message("tool", out.artefact_id, out.data, tool_call_id=call.id))
            else:
                aid = f"{attempt_id}:{turn['seq']}:{j}:result"
                data = NOT_RUN_TEXT.encode("utf-8")
                label_builtin(toolbox.label_store, artefact_id=aid, data=data)
                history.append(Message("tool", aid, data, tool_call_id=call.id))
    return history


def _complete(provider: BaseProvider, history, tools, max_tokens: int, task_id: str,
              purpose: str) -> ModelReply:
    failures = 0
    while True:
        try:
            return provider.complete(history, max_tokens=max_tokens, task_id=task_id, purpose=purpose,
                                     tools=tools)
        except ProviderError:
            failures += 1
            if failures >= PROVIDER_FAILURES:
                raise
