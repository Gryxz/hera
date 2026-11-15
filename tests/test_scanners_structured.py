import gzip
import io
import json
import tarfile
import zipfile
import pytest
from hera.privacy.scanners.structured import scan_structured


def tar_bytes():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo("a.txt")
        info.size = 2
        t.addfile(info, io.BytesIO(b"hi"))
    return buf.getvalue()


def zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "hi")
    return buf.getvalue()


def test_sqlite_by_magic_not_extension():
    assert scan_structured(b"SQLite format 3\x00" + b"\x00" * 100) == ["data:sqlite"]
    assert scan_structured(b"SQLite format 3 is a file format") == []


def test_sql_dump():
    inserts = b"INSERT INTO t VALUES (1);\n" * 3
    assert scan_structured(inserts) == ["data:sql_dump"]
    assert scan_structured(b"create table t (a int);\ninsert into t values (1);") == ["data:sql_dump"]
    assert scan_structured(b"-- PostgreSQL database dump\n") == ["data:sql_dump"]
    assert scan_structured(b"-- MySQL dump 10.13\n") == ["data:sql_dump"]
    assert scan_structured(b"INSERT INTO t VALUES (1);\nINSERT INTO t VALUES (2);") == []
    assert scan_structured(b"CREATE TABLE t (a int);") == []
    assert scan_structured(b"we said INSERT INTO inline, three times: INSERT INTO INSERT INTO") == []


@pytest.mark.parametrize("data", [zip_bytes(), gzip.compress(b"hello"), tar_bytes()])
def test_archives(data):
    assert scan_structured(data) == ["data:archive"]


def test_archive_magic_must_be_at_the_start():
    assert scan_structured(b"text PK\x03\x04") == []


def test_env_file():
    ok = b"APP_NAME=demo\nDEBUG=1\nSECRET_KEY=abc\n"
    assert scan_structured(ok) == ["data:env_file"]
    assert scan_structured(b"export DATABASE_URL=x\nA=1\nB=2") == ["data:env_file"]
    assert scan_structured(b"APP_NAME=demo\nDEBUG=1\nPORT=80\n") == []
    assert scan_structured(b"SECRET_KEY=abc\nOTHER=1\n") == []
    assert scan_structured(b"app_name=demo\nsecret_key=abc\nport=1\n") == []


def test_tabular_personal_csv():
    csv_ = b"first_name,last_name,email\nA,B,a@x.io\n"
    assert scan_structured(csv_) == ["data:tabular_personal"]
    assert scan_structured(b"Customer,City,Total\nA,B,3\n") == ["data:tabular_personal"]
    assert scan_structured(b'"order_id";"customer_email"\n1;a\n') == ["data:tabular_personal"]
    assert scan_structured(b"id,name,score\n1,a,3\n") == []
    assert scan_structured(b"filename,size\na.txt,3\n") == []
    assert scan_structured(b"name,email\n") == []                      # header only


def test_tabular_personal_json():
    rows = [{"Name": "A", "Email": "a@x.io"}]
    assert scan_structured(json.dumps(rows).encode()) == ["data:tabular_personal"]
    jl = b'{"customer": "a", "phone": "1"}\n{"customer": "b", "phone": "2"}\n'
    assert scan_structured(jl) == ["data:tabular_personal"]
    assert scan_structured(json.dumps([{"name": "pkg", "version": "1"}]).encode()) == []
    assert scan_structured(json.dumps({"name": "pkg", "email": "a"}).encode()) == ["data:tabular_personal"]  # one record
    assert scan_structured(b'{\n  "name": "pkg",\n  "version": "1"\n}') == []
    assert scan_structured(b"[1, 2, 3]") == []
    assert scan_structured(b"{not json at all") == []


def test_plain_text_and_empty():
    assert scan_structured(b"") == []
    assert scan_structured(b"def f():\n    return 1\n") == []
