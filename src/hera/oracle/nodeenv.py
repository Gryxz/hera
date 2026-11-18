"""Locating and probing Node.js, shared by the node adapter and by contract checks that run ``{node}``.

Nothing here touches candidate code: the probes run the oracle's own tiny files.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache

MIN_NODE = (22, 6, 0)
_STRIP_FLAG = "--experimental-strip-types"
_PROBE_TS = "const n: number = 1;\nprocess.stdout.write('hera-ts-ok' + String(n));\n"


class NodeUnavailable(RuntimeError):
    """No usable Node.js (missing, too old, or unable to run what the check set needs)."""


@dataclass(frozen=True)
class NodeInfo:
    path: str                              # resolved (real) path of the executable
    version: tuple[int, int, int] | None
    ts_flags: tuple[str, ...] | None       # flags that enable type stripping; None = unsupported

    @property
    def usable(self) -> bool:
        return self.version is not None and self.version >= MIN_NODE


def find_node(explicit: str | None = None) -> str | None:
    """Resolved path of the node executable: ``explicit``, else ``$HERA_NODE``, else the first
    ``node`` on PATH. None if there is none."""
    cand = explicit or os.environ.get("HERA_NODE") or "node"
    found = cand if os.sep in cand else shutil.which(cand)
    if not found or not os.path.isfile(found) or not os.access(found, os.X_OK):
        return None
    return os.path.realpath(found)


def install_dir(node_path: str) -> str:
    """The Node installation directory: two levels above the resolved executable
    (``<prefix>/bin/node`` -> ``<prefix>``)."""
    return os.path.dirname(os.path.dirname(os.path.realpath(node_path)))


def readable_paths(node_path: str) -> list[str]:
    """``read_only`` entries for ``sandbox.run`` that make Node runnable: its install directory.

    On macOS /Users and the home directory are unreadable inside the sandbox except for listed
    tool paths, and a CI runner keeps Node under /Users. Skipped where the directory is the
    file system root, or (Linux) is already part of the sandbox's system bind of /usr."""
    prefix = install_dir(node_path)
    if prefix in ("", "/"):
        return []
    if sys.platform.startswith("linux") and (prefix == "/usr" or prefix.startswith("/usr/")):
        return []
    return [prefix] if os.path.isdir(prefix) else []


def _probe_env() -> dict[str, str]:
    return {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NODE_OPTIONS": ""}


@lru_cache(maxsize=8)
def node_info(path: str) -> NodeInfo:
    """Version and type-stripping support of the node at ``path`` (cached per path)."""
    try:
        r = subprocess.run([path, "--version"], capture_output=True, timeout=30, env=_probe_env())
    except (OSError, subprocess.SubprocessError):
        return NodeInfo(path, None, None)
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\s*", r.stdout.decode("ascii", "replace")) if r.returncode == 0 else None
    if not m:
        return NodeInfo(path, None, None)
    version = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if version < MIN_NODE:
        return NodeInfo(path, version, None)
    flags: tuple[str, ...] | None = None
    with tempfile.TemporaryDirectory(prefix="hera-nodeprobe-") as d:
        with open(os.path.join(d, "probe.mts"), "w", encoding="utf-8") as f:
            f.write(_PROBE_TS)
        for cand in ((_STRIP_FLAG,), ()):          # older 22.x needs the flag, newer ones do not
            try:
                p = subprocess.run([path, "--no-warnings", *cand, "probe.mts"], cwd=d, capture_output=True,
                                   timeout=60, env=_probe_env())
            except (OSError, subprocess.SubprocessError):
                continue
            if p.returncode == 0 and p.stdout == b"hera-ts-ok1":
                flags = cand
                break
    return NodeInfo(path, version, flags)


def require_node(explicit: str | None = None, *, need_typescript: bool = False) -> NodeInfo:
    """The usable node, or NodeUnavailable with a message that says what is missing."""
    path = find_node(explicit)
    if path is None:
        raise NodeUnavailable("node executable not found (looked at $HERA_NODE, then PATH); the node adapter "
                              "needs Node.js 22.6 or newer")
    info = node_info(path)
    if info.version is None:
        raise NodeUnavailable("the node executable could not be run to read its version")
    if not info.usable:
        raise NodeUnavailable("Node.js %d.%d.%d is too old: the node adapter needs 22.6 or newer" % info.version)
    if need_typescript and info.ts_flags is None:
        raise NodeUnavailable("this Node.js cannot strip TypeScript types (needs 22.6+ with "
                              "--experimental-strip-types), but .ts or .mts check files are in use")
    return info
