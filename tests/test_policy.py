import pytest
from hera.privacy.policy import Policy, load_policy
from hera.types import RepoClass


def write(tmp_path, text):
    p = tmp_path / "policy.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_repos_and_terms(tmp_path):
    repo = tmp_path / "pub"
    repo.mkdir()
    pol = load_policy(write(tmp_path, f'''
[[repo]]
id = "pub"
path = "{repo.as_posix()}"
class = "public"
[[term]]
term = "invented-term"
action = "block"
'''))
    assert pol.repo_class("pub") is RepoClass.PUBLIC
    assert pol.terms == [{"term": "invented-term", "action": "block"}]


def test_unknown_repo_is_sensitive(tmp_path):
    assert load_policy(write(tmp_path, "")).repo_class("nope") is RepoClass.SENSITIVE


def test_unknown_class_raises(tmp_path):
    with pytest.raises(ValueError):
        load_policy(write(tmp_path, '[[repo]]\nid = "x"\npath = "/tmp/x"\nclass = "secret-ish"\n'))


def test_missing_file_gives_empty_policy(tmp_path):
    pol = load_policy(tmp_path / "absent.toml")
    assert pol == Policy() and pol.repo_class("anything") is RepoClass.SENSITIVE


def test_repo_for_path(tmp_path):
    outer, inner = tmp_path / "outer", tmp_path / "outer" / "inner"
    inner.mkdir(parents=True)
    (tmp_path / "other").mkdir()
    pol = load_policy(write(tmp_path, f'''
[[repo]]
id = "outer"
path = "{outer.as_posix()}"
class = "public"
[[repo]]
id = "inner"
path = "{inner.as_posix()}"
class = "proprietary"
'''))
    assert pol.repo_for_path(outer / "a.py") == "outer"
    assert pol.repo_for_path(inner / "deep" / "b.py") == "inner"
    assert pol.repo_for_path(outer / "x" / ".." / "a.py") == "outer"
    assert pol.repo_for_path(tmp_path / "other") is None
    assert pol.repo_for_path(tmp_path / "outer-sibling") is None


def test_tilde_path_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "code").mkdir()
    pol = load_policy(write(tmp_path, '[[repo]]\nid = "r"\npath = "~/code"\nclass = "personal"\n'))
    assert pol.repo_for_path(tmp_path / "code" / "f.py") == "r"
