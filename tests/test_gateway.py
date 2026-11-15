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
