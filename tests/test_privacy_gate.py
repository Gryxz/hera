from hera.privacy.gate import LOCAL, permitted_destinations as pd
from hera.types import Grant, Label, RepoClass

DESTS = {"local", "provider-a", "provider-b", "worker-pc"}
NOW = 1000.0


def lab(cls, h="h1", findings=()):
    return Label("a", cls, content_hash=h, restricted_findings=tuple(findings))


def test_public_goes_anywhere():
    assert pd([lab(RepoClass.PUBLIC)], [], DESTS, NOW) == DESTS


def test_unlabelled_and_empty_block_everything_remote():
    assert pd([lab(None)], [], DESTS, NOW) == {LOCAL}
    assert pd([], [], DESTS, NOW) == {LOCAL}


def test_restricted_finding_blocks_even_public():
    assert pd([lab(RepoClass.PUBLIC, findings=["secret"])], [], DESTS, NOW) == {LOCAL}


def test_proprietary_needs_matching_unexpired_grant():
    g = Grant("g1", frozenset({"h1"}), frozenset({"provider-a"}), "implement", expires_at=2000)
    assert pd([lab(RepoClass.PROPRIETARY)], [], DESTS, NOW) == {LOCAL}
    assert pd([lab(RepoClass.PROPRIETARY)], [g], DESTS, NOW) == {LOCAL, "provider-a"}
    assert pd([lab(RepoClass.PROPRIETARY, h="changed")], [g], DESTS, NOW) == {LOCAL}   # content changed
    assert pd([lab(RepoClass.PROPRIETARY)], [g], DESTS, 3000.0) == {LOCAL}             # expired


def test_sensitive_raw_never_leaves_but_declassified_text_can():
    ordinary = Grant("g0", frozenset({"raw", "clean"}), frozenset({"provider-a"}), "x", expires_at=2000)
    g = Grant("g2", frozenset({"clean"}), frozenset({"provider-a"}), "diagnose", expires_at=2000,
              declassified=True)
    # an ordinary grant, even one naming the exact hash, never releases sensitive-origin content
    assert pd([lab(RepoClass.SENSITIVE, h="raw")], [ordinary], DESTS, NOW) == {LOCAL}
    assert pd([lab(RepoClass.SENSITIVE, h="clean")], [ordinary], DESTS, NOW) == {LOCAL}
    assert pd([lab(RepoClass.SENSITIVE, h="raw")], [g], DESTS, NOW) == {LOCAL}
    assert pd([lab(RepoClass.SENSITIVE, h="clean")], [g], DESTS, NOW) == {LOCAL, "provider-a"}
    # a declassified grant still works as a normal grant for proprietary content
    assert pd([lab(RepoClass.PROPRIETARY, h="clean")], [g], DESTS, NOW) == {LOCAL, "provider-a"}


def test_payload_is_intersection_of_its_artefacts():
    g = Grant("g1", frozenset({"h1"}), frozenset({"provider-a"}), "implement", expires_at=2000)
    mixed = [lab(RepoClass.PUBLIC), lab(RepoClass.PROPRIETARY)]
    assert pd(mixed, [g], DESTS, NOW) == {LOCAL, "provider-a"}
    assert pd(mixed + [lab(RepoClass.SENSITIVE, h="raw")], [g], DESTS, NOW) == {LOCAL}


def test_grant_cannot_add_unknown_destination():
    g = Grant("g1", frozenset({"h1"}), frozenset({"somewhere-else"}), "x", expires_at=2000)
    assert pd([lab(RepoClass.PROPRIETARY)], [g], DESTS, NOW) == {LOCAL}
