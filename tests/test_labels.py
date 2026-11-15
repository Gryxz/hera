import hashlib
from hera.privacy.labels import LabelStore, content_hash, derive, label_bytes
from hera.privacy.policy import Policy
from hera.types import Label, RepoClass


def policy(tmp_path):
    return Policy(repos={"pub": (tmp_path, RepoClass.PUBLIC), "prop": (tmp_path, RepoClass.PROPRIETARY),
                         "per": (tmp_path, RepoClass.PERSONAL)})


def test_content_hash_is_sha256():
    assert content_hash(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_put_get_roundtrip_and_missing(conn):
    s = LabelStore(conn)
    assert s.get("nope") is None
    lab = Label("a", RepoClass.PERSONAL, "h", ("secret",), ("p",))
    s.put(lab)
    assert s.get("a") == lab
    s.put(Label("u", None))
    assert s.get("u").repo_class is None


def test_label_bytes_uses_policy(conn, tmp_path):
    s = LabelStore(conn)
    lab = label_bytes(s, policy(tmp_path), artefact_id="a", repo_id="pub", data=b"x")
    assert lab.repo_class is RepoClass.PUBLIC and lab.content_hash == content_hash(b"x")
    assert s.get("a") == lab
    unk = label_bytes(s, policy(tmp_path), artefact_id="b", repo_id="unlisted", data=b"x",
                      findings=["f"])
    assert unk.repo_class is RepoClass.SENSITIVE and unk.restricted_findings == ("f",)


def test_derive_strictest_class_and_union(conn, tmp_path):
    s, pol = LabelStore(conn), policy(tmp_path)
    label_bytes(s, pol, artefact_id="a", repo_id="pub", data=b"1", findings=["f1"])
    label_bytes(s, pol, artefact_id="b", repo_id="prop", data=b"2", findings=["f2", "f1"])
    label_bytes(s, pol, artefact_id="c", repo_id="per", data=b"3")
    d = derive(s, artefact_id="d", inputs=["a", "b", "c"], data=b"new")
    assert d.repo_class is RepoClass.PROPRIETARY
    assert set(d.restricted_findings) == {"f1", "f2"} and len(d.restricted_findings) == 2
    assert d.derived_from == ("a", "b", "c") and d.content_hash == content_hash(b"new")
    assert s.get("d") == d
    assert derive(s, artefact_id="e", inputs=["a", "c"], data=b"z").repo_class is RepoClass.PERSONAL
    label_bytes(s, pol, artefact_id="s", repo_id="unlisted", data=b"4")
    assert derive(s, artefact_id="f", inputs=["a", "s"], data=b"z").repo_class is RepoClass.SENSITIVE


def test_derive_unknown_or_empty_inputs_unlabelled(conn, tmp_path):
    s = LabelStore(conn)
    label_bytes(s, policy(tmp_path), artefact_id="a", repo_id="pub", data=b"1")
    assert derive(s, artefact_id="x", inputs=["a", "ghost"], data=b"z").repo_class is None
    assert derive(s, artefact_id="y", inputs=[], data=b"z").repo_class is None
    assert s.get("y").repo_class is None
