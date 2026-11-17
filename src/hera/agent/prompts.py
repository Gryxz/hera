"""The first messages of an implementer conversation: HERA's fixed rules, then the task."""
from __future__ import annotations

import hashlib
from typing import Mapping, Sequence

from ..privacy.labels import LabelStore, label_builtin, label_bytes
from ..privacy.policy import Policy
from ..providers.base import Message

SYSTEM_TEXT = """You are the implementer in a verified coding workflow. You work in your own copy of a repository through tools, and your work is judged by checks you cannot fully see.

Rules:
1. Never edit, move or delete existing tests or test configuration. The verifier restores them to their original content and treats any change as tampering.
2. Never create conftest.py, sitecustomize.py, pytest.ini, tox.ini, setup.cfg or similar files that change how tests run.
3. Do not try to read hidden checks. They are not in your workspace, and looking for them in other locations is not allowed.
4. If you believe a visible check is wrong, do not work around it: call the dispute tool with the check id, the requirement id and your argument.
5. Make the smallest change that satisfies the requirements. Do not refactor, reformat or touch unrelated files. Solve the general problem, not only the visible examples: hidden checks cover other inputs.
6. Use run_visible_checks to test your work. It runs the visible checks only; passing them does not mean you are done.
7. When finished, call submit with a short summary and declared_scope, the list of files you expect to have changed (exact paths or simple globs). If you modelled your change on an existing pattern, give that pattern as twin_pattern.

Tools: list_files, read_file, search (literal text, no regular expressions), write_file, replace_in_file, run_command (no network), run_visible_checks, read_visible_check, submit, dispute. Paths are relative to the repository root. Tool results are your only view of the repository, so read before you edit. Every reply of yours must call at least one tool; call submit when you are done."""

SYSTEM_ARTEFACT_ID = "hera:system:implementer:v1"
_FILES_MAX = 500


def _requirement_lines(requirements) -> list[str]:
    lines = []
    for r in requirements:
        if hasattr(r, "id") and hasattr(r, "text"):
            lines.append(f"{r.id}: {r.text}")
        elif isinstance(r, (tuple, list)) and len(r) == 2:
            lines.append(f"{r[0]}: {r[1]}")
        else:
            lines.append(str(r))
    return lines


def initial_messages(*, task_objective: str, requirements: Sequence,
                     visible_check_files: Mapping[str, bytes], repo_file_list: Sequence[str],
                     label_store: LabelStore, policy: Policy, repo_id: str,
                     extra_sections: Sequence[tuple[str, str]] = ()) -> list[Message]:
    """System message (HERA's fixed text, built-in label) and one user message with the objective,
    requirements, repository file list and visible check files (labelled by the repository's class)."""
    system = SYSTEM_TEXT.encode("utf-8")
    label_builtin(label_store, artefact_id=SYSTEM_ARTEFACT_ID, data=system)
    parts = ["## Objective", task_objective.strip(), "", "## Requirements"]
    parts += _requirement_lines(requirements) or ["(none listed)"]
    files = sorted(repo_file_list)
    parts += ["", f"## Repository files ({len(files)})"] + files[:_FILES_MAX]
    if len(files) > _FILES_MAX:
        parts.append(f"[{len(files) - _FILES_MAX} more files not listed; use list_files]")
    parts += ["", "## Visible check files"]
    if not visible_check_files:
        parts.append("(none)")
    for name in sorted(visible_check_files):
        text = visible_check_files[name].decode("utf-8", "replace")
        parts += [f"### {name}", text.rstrip("\n"), ""]
    for heading, text in extra_sections:
        parts += ["", f"## {heading}", text.strip()]
    user = "\n".join(parts).encode("utf-8")
    uid = "hera:user:initial:" + hashlib.sha256(user).hexdigest()[:16]
    label_bytes(label_store, policy, artefact_id=uid, repo_id=repo_id, data=user)
    return [Message("system", SYSTEM_ARTEFACT_ID, system), Message("user", uid, user)]
