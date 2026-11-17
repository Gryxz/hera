"""Requirements summary (S7d): the one screen a person reads and approves before checks are frozen.

A summary is written as a ``SUMMARY.toml`` (by hand now, by a model later), imported into the state
store under the SHA-256 of its canonical form, and approved or rejected by a named person. Edit the
file and import it again: new content is a new hash and needs a new approval. Checks are frozen only
against an approved summary whose requirements equal the spec's (see ``hera checks freeze``), and the
hash is stored with the check set.

Blocks and limits (architecture A.15): outcomes (1 to 5), observable behaviour (up to 6 given/expect
examples), constraints (5), assumptions (5, riskiest first), not verifiable automatically (5), out of
scope (3), questions (3, each answered before approval) and the requirements with the planned
verification confidence of each (1 to 6; the limit is what keeps the screen to 40 lines). Every item is
one printable line of at most 200 characters. Validation errors name the field and never echo content.

Library callers (``run_task`` and ``continue_task`` do not look at summaries) must enforce approval
themselves; the command line does it in ``hera run``.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import textwrap
import time
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ..workspace import _check_id
from .model import Requirement

MAX_ITEM_CHARS = 200
MAX_NOTE_CHARS = 500
MAX_DECIDER_CHARS = 80
MAX_FILE_BYTES = 64 * 1024
LIMITS = {"outcomes": 5, "behaviour": 6, "constraints": 5, "assumptions": 5,
          "not_verifiable": 5, "out_of_scope": 3, "questions": 3, "requirements": 6}
CONFIDENCES = ("high", "medium", "low")
NOT_APPROVED, APPROVED, REJECTED = "none", "approved", "rejected"
SCREEN_WIDTH = 100                  # columns; the rendered screen is at most 40 lines of this width
SCREEN_LINES = 40
_GUTTER = 16
_REQ_ID = re.compile(r"R-[0-9]{1,4}")
_HASH = re.compile(r"[0-9a-f]{64}")
_TOP_KEYS = ("task", "outcomes", "behaviour", "constraints", "assumptions", "not_verifiable",
             "out_of_scope", "questions", "requirements")


class SummaryTampered(RuntimeError):
    """A stored summary does not match its hash."""


# ---- validation helpers -------------------------------------------------------------------------------
def _line(value, field: str, limit: int = MAX_ITEM_CHARS) -> str:
    """A non-empty single line of printable text (control characters, newlines and tabs are refused)."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}: must be a non-empty string")
    if len(value) > limit:
        raise ValueError(f"{field}: at most {limit} characters")
    if not value.isprintable():
        raise ValueError(f"{field}: must be one line of printable text (no control characters)")
    return value


def _items(value, block: str, minimum: int = 0) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{block}: must be a list")
    if len(value) > LIMITS[block]:
        raise ValueError(f"{block}: at most {LIMITS[block]} items")
    if len(value) < minimum:
        raise ValueError(f"{block}: needs at least {minimum} item")
    return tuple(_line(v, f"{block}[{i}]") for i, v in enumerate(value, 1))


def _objects(value, block: str, kind) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{block}: must be a list")
    if len(value) > LIMITS[block]:
        raise ValueError(f"{block}: at most {LIMITS[block]} items")
    if not all(isinstance(v, kind) for v in value):
        raise ValueError(f"{block}: wrong item type")
    return tuple(value)


@dataclass(frozen=True)
class Behaviour:
    given: str          # the input or action
    expect: str         # the visible result

    def __post_init__(self):
        _line(self.given, "given")
        _line(self.expect, "expect")


@dataclass(frozen=True)
class Question:
    q: str
    answer: str | None = None

    def __post_init__(self):
        _line(self.q, "q")
        if self.answer is not None:
            _line(self.answer, "answer")


@dataclass(frozen=True)
class SummaryRequirement:
    id: str
    text: str
    confidence: str
    outcome: int | None = None      # 1-based index into the summary's outcomes

    def __post_init__(self):
        if not isinstance(self.id, str) or not _REQ_ID.fullmatch(self.id):
            raise ValueError("id: must match R-[0-9]{1,4}")
        _line(self.text, "text")
        if self.confidence not in CONFIDENCES:
            raise ValueError("confidence: must be 'high', 'medium' or 'low'")
        if self.outcome is not None and (not isinstance(self.outcome, int) or isinstance(self.outcome, bool)
                                         or self.outcome < 1):
            raise ValueError("outcome: must be a whole number from 1")


@dataclass(frozen=True)
class RequirementsSummary:
    task: str
    outcomes: tuple[str, ...]
    requirements: tuple[SummaryRequirement, ...]
    behaviour: tuple[Behaviour, ...] = ()
    constraints: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    not_verifiable: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    questions: tuple[Question, ...] = ()

    def __post_init__(self):
        _check_id(self.task, "task id")
        s = object.__setattr__
        s(self, "outcomes", _items(self.outcomes, "outcomes", 1))
        s(self, "constraints", _items(self.constraints, "constraints"))
        s(self, "assumptions", _items(self.assumptions, "assumptions"))
        s(self, "not_verifiable", _items(self.not_verifiable, "not_verifiable"))
        s(self, "out_of_scope", _items(self.out_of_scope, "out_of_scope"))
        s(self, "behaviour", _objects(self.behaviour, "behaviour", Behaviour))
        s(self, "questions", _objects(self.questions, "questions", Question))
        s(self, "requirements", _objects(self.requirements, "requirements", SummaryRequirement))
        if not self.requirements:
            raise ValueError("requirements: needs at least 1 item")
        ids = [r.id for r in self.requirements]
        if len(set(ids)) != len(ids):
            raise ValueError("requirements: duplicate requirement id")
        if any(r.outcome is not None and r.outcome > len(self.outcomes) for r in self.requirements):
            raise ValueError("requirements.outcome: no such outcome")

    @property
    def unanswered(self) -> int:
        return sum(1 for q in self.questions if q.answer is None)


# ---- parsing -----------------------------------------------------------------------------------------
def _table(raw, field: str, allowed, required=()) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{field}: must be a table")
    for k in raw:
        if k not in allowed:
            raise ValueError(f"{field}: unknown key {str(k)[:40]!r}")
    for k in required:
        if k not in raw:
            raise ValueError(f"{field}: missing key '{k}'")
    return raw


def _wrapped(fn, field: str, raw: dict):
    try:
        return fn(**raw)
    except ValueError as e:
        raise ValueError(f"{field}: {e}") from None


def _tables(value, block: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{block}: must be a list of tables")
    return value


def from_mapping(raw) -> RequirementsSummary:
    """Build a summary from parsed TOML or JSON. Unknown keys anywhere are refused."""
    top = _table(raw, "summary", _TOP_KEYS, ("task", "outcomes", "requirements"))
    behaviour = [_wrapped(Behaviour, f"behaviour[{i}]", _table(t, f"behaviour[{i}]", ("given", "expect"),
                                                               ("given", "expect")))
                 for i, t in enumerate(_tables(top.get("behaviour", []), "behaviour"), 1)]
    questions = [_wrapped(Question, f"questions[{i}]", _table(t, f"questions[{i}]", ("q", "answer"), ("q",)))
                 for i, t in enumerate(_tables(top.get("questions", []), "questions"), 1)]
    reqs = [_wrapped(SummaryRequirement, f"requirements[{i}]",
                     _table(t, f"requirements[{i}]", ("id", "text", "confidence", "outcome"),
                            ("id", "text", "confidence")))
            for i, t in enumerate(_tables(top["requirements"], "requirements"), 1)]
    return RequirementsSummary(
        task=top["task"], outcomes=top["outcomes"], requirements=tuple(reqs), behaviour=tuple(behaviour),
        constraints=top.get("constraints", []), assumptions=top.get("assumptions", []),
        not_verifiable=top.get("not_verifiable", []), out_of_scope=top.get("out_of_scope", []),
        questions=tuple(questions))


def parse_summary(data: bytes) -> RequirementsSummary:
    """``SUMMARY.toml`` bytes -> summary. ValueError names the field, never the content."""
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("file: larger than 64 KB")
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise ValueError("file: not valid UTF-8") from None
    except tomllib.TOMLDecodeError as e:
        where = re.search(r"\(at line \d+, column \d+\)", str(e))
        raise ValueError("file: not valid TOML" + (" " + where.group(0) if where else "")) from None
    return from_mapping(raw)


def load_summary(path) -> RequirementsSummary:
    with Path(path).open("rb") as fh:
        data = fh.read(MAX_FILE_BYTES + 1)
    return parse_summary(data)


# ---- canonical form and hash --------------------------------------------------------------------------
def to_dict(s: RequirementsSummary) -> dict:
    return {
        "task": s.task,
        "outcomes": list(s.outcomes),
        "behaviour": [{"given": b.given, "expect": b.expect} for b in s.behaviour],
        "constraints": list(s.constraints),
        "assumptions": list(s.assumptions),
        "not_verifiable": list(s.not_verifiable),
        "out_of_scope": list(s.out_of_scope),
        "questions": [{"q": q.q, **({"answer": q.answer} if q.answer is not None else {})}
                      for q in s.questions],
        "requirements": [{"id": r.id, "text": r.text, "confidence": r.confidence,
                          **({"outcome": r.outcome} if r.outcome is not None else {})}
                         for r in s.requirements],
    }


def canonical_json(s: RequirementsSummary) -> str:
    """Sorted keys, no whitespace, ASCII only: the same style as the check-set manifest."""
    return json.dumps(to_dict(s), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def summary_hash(s: RequirementsSummary) -> str:
    return hashlib.sha256(canonical_json(s).encode("ascii")).hexdigest()


def _check_hash(value) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value.lower()):
        raise ValueError("summary hash: must be 64 hexadecimal digits")
    return value.lower()


# ---- comparison with the spec -------------------------------------------------------------------------
def requirement_diff(s: RequirementsSummary, requirements) -> list[str]:
    """Ids of requirements that differ between the summary and ``requirements`` (a spec's): present in
    only one, or with a different text. Empty when they are exactly equal."""
    from collections import Counter
    a = Counter((r.id, r.text) for r in s.requirements)
    b = Counter((r.id, r.text) for r in requirements)
    ids = {i for (i, _t) in list((a - b).keys()) + list((b - a).keys())}
    return sorted(ids, key=lambda i: (int(i[2:]) if _REQ_ID.fullmatch(i) else 10 ** 9, i))


def spec_requirements(s: RequirementsSummary) -> tuple[Requirement, ...]:
    return tuple(Requirement(r.id, r.text) for r in s.requirements)


# ---- storage -----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Decision:
    id: int
    summary_hash: str
    decision: str                       # approved | rejected
    decider: str
    note: str | None
    may_leave_machine: bool | None      # approvals only; recorded here, nothing uses it yet
    decided_at: float


def _decision(row) -> Decision:
    return Decision(row["id"], row["summary_hash"], row["decision"], row["decider"], row["note"],
                    None if row["may_leave_machine"] is None else bool(row["may_leave_machine"]),
                    row["decided_at"])


def import_summary(conn: sqlite3.Connection, s: RequirementsSummary) -> str:
    """Store a summary under its hash and return the hash. Immutable: importing the same content
    again changes nothing."""
    h = summary_hash(s)
    conn.execute("INSERT OR IGNORE INTO summaries(hash, task_id, summary_json, created_at) VALUES (?,?,?,?)",
                 (h, s.task, canonical_json(s), time.time()))
    return h


def get_summary(conn: sqlite3.Connection, h: str) -> RequirementsSummary:
    """The stored summary, re-hashed on the way out. ValueError if unknown; SummaryTampered if the
    stored text no longer hashes to its key."""
    h = _check_hash(h)
    row = conn.execute("SELECT task_id, summary_json FROM summaries WHERE hash=?", (h,)).fetchone()
    if row is None:
        raise ValueError("unknown summary: import it with `hera summary import FILE`")
    try:
        s = from_mapping(json.loads(row["summary_json"]))
    except (ValueError, TypeError, KeyError) as e:
        raise SummaryTampered("stored summary is malformed") from e
    if summary_hash(s) != h or s.task != row["task_id"]:
        raise SummaryTampered("stored summary does not match its hash")
    return s


def state(conn: sqlite3.Connection, h: str) -> tuple[str, Decision | None]:
    """``("none", None)`` before any decision, else ``("approved" | "rejected", the latest decision)``."""
    h = _check_hash(h)
    row = conn.execute("SELECT * FROM summary_decisions WHERE summary_hash=? ORDER BY id DESC LIMIT 1",
                       (h,)).fetchone()
    if row is None:
        return NOT_APPROVED, None
    d = _decision(row)
    return d.decision, d


def history(conn: sqlite3.Connection, h: str) -> list[Decision]:
    return [_decision(r) for r in conn.execute(
        "SELECT * FROM summary_decisions WHERE summary_hash=? ORDER BY id", (_check_hash(h),))]


def decide(conn: sqlite3.Connection, h: str, decision: str, decider: str, *, note: str | None = None,
           may_leave_machine: bool | None = None) -> Decision:
    """Append a decision. The latest one is the state, so a rejected summary can still be approved
    later and both stay on record. The decider must be a person (``human:<name>``). An approval needs
    an explicit ``may_leave_machine`` (True or False) and is refused while a question is unanswered; a
    rejection needs a note and carries no such flag."""
    h = _check_hash(h)
    if decision not in (APPROVED, REJECTED):
        raise ValueError("decision: must be 'approved' or 'rejected'")
    if not isinstance(decider, str) or not decider.startswith("human:") or not decider[len("human:"):].strip():
        raise ValueError("decider: must be a person, written human:<name>")
    _line(decider, "decider", MAX_DECIDER_CHARS)
    s = get_summary(conn, h)
    if decision == APPROVED:
        if not isinstance(may_leave_machine, bool):
            raise ValueError("may_leave_machine: an approval must say yes or no")
        if s.unanswered:
            raise ValueError(f"questions: {s.unanswered} unanswered; answer them in the file and import it again")
        if note is not None:
            note = _line(note, "note", MAX_NOTE_CHARS)
    else:
        if may_leave_machine is not None:
            raise ValueError("may_leave_machine: only an approval records it")
        note = _line(note, "note", MAX_NOTE_CHARS)
    cur = conn.execute("INSERT INTO summary_decisions(summary_hash, decision, decider, note, may_leave_machine,"
                       " decided_at) VALUES (?,?,?,?,?,?)",
                       (h, decision, decider, note, None if may_leave_machine is None else int(may_leave_machine),
                        time.time()))
    return _decision(conn.execute("SELECT * FROM summary_decisions WHERE id=?", (cur.lastrowid,)).fetchone())


def approved_summary_for(conn: sqlite3.Connection, task_id: str, h: str) -> RequirementsSummary:
    """The summary ``h`` if it exists, belongs to ``task_id`` and its latest decision is an approval;
    else ValueError saying which of those failed."""
    s = get_summary(conn, h)
    if s.task != task_id:
        raise ValueError("the summary belongs to a different task")
    st, _d = state(conn, h)
    if st != APPROVED:
        raise ValueError(f"the summary is not approved (state: {'no decision yet' if st == NOT_APPROVED else st})")
    return s


def latest_for_task(conn: sqlite3.Connection, task_id: str) -> str | None:
    """Hash of the summary most recently imported for a task (importing old content again does not move it)."""
    row = conn.execute("SELECT hash FROM summaries WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
                       (task_id,)).fetchone()
    return row["hash"] if row else None


def list_summaries(conn: sqlite3.Connection, task_id: str | None = None) -> list[dict]:
    rows = conn.execute("SELECT hash, task_id, created_at FROM summaries"
                        + (" WHERE task_id=?" if task_id else "") + " ORDER BY rowid",
                        (task_id,) if task_id else ()).fetchall()
    out = []
    for r in rows:
        st, d = state(conn, r["hash"])
        out.append({"hash": r["hash"], "task_id": r["task_id"], "created_at": r["created_at"],
                    "state": st, "decision": d})
    return out


# ---- the screen --------------------------------------------------------------------------------------
def _flat(text) -> str:
    """One line of printable text (the same flattening as cli._clean, without the limit)."""
    flat = " ".join(str(text if text is not None else "").split())
    return "".join(ch if ch.isprintable() else "?" for ch in flat)


def display_width(text: str) -> int:
    """Terminal columns of ``text``: wide (East Asian) characters count 2, combining marks 0."""
    n = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def _fit(text: str, width: int) -> tuple[str, bool]:
    """``text`` cut to ``width`` columns, ending in "..." when it was cut."""
    if display_width(text) <= width:
        return text, False
    out, n = [], 0
    for ch in text:
        w = display_width(ch)
        if n + w > width - 3:
            break
        out.append(ch)
        n += w
    return "".join(out) + "...", True


def _rows(label: str, texts: list[str], full: bool, cut: list) -> list[str]:
    """Lines of one block: the label in the left column of its first line, one line per item (cut to
    the screen width, or wrapped when ``full``)."""
    room = SCREEN_WIDTH - _GUTTER
    out = []
    for i, t in enumerate(texts or ["(none)"]):
        first = label.ljust(_GUTTER) if i == 0 else " " * _GUTTER
        if full:
            for j, part in enumerate(textwrap.wrap(t, room) or [""]):
                out.append((first if j == 0 else " " * _GUTTER) + part)
        else:
            t, was_cut = _fit(t, room)
            cut.append(was_cut)
            out.append(first + t)
    return out


def render(s: RequirementsSummary, approval: Decision | None = None, *, full: bool = False) -> str:
    """The one-screen text: at most 40 lines of at most 100 columns whatever the content (an item
    that does not fit ends in "..."; ``full`` wraps instead and has no bound). Blocks follow the
    design's order, then every requirement with its planned verification confidence (LOW is marked),
    then a footer with the first 12 characters of the hash and the approval state. Unanswered
    questions say they block approval."""
    cut: list = []
    n = lambda xs: [f"{i}. {_flat(x)}" for i, x in enumerate(xs, 1)]          # noqa: E731
    body = []
    body += _rows("Outcomes", n(s.outcomes), full, cut)
    body += _rows("Behaviour", n(f"{_flat(b.given)} => {_flat(b.expect)}" for b in s.behaviour), full, cut)
    body += _rows("Constraints", n(s.constraints), full, cut)
    body += _rows("Assumptions", n(s.assumptions), full, cut)
    body += _rows("Not verifiable", n(s.not_verifiable), full, cut)
    body += _rows("Out of scope", n(s.out_of_scope), full, cut)
    qs = []
    for i, q in enumerate(s.questions, 1):
        qs.append(f"{i}. [UNANSWERED - blocks approval] {_flat(q.q)}" if q.answer is None
                  else f"{i}. {_flat(q.q)} => {_flat(q.answer)}")
    body += _rows("Questions", qs, full, cut)
    reqs = []
    for r in s.requirements:
        conf = "LOW !!" if r.confidence == "low" else r.confidence
        link = f"[outcome {r.outcome}] " if r.outcome is not None else ""
        reqs.append(f"{r.id:<7}{conf:<8}{link}{_flat(r.text)}")
    body += _rows("Requirements", reqs, full, cut)

    h = summary_hash(s)
    if approval is None:
        tail = "NOT APPROVED" + (f" | {s.unanswered} unanswered question(s) block approval" if s.unanswered else "")
    elif approval.decision == APPROVED:
        tail = (f"APPROVED | text may leave the machine: {'yes' if approval.may_leave_machine else 'no'}"
                f" | by {_flat(approval.decider)}")
    else:
        tail = f"REJECTED by {_flat(approval.decider)}: {_flat(approval.note)}"
    title = f"Requirements summary: {_flat(s.task)}"
    footer = f"hash {h[:12]} | {tail}"
    if full:
        head, foot = textwrap.wrap(title, SCREEN_WIDTH), textwrap.wrap(footer, SCREEN_WIDTH)
    else:
        head, foot = [_fit(title + (" (cut: --full)" if any(cut) else ""), SCREEN_WIDTH)[0]], \
                     [_fit(footer, SCREEN_WIDTH)[0]]
    return "\n".join(head + body + foot)
