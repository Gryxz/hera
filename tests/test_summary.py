"""The requirements summary: parsing and limits, the hash, the one-screen render, storage and decisions."""
import json
import sqlite3

import pytest

from hera.oracle import summary as sm
from hera.oracle.model import Requirement
from hera.store import db

LINE = "x" * 200


def maximum() -> dict:
    """A summary at every limit, every item 200 characters long."""
    n = lambda tag, k: [f"{tag}{i}".ljust(200, "x") for i in range(k)]            # noqa: E731
    return {
        "task": "t" * 64,
        "outcomes": n("o", 5),
        "behaviour": [{"given": f"g{i}".ljust(200, "x"), "expect": f"e{i}".ljust(200, "x")} for i in range(6)],
        "constraints": n("c", 5), "assumptions": n("a", 5), "not_verifiable": n("n", 5),
        "out_of_scope": n("s", 3),
        "questions": [{"q": f"q{i}".ljust(200, "x"), "answer": f"r{i}".ljust(200, "x")} for i in range(3)],
        "requirements": [{"id": f"R-{i + 1}", "text": f"t{i}".ljust(200, "x"),
                          "confidence": ("low", "medium", "high")[i % 3], "outcome": i % 5 + 1}
                         for i in range(6)],
    }


def small() -> dict:
    return {"task": "demo", "outcomes": ["Amounts can be read"],
            "requirements": [{"id": "R-1", "text": "parse_amount('12.50') returns 12.5", "confidence": "high"}]}


def toml_of(d: dict, reverse: bool = False) -> bytes:
    """TOML text of a summary dict; ``reverse`` lists the keys (and the keys of every table) backwards."""
    q = json.dumps
    keys = list(d)[::-1] if reverse else list(d)
    scalars = [f"{k} = {q(d[k])}" for k in keys if not (isinstance(d[k], list) and d[k] and isinstance(d[k][0], dict))]
    tables = []
    for k in keys:
        if isinstance(d[k], list) and d[k] and isinstance(d[k][0], dict):
            for item in d[k]:
                items = list(item.items())[::-1] if reverse else list(item.items())
                tables += ["", f"[[{k}]]"] + [f"{a} = {q(b)}" for a, b in items]
    return "\n".join(scalars + tables).encode("utf-8")


def parse(d: dict) -> sm.RequirementsSummary:
    return sm.from_mapping(d)


# ---- parsing and limits ------------------------------------------------------------------------------------
def test_a_summary_with_every_block_parses_from_toml():
    d = maximum()
    s = sm.parse_summary(toml_of(d))
    assert s.task == "t" * 64 and len(s.outcomes) == 5 and len(s.behaviour) == 6 and len(s.questions) == 3
    assert s.requirements[0].confidence == "low" and s.requirements[1].outcome == 2
    assert s.unanswered == 0
    assert sm.parse_summary(toml_of(small())).behaviour == ()          # every block but outcomes and requirements is optional


@pytest.mark.parametrize("block", ["outcomes", "behaviour", "constraints", "assumptions", "not_verifiable",
                                   "out_of_scope", "questions", "requirements"])
def test_every_limit_passes_at_its_maximum_and_is_refused_one_above_it(block):
    d = maximum()
    assert parse(d)                                                    # at the maximum
    over = dict(d)
    extra = {"behaviour": {"given": "g", "expect": "e"}, "questions": {"q": "why", "answer": "because"},
             "requirements": {"id": "R-99", "text": "one more", "confidence": "high"}}.get(block, "one more")
    over[block] = list(d[block]) + [extra]
    with pytest.raises(ValueError, match=rf"^{block}: at most {sm.LIMITS[block]} items"):
        parse(over)


def test_the_limits_are_the_designs():
    assert sm.LIMITS["outcomes"] == 5 and sm.LIMITS["behaviour"] == 6 and sm.LIMITS["constraints"] == 5
    assert sm.LIMITS["assumptions"] == 5 and sm.LIMITS["not_verifiable"] == 5
    assert sm.LIMITS["out_of_scope"] == 3 and sm.LIMITS["questions"] == 3


@pytest.mark.parametrize("bad", ["two\nlines", "tab\there", "nul\x00byte", "esc\x1b[31m", "bell\x07", "sep x",
                                 "", "   ", LINE + "x"])
def test_an_item_must_be_one_non_empty_printable_line_of_at_most_200_characters(bad):
    for put in (lambda d: d.update(outcomes=[bad]), lambda d: d.update(constraints=[bad]),
                lambda d: d.update(behaviour=[{"given": bad, "expect": "e"}]),
                lambda d: d.update(questions=[{"q": "q", "answer": bad}]),
                lambda d: d["requirements"][0].update(text=bad)):
        d = small()
        put(d)
        with pytest.raises(ValueError) as e:
            parse(d)
        assert bad.strip() == "" or bad not in str(e.value)            # the message never echoes the content
    d = small()
    d["outcomes"] = [LINE]
    assert parse(d).outcomes == (LINE,)                                # exactly 200 is fine


def test_error_messages_name_the_field_with_its_position():
    d = small()
    d["assumptions"] = ["fine", "bad\nline"]
    with pytest.raises(ValueError, match=r"^assumptions\[2\]: must be one line"):
        parse(d)
    d = small()
    d["behaviour"] = [{"given": "g", "expect": "e"}, {"given": "g", "expect": " "}]
    with pytest.raises(ValueError, match=r"^behaviour\[2\]: expect: must be a non-empty string"):
        parse(d)


def test_a_summary_needs_an_outcome_and_a_requirement():
    d = small()
    d["outcomes"] = []
    with pytest.raises(ValueError, match="^outcomes: needs at least 1"):
        parse(d)
    d = small()
    d["requirements"] = []
    with pytest.raises(ValueError, match="^requirements: needs at least 1"):
        parse(d)
    for key in ("task", "outcomes", "requirements"):
        d = small()
        del d[key]
        with pytest.raises(ValueError, match=f"missing key '{key}'"):
            parse(d)


def test_unknown_keys_are_refused_everywhere():
    d = small()
    d["extra"] = "x"
    with pytest.raises(ValueError, match="^summary: unknown key 'extra'"):
        parse(d)
    for block, item in (("behaviour", {"given": "g", "expect": "e", "why": "x"}),
                        ("questions", {"q": "q", "ask": "x"}),
                        ("requirements", {"id": "R-1", "text": "t", "confidence": "high", "weight": 3})):
        d = small()
        d[block] = [item]
        with pytest.raises(ValueError, match=rf"^{block}\[1\]: unknown key"):
            parse(d)


def test_requirement_ids_must_be_valid_and_unique():
    d = small()
    d["requirements"].append({"id": "R-1", "text": "again", "confidence": "low"})
    with pytest.raises(ValueError, match="duplicate requirement id"):
        parse(d)
    for bad in ("R1", "r-1", "R-", "R-12345", "X-1", ""):
        d = small()
        d["requirements"][0]["id"] = bad
        with pytest.raises(ValueError, match=r"requirements\[1\]: id"):
            parse(d)


@pytest.mark.parametrize("bad", [0, 2, -1, True, "1", 1.0])
def test_an_outcome_index_must_be_in_range(bad):
    d = small()                                    # one outcome: only 1 is valid
    d["requirements"][0]["outcome"] = bad
    with pytest.raises(ValueError, match="outcome"):
        parse(d)
    d["requirements"][0]["outcome"] = 1
    assert parse(d).requirements[0].outcome == 1


@pytest.mark.parametrize("bad", ["HIGH", "urgent", "", None, 3])
def test_a_confidence_must_be_high_medium_or_low(bad):
    d = small()
    d["requirements"][0]["confidence"] = bad
    with pytest.raises(ValueError, match="confidence"):
        parse(d)


def test_the_file_must_be_small_utf8_toml_and_errors_do_not_echo_it():
    with pytest.raises(ValueError, match="larger than 64 KB"):
        sm.parse_summary(b"#" * (sm.MAX_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="not valid UTF-8"):
        sm.parse_summary(b"\xff\xfe")
    with pytest.raises(ValueError, match=r"^file: not valid TOML \(at line 2") as e:
        sm.parse_summary(b'task = "demo"\nSECRET-CANARY ===\n')
    assert "SECRET-CANARY" not in str(e.value)


# ---- the hash ----------------------------------------------------------------------------------------------
def test_the_hash_is_stable_under_key_reordering_in_the_toml():
    d = maximum()
    a, b = sm.parse_summary(toml_of(d)), sm.parse_summary(toml_of(d, reverse=True))
    assert a == b and sm.summary_hash(a) == sm.summary_hash(b)
    assert len(sm.summary_hash(a)) == 64 and sm.canonical_json(a).isascii()
    compact = sm.canonical_json(sm.parse_summary(toml_of(small())))
    assert ", " not in compact and '": ' not in compact


MUTATIONS = {
    "task": lambda d: d.update(task="other"),
    "outcome text": lambda d: d["outcomes"].__setitem__(0, "Amounts can be read!"),
    "outcome added": lambda d: d["outcomes"].append("another"),
    "outcome order": lambda d: d.update(outcomes=["b", "a"]),
    "behaviour": lambda d: d.update(behaviour=[{"given": "g", "expect": "e"}]),
    "constraint": lambda d: d.update(constraints=["c"]),
    "assumption": lambda d: d.update(assumptions=["a"]),
    "not verifiable": lambda d: d.update(not_verifiable=["n"]),
    "out of scope": lambda d: d.update(out_of_scope=["s"]),
    "question": lambda d: d.update(questions=[{"q": "q"}]),
    "answer": lambda d: d.update(questions=[{"q": "q", "answer": "a"}]),
    "req text": lambda d: d["requirements"][0].update(text="different"),
    "req id": lambda d: d["requirements"][0].update(id="R-2"),
    "confidence": lambda d: d["requirements"][0].update(confidence="low"),
    "outcome link": lambda d: d["requirements"][0].update(outcome=1),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_the_hash_changes_with_any_content_change(name):
    base = small()
    base["outcomes"] = ["a", "b"] if name == "outcome order" else base["outcomes"]
    changed = json.loads(json.dumps(base))
    MUTATIONS[name](changed)
    assert sm.summary_hash(parse(base)) != sm.summary_hash(parse(changed))


# ---- the screen --------------------------------------------------------------------------------------------
def _decision(kind="approved", may=False, note=None, decider="human:" + "n" * 74):
    return sm.Decision(1, "0" * 64, kind, decider, note, may if kind == "approved" else None, 1.0)


def _bound(text: str):
    lines = text.split("\n")
    assert len(lines) <= sm.SCREEN_LINES, len(lines)
    assert all(len(ln) <= sm.SCREEN_WIDTH and sm.display_width(ln) <= sm.SCREEN_WIDTH for ln in lines)
    return lines


def test_a_summary_at_every_limit_renders_in_at_most_40_lines_of_at_most_100_columns():
    s = parse(maximum())
    for approval in (None, _decision("approved", True), _decision("rejected", note="n" * 200)):
        assert len(_bound(sm.render(s, approval))) == 40            # the bound is met exactly, not loosely


def test_the_bound_holds_for_wide_characters_and_unanswered_questions_too():
    d = maximum()
    wide = "あ" * 100                                               # 100 characters, 200 columns
    for key in ("outcomes", "constraints", "assumptions", "not_verifiable", "out_of_scope"):
        d[key] = [wide] * len(d[key])
    d["behaviour"] = [{"given": wide, "expect": wide}] * 6
    d["questions"] = [{"q": wide}] * 3
    d["requirements"] = [dict(r, text=wide) for r in d["requirements"]]
    s = parse(d)
    assert s.unanswered == 3
    _bound(sm.render(s))
    _bound(sm.render(s, _decision("approved", False, decider="human:" + "あ" * 35)))


def test_the_screen_shows_every_block_in_order_the_confidence_and_the_footer():
    d = small()
    d.update(constraints=["add is unchanged"], assumptions=["empty means error"], not_verifiable=["looks fine"],
             out_of_scope=["currencies"], behaviour=[{"given": "parse_amount('7')", "expect": "7.0"}],
             questions=[{"q": "Allow commas?"}],
             requirements=[{"id": "R-1", "text": "reads amounts", "confidence": "high", "outcome": 1},
                           {"id": "R-2", "text": "rejects empty", "confidence": "low"}])
    s = parse(d)
    text = sm.render(s)
    labels = ["Outcomes", "Behaviour", "Constraints", "Assumptions", "Not verifiable", "Out of scope", "Questions",
              "Requirements"]
    at = [text.index("\n" + lb) for lb in labels]
    assert at == sorted(at)
    assert "parse_amount('7') => 7.0" in text and "[UNANSWERED - blocks approval] Allow commas?" in text
    assert "R-1    high" in text and "[outcome 1] reads amounts" in text
    assert "R-2    LOW !!" in text and "!!" not in text.split("R-1")[1].split("R-2")[0]
    foot = text.splitlines()[-1]
    assert foot.startswith(f"hash {sm.summary_hash(s)[:12]} | NOT APPROVED") and "1 unanswered question(s)" in foot
    assert text.splitlines()[0] == "Requirements summary: demo"
    assert "(none)" in sm.render(parse(small())).split("Constraints")[1].splitlines()[0]
    answered = sm.render(parse(dict(d, questions=[{"q": "Allow commas?", "answer": "no"}])))
    assert "Allow commas? => no" in answered and "UNANSWERED" not in answered


def test_the_footer_names_the_approval_state():
    s = parse(small())
    assert sm.render(s, _decision("approved", True, decider="human:ana")).splitlines()[-1].endswith(
        "APPROVED | text may leave the machine: yes | by human:ana")
    assert "text may leave the machine: no" in sm.render(s, _decision("approved", False))
    assert sm.render(s, _decision("rejected", note="too vague", decider="human:ana")).splitlines()[-1].endswith(
        "REJECTED by human:ana: too vague")


def test_everything_shown_is_flattened_to_printable_single_line_text():
    s = parse(small())
    evil = _decision("rejected", note="line one\nline two\x1b[31m red\x07", decider="human:a\tb")
    text = sm.render(s, evil)
    assert "\x1b" not in text and "\x07" not in text and "\t" not in text
    assert len(text.split("\n")) == len(sm.render(s).split("\n"))
    assert "line one line two?[31m red?" in text


def test_a_cut_line_ends_in_dots_says_so_and_full_shows_everything():
    d = small()
    d["outcomes"] = ["word " * 40]                                  # 199 characters
    s = parse(d)
    short, full = sm.render(s), sm.render(s, full=True)
    assert "..." in short and short.splitlines()[0].endswith("(cut: --full)")
    assert "(cut" not in sm.render(parse(small()))
    assert "..." not in full and "(cut" not in full
    assert " ".join(ln.strip() for ln in full.splitlines()[1:4]).count("word") == 40
    assert max(len(ln) for ln in full.splitlines()) <= sm.SCREEN_WIDTH


# ---- storage and decisions ---------------------------------------------------------------------------------
def test_importing_the_same_content_again_is_a_no_op(conn):
    s = parse(small())
    h = sm.import_summary(conn, s)
    first = conn.execute("SELECT * FROM summaries").fetchall()
    assert sm.import_summary(conn, parse(small())) == h == sm.summary_hash(s)
    assert [tuple(r) for r in conn.execute("SELECT * FROM summaries")] == [tuple(r) for r in first]
    assert sm.get_summary(conn, h) == s
    assert sm.get_summary(conn, h.upper()) == s
    d = small()
    d["outcomes"] = ["edited"]
    assert sm.import_summary(conn, parse(d)) != h and conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 2


def test_summaries_and_decisions_cannot_be_changed_or_deleted(conn):
    h = sm.import_summary(conn, parse(small()))
    sm.decide(conn, h, "approved", "human:ana", may_leave_machine=False)
    for sql in ("UPDATE summaries SET task_id='x'", "DELETE FROM summaries",
                "UPDATE summary_decisions SET decision='rejected'", "DELETE FROM summary_decisions"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    assert sm.state(conn, h)[0] == "approved"


def test_a_stored_summary_that_no_longer_matches_its_hash_is_refused(conn):
    h = sm.import_summary(conn, parse(small()))
    conn.execute("DROP TRIGGER summaries_no_update")
    d = small()
    d["outcomes"] = ["swapped"]
    conn.execute("UPDATE summaries SET summary_json=?", (json.dumps(d),))
    with pytest.raises(sm.SummaryTampered):
        sm.get_summary(conn, h)
    conn.execute("UPDATE summaries SET summary_json='not json'")
    with pytest.raises(sm.SummaryTampered):
        sm.get_summary(conn, h)


def test_unknown_or_malformed_hashes_are_refused(conn):
    with pytest.raises(ValueError, match="unknown summary"):
        sm.get_summary(conn, "0" * 64)
    for bad in ("", "abc", "z" * 64, None, "0" * 63):
        with pytest.raises(ValueError, match="64 hexadecimal"):
            sm.state(conn, bad)
    with pytest.raises(ValueError, match="unknown summary"):
        sm.decide(conn, "0" * 64, "approved", "human:ana", may_leave_machine=True)


def test_approve_reject_and_approve_again_keeps_the_whole_history(conn):
    h = sm.import_summary(conn, parse(small()))
    assert sm.state(conn, h) == ("none", None)
    sm.decide(conn, h, "approved", "human:ana", may_leave_machine=False)
    rej = sm.decide(conn, h, "rejected", "human:ben", note="scope is too wide")
    assert sm.state(conn, h) == ("rejected", rej) and rej.may_leave_machine is None
    last = sm.decide(conn, h, "approved", "human:ana", may_leave_machine=True, note="narrowed by agreement")
    st, d = sm.state(conn, h)
    assert st == "approved" and d == last and d.may_leave_machine is True and d.note == "narrowed by agreement"
    assert [(x.decision, x.decider, x.may_leave_machine) for x in sm.history(conn, h)] == [
        ("approved", "human:ana", False), ("rejected", "human:ben", None), ("approved", "human:ana", True)]
    assert conn.execute("SELECT may_leave_machine FROM summary_decisions ORDER BY id").fetchall()[1][0] is None


def test_an_approval_is_refused_while_a_question_has_no_answer(conn):
    d = small()
    d["questions"] = [{"q": "Allow commas?"}, {"q": "Round?", "answer": "no"}]
    h = sm.import_summary(conn, parse(d))
    with pytest.raises(ValueError, match="1 unanswered"):
        sm.decide(conn, h, "approved", "human:ana", may_leave_machine=False)
    assert sm.state(conn, h) == ("none", None) and conn.execute("SELECT COUNT(*) FROM summary_decisions").fetchone()[0] == 0
    sm.decide(conn, h, "rejected", "human:ana", note="answer the question first")           # rejecting is still allowed
    d["questions"][0]["answer"] = "yes"
    h2 = sm.import_summary(conn, parse(d))                           # the edit is a new hash with its own approval
    assert h2 != h
    sm.decide(conn, h2, "approved", "human:ana", may_leave_machine=False)
    assert sm.state(conn, h2)[0] == "approved" and sm.state(conn, h)[0] == "rejected"


def test_an_approval_must_say_whether_the_text_may_leave_the_machine(conn):
    h = sm.import_summary(conn, parse(small()))
    for bad in (None, "yes", "no", 1, 0):
        with pytest.raises(ValueError, match="^may_leave_machine: an approval must say yes or no"):
            sm.decide(conn, h, "approved", "human:ana", may_leave_machine=bad)
    with pytest.raises(ValueError, match="only an approval"):
        sm.decide(conn, h, "rejected", "human:ana", note="no", may_leave_machine=False)
    assert sm.history(conn, h) == []


def test_the_decider_must_be_a_person_and_a_rejection_needs_a_note(conn):
    h = sm.import_summary(conn, parse(small()))
    for bad in ("ana", "model:gpt", "human:", "human:  ", "Human:ana", "", None, "human:" + "a" * 80, "human:a\nb"):
        with pytest.raises(ValueError, match="^decider"):
            sm.decide(conn, h, "approved", bad, may_leave_machine=True)
    for bad in (None, "", "  ", "two\nlines"):
        with pytest.raises(ValueError, match="^note"):
            sm.decide(conn, h, "rejected", "human:ana", note=bad)
    with pytest.raises(ValueError, match="^decision"):
        sm.decide(conn, h, "maybe", "human:ana")
    assert sm.history(conn, h) == []


def test_approved_summary_for_checks_existence_task_and_state(conn):
    h = sm.import_summary(conn, parse(small()))
    with pytest.raises(ValueError, match="no decision yet"):
        sm.approved_summary_for(conn, "demo", h)
    sm.decide(conn, h, "rejected", "human:ana", note="no")
    with pytest.raises(ValueError, match="state: rejected"):
        sm.approved_summary_for(conn, "demo", h)
    sm.decide(conn, h, "approved", "human:ana", may_leave_machine=False)
    assert sm.approved_summary_for(conn, "demo", h) == parse(small())
    with pytest.raises(ValueError, match="different task"):
        sm.approved_summary_for(conn, "another", h)
    with pytest.raises(ValueError, match="unknown summary"):
        sm.approved_summary_for(conn, "demo", "1" * 64)


def test_requirement_diff_names_every_differing_id():
    s = parse(dict(small(), requirements=[
        {"id": "R-1", "text": "one", "confidence": "high"}, {"id": "R-2", "text": "two", "confidence": "high"},
        {"id": "R-10", "text": "ten", "confidence": "low"}]))
    same = [Requirement("R-10", "ten"), Requirement("R-1", "one"), Requirement("R-2", "two")]
    assert sm.requirement_diff(s, same) == []
    assert sm.requirement_diff(s, [Requirement("R-1", "one"), Requirement("R-2", "TWO"),
                                   Requirement("R-3", "three")]) == ["R-2", "R-3", "R-10"]
    assert sm.requirement_diff(s, same + [Requirement("R-1", "one")]) == ["R-1"]     # a duplicate in the spec


def test_latest_and_list_follow_import_order(conn):
    assert sm.latest_for_task(conn, "demo") is None and sm.list_summaries(conn) == []
    a = sm.import_summary(conn, parse(small()))
    d = small()
    d["outcomes"] = ["second"]
    b = sm.import_summary(conn, parse(d))
    sm.import_summary(conn, parse(small()))                          # old content again does not move "latest"
    assert sm.latest_for_task(conn, "demo") == b and sm.latest_for_task(conn, "other") is None
    sm.decide(conn, a, "approved", "human:ana", may_leave_machine=False)
    rows = sm.list_summaries(conn, "demo")
    assert [(r["hash"], r["state"]) for r in rows] == [(a, "approved"), (b, "none")]
    assert sm.list_summaries(conn, "other") == []
