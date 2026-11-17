"""`hera dispute list` and `hera dispute resolve`: listing, every outcome, every refusal."""
import dataclasses
import time

import pytest

from hera import cli, paths
from hera.oracle.checkstore import freeze
from hera.oracle.disputes import open_dispute
from hera.store import db
from toyrepo import CHECK_FILES, CHECKS, REQUIREMENTS

LONG_ARGUMENT = "the example in R-1 contradicts the text. " * 12 + "END-OF-ARGUMENT"


@pytest.fixture
def world(hera_home):
    """An initialised store with two tasks, a check set for each, attempts by `impl-model-a` for task toy,
    and two open disputes about task toy."""
    assert cli.main(["init"]) == 0
    conn = db.connect(paths.db_path())
    checks = tuple(dataclasses.replace(c, author_model="author-model-a") for c in CHECKS)
    w = type("World", (), {})()
    w.conn = conn
    w.cs = freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=checks, files=dict(CHECK_FILES))
    files2 = dict(CHECK_FILES)
    files2["test_parse_amount.py"] += b"\n# amended\n"
    w.cs2 = freeze(conn, task_id="toy", requirements=REQUIREMENTS, checks=checks, files=files2)
    w.other = freeze(conn, task_id="other", requirements=REQUIREMENTS, checks=checks, files=dict(CHECK_FILES))
    conn.execute("INSERT INTO attempts(id,task_id,seq,tier,provider,model_id,candidate,status,started_at) "
                 "VALUES('toy-a1','toy',1,'default','p','impl-model-a','toy-c1','disputed',?)", (time.time(),))
    w.d1 = open_dispute(conn, task_id="toy", checkset_hash=w.cs.hash, check_id="chk-r1-basic",
                        requirement_id="R-1", argument=LONG_ARGUMENT)
    w.d2 = open_dispute(conn, task_id="toy", checkset_hash=w.cs.hash, check_id="chk-r2-empty",
                        requirement_id="R-2", argument="line one\nline two \x1b[31mred")
    conn.execute("UPDATE disputes SET opened_at=opened_at-10 WHERE id=?", (w.d1,))
    yield w
    conn.close()


def row(w, d):
    return dict(w.conn.execute("SELECT * FROM disputes WHERE id=?", (d,)).fetchone())


def resolve(w, d, *args):
    return cli.main(["dispute", "resolve", d, *args])


def test_list_shows_every_field_and_cuts_the_argument(world, capsys):
    assert cli.main(["dispute", "list"]) == 0
    out = capsys.readouterr().out
    assert world.d1 in out and world.d2 in out
    first = out.splitlines()[0]
    for part in ("task=toy", "check=chk-r1-basic", "requirement=R-1", "status=open", "opened="):
        assert part in first
    assert first.split("opened=")[1].endswith("Z")
    arg = next(line for line in out.splitlines() if "contradicts" in line).strip()
    assert arg.startswith("argument: the example in R-1") and "END-OF-ARGUMENT" not in arg
    assert len(arg) == len("argument: ") + 200
    assert "line one line two" in out and "\x1b" not in out              # one printable line per argument
    assert out.index(world.d1) < out.index(world.d2)                      # oldest first


def test_list_open_hides_resolved_disputes(world, capsys):
    assert resolve(world, world.d1, "--outcome", "upheld", "--resolver", "human:ana") == 0
    capsys.readouterr()
    assert cli.main(["dispute", "list", "--open"]) == 0
    out = capsys.readouterr().out
    assert world.d1 not in out and world.d2 in out
    assert cli.main(["dispute", "list"]) == 0
    out = capsys.readouterr().out
    assert "status=resolved" in out and "outcome=upheld" in out and "resolver=human:ana" in out


def test_list_with_nothing_to_show(hera_home, capsys):
    assert cli.main(["init"]) == 0
    assert cli.main(["dispute", "list", "--open"]) == 0
    assert "no open disputes" in capsys.readouterr().out


def test_upheld(world, capsys):
    assert resolve(world, world.d1, "--outcome", "upheld", "--resolver", "human:ana") == 0
    out = capsys.readouterr().out
    assert "resolved: upheld" in out and "hera task continue toy" in out
    r = row(world, world.d1)
    assert (r["status"], r["outcome"], r["resolver"], r["new_checkset_hash"]) == ("resolved", "upheld", "human:ana", None)
    assert r["resolved_at"]


def test_requirement_amended(world):
    assert resolve(world, world.d1, "--outcome", "requirement_amended", "--resolver", "human:ana") == 0
    assert row(world, world.d1)["outcome"] == "requirement_amended"


def test_check_amended_records_the_new_check_set(world):
    assert resolve(world, world.d1, "--outcome", "check_amended", "--resolver", "human:ana",
                   "--new-checkset", world.cs2.hash) == 0
    r = row(world, world.d1)
    assert r["outcome"] == "check_amended" and r["new_checkset_hash"] == world.cs2.hash
    assert world.conn.execute("SELECT supersedes FROM checksets WHERE hash=?",
                              (world.cs2.hash,)).fetchone()[0] == world.cs.hash


@pytest.mark.parametrize("resolver", ["gpt-whatever", "impl-model-a", "ana", "human:", "human:  ", "Human:ana", ""])
def test_a_resolver_that_is_not_a_person_is_refused(world, capsys, resolver):
    assert resolve(world, world.d1, "--outcome", "upheld", "--resolver", resolver) == 2
    assert "person" in capsys.readouterr().err
    assert row(world, world.d1)["status"] == "open"


def test_the_independence_rule_still_applies_with_the_tasks_implementer_models(world, capsys):
    world.conn.execute("INSERT INTO attempts(id,task_id,seq,tier,provider,model_id,candidate,status,started_at) "
                       "VALUES('toy-a2','toy',2,'strong','p','human:bob','toy-c2','submitted',?)", (time.time(),))
    assert resolve(world, world.d1, "--outcome", "upheld", "--resolver", "human:bob") == 2
    assert "independent" in capsys.readouterr().err and row(world, world.d1)["status"] == "open"
    world.conn.execute("UPDATE checks SET author_model='human:carol' WHERE id='chk-r1-basic'")
    assert resolve(world, world.d1, "--outcome", "upheld", "--resolver", "human:CAROL") == 2
    assert row(world, world.d1)["status"] == "open"


def test_check_amended_refusals(world, capsys):
    base = ["--outcome", "check_amended", "--resolver", "human:ana"]
    assert resolve(world, world.d1, *base) == 2 and "--new-checkset" in capsys.readouterr().err
    assert resolve(world, world.d1, *base, "--new-checkset", "0" * 64) == 2 and "unknown check set" in capsys.readouterr().err
    assert resolve(world, world.d1, *base, "--new-checkset", world.other.hash) == 2
    assert "different task" in capsys.readouterr().err
    assert resolve(world, world.d1, *base, "--new-checkset", world.cs.hash) == 2          # the same set is no amendment
    assert row(world, world.d1)["status"] == "open"
    assert world.conn.execute("SELECT supersedes FROM checksets WHERE hash=?", (world.other.hash,)).fetchone()[0] is None


def test_a_new_check_set_only_goes_with_check_amended(world, capsys):
    assert resolve(world, world.d1, "--outcome", "upheld", "--resolver", "human:ana", "--new-checkset", world.cs2.hash) == 2
    assert "check_amended" in capsys.readouterr().err and row(world, world.d1)["status"] == "open"


def test_a_resolved_or_unknown_dispute_is_refused(world, capsys):
    assert resolve(world, world.d1, "--outcome", "upheld", "--resolver", "human:ana") == 0
    capsys.readouterr()
    assert resolve(world, world.d1, "--outcome", "requirement_amended", "--resolver", "human:ben") == 2
    assert "already resolved" in capsys.readouterr().err
    assert row(world, world.d1)["resolver"] == "human:ana" and row(world, world.d1)["outcome"] == "upheld"
    assert resolve(world, "disp-nope", "--outcome", "upheld", "--resolver", "human:ana") == 2
    assert "no such dispute" in capsys.readouterr().err
