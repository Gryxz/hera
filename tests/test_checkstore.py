"""Frozen check store and check objects. Invented data only."""
import os
import stat

import pytest

from hera.oracle import checkstore
from hera.oracle.checkstore import CheckStoreTampered, freeze, hidden_dir, load, supersede, verify_integrity, visible_dir
from hera.oracle.model import DraftCheck, Requirement
from toyrepo import CHECK_FILES, CHECKS, REQUIREMENTS


def do_freeze(conn, **over):
    kw = dict(task_id="toy", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES))
    kw.update(over)
    return freeze(conn, **kw)


def writable(p):
    os.chmod(p, 0o755 if p.is_dir() else 0o644)


def test_hash_is_deterministic_and_order_independent(conn, hera_home):
    a = do_freeze(conn)
    b = do_freeze(conn, requirements=tuple(reversed(REQUIREMENTS)), checks=tuple(reversed(CHECKS)))
    assert a.hash == b.hash and len(a.hash) == 64


def test_one_byte_changes_the_hash(conn, hera_home):
    a = do_freeze(conn)
    files = dict(CHECK_FILES)
    files["test_calc_unchanged.py"] = files["test_calc_unchanged.py"] + b" "
    assert do_freeze(conn, files=files).hash != a.hash
    assert do_freeze(conn, summary_text="x").hash != a.hash
    reqs = (Requirement("R-1", "parse_amount('12.50') returns 12.5 "),) + REQUIREMENTS[1:]
    assert do_freeze(conn, requirements=reqs).hash != a.hash
    assert do_freeze(conn, task_id="other").hash != a.hash


def test_layout_and_read_only_modes(conn, hera_home):
    cs = do_freeze(conn)
    assert cs.root == hera_home / "checks" / "toy" / cs.hash
    assert (visible_dir(cs) / "test_parse_amount.py").read_bytes() == CHECK_FILES["test_parse_amount.py"]
    assert (visible_dir(cs) / "test_calc_unchanged.py").is_file()
    assert (hidden_dir(cs) / "test_parse_hidden_zq.py").is_file()
    assert not (visible_dir(cs) / "test_parse_hidden_zq.py").exists()
    assert (cs.root / "manifest.json").is_file()
    for dirpath, dirnames, filenames in os.walk(cs.root):
        assert stat.S_IMODE(os.stat(dirpath).st_mode) == 0o555
        for f in filenames:
            assert stat.S_IMODE(os.stat(os.path.join(dirpath, f)).st_mode) == 0o444
    assert cs.adapter == "pytest" and cs.uncovered == ()
    verify_integrity(cs)


def test_mixing_visibilities_in_a_file_is_rejected(conn, hera_home):
    bad = CHECKS + (DraftCheck("chk-mix", ("R-1",), "hidden", "test_parse_amount.py", "test_other"),)
    with pytest.raises(ValueError, match="mix"):
        do_freeze(conn, checks=bad)


@pytest.mark.parametrize("rel", ["../test_x.py", "/etc/test_x.py", "a/../test_x.py", "a//test_x.py",
                                 "./test_x.py", "a\\test_x.py", "test_x.txt", "x.py", "a/.hidden/test_x.py",
                                 "", "test_.py"])
def test_path_traversal_and_bad_relpaths_rejected(rel):
    with pytest.raises(ValueError, match="relpath"):
        DraftCheck("c1", ("R-1",), "visible", rel, "test_a")


def test_field_validation_names_field_not_content():
    for kwargs, field in [
        (dict(check_id="bad id"), "check_id"), (dict(requirement_ids=()), "requirement_ids"),
        (dict(requirement_ids=("R-99999",)), "requirement_ids"), (dict(visibility="secret"), "visibility"),
        (dict(node="helper"), "node"), (dict(kind="other"), "kind"), (dict(author_role="x"), "author_role"),
    ]:
        base = dict(check_id="c1", requirement_ids=("R-1",), visibility="visible", relpath="test_a.py", node="test_a")
        base.update(kwargs)
        with pytest.raises(ValueError, match=field):
            DraftCheck(**base)
    with pytest.raises(ValueError, match="Requirement.id"):
        Requirement("X-1", "t")


def test_freeze_input_validation(conn, hera_home):
    with pytest.raises(ValueError, match="not supplied"):
        do_freeze(conn, files={"test_parse_amount.py": b"x"})
    with pytest.raises(ValueError, match="not referenced"):
        do_freeze(conn, files={**CHECK_FILES, "test_extra.py": b"x"})
    with pytest.raises(ValueError, match="undefined requirement"):
        do_freeze(conn, requirements=REQUIREMENTS[:1])
    with pytest.raises(ValueError, match="duplicate check_id"):
        do_freeze(conn, checks=CHECKS + (CHECKS[0],))
    with pytest.raises(ValueError, match="same function"):
        dup = DraftCheck("chk-dup", ("R-1",), "visible", "test_parse_amount.py", "test_basic_amount")
        do_freeze(conn, checks=CHECKS + (dup,))
    with pytest.raises(ValueError, match="task id"):
        do_freeze(conn, task_id="../x")


def test_idempotent_refreeze_and_conflicting_directory(conn, hera_home):
    a = do_freeze(conn)
    b = do_freeze(conn)
    assert a == b
    assert conn.execute("SELECT COUNT(*) FROM checksets").fetchone()[0] == 1
    # same hash directory, different content on disk -> raises
    f = visible_dir(a) / "test_parse_amount.py"
    writable(f.parent)
    writable(f)
    f.write_bytes(b"def test_basic_amount(): pass\n")
    with pytest.raises(CheckStoreTampered):
        do_freeze(conn)


def test_verify_integrity_detects_modified_deleted_and_added(conn, hera_home):
    cs = do_freeze(conn)
    verify_integrity(cs)
    f = visible_dir(cs) / "test_parse_amount.py"
    writable(f.parent)
    writable(f)
    original = f.read_bytes()
    f.write_bytes(original + b"#")
    with pytest.raises(CheckStoreTampered):
        verify_integrity(cs)
    f.write_bytes(original)
    verify_integrity(cs)
    f.unlink()
    with pytest.raises(CheckStoreTampered):
        verify_integrity(cs)
    f.write_bytes(original)
    verify_integrity(cs)
    extra = visible_dir(cs) / "test_extra.py"
    extra.write_text("x")
    with pytest.raises(CheckStoreTampered):
        verify_integrity(cs)
    extra.unlink()
    writable(cs.root)          # the store is read-only; a non-root user must lift that to tamper
    (cs.root / "stray").mkdir()
    with pytest.raises(CheckStoreTampered):
        verify_integrity(cs)
    (cs.root / "stray").rmdir()
    verify_integrity(cs)
    m = cs.root / "manifest.json"
    writable(cs.root)
    writable(m)
    m.write_bytes(m.read_bytes().replace(b"R-3", b"R-4"))
    with pytest.raises(CheckStoreTampered):
        verify_integrity(cs)


def test_symlink_in_store_is_tampering(conn, hera_home, tmp_path):
    cs = do_freeze(conn)
    writable(visible_dir(cs))
    os.symlink(tmp_path, visible_dir(cs) / "link")
    with pytest.raises(CheckStoreTampered):
        verify_integrity(cs)


def test_database_rows_and_load(conn, hera_home):
    cs = do_freeze(conn, summary_text="summary one")
    assert conn.execute("SELECT runner_cmd, task_id, supersedes FROM checksets WHERE hash=?", (cs.hash,)).fetchone()[:] \
        == ("pytest", "toy", None)
    assert {r[0] for r in conn.execute("SELECT id FROM requirements")} == {"toy:R-1", "toy:R-2", "toy:R-3"}
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM checks WHERE checkset_hash=?", (cs.hash,))}
    assert set(rows) == {c.check_id for c in CHECKS}
    assert rows["chk-hidden-r1-seven"]["visibility"] == "hidden" and rows["chk-r3-add"]["kind"] == "regression"
    assert rows["chk-hidden-r1-seven"]["sha256"] == cs.files["test_parse_hidden_zq.py"]
    assert rows["chk-hidden-r1-seven"]["author_role"] == "hidden"
    again = load(conn, cs.hash)
    assert again == cs
    verify_integrity(again)
    with pytest.raises(KeyError):
        load(conn, "0" * 64)
    conn.execute("UPDATE checksets SET manifest_json=replace(manifest_json,'R-3','R-9') WHERE hash=?", (cs.hash,))
    with pytest.raises(CheckStoreTampered):
        load(conn, cs.hash)


def test_uncovered_requirement_reported(conn, hera_home):
    reqs = REQUIREMENTS + (Requirement("R-4", "something nobody checks"),)
    cs = do_freeze(conn, requirements=reqs)
    assert cs.uncovered == ("R-4",)
    assert load(conn, cs.hash).uncovered == ("R-4",)


def test_supersede(conn, hera_home):
    a = do_freeze(conn)
    b = do_freeze(conn, summary_text="revised")
    supersede(conn, a.hash, b.hash)
    assert conn.execute("SELECT supersedes FROM checksets WHERE hash=?", (b.hash,)).fetchone()[0] == a.hash
    assert conn.execute("SELECT supersedes FROM checksets WHERE hash=?", (a.hash,)).fetchone()[0] is None
    with pytest.raises(KeyError):
        supersede(conn, a.hash, "f" * 64)
    with pytest.raises(ValueError):
        supersede(conn, a.hash, a.hash)
    other = do_freeze(conn, task_id="other")
    with pytest.raises(ValueError):
        supersede(conn, a.hash, other.hash)


def test_new_rows_are_unvalidated_and_health_is_a_column_update_outside_the_hash(conn, hera_home):
    import json
    from hera.types import CheckHealth
    cs = do_freeze(conn)
    assert {r[0] for r in conn.execute("SELECT health FROM checks")} == {"unvalidated"}
    assert checkstore.health_counts(conn, cs.hash) == {"unvalidated": len(CHECKS)}
    again = do_freeze(conn, health={"chk-r1-basic": (CheckHealth.WEAK, {"rule": "passes_before_change"})})
    assert again.hash == cs.hash                                   # health is not part of the hash
    verify_integrity(again)
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM checks WHERE checkset_hash=?", (cs.hash,))}
    assert rows["chk-r1-basic"]["health"] == "weak"
    assert json.loads(rows["chk-r1-basic"]["health_evidence_json"]) == {"rule": "passes_before_change"}
    assert rows["chk-r2-empty"]["health"] == "unvalidated"
    assert checkstore.health_line(checkstore.health_counts(conn, cs.hash)) == \
        f"check health: 1 weak (pass before the change), {len(CHECKS) - 1} unvalidated of {len(CHECKS)}"
    do_freeze(conn)                                                # freezing again without health keeps the result
    assert conn.execute("SELECT health FROM checks WHERE id='chk-r1-basic'").fetchone()[0] == "weak"


def test_health_refuses_unknown_checks_and_labels(conn, hera_home):
    from hera.types import CheckHealth
    with pytest.raises(ValueError):
        do_freeze(conn, health={"no-such-check": (CheckHealth.WEAK, {})})
    with pytest.raises(ValueError):
        do_freeze(conn, health={"chk-r1-basic": ("great", {})})
    assert conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == 0     # nothing half-recorded


def test_health_line_is_silent_when_everything_is_valid():
    assert checkstore.health_line({"valid": 3}) is None and checkstore.health_line({}) is None
