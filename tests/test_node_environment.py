"""Finding and probing Node, the node-specific rules of check objects, and the CI guard.

Nothing here needs a sandbox. Tests that need a working Node skip with a clear reason, except on CI,
where ``test_node_is_usable_on_ci`` fails instead (a CI run must never silently skip the node adapter)."""
import dataclasses
import os
import stat

import pytest

from hera.oracle import nodeenv
from hera.oracle.adapters.node_adapter import NodeAdapter
from hera.oracle.checkstore import freeze
from hera.oracle.model import DraftCheck, validate_draft, validate_relpath
from hera.oracle.nodeenv import NodeInfo, NodeUnavailable
from toyrepo_js import CHECK_FILES, CHECKS, NODE_OK, NODE_SKIP_REASON, REQUIREMENTS, needs_node


def test_node_is_usable_on_ci():
    """On CI (CI=true) Node 22.6+ with type stripping must be present: nothing may skip for that
    reason. Elsewhere this passes, and the node tests skip with the reason below."""
    if os.environ.get("CI", "").lower() == "true":
        assert NODE_OK, "CI must provide a usable Node.js (actions/setup-node with node-version 22): " + NODE_SKIP_REASON


@needs_node
def test_real_node_is_found_and_probed():
    path = nodeenv.find_node()
    info = nodeenv.node_info(path)
    assert info.usable and info.version >= nodeenv.MIN_NODE and info.ts_flags is not None
    assert nodeenv.require_node().path == path
    assert nodeenv.install_dir(path) == os.path.dirname(os.path.dirname(os.path.realpath(path)))


def _fake_node(tmp_path, body="#!/bin/sh\necho v22.9.0\n"):
    prefix = tmp_path / "node-v22" 
    (prefix / "bin").mkdir(parents=True)
    exe = prefix / "bin" / "node"
    exe.write_text(body)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return prefix, exe


def test_find_node_explicit_environment_and_missing(tmp_path, monkeypatch):
    prefix, exe = _fake_node(tmp_path)
    assert nodeenv.find_node(str(exe)) == os.path.realpath(exe)
    monkeypatch.setenv("HERA_NODE", str(exe))
    assert nodeenv.find_node() == os.path.realpath(exe)
    monkeypatch.setenv("HERA_NODE", str(tmp_path / "nope"))
    assert nodeenv.find_node() is None
    assert nodeenv.find_node(str(tmp_path / "also-nope")) is None


def test_install_directory_is_two_levels_above_the_resolved_executable(tmp_path):
    prefix, exe = _fake_node(tmp_path)
    link_dir = tmp_path / "elsewhere"
    link_dir.mkdir()
    os.symlink(exe, link_dir / "node")                         # a version-manager style shim
    assert nodeenv.install_dir(str(link_dir / "node")) == os.path.realpath(prefix)
    assert nodeenv.readable_paths(str(link_dir / "node")) == [os.path.realpath(prefix)]


def test_install_directory_in_the_system_roots_is_not_added_twice():
    if os.path.exists("/usr/bin/env"):
        assert nodeenv.readable_paths("/usr/bin/env") == ([] if os.name == "posix" and
                                                           __import__("sys").platform.startswith("linux") else ["/usr"])
    assert nodeenv.readable_paths("/node") == []


def test_old_or_missing_node_is_refused_with_a_clear_error(tmp_path, monkeypatch):
    prefix, exe = _fake_node(tmp_path)
    monkeypatch.setattr(nodeenv, "node_info", lambda p: NodeInfo(p, (20, 11, 0), None))
    with pytest.raises(NodeUnavailable, match=r"20\.11\.0 is too old.*22\.6"):
        nodeenv.require_node(str(exe))
    with pytest.raises(NodeUnavailable, match="not found"):
        nodeenv.require_node(str(tmp_path / "nope"))
    monkeypatch.setattr(nodeenv, "node_info", lambda p: NodeInfo(p, None, None))
    with pytest.raises(NodeUnavailable, match="could not be run"):
        nodeenv.require_node(str(exe))


def test_node_without_type_stripping_refuses_typescript_checks_only(conn, hera_home, tmp_path, monkeypatch):
    prefix, exe = _fake_node(tmp_path)
    monkeypatch.setattr(nodeenv, "node_info", lambda p: NodeInfo(p, (22, 6, 0), None))
    assert nodeenv.require_node(str(exe)).usable                              # fine for JavaScript
    with pytest.raises(NodeUnavailable, match="cannot strip TypeScript"):
        nodeenv.require_node(str(exe), need_typescript=True)
    cs = freeze(conn, task_id="toyjs", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES), adapter="node")
    ad = NodeAdapter(node=str(exe))
    co = tmp_path / "co"
    co.mkdir()
    with pytest.raises(NodeUnavailable, match="cannot strip TypeScript"):          # CHECKS has a .test.ts file
        ad.prepare(co, cs, base_tree={}, include_hidden=True)
    js_only = (CHECKS[0], CHECKS[1], CHECKS[2])
    cs_js = freeze(conn, task_id="toyjs", requirements=REQUIREMENTS[:2], checks=js_only,
                   files={k: v for k, v in CHECK_FILES.items() if not k.endswith(".ts")}, adapter="node")
    ad.prepare(co, cs_js, base_tree={}, include_hidden=True)                       # JavaScript-only: accepted
    with pytest.raises(NodeUnavailable, match="cannot strip TypeScript"):
        ad.run_project_tests(co, ("a/x.test.ts",), allow_unsandboxed=True)


def test_adapter_refuses_when_node_is_missing(conn, hera_home, tmp_path):
    cs = freeze(conn, task_id="toyjs", requirements=REQUIREMENTS, checks=CHECKS, files=dict(CHECK_FILES), adapter="node")
    co = tmp_path / "co"
    co.mkdir()
    with pytest.raises(NodeUnavailable, match="not found"):
        NodeAdapter(node=str(tmp_path / "missing-node")).prepare(co, cs, base_tree={}, include_hidden=True)


# ---- check objects -------------------------------------------------------------------------------
@pytest.mark.parametrize("rel,ok", [
    ("a.test.js", True), ("a.test.mjs", True), ("a.test.ts", True), ("a.test.mts", True), ("d/e/a.test.js", True),
    ("a.test.cjs", False), ("a.js", False), ("test_a.py", False), (".test.js", False), ("a.test.js.map", False),
    ("../a.test.js", False), ("/a.test.js", False), ("a b.test.js", False)])
def test_node_relpaths(rel, ok):
    if ok:
        assert validate_relpath(rel, "node") == rel
    else:
        with pytest.raises(ValueError):
            validate_relpath(rel, "node")


@pytest.mark.parametrize("name,ok", [
    ("basic_amount", True), ("parses 12.50", True), ("a:b-c", True), ("A", True),
    ("", False), (" lead", False), ("trail ", False), ("x/y", False), ("it's", False), ("a#b", False),
    ("hera_must_pass", False), ("hera_must_fail", False), ("x" * 121, False)])
def test_node_check_names(name, ok):
    def make():
        return DraftCheck("c", ("R-1",), "visible", "a.test.js", name, runner="node")
    if ok:
        assert make().node == name
    else:
        with pytest.raises(ValueError):
            make()


def test_the_adapter_decides_which_runner_it_runs(conn, hera_home):
    a = DraftCheck("c1", ("R-1",), "visible", "a.test.js", "one", runner="node")
    validate_draft(REQUIREMENTS, (a, dataclasses.replace(a, check_id="c2", node="two")))
    with pytest.raises(ValueError, match="same function"):
        validate_draft(REQUIREMENTS, (a, dataclasses.replace(a, check_id="c2")))
    py = DraftCheck("c3", ("R-1",), "visible", "test_a.py", "test_a")
    with pytest.raises(ValueError, match="runs only 'node' and 'contract'"):
        freeze(conn, task_id="toyjs", requirements=REQUIREMENTS, checks=(a, py), adapter="node",
               files={"a.test.js": b"", "test_a.py": b""})
    with pytest.raises(ValueError, match="runs only 'pytest' and 'contract'"):
        freeze(conn, task_id="toyjs", requirements=REQUIREMENTS, checks=(a,), adapter="pytest",
               files={"a.test.js": b""})
    assert dataclasses.replace(a, check_id="c9").runner == "node"
