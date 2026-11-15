"""Frozen check store: checks are hashed, written read-only outside every worktree, and
recorded in the state store.

Layout: ``<checks_dir>/<task_id>/<hash>/visible/<relpath>`` for files with visible checks,
``.../hidden/<relpath>`` for files with only hidden checks, and ``.../manifest.json``. The
directory name is the check-set hash, so the store is content-addressed: ``verify_integrity``
re-derives the hash from the manifest and re-hashes every file.

Requirement ids are only unique within a task, but ``requirements.id`` is the table's primary
key, so rows there are keyed ``<task_id>:<requirement id>``. ``checks.requirement_id`` holds the
check's requirement ids joined with commas (one row per check; the table's key is the check id).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import stat
import time
from pathlib import Path

from .. import paths
from ..workspace import _check_id
from .model import CheckSet, DraftCheck, Requirement, validate_draft, validate_relpath


class CheckStoreTampered(RuntimeError):
    """The frozen check set on disk (or its database record) does not match its hash."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _check_dict(c: DraftCheck) -> dict:
    return {"check_id": c.check_id, "requirement_ids": sorted(c.requirement_ids),
            "visibility": c.visibility, "relpath": c.relpath, "node": c.node,
            "author_role": c.author_role, "author_model": c.author_model, "kind": c.kind}


def _manifest(task_id, adapter, requirements, checks, files_sha, summary_text) -> dict:
    return {
        "task_id": task_id,
        "adapter": adapter,
        "requirements": [[r.id, _sha(r.text.encode("utf-8"))]
                         for r in sorted(requirements, key=lambda r: r.id)],
        "checks": [_check_dict(c) for c in sorted(checks, key=lambda c: c.check_id)],
        "files": dict(sorted(files_sha.items())),
        "summary_sha256": _sha(summary_text.encode("utf-8")),
    }


def _stored_doc(manifest: dict, requirements, summary_text: str) -> bytes:
    return _canon({"manifest": manifest,
                   "texts": {"requirements": {r.id: r.text for r in requirements},
                             "summary": summary_text}})


def _file_visibility(checks) -> dict[str, str]:
    return {c.relpath: c.visibility for c in checks}


def _expected_files(manifest: dict) -> dict[str, str]:
    """Store-relative path -> sha256 for every payload file."""
    vis = {c["relpath"]: c["visibility"] for c in manifest["checks"]}
    return {f"{vis[rel]}/{rel}": sha for rel, sha in manifest["files"].items()}


def _uncovered(requirements, checks) -> tuple[str, ...]:
    covered = {r for c in checks for r in c.requirement_ids}
    return tuple(sorted(r.id for r in requirements if r.id not in covered))


def _root(task_id: str, checkset_hash: str) -> Path:
    return paths.checks_dir() / task_id / checkset_hash


def _build(manifest: dict, texts: dict, root: Path) -> CheckSet:
    reqs = tuple(Requirement(i, texts["requirements"][i]) for i, _h in manifest["requirements"])
    checks = tuple(DraftCheck(c["check_id"], tuple(c["requirement_ids"]), c["visibility"],
                              c["relpath"], c["node"], c["author_role"], c["author_model"],
                              c["kind"]) for c in manifest["checks"])
    return CheckSet(hash=_sha(_canon(manifest)), task_id=manifest["task_id"], requirements=reqs,
                    checks=checks, files=dict(manifest["files"]), adapter=manifest["adapter"],
                    root=root, uncovered=_uncovered(reqs, checks))


def _chmod_readonly(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for f in filenames:
            os.chmod(os.path.join(dirpath, f), 0o444)
    for dirpath, dirnames, _f in os.walk(root, topdown=False):
        os.chmod(dirpath, 0o555)


def freeze(conn: sqlite3.Connection, *, task_id: str, requirements, checks,
           files: dict[str, bytes], adapter: str = "pytest", summary_text: str = "") -> CheckSet:
    """Hash, write read-only and record a check set. Idempotent for identical content."""
    _check_id(task_id, "task id")
    requirements, checks = tuple(requirements), tuple(checks)
    if not isinstance(adapter, str) or not adapter or not all(ch.isalnum() or ch in "_-" for ch in adapter):
        raise ValueError("adapter: must be a short identifier")
    if not isinstance(files, dict) or not all(isinstance(v, (bytes, bytearray)) for v in files.values()):
        raise ValueError("files: must map relpath to bytes")
    for rel in files:
        validate_relpath(rel)
    validate_draft(requirements, checks)
    used = {c.relpath for c in checks}
    if used - set(files):
        raise ValueError("files: a check refers to a file that was not supplied")
    if set(files) - used:
        raise ValueError("files: a supplied file is not referenced by any check")
    files = {k: bytes(v) for k, v in files.items()}
    files_sha = {rel: _sha(data) for rel, data in files.items()}
    manifest = _manifest(task_id, adapter, requirements, checks, files_sha, summary_text)
    h = _sha(_canon(manifest))
    root = _root(task_id, h)
    cs = CheckSet(hash=h, task_id=task_id, requirements=tuple(sorted(requirements, key=lambda r: r.id)),
                  checks=tuple(sorted(checks, key=lambda c: c.check_id)), files=dict(sorted(files_sha.items())),
                  adapter=adapter, root=root, uncovered=_uncovered(requirements, checks))
    doc = _stored_doc(manifest, requirements, summary_text)

    if root.exists() or root.is_symlink():
        verify_integrity(cs)             # same hash, different bytes on disk -> raises
        if (root / "manifest.json").read_bytes() != doc:
            raise CheckStoreTampered("stored manifest differs from the one being frozen")
    else:
        parent = root.parent
        parent.mkdir(parents=True, exist_ok=True)
        tmp = parent / f".tmp-{secrets.token_hex(6)}"
        tmp.mkdir()
        try:
            vis = _file_visibility(checks)
            for rel, data in files.items():
                dest = tmp / vis[rel] / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            (tmp / "manifest.json").write_bytes(doc)
            os.rename(tmp, root)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        _chmod_readonly(root)
        verify_integrity(cs)

    _record(conn, cs, manifest, doc)
    return cs


def _record(conn, cs: CheckSet, manifest: dict, doc: bytes) -> None:
    now = time.time()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in cs.requirements:
            conn.execute("INSERT OR IGNORE INTO requirements(id, task_id, text, created_at) VALUES (?,?,?,?)",
                         (f"{cs.task_id}:{r.id}", cs.task_id, r.text, now))
        conn.execute("INSERT OR IGNORE INTO checksets(hash, task_id, runner_cmd, manifest_json, frozen_at, supersedes)"
                     " VALUES (?,?,?,?,?,NULL)", (cs.hash, cs.task_id, cs.adapter, doc.decode("ascii"), now))
        for c in cs.checks:
            conn.execute("INSERT OR IGNORE INTO checks(id, checkset_hash, requirement_id, kind, visibility, sha256,"
                         " author_model, author_role, health) VALUES (?,?,?,?,?,?,?,?,'valid')",
                         (c.check_id, cs.hash, ",".join(sorted(c.requirement_ids)), c.kind, c.visibility,
                          cs.files[c.relpath], c.author_model, c.author_role))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def load(conn: sqlite3.Connection, checkset_hash: str) -> CheckSet:
    """Rebuild a CheckSet from its database record. Does not read the files: call
    ``verify_integrity`` before using them."""
    row = conn.execute("SELECT task_id, manifest_json FROM checksets WHERE hash=?", (checkset_hash,)).fetchone()
    if row is None:
        raise KeyError("unknown check set")
    try:
        doc = json.loads(row["manifest_json"])
        cs = _build(doc["manifest"], doc["texts"], _root(row["task_id"], checkset_hash))
        recomputed = _sha(_canon(doc["manifest"]))
    except (KeyError, TypeError, ValueError) as e:
        raise CheckStoreTampered("stored manifest is malformed") from e
    if recomputed != checkset_hash or cs.task_id != row["task_id"]:
        raise CheckStoreTampered("stored manifest does not match its hash")
    for (rid, rsha) in doc["manifest"]["requirements"]:
        if _sha(doc["texts"]["requirements"][rid].encode("utf-8")) != rsha:
            raise CheckStoreTampered("stored requirement text does not match its hash")
    return cs


def verify_integrity(checkset: CheckSet) -> None:
    """Re-hash the manifest and every stored file. Raises CheckStoreTampered on any mismatch,
    missing file, extra file or directory, or symlink."""
    root = checkset.root
    try:
        if root.is_symlink() or not root.is_dir():
            raise CheckStoreTampered("check set directory is missing")
        raw = (root / "manifest.json").read_bytes()
        doc = json.loads(raw)
        manifest, texts = doc["manifest"], doc["texts"]
    except CheckStoreTampered:
        raise
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise CheckStoreTampered("manifest is missing or unreadable") from e
    if _sha(_canon(manifest)) != checkset.hash or root.name != checkset.hash:
        raise CheckStoreTampered("manifest does not hash to the check set hash")
    if raw != _canon(doc):
        raise CheckStoreTampered("manifest is not in canonical form")
    try:
        rebuilt = _build(manifest, texts, root)
        for rid, rsha in manifest["requirements"]:
            if _sha(texts["requirements"][rid].encode("utf-8")) != rsha:
                raise CheckStoreTampered("requirement text does not match its hash")
        if _sha(texts["summary"].encode("utf-8")) != manifest["summary_sha256"]:
            raise CheckStoreTampered("summary text does not match its hash")
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise CheckStoreTampered("manifest is malformed") from e
    if (rebuilt.checks, rebuilt.requirements, rebuilt.files, rebuilt.task_id, rebuilt.adapter) != \
            (checkset.checks, checkset.requirements, checkset.files, checkset.task_id, checkset.adapter):
        raise CheckStoreTampered("manifest differs from the check set in memory")

    expected = _expected_files(manifest)
    expected["manifest.json"] = ""
    expected_dirs = {""}
    for rel in expected:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            expected_dirs.add("/".join(parts[:i]))
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        for d in dirnames:
            rd = f"{rel_dir}/{d}" if rel_dir else d
            if os.path.islink(os.path.join(dirpath, d)) or rd not in expected_dirs:
                raise CheckStoreTampered("unexpected directory in the check set")
        for f in filenames:
            rf = f"{rel_dir}/{f}" if rel_dir else f
            p = os.path.join(dirpath, f)
            if rf not in expected or os.path.islink(p) or not stat.S_ISREG(os.lstat(p).st_mode):
                raise CheckStoreTampered("unexpected file in the check set")
    for rel, sha in expected.items():
        if rel == "manifest.json":
            continue
        p = root / rel
        try:
            data = p.read_bytes()
        except OSError as e:
            raise CheckStoreTampered("a check file is missing") from e
        if _sha(data) != sha:
            raise CheckStoreTampered("a check file does not match its hash")


def visible_dir(checkset: CheckSet) -> Path:
    """Directory of visible check files. This is the ONLY check-store directory that may ever
    be handed (mounted, copied, shown) to an implementer workspace."""
    return checkset.root / "visible"


def hidden_dir(checkset: CheckSet) -> Path:
    """Directory of hidden check files. NEVER give this path, its contents, file names or any
    failure text from it to an implementer: it is for the oracle's verification runs only."""
    return checkset.root / "hidden"


def supersede(conn: sqlite3.Connection, old_hash: str, new_checkset_hash: str) -> None:
    """Record that ``new_checkset_hash`` replaces ``old_hash`` (used later by disputes)."""
    if old_hash == new_checkset_hash:
        raise ValueError("a check set cannot supersede itself")
    rows = {r["hash"]: r["task_id"] for r in conn.execute(
        "SELECT hash, task_id FROM checksets WHERE hash IN (?,?)", (old_hash, new_checkset_hash))}
    if old_hash not in rows or new_checkset_hash not in rows:
        raise KeyError("unknown check set")
    if rows[old_hash] != rows[new_checkset_hash]:
        raise ValueError("check sets belong to different tasks")
    conn.execute("UPDATE checksets SET supersedes=? WHERE hash=?", (old_hash, new_checkset_hash))
