"""All locations derive from HERA_HOME; no absolute paths anywhere else (DA-4)."""
from __future__ import annotations
import os
from pathlib import Path


def hera_home() -> Path:
    env = os.environ.get("HERA_HOME")
    return Path(env).expanduser() if env else Path.home() / ".hera"


def db_path() -> Path:
    return hera_home() / "state.sqlite"


def policy_dir() -> Path:
    return hera_home() / "policy"


def checks_dir() -> Path:
    return hera_home() / "checks"


def work_dir() -> Path:
    return hera_home() / "work"


def ensure_layout() -> Path:
    home = hera_home()
    for p in (home, policy_dir(), checks_dir(), home / "artifacts", home / "logs", work_dir()):
        p.mkdir(parents=True, exist_ok=True)
    return home
