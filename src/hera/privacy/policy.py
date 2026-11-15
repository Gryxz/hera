"""Local privacy policy: which repository has which class. Missing entry means sensitive."""
from __future__ import annotations
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from ..types import RepoClass


@dataclass
class Policy:
    repos: dict[str, tuple[Path, RepoClass]] = field(default_factory=dict)
    terms: list[dict] = field(default_factory=list)     # deny-list; used by privacy/scanners/terms.py

    def repo_class(self, repo_id: str) -> RepoClass:
        entry = self.repos.get(repo_id)
        return entry[1] if entry else RepoClass.SENSITIVE

    def repo_for_path(self, path: str | Path) -> str | None:
        """Id of the repository whose directory contains `path` (most specific wins)."""
        target = Path(path).expanduser().resolve()
        best: tuple[int, str] | None = None
        for repo_id, (root, _cls) in self.repos.items():
            if target == root or root in target.parents:
                depth = len(root.parts)
                if best is None or depth > best[0]:
                    best = (depth, repo_id)
        return best[1] if best else None


def load_policy(path: str | Path) -> Policy:
    p = Path(path)
    if not p.exists():
        return Policy()
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    repos: dict[str, tuple[Path, RepoClass]] = {}
    for entry in data.get("repo", []):
        repo_id = str(entry["id"])
        try:
            cls = RepoClass(entry["class"])
        except ValueError:
            raise ValueError(f"unknown repository class for {repo_id!r}") from None
        repos[repo_id] = (Path(str(entry["path"])).expanduser().resolve(), cls)
    return Policy(repos=repos, terms=[dict(t) for t in data.get("term", [])])
