"""Disputes: opening, independence of the resolver, supersession, and the migration table."""
import dataclasses

import pytest

from hera.oracle import checkstore
from hera.oracle.checkstore import freeze
from hera.oracle.disputes import has_open_dispute, open_dispute, resolve_dispute
from toyrepo import CHECK_FILES, CHECKS, REQUIREMENTS


@pytest.fixture
def cs(conn, hera_home):
    checks = tuple(dataclasses.replace(c, author_model="author-model-a") for c in CHECKS)
    return freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=checks, files=dict(CHECK_FILES))


def opened(conn, cs, **kw):
    args = dict(task_id="toy", checkset_hash=cs.hash, check_id="chk-r1-basic", requirement_id="R-1", argument="the check is wrong")
    args.update(kw)
    return open_dispute(conn, **args)


def test_open_and_has_open(conn, cs):
    assert not has_open_dispute(conn, "toy")
    d = opened(conn, cs)
    assert has_open_dispute(conn, "toy") and not has_open_dispute(conn, "other")
    row = conn.execute("SELECT * FROM disputes WHERE id=?", (d,)).fetchone()
    assert row["status"] == "open" and row["check_id"] == "chk-r1-basic" and row["opened_at"]


def test_argument_is_capped_and_required(conn, cs):
    d = opened(conn, cs, argument="x" * 9000)
    assert len(conn.execute("SELECT argument FROM disputes WHERE id=?", (d,)).fetchone()[0]) == 4000
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            opened(conn, cs, argument=bad)


def test_hidden_unknown_and_mismatched_checks_all_give_the_same_error(conn, cs):
    errs = []
    for kw in ({"check_id": "chk-hidden-r1-seven"}, {"check_id": "chk-does-not-exist"},
               {"requirement_id": "R-2"}, {"task_id": "other"}, {"checkset_hash": "0" * 64}):
        with pytest.raises(ValueError) as e:
            opened(conn, cs, **kw)
        errs.append(str(e.value))
    assert len(set(errs)) == 1 and "hidden" not in errs[0] and "chk-" not in errs[0]
    assert not has_open_dispute(conn, "toy")


def test_resolver_must_be_independent(conn, cs):
    d = opened(conn, cs)
    for who in ("author-model-a", " Author-Model-A "):
        with pytest.raises(ValueError):
            resolve_dispute(conn, d, outcome="upheld", resolver=who, implementer_model="impl-b")
    with pytest.raises(ValueError):
        resolve_dispute(conn, d, outcome="upheld", resolver="impl-b", implementer_model="impl-b")
    with pytest.raises(ValueError):
        resolve_dispute(conn, d, outcome="upheld", resolver="", implementer_model="impl-b")
    assert has_open_dispute(conn, "toy")
    resolve_dispute(conn, d, outcome="upheld", resolver="reviewer-c", implementer_model="impl-b")
    assert not has_open_dispute(conn, "toy")
    row = conn.execute("SELECT * FROM disputes WHERE id=?", (d,)).fetchone()
    assert (row["status"], row["outcome"], row["resolver"]) == ("resolved", "upheld", "reviewer-c")
    with pytest.raises(ValueError):                         # cannot be resolved twice
        resolve_dispute(conn, d, outcome="upheld", resolver="reviewer-c", implementer_model="impl-b")
    with pytest.raises(KeyError):
        resolve_dispute(conn, "disp-nope", outcome="upheld", resolver="reviewer-c", implementer_model="impl-b")


def test_check_amended_supersedes_and_needs_the_new_hash(conn, cs):
    new = freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES), summary_text="v2")
    d = opened(conn, cs)
    with pytest.raises(ValueError):
        resolve_dispute(conn, d, outcome="check_amended", resolver="reviewer-c", implementer_model="impl-b")
    with pytest.raises(KeyError):
        resolve_dispute(conn, d, outcome="check_amended", resolver="reviewer-c", implementer_model="impl-b",
                        new_checkset_hash="f" * 64)
    assert has_open_dispute(conn, "toy")                      # failed resolutions leave it open
    resolve_dispute(conn, d, outcome="check_amended", resolver="reviewer-c", implementer_model="impl-b",
                    new_checkset_hash=new.hash)
    assert conn.execute("SELECT supersedes FROM checksets WHERE hash=?", (new.hash,)).fetchone()[0] == cs.hash
    assert conn.execute("SELECT new_checkset_hash FROM disputes WHERE id=?", (d,)).fetchone()[0] == new.hash


def test_requirement_amended_needs_a_human(conn, cs):
    d = opened(conn, cs)
    with pytest.raises(ValueError):
        resolve_dispute(conn, d, outcome="requirement_amended", resolver="reviewer-c", implementer_model="impl-b")
    resolve_dispute(conn, d, outcome="requirement_amended", resolver="human:alice", implementer_model="impl-b")
    assert not has_open_dispute(conn, "toy")
    with pytest.raises(ValueError):
        resolve_dispute(conn, opened(conn, cs), outcome="bogus", resolver="human:alice", implementer_model="impl-b")
