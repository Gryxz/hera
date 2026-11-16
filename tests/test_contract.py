"""Contract check files: parser and evaluator. Invented data only."""
import os
import sys

import pytest

from hera.oracle.contract import ContractCheck, evaluate, load_contract_file
from hera.sandbox import RunResult
from hera.types import CheckStatus as S

REL = "x.contract.toml"


def load(text, rel=REL):
    return load_contract_file(text.encode() if isinstance(text, str) else text, rel)


def one(extra="", expect='exit_code = 0', head='node = "n1"\nargv = ["echo", "hi"]\n'):
    return f"[[check]]\n{head}{extra}\n[check.expect]\n{expect}\n"


def rr(out=b"", err=b"", code=0, timed_out=False, truncated=False):
    return RunResult(code, out, err, timed_out, truncated, 0.1, "none")


# ---- parser: accepted --------------------------------------------------------------------------
def test_every_key_is_parsed():
    (c,) = load('''
[[check]]
node = "parse_twelve_fifty"
argv = ["{python}", "-c", "print(1)", "12.50"]
stdin = "abc"
timeout_s = 2.5
env = { LANG = "C.UTF-8", MY_VAR = "1" }
files = { "input.json" = "{}", "d/e.txt" = "x" }
[check.expect]
exit_code = 0
stdout_equals = "12.5\\n"
stdout_contains = ["12.5"]
stderr_contains = ["ValueError"]
stdout_json = { value = 12.5 }
stdout_json_subset = { ok = true }
files_equal = { "out.txt" = "done\\n" }
files_contain = { "log.txt" = ["started"] }
''')
    assert isinstance(c, ContractCheck) and c.node == "parse_twelve_fifty" and c.relpath == REL
    assert c.argv == ("{python}", "-c", "print(1)", "12.50") and c.stdin == "abc" and c.timeout_s == 2.5
    assert c.env == (("LANG", "C.UTF-8"), ("MY_VAR", "1")) and c.files == (("d/e.txt", "x"), ("input.json", "{}"))
    assert c.exit_code == 0 and c.stdout_equals == "12.5\n" and c.stdout_contains == ("12.5",)
    assert c.stderr_contains == ("ValueError",) and c.has_stdout_json and c.stdout_json == {"value": 12.5}
    assert c.stdout_json_subset == {"ok": True} and c.files_equal == (("out.txt", "done\n"),)
    assert c.files_contain == (("log.txt", ("started",)),)
    assert c.resolved_argv("/py") == ["/py", "-c", "print(1)", "12.50"]


def test_defaults_and_several_checks_per_file():
    cs = load(one() + one(head='node = "n2"\nargv = ["echo"]\n'))
    assert [c.node for c in cs] == ["n1", "n2"] and cs[0].timeout_s == 10 and cs[0].stdin == "" and cs[0].env == ()


def test_stdout_json_may_be_any_toml_json_value():
    for lit in ("false", "0", '""', "[]", "[1, 2]", '"s"', "{}"):
        (c,) = load(one(expect=f"stdout_json = {lit}"))
        assert c.has_stdout_json


def test_python_placeholder_only_replaces_whole_elements():
    (c,) = load(one(head='node = "n"\nargv = ["{python}", "-c", "x = {python}"]\n'))
    assert c.resolved_argv("/py") == ["/py", "-c", "x = {python}"]
    (c,) = load(one(head='node = "n"\nargv = ["bin/tool", "a"]\n'))
    assert c.argv[0] == "bin/tool"


# ---- parser: rejected --------------------------------------------------------------------------
def rejects(text, match=None, rel=REL):
    with pytest.raises(ValueError, match=match) as e:
        load(text, rel)
    return str(e.value)


def test_unknown_keys_are_named_and_content_is_never_echoed():
    assert "'bogus'" in rejects("bogus = 1\n" + one())
    assert "'extra'" in rejects(one("extra = 1"))
    assert "'regex'" in rejects(one(expect='exit_code = 0\nregex = "a"'))
    msg = rejects(one(expect='exit_code = 0\nstdout_matches = "SECRET-PAYLOAD"'))
    assert "stdout_matches" in msg and "SECRET-PAYLOAD" not in msg


def test_toml_errors_do_not_echo_content():
    msg = rejects("[[check\nSECRET-PAYLOAD = ")
    assert "SECRET-PAYLOAD" not in msg
    rejects(b"\xff\xfe\x00")
    rejects("")
    rejects("check = 3")


@pytest.mark.parametrize("prog", ["/bin/echo", "\\\\host\\\\x", "C:/x", "-rf", "../x", "a/../x", "a//b", "x/", ""])
def test_argv0_must_be_bare_or_relative(prog):
    rejects(one(head=f'node = "n"\nargv = ["{prog}"]\n'), "argv")


@pytest.mark.parametrize("argv", ['[]', '"x"', '[1]', '["a\\u0000b"]', "[" + ",".join(['"a"'] * 65) + "]"])
def test_argv_shape(argv):
    rejects(one(head=f'node = "n"\nargv = {argv}\n'), "argv")


@pytest.mark.parametrize("name", ["PATH", "HOME", "TMPDIR", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
                                  "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "HERA_HOME",
                                  "lower", "1BAD", "A-B"])
def test_forbidden_or_malformed_env_names(name):
    rejects(one(f'env = {{ "{name}" = "x" }}'), "env")


def test_env_value_must_be_a_string():
    rejects(one("env = { A = 1 }"), "env")


def test_oversized_inputs():
    rejects(one(f'stdin = "{"a" * (64 * 1024 + 1)}"'), "stdin")
    load(one(f'stdin = "{"a" * (64 * 1024)}"'))
    rejects(one(f'files = {{ "a.txt" = "{"a" * (64 * 1024 + 1)}" }}'), "files")
    rejects("#" * (256 * 1024 + 1), "larger")
    rejects(b"#" * (256 * 1024 + 1), "larger")


@pytest.mark.parametrize("path", ["/abs", "../x", "a/../b", "", "a//b", "a\\\\b", "_hera_checks/x", "_hera_results", "sp ace"])
def test_input_and_expected_file_paths(path):
    rejects(one(f'files = {{ "{path}" = "x" }}'), "files")
    rejects(one(expect=f'files_equal = {{ "{path}" = "x" }}'), "files_equal")
    rejects(one(expect=f'files_contain = {{ "{path}" = ["x"] }}'), "files_contain")


def test_duplicate_node_and_bad_node_names():
    assert "duplicate node" in rejects(one() + one())
    for bad in ("", "1abc", "a-b", "a" * 65, "has space"):
        rejects(one(head=f'node = "{bad}"\nargv = ["x"]\n'), "node")
    load(one(head=f'node = "{"a" * 64}"\nargv = ["x"]\n'))


def test_too_many_checks():
    many = "".join(one(head=f'node = "n{i}"\nargv = ["x"]\n') for i in range(201))
    rejects(many, "more than 200")
    load("".join(one(head=f'node = "n{i}"\nargv = ["x"]\n') for i in range(200)))


def test_no_expectations_or_vacuous_ones():
    rejects("[[check]]\nnode = 'n'\nargv = ['x']\n[check.expect]\n", "at least one")
    rejects("[[check]]\nnode = 'n'\nargv = ['x']\n", "expect")
    for e in ("stdout_contains = []", 'stdout_contains = [""]', "stdout_json_subset = {}", "files_equal = {}",
              'files_contain = { "a" = [] }', 'exit_code = "0"', "exit_code = true", "exit_code = 300"):
        rejects(one(expect=e))


@pytest.mark.parametrize("bad", ["0", "-1", "121", '"5"', "nan", "inf", "true"])
def test_timeout_bounds(bad):
    rejects(one(f"timeout_s = {bad}"), "timeout_s")
    load(one("timeout_s = 120"))


def test_json_values_must_be_json():
    rejects(one(expect="stdout_json = 1979-05-27"), "JSON")
    rejects(one(expect="stdout_json = nan"), "JSON")
    rejects(one(expect="stdout_json = " + "[" * 70 + "]" * 70), "nested")


def test_relpath_must_be_a_contract_file():
    for rel in ("x.toml", "x.py", "test_x.py", "/x.contract.toml", "../x.contract.toml", ".contract.toml"):
        with pytest.raises(ValueError):
            load(one(), rel)
    load(one(), "d/e/x.contract.toml")


# ---- evaluate ----------------------------------------------------------------------------------
def ev(expect, result, tmp_path=None, head=None, extra=""):
    (c,) = load(one(extra, expect, head) if head else one(extra, expect))
    return evaluate(c, result, tmp_path or "/nonexistent")


def test_exit_code():
    assert ev("exit_code = 0", rr())[0] is S.PASS
    st, failed = ev("exit_code = 0", rr(code=3))
    assert st is S.FAIL and failed == ["exit_code: expected 0, got 3"]
    assert ev("exit_code = -9", rr(code=-9))[0] is S.PASS


def test_stdout_equals_normalises_line_endings():
    assert ev('stdout_equals = "a\\nb\\n"', rr(out=b"a\r\nb\r\n"))[0] is S.PASS
    assert ev('stdout_equals = "a\\r\\nb\\n"', rr(out=b"a\nb\n"))[0] is S.PASS
    assert ev('stdout_equals = "a\\n"', rr(out=b"a\n\n"))[0] is S.FAIL                  # exact otherwise
    assert ev('stdout_equals = "a"', rr(out=b"a\n"))[0] is S.FAIL
    assert ev('stdout_equals = "\xe9"', rr(out="\xe9".encode()))[0] is S.PASS
    assert ev('stdout_equals = "a"', rr(out=b"\xffa"))[0] is S.FAIL                     # invalid UTF-8 does not crash


def test_contains():
    assert ev('stdout_contains = ["12", "5"]', rr(out=b"12.5"))[0] is S.PASS
    st, failed = ev('stdout_contains = ["12", "9"]', rr(out=b"12.5"))
    assert st is S.FAIL and failed == ["stdout_contains: missing '9'"]
    assert ev('stderr_contains = ["ValueError"]', rr(err=b"x ValueError y"))[0] is S.PASS
    assert ev('stderr_contains = ["ValueError"]', rr(out=b"ValueError"))[0] is S.FAIL   # right stream only
    assert ev('stdout_contains = ["a\\nb"]', rr(out=b"a\r\nb"))[0] is S.PASS


def test_stdout_json_equality_is_strict():
    assert ev("stdout_json = { value = 12.5 }", rr(out=b'{"value": 12.5}'))[0] is S.PASS
    assert ev("stdout_json = { value = 12.5 }", rr(out=b'{"value": 12.5, "x": 1}'))[0] is S.FAIL
    assert ev("stdout_json = [1, 2]", rr(out=b"[1, 2.0]"))[0] is S.PASS                   # numbers compare by value
    assert ev("stdout_json = 1", rr(out=b"true"))[0] is S.FAIL                            # a bool is not a number
    assert ev("stdout_json = true", rr(out=b"1"))[0] is S.FAIL
    assert ev("stdout_json = false", rr(out=b"false\n"))[0] is S.PASS
    assert ev("stdout_json = []", rr(out=b"[]"))[0] is S.PASS
    assert ev('stdout_json = "s"', rr(out=b'"s"'))[0] is S.PASS
    assert ev("stdout_json = [1, 2]", rr(out=b"[2, 1]"))[0] is S.FAIL


def test_stdout_json_subset():
    out = b'{"ok": true, "n": {"a": 1, "b": [1, 2]}, "z": 0}'
    assert ev("stdout_json_subset = { ok = true }", rr(out=out))[0] is S.PASS
    assert ev("stdout_json_subset = { n = { a = 1 } }", rr(out=out))[0] is S.PASS         # recursive for dicts
    assert ev("stdout_json_subset = { n = { b = [1, 2] } }", rr(out=out))[0] is S.PASS
    assert ev("stdout_json_subset = { n = { b = [1] } }", rr(out=out))[0] is S.FAIL        # lists are exact
    assert ev("stdout_json_subset = { n = { a = 2 } }", rr(out=out))[0] is S.FAIL
    assert ev("stdout_json_subset = { missing = 1 }", rr(out=out))[0] is S.FAIL
    assert ev("stdout_json_subset = { ok = 1 }", rr(out=out))[0] is S.FAIL
    assert ev("stdout_json_subset = { ok = true }", rr(out=b"[1]"))[0] is S.FAIL           # not an object


@pytest.mark.parametrize("out", [b"not json", b"", b"{", b"\xff\xfe", b"NaN", b'{"a": Infinity}', b"1" * 5000,
                                 b'{"a":1}garbage'])
def test_invalid_json_fails_the_expectation_without_crashing(out):
    for expect in ("stdout_json = 1", "stdout_json_subset = { a = 1 }"):
        st, failed = ev(expect, rr(out=out))
        assert st is S.FAIL and failed and "JSON" in failed[0] or "json" in failed[0]


def test_deeply_nested_and_huge_json_do_not_crash():
    deep = b"[" * 200_000 + b"]" * 200_000
    st, failed = ev("stdout_json = []", rr(out=deep))
    assert st is S.FAIL and "stdout_json" in failed[0]
    st, failed = ev("stdout_json_subset = { a = 1 }", rr(out=b'{"a":' * 50_000 + b"1" + b"}" * 50_000))
    assert st is S.FAIL
    big = b"[" + b"1," * (600_000) + b"1]"                                              # > 1 MB
    st, failed = ev("stdout_json = []", rr(out=big))
    assert st is S.FAIL and "1 MB" in failed[0]
    ok = b"[" + b"1," * 100_000 + b"1]"                                                  # < 1 MB parses fine
    assert ev("stdout_json = " + "[" + ",".join(["1"] * 100_001) + "]", rr(out=ok))[0] is S.PASS


def test_huge_plain_output_is_just_a_mismatch():
    assert ev('stdout_equals = "x"', rr(out=b"y" * 5_000_000))[0] is S.FAIL
    assert ev("exit_code = 0", rr(truncated=True))[0] is S.FAIL                            # truncated output never passes


def test_files(tmp_path):
    (tmp_path / "out.txt").write_bytes(b"done\r\n")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "log.txt").write_text("started ok\n")
    e = 'files_equal = { "out.txt" = "done\\n" }\nfiles_contain = { "d/log.txt" = ["started", "ok"] }'
    assert ev(e, rr(), tmp_path)[0] is S.PASS
    st, failed = ev('files_equal = { "out.txt" = "other" }', rr(), tmp_path)
    assert st is S.FAIL and failed == ["files_equal: out.txt differs from the expected text"]
    assert ev('files_equal = { "nope.txt" = "x" }', rr(), tmp_path)[1] == ["files_equal: nope.txt does not exist"]
    assert ev('files_contain = { "d/log.txt" = ["zzz"] }', rr(), tmp_path)[0] is S.FAIL
    assert ev('files_equal = { "d" = "x" }', rr(), tmp_path)[0] is S.FAIL                  # a directory is not a file


def test_files_equal_on_a_symlink_fails(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("done\n")
    (tmp_path / "out.txt").symlink_to(real)
    st, failed = ev('files_equal = { "out.txt" = "done\\n" }', rr(), tmp_path)
    assert st is S.FAIL and "symbolic link" in failed[0]
    assert ev('files_contain = { "out.txt" = ["done"] }', rr(), tmp_path)[0] is S.FAIL
    (tmp_path / "linkdir").symlink_to(tmp_path / "sub")                                    # link in the middle of the path
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.txt").write_text("x")
    assert ev('files_equal = { "linkdir/f.txt" = "x" }', rr(), tmp_path)[0] is S.FAIL
    (tmp_path / "dangling").symlink_to(tmp_path / "missing")
    assert ev('files_equal = { "dangling" = "x" }', rr(), tmp_path)[0] is S.FAIL


def test_oversized_and_special_files_fail(tmp_path):
    (tmp_path / "big.txt").write_bytes(b"a" * (1024 * 1024 + 1))
    st, failed = ev('files_contain = { "big.txt" = ["a"] }', rr(), tmp_path)
    assert st is S.FAIL and "1 MB" in failed[0]
    (tmp_path / "ok.txt").write_bytes(b"a" * (1024 * 1024))
    assert ev('files_contain = { "ok.txt" = ["a"] }', rr(), tmp_path)[0] is S.PASS
    if hasattr(os, "mkfifo"):
        os.mkfifo(tmp_path / "pipe")
        assert ev('files_contain = { "pipe" = ["a"] }', rr(), tmp_path)[0] is S.FAIL       # does not hang


def test_timeout_and_not_started_statuses():
    assert ev("exit_code = 0", rr(code=None, timed_out=True))[0] is S.TIMEOUT
    assert ev("exit_code = 0", rr(code=None))[0] is S.ERROR
    assert ev("exit_code = 1", rr(code=None))[0] is S.ERROR                                # never a match


def test_all_failures_are_listed():
    st, failed = ev('exit_code = 0\nstdout_equals = "a"\nstdout_contains = ["b"]', rr(out=b"c", code=2))
    assert st is S.FAIL and len(failed) == 3


# ---- static: the oracle side never loads or runs candidate code --------------------------------
def test_oracle_modules_import_no_code_loading_machinery_and_never_eval():
    import ast
    import hera.oracle.contract as c
    import hera.oracle.contract_runner as r
    banned = {"importlib", "runpy", "pytest", "_pytest", "imp", "pkgutil", "zipimport", "code", "codeop",
              "pickle", "marshal", "ctypes"}
    for mod in (c, r):
        tree = ast.parse(open(mod.__file__, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]} if node.level == 0 else set()
            elif isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):            # builtins (re.compile is a method, not this)
                    assert f.id not in ("exec", "eval", "compile", "__import__", "execfile"), (mod.__name__, f.id)
                elif isinstance(f, ast.Attribute):
                    assert f.attr not in ("exec", "eval", "import_module", "__import__"), (mod.__name__, f.attr)
                continue
            else:
                continue
            assert not names & banned, (mod.__name__, names)
