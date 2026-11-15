from hera.privacy.policy import Policy
from hera.privacy.scanners import BLOCKING, default_scanners
from hera.privacy.scanners.terms import make_term_scanner


def test_blocking_predicate():
    assert BLOCKING([]) == []
    assert BLOCKING(["term:signal#0"]) == []
    assert BLOCKING(["term:signal#0", "term:signal#1"]) == []
    assert BLOCKING(["term:review#0"]) == ["term:review#0"]
    assert BLOCKING(["term:block#2", "term:signal#0"]) == ["term:block#2"]
    assert BLOCKING(["secret:jwt"]) == ["secret:jwt"]
    assert BLOCKING(["data:sqlite", "term:signal#0"]) == ["data:sqlite"]
    assert BLOCKING(["term:signal#0", "pii:email"]) == ["term:signal#0", "pii:email"]
    assert BLOCKING(["pii:phone"]) == ["pii:phone"]


def test_default_scanners_composition():
    assert len(default_scanners()) == 3
    assert len(default_scanners(Policy())) == 3
    four = default_scanners(Policy(terms=[{"term": "zorbulon", "action": "block"}]))
    assert len(four) == 4 and four[3](b"zorbulon") == ["term:block#0"]
    assert all(callable(s) for s in four)
    assert hasattr(make_term_scanner([]), "skipped")


def test_combination_comes_from_default_scanners():
    out = [f for s in default_scanners() for f in s(b"ines@quillmere.io (212) 555-0275")]
    assert "pii:combination" in out
