"""The implementer's tools: path rules, size caps, literal search, command limits, labels."""
import hashlib
import os
import socket
import threading
import time

import pytest

from hera.agent.tools import TOOL_SPECS, ToolBox
from hera.privacy.labels import LabelStore
from hera.privacy.policy import Policy
from hera.sandbox import detect_mechanism
from hera.types import RepoClass, ToolCall

NO_SANDBOX = detect_mechanism() == "none"


class Rig:
    _count = 0

    def __init__(self, conn, toy, repo_class=RepoClass.PUBLIC, **kw):
        Rig._count += 1
        self.wt = toy.repo.new_candidate(f"w{Rig._count}")
        self.labels = LabelStore(conn)
        self.policy = Policy(repos={"toy": (None, repo_class)})
        kw.setdefault("allow_unsandboxed", True)
        self.tb = ToolBox(self.wt, repo_id="toy", label_store=self.labels, policy=self.policy, **kw)
        self.root = self.wt.path
        self.n = 0

    def call(self, name, **args):
        self.n += 1
        return self.tb.execute(ToolCall(f"c{self.n}", name, args), artefact_id=f"r{self.n}")

    def text(self, name, **args) -> str:
        return self.call(name, **args).data.decode()


@pytest.fixture
def rig(conn, toy):
    return Rig(conn, toy)


def test_specs_cover_every_tool_and_name_unique():
    names = [t["name"] for t in TOOL_SPECS]
    assert names == ["list_files", "read_file", "search", "write_file", "replace_in_file", "run_command",
                     "run_visible_checks", "read_visible_check", "submit", "dispute"]
    assert all(t["parameters"]["type"] == "object" and t["description"] for t in TOOL_SPECS)


# ---- list_files / read_file -----------------------------------------------------------------------
def test_list_files_and_hidden_names(rig):
    out = rig.text("list_files")
    assert out.splitlines() == ["calc.py", "conftest.py", "tests/"]       # no .git
    assert rig.text("list_files", path="tests") == "test_calc.py"
    (rig.root / "_hera_results").mkdir()
    (rig.root / "_hera_checks").mkdir()
    assert "_hera_" not in rig.text("list_files")
    assert rig.text("list_files", path="calc.py").startswith("error:")
    assert rig.text("list_files", path="nope").startswith("error:")


def test_list_files_cap(rig):
    d = rig.root / "many"
    d.mkdir()
    for i in range(520):
        (d / f"f{i:04d}.txt").write_text("x")
    lines = rig.text("list_files", path="many").splitlines()
    assert len(lines) == 501 and lines[-1].startswith("[20 more entries")


def test_read_file_basic_ranges_and_caps(rig):
    assert rig.text("read_file", path="calc.py") == "def add(a, b):\n    return a + b\n"
    (rig.root / "big.txt").write_text("".join(f"line {i}\n" for i in range(1, 1001)))
    out = rig.text("read_file", path="big.txt")
    assert out.startswith("line 1\n") and "line 400\n" in out and "line 401" not in out
    assert out.rstrip().endswith("[truncated; continue with start_line=401]")
    out = rig.text("read_file", path="big.txt", start_line=990, max_lines=5)
    assert out.splitlines()[0] == "line 990" and out.splitlines()[4] == "line 994" and "truncated" in out
    assert rig.text("read_file", path="big.txt", start_line=1000).strip() == "line 1000"
    assert rig.text("read_file", path="big.txt", start_line=5000) == "(no lines in that range)"
    assert rig.text("read_file", path="big.txt", start_line=0).startswith("error:")
    (rig.root / "wide.txt").write_text("".join("y" * 199 + "\n" for _ in range(399)))
    out = rig.call("read_file", path="wide.txt").data
    assert len(out) <= 64 * 1024 and b"truncated" in out
    (rig.root / "one.txt").write_text("z" * 300_000)
    out = rig.call("read_file", path="one.txt").data
    assert len(out) <= 64 * 1024
    (rig.root / "empty.txt").write_text("")
    assert rig.text("read_file", path="empty.txt") == "(empty file)"


def test_read_file_rejects_binary_directory_and_missing(rig):
    (rig.root / "b.bin").write_bytes(b"abc\0def")
    assert rig.text("read_file", path="b.bin").startswith("error:")
    assert rig.text("read_file", path="tests").startswith("error:")
    assert rig.text("read_file", path="missing.py").startswith("error:")
    (rig.root / "latin.txt").write_bytes(b"caf\xe9\n")
    assert rig.text("read_file", path="latin.txt") == "caf\ufffd\n"


# ---- search ------------------------------------------------------------------------------------------
def test_search_is_literal_and_scoped(rig):
    (rig.root / "t.txt").write_text("alpha\na.b\naxb\nA+B (x)\n")
    out = rig.text("search", text="a.b")
    assert "t.txt:2: a.b" in out and "axb" not in out
    assert "t.txt:4: A+B (x)" in rig.text("search", text="A+B (x)")
    assert rig.text("search", text=".*") == "no matches"
    assert rig.text("search", text="return a + b", path="calc.py") == "calc.py:2: return a + b"
    assert rig.text("search", text="add", path="tests").startswith("tests/test_calc.py:")
    assert rig.text("search", text="zzz-not-there") == "no matches"
    for bad in ("", "two\nlines", "x" * 501):
        assert rig.text("search", text=bad).startswith("error:")


def test_search_caps_and_skips(rig):
    (rig.root / "many.txt").write_text("".join(f"needle {i} " + "e" * 400 + "\n" for i in range(300)))
    lines = rig.text("search", text="needle").splitlines()
    assert len(lines) == 201 and lines[-1].startswith("[stopped after 200 hits")
    assert all(len(l.split(": ", 1)[1]) <= 200 for l in lines[:200])
    (rig.root / "bin.dat").write_bytes(b"needle\0binary")
    os.symlink(str(rig.root / "many.txt"), rig.root / "link.txt")
    assert "bin.dat" not in rig.text("search", text="needle") and "link.txt" not in rig.text("search", text="needle")
    # content of .git is never searched (the worktree's own .git file says "gitdir:")
    assert rig.text("search", text="gitdir") == "no matches"


# ---- write_file / replace_in_file -----------------------------------------------------------------
def test_write_file_creates_parents_and_caps(rig):
    assert rig.text("write_file", path="pkg/sub/mod.py", content="x = 1\n").startswith("ok:")
    assert (rig.root / "pkg/sub/mod.py").read_text() == "x = 1\n"
    assert rig.text("write_file", path="pkg/sub/mod.py", content="é").startswith("ok:")
    assert (rig.root / "pkg/sub/mod.py").read_bytes() == "é".encode()
    assert rig.text("write_file", path="ok.txt", content="a" * (256 * 1024)).startswith("ok:")
    assert rig.text("write_file", path="big.txt", content="a" * (256 * 1024 + 1)).startswith("error:")
    assert not (rig.root / "big.txt").exists()
    assert rig.text("write_file", path="s.txt", content="\ud800").startswith("error:")      # lone surrogate
    assert rig.text("write_file", path="tests", content="x").startswith("error:")           # a directory
    assert rig.text("write_file", path="calc.py/inner", content="x").startswith("error:")   # a file as parent


def test_replace_requires_exactly_one_occurrence(rig):
    assert rig.text("replace_in_file", path="calc.py", old="a + b", new="b + a").startswith("ok:")
    assert (rig.root / "calc.py").read_text() == "def add(a, b):\n    return b + a\n"
    (rig.root / "dup.txt").write_text("x\nx\nx\n")
    out = rig.text("replace_in_file", path="dup.txt", old="x", new="y")
    assert out.startswith("error:") and "3 times" in out
    assert (rig.root / "dup.txt").read_text() == "x\nx\nx\n"
    assert "not found" in rig.text("replace_in_file", path="calc.py", old="nope", new="y")
    assert rig.text("replace_in_file", path="calc.py", old="", new="y").startswith("error:")
    assert rig.text("replace_in_file", path="missing.py", old="a", new="b").startswith("error:")
    (rig.root / "huge.txt").write_text("a" * 200_000 + "Q")
    assert rig.text("replace_in_file", path="huge.txt", old="Q", new="b" * 100_000).startswith("error:")
    assert (rig.root / "huge.txt").read_text().endswith("Q")


# ---- path rules for every file tool -----------------------------------------------------------------
BAD_PATHS = ["../x", "a/../../x", "/etc/passwd", "C:/Windows", ".git", ".git/config", ".GIT/config",
             "a/.Git/x", ".git ", "_hera_checks/x", "_hera_results", "_hera_project/p", "d/_hera_checks",
             "a\\b", "a\0b", ""]


@pytest.mark.parametrize("bad", BAD_PATHS)
def test_every_file_tool_rejects_bad_paths(rig, bad):
    for name, args in (("list_files", {"path": bad}), ("read_file", {"path": bad}),
                       ("search", {"text": "x", "path": bad}), ("write_file", {"path": bad, "content": "x"}),
                       ("replace_in_file", {"path": bad, "old": "a", "new": "b"}),
                       ("read_visible_check", {"path": bad})):
        out = rig.text(name, **args)
        assert out.startswith("error:"), (name, bad, out)


def test_nothing_was_written_by_rejected_writes(rig, tmp_path):
    for bad in ("../escape.txt", "_hera_checks/x.py", ".git/hooks/pre-commit", ".GIT/x"):
        rig.text("write_file", path=bad, content="x")
    assert not (rig.root.parent / "escape.txt").exists()
    assert not (rig.root / "_hera_checks").exists()
    assert not (rig.root / ".GIT").exists()
    assert sorted(os.listdir(rig.root)) == [".git", "calc.py", "conftest.py", "tests"]


def test_dot_segments_that_stay_inside_are_fine(rig):
    assert rig.text("read_file", path="./tests/./test_calc.py").startswith("from calc import add")
    assert rig.text("read_file", path="tests//test_calc.py").startswith("from calc import add")
    assert rig.text("list_files", path=".").startswith("calc.py")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_symlinks(rig, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside data")
    os.symlink(str(outside / "secret.txt"), rig.root / "esc_file")
    os.symlink(str(outside), rig.root / "esc_dir")
    os.symlink("../../outside", rig.root / "rel_esc")
    os.symlink(".git", rig.root / "to_git")
    os.symlink("calc.py", rig.root / "inner_link")
    os.symlink("tests", rig.root / "inner_dir")
    for name, args in (("read_file", {"path": "esc_file"}), ("read_file", {"path": "esc_dir/secret.txt"}),
                       ("list_files", {"path": "esc_dir"}), ("list_files", {"path": "rel_esc"}),
                       ("search", {"text": "outside", "path": "esc_dir"}),
                       ("read_file", {"path": "to_git/HEAD"}), ("list_files", {"path": "to_git"}),
                       ("write_file", {"path": "esc_dir/new.txt", "content": "x"}),
                       ("write_file", {"path": "esc_file", "content": "overwritten"}),
                       ("replace_in_file", {"path": "esc_file", "old": "outside", "new": "x"}),
                       ("write_file", {"path": "to_git/x", "content": "x"})):
        assert rig.text(name, **args).startswith("error:"), (name, args)
    assert (outside / "secret.txt").read_text() == "outside data"
    assert sorted(os.listdir(outside)) == ["secret.txt"]
    # a symlink that stays inside may be read, but a write never goes through a symlink at the target
    assert rig.text("read_file", path="inner_link").startswith("def add")
    assert rig.text("read_file", path="inner_dir/test_calc.py").startswith("from calc")
    assert rig.text("write_file", path="inner_link", content="x").startswith("error:")
    assert rig.text("replace_in_file", path="inner_link", old="a + b", new="x").startswith("error:")
    assert (rig.root / "calc.py").read_text().startswith("def add")
    assert os.path.islink(rig.root / "inner_link")
    # search ignores symlinks entirely
    assert "inner_link" not in rig.text("search", text="return")


# ---- run_command ---------------------------------------------------------------------------------------
def test_run_command_basics(rig):
    out = rig.text("run_command", argv=["{python}", "-c", "import os,sys; print(os.getcwd()); sys.exit(3)"])
    assert out.startswith("exit code: 3\n")
    assert os.path.realpath(rig.root) in out
    out = rig.text("run_command", argv=["{python}", "-c", "import sys; print('o'); print('e', file=sys.stderr)"])
    assert out.startswith("exit code: 0") and "o\n" in out and "e\n" in out


def test_run_command_does_not_leave_pytest_cache(rig):
    rig.text("run_command", argv=["{python}", "-m", "pytest", "-q", "tests"])
    assert not (rig.root / ".pytest_cache").exists()


def test_run_command_timeout(rig):
    t0 = time.monotonic()
    out = rig.text("run_command", argv=["{python}", "-c", "import time; time.sleep(60)"], timeout_s=1)
    assert out.startswith("timed out after 1s") and time.monotonic() - t0 < 30
    # the limit is clamped, never raised
    out = rig.text("run_command", argv=["{python}", "-c", "print(1)"], timeout_s=10_000)
    assert out.startswith("exit code: 0")


def test_run_command_output_is_the_last_16kb(rig):
    code = "import sys; sys.stdout.write('A' * 100000 + 'END-MARKER')"
    out = rig.call("run_command", argv=["{python}", "-c", code]).data
    assert out.endswith(b"END-MARKER") and b"earlier output omitted" in out
    assert len(out) < 16 * 1024 + 200


def test_run_command_argument_errors(rig):
    for argv in ([], [""], ["-c", "x"], "python", [1, 2], ["a"] * 101, ["a\0b"]):
        assert rig.text("run_command", argv=argv).startswith("error:"), argv
    assert rig.text("run_command", argv=["definitely-not-a-program-xyz"]).startswith("error:") or \
        "exit code" in rig.text("run_command", argv=["definitely-not-a-program-xyz"])


def test_run_command_refuses_without_a_mechanism_unless_allowed(conn, toy):
    r = Rig(conn, toy, allow_unsandboxed=False)
    out = r.text("run_command", argv=["{python}", "-c", "print(1)"])
    if NO_SANDBOX:
        assert out == "error: no sandbox is available, so commands cannot be run"
    else:
        assert out.startswith("exit code: 0")


@pytest.mark.skipif(NO_SANDBOX, reason="no sandbox mechanism (bwrap or sandbox-exec) on this machine")
def test_run_command_has_no_network_and_cannot_write_outside(conn, toy, tmp_path):
    r = Rig(conn, toy, allow_unsandboxed=False)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    accepted = []

    def accept():
        srv.settimeout(5)
        try:
            c, _ = srv.accept()
            accepted.append(c)
        except OSError:
            pass
    t = threading.Thread(target=accept, daemon=True)
    t.start()
    code = ("import socket,sys\ns=socket.socket(); s.settimeout(3)\n"
            f"try:\n    s.connect(('127.0.0.1', {port})); print('CONNECTED')\n"
            "except OSError as e:\n    print('NO NETWORK')\n")
    out = r.text("run_command", argv=["{python}", "-c", code])
    srv.close()
    t.join(timeout=8)
    assert "NO NETWORK" in out and "CONNECTED" not in out and accepted == []
    outside = tmp_path / "written-by-child.txt"
    code = f"open({str(outside)!r}, 'w').write('x')"
    out = r.text("run_command", argv=["{python}", "-c", code])
    assert "exit code: 0" not in out and not outside.exists()


# ---- visible checks -----------------------------------------------------------------------------------
def test_run_visible_checks_commits_then_calls_the_injected_verifier(conn, toy):
    seen = []

    def verifier(commit):
        seen.append(commit)
        return "Verification result: FAIL\n- chk-r1-basic failing"
    r = Rig(conn, toy, verify_visible=verifier)
    r.text("write_file", path="calc.py", content="def add(a, b):\n    return a + b\n# edit\n")
    out = r.call("run_visible_checks")
    assert out.data.decode() == "Verification result: FAIL\n- chk-r1-basic failing" and not out.builtin
    (commit,) = seen
    assert len(commit) == 40 and r.wt.head() == commit
    assert "# edit" in toy.repo.read_tree(commit)["calc.py"].decode()


def test_run_visible_checks_errors_never_leak_exception_text(conn, toy):
    def boom(commit):
        raise RuntimeError("hidden-check-name-xyz")
    r = Rig(conn, toy, verify_visible=boom)
    out = r.text("run_visible_checks")
    assert out == "error: the visible checks could not be run" and "xyz" not in out
    assert Rig(conn, toy, verify_visible=None).text("run_visible_checks").startswith("error:")
    long = Rig(conn, toy, verify_visible=lambda c: "x" * 100_000)
    assert len(long.call("run_visible_checks").data) < 33_000


def test_read_visible_check(conn, toy, tmp_path):
    vis = tmp_path / "vis"
    (vis / "sub").mkdir(parents=True)
    (vis / "test_a.py").write_text("def test_a():\n    assert True\n")
    (vis / "sub" / "test_b.py").write_text("B\n")
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "test_h.py").write_text("HIDDEN SECRET CHECK\n")
    os.symlink(str(hidden), vis / "link_to_hidden")
    os.symlink(str(hidden / "test_h.py"), vis / "link_file")
    r = Rig(conn, toy, visible_checks_dir=vis)
    assert r.text("read_visible_check", path="test_a.py") == "def test_a():\n    assert True\n"
    assert r.text("read_visible_check", path="sub/test_b.py") == "B\n"
    assert "test_a.py" in r.text("read_visible_check", path=".")
    for bad in ("../hidden/test_h.py", "link_to_hidden/test_h.py", "link_file", "link_to_hidden", "/etc/passwd"):
        out = r.text("read_visible_check", path=bad)
        assert out.startswith("error:") and "HIDDEN" not in out, bad
    assert r.text("read_visible_check", path="missing.py").startswith("error:")
    assert Rig(conn, toy).text("read_visible_check", path="test_a.py").startswith("error:")
    # read-only: there is no tool that writes there, and the file tools cannot name it
    assert r.text("write_file", path="../vis/test_a.py", content="x").startswith("error:")
    assert (vis / "test_a.py").read_text().startswith("def test_a")


# ---- submit / dispute / bad calls ------------------------------------------------------------------
def test_submit_and_dispute(rig):
    out = rig.call("submit", summary="done", declared_scope=["calc.py", "pkg/*.py"], twin_pattern="parse_x")
    assert out.ended == "submitted" and out.builtin
    assert out.payload == {"summary": "done", "declared_scope": ["calc.py", "pkg/*.py"], "twin_pattern": "parse_x"}
    assert rig.call("submit", summary="d", declared_scope=[]).payload["twin_pattern"] == ""
    for args in ({"summary": "", "declared_scope": []}, {"summary": "x", "declared_scope": ["../x"]},
                 {"summary": "x", "declared_scope": ["/abs"]}, {"summary": "x", "declared_scope": [".git/x"]},
                 {"summary": "x", "declared_scope": "calc.py"}, {"summary": "x"},
                 {"summary": "x", "declared_scope": [], "extra": 1},
                 {"summary": "x" * 4001, "declared_scope": []},
                 {"summary": "x", "declared_scope": [], "twin_pattern": "t" * 301}):
        o = rig.call("submit", **args)
        assert o.ended is None and o.data.decode().startswith("error:"), args
    out = rig.call("dispute", check_id="chk-r1-basic", requirement_id="R-1", argument="the example is wrong")
    assert out.ended == "disputed"
    assert out.payload == {"check_id": "chk-r1-basic", "requirement_id": "R-1", "argument": "the example is wrong"}
    for args in ({"check_id": "bad id", "requirement_id": "R-1", "argument": "a"},
                 {"check_id": "c", "requirement_id": "1", "argument": "a"},
                 {"check_id": "c", "requirement_id": "R-1", "argument": " "}):
        assert rig.call("dispute", **args).ended is None


def test_bad_calls_come_back_as_errors(rig):
    assert rig.text("no_such_tool") == "error: unknown tool; use one of the listed tools"
    inv = rig.tb.execute(ToolCall("x", "__invalid__", {"raw": "{oops"}), artefact_id="inv")
    assert inv.data.decode().startswith("error:") and "oops" not in inv.data.decode()
    assert rig.text("read_file").startswith("error:")                      # missing argument
    assert rig.text("read_file", path=5).startswith("error:")             # wrong type
    assert rig.text("read_file", path="calc.py", start_line=True).startswith("error:")   # bool is not an int
    assert rig.text("read_file", path="calc.py", start_line="1").startswith("error:")
    assert rig.text("list_files", bogus=1).startswith("error:")
    bad = rig.tb.execute(ToolCall("x", "read_file", ["not", "a", "dict"]), artefact_id="b")
    assert bad.data.decode().startswith("error:")
    for name in ("list_files", "search", "run_command"):
        assert rig.text(name, **({"text": 3} if name == "search" else {"argv": 5} if name == "run_command"
                                 else {"path": 3})).startswith("error:")


def test_an_unexpected_exception_inside_a_tool_becomes_an_error(rig, monkeypatch):
    monkeypatch.setattr(rig.tb, "_list_files", lambda **kw: 1 / 0)
    rig.tb._table["list_files"] = (rig.tb._list_files, {"path": (str, False)})
    assert rig.text("list_files") == "error: the tool failed unexpectedly"


# ---- labels -----------------------------------------------------------------------------------------
def test_results_are_labelled_by_the_repository_class(conn, toy):
    for cls in (RepoClass.PUBLIC, RepoClass.PROPRIETARY, RepoClass.SENSITIVE):
        r = Rig(conn, toy, repo_class=cls)
        out = r.call("read_file", path="calc.py")
        lab = r.labels.get(out.artefact_id)
        assert lab == out.label and lab.repo_class is cls and not out.builtin
        assert lab.content_hash == hashlib.sha256(out.data).hexdigest()
        assert r.labels.get(r.call("list_files").artefact_id).repo_class is cls
        assert r.labels.get(r.call("run_command", argv=["{python}", "-c", "print(1)"]).artefact_id).repo_class is cls


def test_fixed_sentences_get_the_builtin_label(rig):
    for out in (rig.call("read_file", path="../x"), rig.call("write_file", path="a.txt", content="x"),
                rig.call("no_such_tool"), rig.call("submit", summary="s", declared_scope=[])):
        assert out.builtin and out.label.repo_class is RepoClass.PUBLIC
        assert out.label.derived_from == ("hera-builtin",)
        assert rig.labels.get(out.artefact_id) == out.label
    # an error never echoes the bad path
    assert "secretdir" not in rig.text("read_file", path="../secretdir/x")


def test_a_sensitive_repository_never_gets_a_builtin_label_on_content(conn, toy):
    r = Rig(conn, toy, repo_class=RepoClass.SENSITIVE)
    out = r.call("run_command", argv=["{python}", "-c", "print(open('calc.py').read())"])
    assert out.label.repo_class is RepoClass.SENSITIVE and b"def add" in out.data
    out = r.call("run_visible_checks")                      # no verifier: a fixed error
    assert out.builtin
