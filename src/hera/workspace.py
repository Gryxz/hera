"""Per-task bare repository, per-candidate worktrees, and fresh verification checkouts.

Two properties this module exists for:

1. Nothing from an implementer's workspace reaches verification except the commit itself.
   A verification checkout is built from the commit's tree objects, by hash, into a new
   directory that has no ``.git`` at all.
2. Git is never allowed to run code chosen by the candidate (hooks, filters, fsmonitor,
   external diff drivers). Every git call carries a hardened configuration and a scrubbed
   environment, and the repository config is purged of dangerous keys before use.

Standard library only; ``git`` is called as a subprocess. No network is used: the only
"remote" operation is a local-path clone.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from . import paths

_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_HASH_RE = re.compile(r"[0-9a-fA-F]{40}")
_SCP_LIKE = re.compile(r"^[^/\\]*:")           # "host:path", "ext::cmd", "user@host:path"
_IDENT = ("-c", "user.name=HERA", "-c", "user.email=hera@localhost")

# Passed on every invocation. hooksPath=devnull disables all hooks, fsmonitor=false stops a
# configured fsmonitor program from running, protocol.file.allow=user keeps file transports
# restricted, gc.auto=0 stops git from spawning background maintenance.
_HARDEN = ("-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
           "-c", "protocol.file.allow=user", "-c", "protocol.ext.allow=never",
           "-c", "commit.gpgsign=false", "-c", "gc.auto=0")

# Config keys that can make git run a program or read files the candidate chose. They are
# removed from the repository config before any command that touches a candidate worktree.
_UNSAFE_PREFIXES = ("filter.", "diff.", "merge.", "alias.", "include.", "includeif.",
                    "credential.", "url.", "http.", "gpg.", "mergetool.", "difftool.",
                    "pager.", "submodule.", "sendemail.", "uploadpack.", "receive.")
_UNSAFE_KEYS = frozenset({
    "core.sshcommand", "core.editor", "core.pager", "core.fsmonitor", "core.hookspath",
    "core.askpass", "core.gitproxy", "core.attributesfile", "core.worktree",
    "core.alternaterefscommand", "core.sparsecheckout", "extensions.worktreeconfig",
    "sequence.editor", "gitcvs.dbdriver",
})


class WorkspaceError(RuntimeError):
    """A git operation failed or the workspace is in an unexpected state."""


_private_home: str | None = None


def _scratch_home() -> str:
    """An empty directory used as HOME for git, so no user file can be found through it."""
    global _private_home
    if _private_home is None or not os.path.isdir(_private_home):
        d = tempfile.mkdtemp(prefix="hera-git-home-")
        _private_home = d
        atexit.register(shutil.rmtree, d, True)
    return _private_home


def _git_exe() -> str:
    exe = shutil.which("git")
    if exe is None:
        raise WorkspaceError("git was not found on PATH")
    return exe


def _git_env() -> dict[str, str]:
    git_dir = os.path.dirname(_git_exe())
    return {
        "PATH": os.pathsep.join(dict.fromkeys([git_dir, "/usr/bin", "/bin"])),
        "HOME": _scratch_home(),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }


def _git(args, *, git_dir: Path | None = None, work_tree: Path | None = None,
         cwd: Path | None = None, input: bytes | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    cmd = [_git_exe(), *_HARDEN]
    if git_dir is not None:
        cmd += ["--git-dir", str(git_dir)]
    if work_tree is not None:
        cmd += ["--work-tree", str(work_tree)]
    cmd += [str(a) for a in args]
    try:
        r = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=_git_env(), input=input,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    except subprocess.TimeoutExpired as e:
        raise WorkspaceError(f"git {args[0]} timed out") from e
    if check and r.returncode != 0:
        raise WorkspaceError(f"git {args[0]} failed: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r


def _check_id(value: str, what: str) -> str:
    if (not isinstance(value, str) or not _ID_RE.fullmatch(value) or value.startswith(".")):
        raise ValueError(f"invalid {what}: must match [A-Za-z0-9_.-]{{1,64}} and not start with '.'")
    return value


def _check_hash(value: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise ValueError("a full 40-character commit hash is required")
    return value.lower()


def _bad_source(source: str) -> bool:
    return "://" in source or bool(_SCP_LIKE.match(source)) or source.startswith("-")


def find_escaping_symlinks(root) -> list[str]:
    """Relative paths of symlinks under ``root`` whose fully resolved target is outside it."""
    root_real = os.path.realpath(root)
    bad: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for n in (*dirnames, *filenames):
            p = os.path.join(dirpath, n)
            if os.path.islink(p):
                real = os.path.realpath(p)
                if real != root_real and not real.startswith(root_real + os.sep):
                    bad.append(os.path.relpath(p, root))
    return sorted(bad)


def _rm_tree(path: Path) -> None:
    def onerror(func, p, _exc):
        try:
            os.chmod(os.path.dirname(p), 0o700)
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerror)


@dataclass
class Worktree:
    task: "TaskRepo"
    name: str
    path: Path
    branch: str
    git_dir: Path           # the worktree's admin directory inside the bare repository

    def _git(self, args, **kw):
        # Explicit --git-dir/--work-tree: never trust the ".git" file inside the worktree,
        # which the implementer can rewrite to point anywhere.
        self.task._scrub_config()
        return _git(args, git_dir=self.git_dir, work_tree=self.path, cwd=self.task.root, **kw)

    def head(self) -> str:
        return self._git(["rev-parse", "HEAD"]).stdout.decode().strip()

    def commit_all(self, message: str) -> str:
        """Stage everything and commit with the fixed HERA identity. Returns the commit hash,
        or the current HEAD if there was nothing to commit."""
        self._git(["add", "-A"])
        if self._git(["diff-index", "--cached", "--quiet", "HEAD", "--"], check=False).returncode == 0:
            return self.head()
        self._git([*_IDENT, "commit", "--quiet", "--no-verify", "-m", message])
        return self.head()


class TaskRepo:
    def __init__(self, task_id: str, base_commit: str):
        self.task_id = _check_id(task_id, "task id")
        self.root = paths.work_dir() / task_id
        self.bare = self.root / "repo.git"
        self.base_commit = base_commit

    # ---- creation ------------------------------------------------------------------
    @classmethod
    def create(cls, task_id: str, source_repo_path, base_commit: str | None = None) -> "TaskRepo":
        _check_id(task_id, "task id")
        src_text = os.fspath(source_repo_path)
        if _bad_source(src_text):
            raise ValueError("source must be a local directory path, not a URL or remote spec")
        src = Path(src_text).expanduser()
        if not src.is_dir():
            raise ValueError("source repository path is not a directory")
        src = src.resolve()
        root = paths.work_dir() / task_id
        if root.exists():
            raise FileExistsError(f"task workspace already exists: {task_id}")
        root.mkdir(parents=True)
        try:
            _git(["clone", "--bare", "--no-hardlinks", "--quiet", "--template=", "--", str(src),
                  str(root / "repo.git")], cwd=root)
            bare = root / "repo.git"
            repo = cls.__new__(cls)
            repo.task_id, repo.root, repo.bare = task_id, root, bare
            repo._scrub_config()
            head = _git(["rev-parse", "--verify", "HEAD^{commit}"], git_dir=bare, cwd=root,
                        check=False)
            if base_commit is None:
                if head.returncode != 0:
                    raise ValueError("source repository has no commits")
                base = head.stdout.decode().strip()
            else:
                base = _check_hash(base_commit)
            repo.base_commit = base
            repo._require_commit(base)
            (root / "meta.json").write_text(json.dumps({"base_commit": base}), encoding="utf-8")
            return repo
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise

    @classmethod
    def open(cls, task_id: str) -> "TaskRepo":
        _check_id(task_id, "task id")
        root = paths.work_dir() / task_id
        meta = root / "meta.json"
        if not meta.is_file() or not (root / "repo.git").is_dir():
            raise FileNotFoundError(f"no such task workspace: {task_id}")
        base = _check_hash(json.loads(meta.read_text(encoding="utf-8"))["base_commit"])
        repo = cls(task_id, base)
        repo._require_commit(base)
        return repo

    # ---- helpers -------------------------------------------------------------------
    def _g(self, args, **kw):
        return _git(args, git_dir=self.bare, cwd=self.root, **kw)

    def _scrub_config(self) -> None:
        """Remove config keys that could make git execute candidate-chosen programs.
        Candidate worktrees share this config, so the implementer can write to it."""
        cfg = self.bare / "config"
        if cfg.is_file():
            r = _git(["config", "--file", str(cfg), "-z", "--list", "--name-only"], cwd=self.root)
            for raw in r.stdout.split(b"\0"):
                key = raw.decode("utf-8", "replace")
                low = key.lower()
                if low in _UNSAFE_KEYS or low.startswith(_UNSAFE_PREFIXES):
                    _git(["config", "--file", str(cfg), "--unset-all", key], cwd=self.root,
                         check=False)
        for f in [self.bare / "config.worktree", *self.bare.glob("worktrees/*/config.worktree")]:
            f.unlink(missing_ok=True)

    def _require_commit(self, commit: str) -> str:
        commit = _check_hash(commit)
        if self._g(["cat-file", "-e", f"{commit}^{{commit}}"], check=False).returncode != 0:
            raise ValueError("unknown commit")
        return commit

    def _require_descendant(self, commit: str) -> str:
        commit = self._require_commit(commit)
        r = self._g(["merge-base", "--is-ancestor", self.base_commit, commit], check=False)
        if r.returncode == 1:
            raise ValueError("commit is not a descendant of the task base commit")
        if r.returncode != 0:
            raise WorkspaceError(f"git merge-base failed: {r.stderr.decode('utf-8', 'replace')}")
        return commit

    # ---- candidates ----------------------------------------------------------------
    def new_candidate(self, name: str) -> Worktree:
        _check_id(name, "candidate name")
        self._scrub_config()
        path = self.root / "candidates" / name
        if path.exists():
            raise FileExistsError(f"candidate already exists: {name}")
        path.parent.mkdir(exist_ok=True)
        branch = f"cand/{name}"
        self._g(["worktree", "add", "--quiet", "-b", branch, str(path), self.base_commit])
        # Trusted moment: we just created the worktree, so discovery from it is safe.
        admin = _git(["rev-parse", "--absolute-git-dir"], cwd=path).stdout.decode().strip()
        return Worktree(self, name, path, branch, Path(admin))

    # ---- verification --------------------------------------------------------------
    def verification_checkout(self, commit: str) -> Path:
        """Create a new directory holding exactly the tree of ``commit`` and nothing else.

        Method: read the tree with ``ls-tree`` and the blobs with ``cat-file --batch``, then
        write files ourselves. Chosen over ``git archive | tar`` because archive honours
        ``export-ignore``/``export-subst`` attributes from the commit (content the candidate
        controls would change what is checked out), and over ``worktree add`` because that
        leaves a ``.git`` link and runs checkout machinery (filters, attributes). Reading raw
        blobs applies no conversion at all, and there is no repository in the result, so
        candidate-controlled git config or hooks cannot run there.
        """
        commit = self._require_descendant(commit)
        self._scrub_config()
        verify_root = self.root / "verify"
        verify_root.mkdir(exist_ok=True)
        dest = verify_root / secrets.token_hex(8)
        dest.mkdir()
        try:
            self._materialize(commit, dest)
            escaping = find_escaping_symlinks(dest)
            if escaping:
                raise ValueError(f"commit contains symlinks pointing outside the checkout: {escaping}")
        except BaseException:
            _rm_tree(dest)
            raise
        return dest

    def _materialize(self, commit: str, dest: Path) -> None:
        out = self._g(["ls-tree", "-r", "-z", "--full-tree", commit]).stdout
        entries: list[tuple[str, str, bytes]] = []
        for rec in out.split(b"\0"):
            if not rec:
                continue
            meta, _, rel = rec.partition(b"\t")
            mode, _typ, sha = meta.decode().split(" ")
            parts = os.fsdecode(rel).split("/")
            if any(p in ("", ".", "..") or p.casefold().rstrip(" .") == ".git" for p in parts) \
                    or "\0" in os.fsdecode(rel):
                raise ValueError("commit contains a path that is not allowed in a checkout")
            if mode not in ("100644", "100755", "120000", "160000"):
                raise ValueError(f"unsupported tree entry mode {mode}")
            entries.append((mode, "/".join(parts), sha.encode()))
        dest_real = os.path.realpath(dest)
        blobs = [e for e in entries if e[0] != "160000"]
        proc = subprocess.Popen([_git_exe(), *_HARDEN, "--git-dir", str(self.bare), "cat-file",
                                 "--batch"], cwd=str(self.root), env=_git_env(),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)

        def feed():
            try:
                proc.stdin.write(b"".join(sha + b"\n" for _m, _p, sha in blobs))
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        t = threading.Thread(target=feed, daemon=True)
        t.start()
        try:
            for mode, rel, _sha in entries:
                target = dest / rel
                parent = target.parent
                parent.mkdir(parents=True, exist_ok=True)
                pr = os.path.realpath(parent)
                if pr != dest_real and not pr.startswith(dest_real + os.sep):
                    raise ValueError("path escapes the checkout directory")
                if mode == "160000":
                    target.mkdir(exist_ok=True)      # submodules are never initialised
                    continue
                header = proc.stdout.readline().split()
                if len(header) != 3 or header[1] != b"blob":
                    raise WorkspaceError("unexpected output from git cat-file")
                size = int(header[2])
                data = proc.stdout.read(size)
                proc.stdout.read(1)
                if len(data) != size:
                    raise WorkspaceError("short read from git cat-file")
                if mode == "120000":
                    os.symlink(os.fsdecode(data), target)
                    continue
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.chmod(target, 0o755 if mode == "100755" else 0o644)
        except FileExistsError as e:
            raise ValueError(f"conflicting paths in commit: {e.filename}") from e
        finally:
            proc.kill()
            proc.wait()
            t.join(timeout=5)

    def cleanup_verification(self, path) -> None:
        """Remove a verification directory. Refuses anything not strictly under verify/."""
        p = Path(path)
        verify_root = (self.root / "verify").resolve()
        if p.is_symlink():
            raise ValueError("refusing to remove a symlink")
        real = p.resolve()
        if real == verify_root or verify_root not in real.parents:
            raise ValueError("path is not inside this task's verify directory")
        if real.exists():
            _rm_tree(real)

    # ---- reading a tree ------------------------------------------------------------
    def read_tree(self, commit: str, select=None, *, max_total_bytes: int = 64 * 1024 * 1024
                  ) -> dict[str, bytes]:
        """Map path -> raw blob bytes for the regular files of ``commit``'s tree.

        ``select`` is an optional predicate on the POSIX path; only selected files are read.
        Symlinks and submodules are skipped. Raw blobs are read with plumbing (``ls-tree``,
        ``cat-file --batch``): no attributes, filters or conversions apply.
        """
        commit = self._require_commit(commit)
        out = self._g(["ls-tree", "-r", "-z", "--full-tree", commit]).stdout
        wanted: list[tuple[str, bytes]] = []
        for rec in out.split(b"\0"):
            if not rec:
                continue
            meta, _, rel = rec.partition(b"\t")
            mode, _typ, sha = meta.decode().split(" ")
            path = os.fsdecode(rel)
            if mode not in ("100644", "100755") or (select is not None and not select(path)):
                continue
            wanted.append((path, sha.encode()))
        if not wanted:
            return {}
        r = self._g(["cat-file", "--batch"], input=b"".join(sha + b"\n" for _p, sha in wanted))
        data, pos, total = r.stdout, 0, 0
        result: dict[str, bytes] = {}
        for path, _sha in wanted:
            nl = data.index(b"\n", pos)
            header = data[pos:nl].split()
            if len(header) != 3 or header[1] != b"blob":
                raise WorkspaceError("unexpected output from git cat-file")
            size = int(header[2])
            total += size
            if total > max_total_bytes:
                raise WorkspaceError("tree is larger than the allowed size")
            result[path] = data[nl + 1: nl + 1 + size]
            pos = nl + 1 + size + 1
        return result

    # ---- diffs ---------------------------------------------------------------------
    def _diff_tree(self, base: str, commit: str, *flags: str) -> bytes:
        base, commit = self._require_commit(base), self._require_commit(commit)
        return self._g(["diff-tree", "-r", "-z", "--no-renames", "--no-ext-diff", "--no-textconv",
                        *flags, base, commit]).stdout

    def changed_paths(self, base: str, commit: str) -> list[str]:
        out = self._diff_tree(base, commit, "--name-only")
        return sorted(os.fsdecode(x) for x in out.split(b"\0") if x)

    def diff_stat(self, base: str, commit: str) -> dict:
        out = self._diff_tree(base, commit, "--numstat")
        files = ins = dels = 0
        for rec in out.split(b"\0"):
            if not rec:
                continue
            a, d, _path = rec.split(b"\t", 2)
            files += 1
            if a != b"-":                           # "-" marks a binary file
                ins += int(a)
                dels += int(d)
        return {"files": files, "insertions": ins, "deletions": dels}
