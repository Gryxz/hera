import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest
from hera.privacy import translog
from hera.privacy.gateway import Destination, Gateway, Segment
from hera.privacy.grants import GrantStore
from hera.privacy.labels import LabelStore, content_hash, label_bytes
from hera.privacy.policy import Policy
from hera.types import RepoClass

KEY = "Bearer invented-key-0000"


@pytest.fixture
def sink():
    received: list[bytes] = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, received
    srv.shutdown()
    srv.server_close()


class Env:
    def __init__(self, conn, sink_srv, **kw):
        self.conn = conn
        self.now = 1000.0
        self.labels, self.grants = LabelStore(conn), GrantStore(conn)
        self.policy = Policy(repos={"pub": (None, RepoClass.PUBLIC), "prop": (None, RepoClass.PROPRIETARY),
                                    "sens": (None, RepoClass.SENSITIVE)})
        url = f"http://127.0.0.1:{sink_srv.server_address[1]}/v1"
        self.gw = Gateway(conn, self.labels, self.grants,
                          {"prov": Destination("prov", url, {"Authorization": KEY})},
                          clock=lambda: self.now, timeout=5, **kw)

    def label(self, aid, repo, data):
        label_bytes(self.labels, self.policy, artefact_id=aid, repo_id=repo, data=data)
        return Segment(aid, data)

    def rows(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM transmissions ORDER BY seq")]


@pytest.fixture
def env(conn, sink):
    return Env(conn, sink[0])


def test_public_delivered_and_logged(env, sink):
    seg = env.label("a", "pub", b"hello world")
    r = env.gw.send("prov", [seg], purpose="implement", task_id="t1")
    assert r.sent and r.status == 200 and r.body == b"ok"
    assert sink[1] == [b"hello world"]
    row = env.rows()[-1]
    assert row["decision"] == "sent" and row["task_id"] == "t1"
    assert json.loads(row["artefacts_json"]) == [{"id": "a", "repo_class": "public"}]
    assert row["payload_hash"] == content_hash(b"hello world")


def test_envelope_and_join(env, sink):
    s1, s2 = env.label("a", "pub", b"one"), env.label("b", "pub", b"two")
    env.gw.send("prov", [s1, s2], purpose="p")
    env.gw.send("prov", [s1], purpose="p", envelope=lambda segs: b"<" + segs[0].data + b">")
    assert sink[1] == [b"one\ntwo", b"<one>"]


def blocked(env, sink, reason, *a, **kw):
    n = len(sink[1])
    r = env.gw.send(*a, **kw)
    assert not r.sent and r.reason == reason and len(sink[1]) == n
    row = env.rows()[-1]
    assert row["decision"] == "blocked" and row["reason"] == reason
    return row


def test_unlabelled_blocked(env, sink):
    row = blocked(env, sink, "unlabelled segment", "prov", [Segment("ghost", b"data")], purpose="p")
    assert sink[1] == [] and json.loads(row["artefacts_json"])[0]["repo_class"] is None


def test_sensitive_raw_blocked(env, sink):
    blocked(env, sink, "destination not permitted for these labels", "prov",
            [env.label("s", "sens", b"raw")], purpose="p")
    assert sink[1] == []


def test_proprietary_grant_lifecycle(env, sink):
    seg = env.label("p", "prop", b"v1 code")
    blocked(env, sink, "destination not permitted for these labels", "prov", [seg], purpose="p")
    env.grants.create(content_hashes=[content_hash(b"v1 code")], destinations=["prov"],
                      purpose="p", expires_at=2000, grant_id="g1")
    assert env.gw.send("prov", [seg], purpose="p").sent and sink[1] == [b"v1 code"]
    assert json.loads(env.rows()[-1]["grant_ids_json"]) == ["g1"]
    blocked(env, sink, "content changed since labelling", "prov", [Segment("p", b"v2 code")], purpose="p")
    env.label("p", "prop", b"v2 code")           # relabelled, but the grant is bound to v1's hash
    blocked(env, sink, "destination not permitted for these labels", "prov",
            [Segment("p", b"v2 code")], purpose="p")
    env.label("p", "prop", b"v1 code")
    env.now = 2500.0                              # grant expired
    blocked(env, sink, "destination not permitted for these labels", "prov", [seg], purpose="p")


def test_scanner_finding_blocks_without_logging_text(conn, sink):
    secret = b"hunter2-invented-secret"
    e = Env(conn, sink[0], scanners=[lambda d: ["password-like"] if b"hunter2" in d else []])
    seg = e.label("a", "pub", b"x " + secret)
    r = e.gw.send("prov", [seg], purpose="p")
    assert not r.sent and r.reason == "restricted data finding" and sink[1] == []
    row = e.rows()[-1]
    assert "password-like" in row["scanner_json"] and secret.decode() not in str(row)
    assert secret.decode() not in r.reason


def test_scanner_exception_blocks(conn, sink):
    def boom(data):
        raise RuntimeError("details")
    e = Env(conn, sink[0], scanners=[boom])
    row = blocked(e, sink, "scanner error", "prov", [e.label("a", "pub", b"x")], purpose="p")
    assert "details" not in str(row)


def test_oversize_blocks(conn, sink):
    e = Env(conn, sink[0], max_payload_bytes=10)
    blocked(e, sink, "oversize payload", "prov", [e.label("a", "pub", b"x" * 11)], purpose="p")
    assert e.gw.send("prov", [e.label("b", "pub", b"x" * 10)], purpose="p").sent


def test_unknown_destination_and_local(env, sink):
    seg = env.label("a", "pub", b"x")
    blocked(env, sink, "unknown destination", "nowhere", [seg], purpose="p")
    blocked(env, sink, "local is not a network destination", "local", [seg], purpose="p")
    blocked(env, sink, "no labelled segments", "prov", [], purpose="p")


def test_chain_intact_and_key_never_logged(env, sink):
    seg = env.label("a", "pub", b"x")
    env.gw.send("prov", [seg], purpose="p")
    env.gw.send("prov", [Segment("ghost", b"y")], purpose="p")
    env.gw.send("local", [seg], purpose="p")
    assert translog.verify(env.conn) == (True, None)
    assert KEY not in str(env.rows()) and "invented-key" not in str(env.rows())
    assert "invented-key" not in repr(env.gw.destinations["prov"])


def test_transport_error_does_not_raise(conn):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    class Srv:
        server_address = ("127.0.0.1", port)
    e = Env(conn, Srv())
    r = e.gw.send("prov", [e.label("a", "pub", b"x")], purpose="p")
    assert not r.sent and r.reason.startswith("transport error")
    assert e.rows()[-1]["decision"] == "sent"        # bytes may have left; record stays


def test_sensitive_content_needs_a_declassified_grant(env, sink):
    """A grant naming the exact hash of sensitive content does not release it unless the user
    marked that exact text as declassified."""
    raw = env.label("s-raw", "sens", b"raw sensitive bytes")
    env.grants.create(content_hashes={content_hash(raw.data)}, destinations={"prov"},
                      purpose="x", expires_at=2000)
    assert env.gw.send("prov", [raw], purpose="implement").sent is False
    assert sink[1] == []
    clean = env.label("s-clean", "sens", b"generic question, approved by the user")
    env.grants.create(content_hashes={content_hash(clean.data)}, destinations={"prov"},
                      purpose="diagnose", expires_at=2000, declassified=True)
    assert env.gw.send("prov", [clean], purpose="diagnose").sent is True
    assert sink[1] == [clean.data]
    assert env.gw.send("prov", [raw], purpose="implement").sent is False     # raw still blocked
    assert translog.verify(env.conn) == (True, None)


# -- scanners v0 wired into the gateway -----------------------------------------------------
def scanned_env(conn, sink, terms=(), **kw):
    from hera.privacy.scanners import BLOCKING, default_scanners
    policy = Policy(terms=list(terms))
    return Env(conn, sink[0], scanners=default_scanners(policy), blocking=BLOCKING, **kw)


def test_planted_key_in_public_segment_is_blocked_by_default_scanners(conn, sink):
    from hera.privacy.scanners import default_scanners
    key = "AKIA" + "TESTFAKE0000KEY1"                          # invented, format-valid
    e = Env(conn, sink[0], scanners=default_scanners())
    seg = e.label("a", "pub", f"config = 1\nnote: {key}\n".encode())
    r = e.gw.send("prov", [seg], purpose="implement")
    assert not r.sent and r.reason == "restricted data finding" and sink[1] == []
    row = e.rows()[-1]
    assert row["decision"] == "blocked"
    assert "secret:aws_access_key" in row["scanner_json"]
    assert key not in str(row) and key not in r.reason and "TESTFAKE" not in str(row)
    assert translog.verify(conn) == (True, None)


def test_signal_only_term_does_not_block_but_is_recorded(conn, sink):
    e = scanned_env(conn, sink, terms=[{"term": "kestrelforge", "match": "word", "action": "signal"}])
    seg = e.label("a", "pub", b"notes about kestrelforge here")
    assert e.gw.send("prov", [seg], purpose="p").sent
    assert sink[1] == [seg.data]
    row = e.rows()[-1]
    assert row["decision"] == "sent" and "term:signal#0" in row["scanner_json"]
    assert "kestrelforge" not in str(row)


def test_signal_term_with_email_blocks(conn, sink):
    e = scanned_env(conn, sink, terms=[{"term": "kestrelforge", "match": "word", "action": "signal"}])
    seg = e.label("a", "pub", b"kestrelforge contact ines@quillmere.io")
    r = e.gw.send("prov", [seg], purpose="p")
    assert not r.sent and r.reason == "restricted data finding" and sink[1] == []
    scanner = json.loads(e.rows()[-1]["scanner_json"])
    assert "term:signal#0" in scanner["blocking"] and "pii:email" in scanner["blocking"]
    assert "quillmere" not in str(e.rows()[-1]) and "kestrelforge" not in str(e.rows()[-1])


def test_signal_and_email_in_different_segments_still_block(conn, sink):
    e = scanned_env(conn, sink, terms=[{"term": "kestrelforge", "match": "word", "action": "signal"}])
    s1, s2 = e.label("a", "pub", b"kestrelforge"), e.label("b", "pub", b"mail ines@quillmere.io")
    assert not e.gw.send("prov", [s1, s2], purpose="p").sent and sink[1] == []


def test_review_term_is_held_for_user_review(conn, sink):
    e = scanned_env(conn, sink, terms=[{"term": "mirelark", "match": "exact", "action": "review"}])
    row = blocked(e, sink, "held for user review", "prov",
                  [e.label("a", "pub", b"the mirelark file")], purpose="p")
    assert "term:review#0" in row["scanner_json"] and "mirelark" not in str(row)


def test_block_term_blocks_without_echoing_it(conn, sink):
    e = scanned_env(conn, sink, terms=[{"term": "zorbulon", "match": "fuzzy", "action": "block"}])
    r = e.gw.send("prov", [e.label("a", "pub", b"z0rbul0n plans")], purpose="p")
    assert not r.sent and r.reason == "restricted data finding" and "zorbulon" not in str(e.rows()[-1])


def test_default_blocking_predicate_blocks_every_finding(conn, sink):
    from hera.privacy.scanners import default_scanners
    terms = [{"term": "kestrelforge", "match": "word", "action": "signal"}]
    e = Env(conn, sink[0], scanners=default_scanners(Policy(terms=terms)))    # no `blocking` given
    assert not e.gw.send("prov", [e.label("a", "pub", b"kestrelforge")], purpose="p").sent


def test_blocking_predicate_failure_fails_closed(conn, sink):
    def boom(findings):
        raise RuntimeError("details")
    e = Env(conn, sink[0], scanners=[lambda d: ["x"]], blocking=boom)
    blocked(e, sink, "internal error", "prov", [e.label("a", "pub", b"x")], purpose="p")
