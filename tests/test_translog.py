import pytest
from hera.privacy import translog


def test_chain_and_tamper_detection(conn):
    h1 = translog.append(conn, destination="provider-a", purpose="implement", decision="sent",
                         payload=b"hello", task_id="t1", artefacts=[{"id": "a", "label": "public"}])
    h2 = translog.append(conn, destination="provider-a", purpose="tool-result", decision="blocked",
                         payload=b"secret-ish", reason="unlabelled segment")
    assert h1 != h2 and translog.verify(conn) == (True, None)
    row = conn.execute("SELECT * FROM transmissions WHERE seq=1").fetchone()
    assert row["bytes"] == 5 and b"hello" not in str(dict(row)).encode()     # no payload stored
    conn.execute("UPDATE transmissions SET decision='blocked' WHERE seq=1")
    assert translog.verify(conn) == (False, 1)


def test_deleted_row_breaks_chain(conn):
    for i in range(3):
        translog.append(conn, destination="d", purpose="p", decision="sent", payload=bytes([i]))
    conn.execute("DELETE FROM transmissions WHERE seq=2")
    assert translog.verify(conn) == (False, 3)


def test_bad_decision_rejected(conn):
    with pytest.raises(ValueError):
        translog.append(conn, destination="d", purpose="p", decision="maybe", payload=b"")
