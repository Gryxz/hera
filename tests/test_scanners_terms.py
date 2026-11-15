import pytest
from hera.privacy.policy import load_policy
from hera.privacy.scanners.terms import make_term_scanner, normalise


def scan(terms, text):
    return make_term_scanner(terms)(text.encode())


def T(term, match, action="block"):
    return {"term": term, "kind": "project", "match": match, "action": action}


def test_exact_is_case_sensitive_substring():
    t = [T("Zorbulon", "exact")]
    assert scan(t, "the Zorbulon file") == ["term:block#0"]
    assert scan(t, "the zorbulon file") == []
    assert scan(t, "preZorbulonpost") == ["term:block#0"]


def test_case_insensitive_substring():
    t = [T("Zorbulon", "case_insensitive")]
    assert scan(t, "ZORBULONS") == ["term:block#0"]
    assert scan(t, "zorbulon") == ["term:block#0"]
    assert scan(t, "zorb ulon") == []


def test_word_boundaries():
    t = [T("zorbulon", "word")]
    assert scan(t, "see Zorbulon, now") == ["term:block#0"]
    assert scan(t, "zorbulonic") == []
    assert scan(t, "xzorbulon") == []
    assert scan(t, "a_zorbulon") == []
    assert scan([T("two words", "word")], "a Two Words here") == ["term:block#0"]


@pytest.mark.parametrize("text", [
    "z o r b u l o n", "z.o.r.b.u.l.o.n", "z-o_r-b u.l-o_n", "z0rbul0n", "Z0RBUL0N", "zorbu1on", "zorbuion",
    "z\u200borb\u200dulon", "z\u043erbul\u043en", "z\u03bfrbul\u03bfn",
    "a zorbulon b",
])
def test_fuzzy(text):
    assert scan([T("zorbulon", "fuzzy")], text) == ["term:block#0"]


def test_fuzzy_negatives_and_term_normalisation():
    assert scan([T("zorbulon", "fuzzy")], "zorbulo n x") == ["term:block#0"]     # joins letters
    assert scan([T("zorbulon", "fuzzy")], "zorbul") == []
    assert scan([T("Z0rbu-lon", "fuzzy")], "ZORBULON") == ["term:block#0"]
    assert scan([T("acme", "fuzzy")], "@cme and $ome") == ["term:block#0"]
    assert normalise("A.B-C d​") == "abcd"


def test_index_and_action_naming():
    terms = [T("alpha1", "word", "signal"), T("bravo", "exact", "review"), T("charlie", "exact"),
             {"term": "delta"}]
    out = scan(terms, "alpha1 bravo charlie DELTA")
    assert out == ["term:signal#0", "term:review#1", "term:block#2", "term:block#3"]
    assert scan(terms, "only charlie") == ["term:block#2"]


def test_findings_never_contain_term_text():
    for f in scan([T("secretproject", "fuzzy")], "the S3CRET project"):
        assert "secret" not in f.lower() and f == "term:block#0"


def test_short_terms_are_skipped_and_reported():
    scanner = make_term_scanner([T("ab", "exact"), T("good", "exact"), {"term": " x "}, T("a-b", "fuzzy")])
    assert scanner.skipped == [0, 2, 3]
    assert scanner(b"ab good x") == ["term:block#1"]
    assert not make_term_scanner([T("fine", "exact")]).skipped


def test_unknown_action_blocks_and_unknown_match_is_broad():
    out = scan([{"term": "bravo", "action": "bogus", "match": "bogus"}], "b r a v o")
    assert out == ["term:block#0"]


def test_empty_terms_and_empty_input():
    assert scan([], "anything") == []
    assert make_term_scanner([]).skipped == []
    assert scan([T("bravo", "exact")], "") == []


def test_loaded_from_policy_file(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text('[[term]]\nterm = "zorbulon"\nkind = "project"\nmatch = "word"\naction = "review"\n'
                 'scope = "any"\n', encoding="utf-8")
    pol = load_policy(p)
    assert make_term_scanner(pol.terms)(b"x Zorbulon y") == ["term:review#0"]
