"""Workspaces: bare repo, candidate worktrees, verification checkouts. Invented data only."""
import os
import subprocess
from pathlib import Path

import pytest

from hera import paths
from hera.workspace import TaskRepo, WorkspaceError, find_escaping_symlinks, _git_env

ENV = {**_git_env()}


def git(cwd, *args):
    cmd = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "user.name=T", "-c", "user.email=t@example.invalid",
           "-c", "commit.gpgsign=false", *args]
    r = subprocess.run(cmd, cwd=cwd, env=ENV, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERA_HOME", str(tmp_path / "hera"))
    paths.ensure_layout()
    return tmp_path / "hera"


@pytest.fixture
def source(tmp_path):
    d = tmp_path / "src repo"
    d.mkdir()
    git(d, "init", "-q", "-b", "main")
    (d / "a.txt").write_text("alpha\n")
    (d / "sub").mkdir()
    (d / "sub" / "b.txt").write_text("beta\n")
    (d / "run.sh").write_text("#!/bin/sh\n")
    (d / "run.sh").chmod(0o755)
    (d / ".gitignore").write_text("*.log\n")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "init")
    return d


@pytest.fixture
def repo(home, source):
    return TaskRepo.create("t1", source)


def tree_files(root: Path):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() or p.is_symlink())


def test_work_dir_in_layout(home):
    assert paths.work_dir() == home / "work" and paths.work_dir().is_dir()


def test_create_open(home, source):
    r = TaskRepo.create("t1", source)
    assert r.bare == home / "work" / "t1" / "repo.git" and (r.bare / "HEAD").exists()
    assert r.base_commit == git(source, "rev-parse", "HEAD")
    r2 = TaskRepo.open("t1")
    assert r2.base_commit == r.base_commit
    with pytest.raises(FileExistsError):
        TaskRepo.create("t1", source)
    with pytest.raises(FileNotFoundError):
        TaskRepo.open("nope")


def test_create_with_explicit_base(home, source):
    first = git(source, "rev-parse", "HEAD")
    (source / "c.txt").write_text("c\n")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "second")
    r = TaskRepo.create("t2", source, base_commit=first)
    assert r.base_commit == first
    with pytest.raises(ValueError):
        TaskRepo.create("t3", source, base_commit="abc123")
    assert not (home / "work" / "t3").exists()


@pytest.mark.parametrize("bad", ["", ".hidden", "..", "a/b", "a b", "x" * 65, "ü", "a\nb"])
def test_bad_ids(home, source, bad):
    with pytest.raises(ValueError):
        TaskRepo.create(bad, source)
    with pytest.raises(ValueError):
        TaskRepo.open(bad)


def test_bad_candidate_names(repo):
    for bad in ("", ".x", "a/b", "..", "x" * 65):
        with pytest.raises(ValueError):
            repo.new_candidate(bad)


@pytest.mark.parametrize("bad", ["https://example.invalid/r.git", "git@example.invalid:r.git",
                                 "ssh://example.invalid/r", "file:///tmp/x", "ext::sh -c id",
                                 "host:path", "-oProxyCommand=x"])
def test_url_like_sources_rejected(home, bad):
    with pytest.raises(ValueError):
        TaskRepo.create("t9", bad)
    assert not (home / "work" / "t9").exists()


def test_missing_source_dir(home, tmp_path):
    with pytest.raises(ValueError):
        TaskRepo.create("t9", tmp_path / "does-not-exist")


def test_candidates_independent_and_commit_all(repo):
    c1, c2 = repo.new_candidate("one"), repo.new_candidate("two")
    assert c1.path != c2.path and c1.branch == "cand/one"
    assert c1.head() == c2.head() == repo.base_commit
    (c1.path / "new.txt").write_text("hello\n")
    h1 = c1.commit_all("add new")
    assert h1 != repo.base_commit and len(h1) == 40
    assert c2.head() == repo.base_commit and not (c2.path / "new.txt").exists()
    assert c1.commit_all("again") == h1                       # nothing to commit
    author = git(repo.bare, "log", "-1", "--format=%an <%ae>", h1)
    assert author == "HERA <hera@localhost>"
    with pytest.raises(FileExistsError):
        repo.new_candidate("one")


def test_verification_checkout_exact_tree(repo, tmp_path):
    c = repo.new_candidate("v")
    (c.path / "a.txt").write_text("changed\n")
    (c.path / "added.py").write_text("x = 1\n")
    (c.path / "run.sh").chmod(0o755)
    # things that must NOT reach verification
    (c.path / "untracked_after.txt").write_text("u")       # becomes untracked below
    (c.path / "debug.log").write_text("ignored")           # ignored by .gitignore
    (c.path / ".venv" / "bin").mkdir(parents=True)
    (c.path / ".venv" / "bin" / "python").write_text("venv")
    (c.path / "__pycache__").mkdir()
    (c.path / "__pycache__" / "m.pyc").write_text("cache")
    # commit only tracked changes + added.py, leaving others untracked
    git_dir = c.git_dir
    for f in ("a.txt", "added.py"):
        subprocess.run(["git", "--git-dir", str(git_dir), "--work-tree", str(c.path), "add", f],
                       env=ENV, check=True)
    subprocess.run(["git", "--git-dir", str(git_dir), "--work-tree", str(c.path), "-c", "user.name=T",
                    "-c", "user.email=t@e.invalid", "commit", "-q", "-m", "m"], env=ENV, check=True)
    commit = c.head()
    v1 = repo.verification_checkout(commit)
    v2 = repo.verification_checkout(commit)
    assert v1 != v2 and v1.parent == repo.root / "verify"
    assert tree_files(v1) == [".gitignore", "a.txt", "added.py", "run.sh", "sub/b.txt"]
    assert (v1 / "a.txt").read_text() == "changed\n"
    assert os.access(v1 / "run.sh", os.X_OK) and not os.access(v1 / "a.txt", os.X_OK)
    assert not (v1 / ".git").exists() and not any(p.name == ".git" for p in v1.rglob("*"))
    repo.cleanup_verification(v1)
    assert not v1.exists() and v2.exists()


def test_verification_rejections(repo, tmp_path):
    c = repo.new_candidate("r")
    (c.path / "f").write_text("1")
    good = c.commit_all("one")
    repo.verification_checkout(good)
    with pytest.raises(ValueError):
        repo.verification_checkout(good[:12])
    with pytest.raises(ValueError):
        repo.verification_checkout("0" * 40)
    with pytest.raises(ValueError):
        repo.verification_checkout("not-a-hash" + "0" * 30)
    # a commit that exists in the repo but is not descended from the base
    empty_tree = subprocess.run(["git", "--git-dir", str(repo.bare), "mktree"], input="", env=ENV,
                                capture_output=True, text=True, check=True).stdout.strip()
    orphan = subprocess.run(["git", "--git-dir", str(repo.bare), "-c", "user.name=T", "-c", "user.email=t@e.invalid",
                             "commit-tree", empty_tree, "-m", "orphan"], env=ENV, capture_output=True,
                            text=True, check=True).stdout.strip()
    with pytest.raises(ValueError, match="descendant"):
        repo.verification_checkout(orphan)
    assert list((repo.root / "verify").iterdir()) != [] and len(list((repo.root / "verify").iterdir())) == 1


def test_hooks_and_config_do_not_run(repo, tmp_path):
    marker = tmp_path / "MARKER"
    c = repo.new_candidate("h")
    hook_body = f"#!/bin/sh\necho ran >> '{marker}'\n"
    for hooks in (repo.bare / "hooks", c.git_dir / "hooks", c.path / ".githooks"):
        hooks.mkdir(parents=True, exist_ok=True)
        for name in ("pre-commit", "post-checkout", "post-commit", "commit-msg", "prepare-commit-msg"):
            (hooks / name).write_text(hook_body)
            (hooks / name).chmod(0o755)
    # candidate-local config: hooks path, fsmonitor, a clean filter + attribute
    subprocess.run(["git", "-C", str(c.path), "config", "core.hooksPath", str(c.path / ".githooks")], env=ENV, check=True)
    subprocess.run(["git", "-C", str(c.path), "config", "core.fsmonitor", f"{marker}.fsm"], env=ENV, check=True)
    (Path(str(marker) + ".fsm")).write_text(hook_body)
    os.chmod(str(marker) + ".fsm", 0o755)
    subprocess.run(["git", "-C", str(c.path), "config", "filter.evil.clean", f"sh -c 'echo f >> {marker}; cat'"], env=ENV, check=True)
    (c.path / ".gitattributes").write_text("* filter=evil\n")
    (c.path / "x.txt").write_text("x\n")
    commit = c.commit_all("with hooks planted")
    v = repo.verification_checkout(commit)
    assert (v / "x.txt").read_text() == "x\n"
    assert not marker.exists()
    cfg = (repo.bare / "config").read_text().lower()
    assert "filter" not in cfg and "hookspath" not in cfg and "fsmonitor" not in cfg


def test_rewritten_dotgit_file_is_not_trusted(repo, tmp_path, source):
    c = repo.new_candidate("d")
    (c.path / ".git").write_text(f"gitdir: {source / '.git'}\n")       # points at another repo
    (c.path / "z.txt").write_text("z")
    before = git(source, "rev-parse", "HEAD")
    c.commit_all("z")
    assert git(source, "rev-parse", "HEAD") == before


def test_escaping_symlinks(repo, tmp_path):
    c = repo.new_candidate("s")
    os.symlink("/etc/hosts", c.path / "hosts")
    commit = c.commit_all("abs link")
    with pytest.raises(ValueError, match="symlink"):
        repo.verification_checkout(commit)
    assert list((repo.root / "verify").iterdir()) == []
    c2 = repo.new_candidate("s2")
    (c2.path / "d").mkdir()
    os.symlink("../../..", c2.path / "d" / "up")
    commit2 = c2.commit_all("rel link")
    with pytest.raises(ValueError, match="symlink"):
        repo.verification_checkout(commit2)
    c3 = repo.new_candidate("s3")
    os.symlink("a.txt", c3.path / "ok-link")
    ok = repo.verification_checkout(c3.commit_all("inside link"))
    assert (ok / "ok-link").is_symlink() and find_escaping_symlinks(ok) == []


def test_find_escaping_symlinks_direct(tmp_path):
    root = tmp_path / "r"
    (root / "d").mkdir(parents=True)
    os.symlink("../..", root / "d" / "up")
    os.symlink(str(tmp_path), root / "abs")
    os.symlink("d", root / "fine")
    os.symlink("loop", root / "loop")
    assert find_escaping_symlinks(root) == ["abs", "d/up"]


def test_changed_paths_and_stat(repo):
    c = repo.new_candidate("p")
    (c.path / "a.txt").write_text("alpha\nmore\nlines\n")          # +2
    (c.path / "name with space.txt").write_text("1\n2\n")           # +2
    (c.path / "weird\nname.txt").write_text("x\n")                  # +1
    (c.path / "sub" / "b.txt").unlink()                             # -1
    (c.path / "blob.bin").write_bytes(b"\x00\x01\x02")
    commit = c.commit_all("changes")
    assert repo.changed_paths(repo.base_commit, commit) == sorted(
        ["a.txt", "name with space.txt", "weird\nname.txt", "sub/b.txt", "blob.bin"])
    assert repo.diff_stat(repo.base_commit, commit) == {"files": 5, "insertions": 5, "deletions": 1}
    assert repo.changed_paths(commit, commit) == []
    assert repo.diff_stat(commit, commit) == {"files": 0, "insertions": 0, "deletions": 0}


def test_cleanup_refuses_outside(repo, tmp_path):
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "f").write_text("keep")
    for bad in (outside, repo.root, repo.root / "verify", repo.root / "candidates",
                repo.root / "verify" / ".." / "candidates"):
        with pytest.raises(ValueError):
            repo.cleanup_verification(bad)
    assert (outside / "f").exists()
    v = repo.verification_checkout(repo.base_commit)
    link = repo.root / "verify" / "link"
    os.symlink(outside, link)
    with pytest.raises(ValueError):
        repo.cleanup_verification(link)
    assert (outside / "f").exists()
    repo.cleanup_verification(v)
    assert not v.exists()


def test_submodule_entry_not_initialised(repo):
    c = repo.new_candidate("sm")
    nested = c.path / "vendor" / "lib"
    nested.mkdir(parents=True)
    git(nested, "init", "-q")                                   # an embedded repository
    (nested / "inner.txt").write_text("inner\n")
    git(nested, "add", "-A")
    git(nested, "commit", "-q", "-m", "inner")
    commit = c.commit_all("gitlink")
    assert git(repo.bare, "ls-tree", commit, "vendor/lib").startswith("160000 commit")
    v = repo.verification_checkout(commit)
    assert (v / "vendor" / "lib").is_dir() and list((v / "vendor" / "lib").iterdir()) == []


def test_workspace_error_type():
    assert issubclass(WorkspaceError, RuntimeError)
