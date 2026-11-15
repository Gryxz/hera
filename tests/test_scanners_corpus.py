"""Seeded corpus: recall per (category, transformation) and false-block rate on look-alikes.

All data is invented and generated at runtime from a fixed seed, so no key-shaped literal sits in
the repository. Run with `pytest -s tests/test_scanners_corpus.py` to see the table.

Only plain-form recall, the base64 / URL-encoded / unicode-escaped / zero-width recall for secrets,
and the benign false-block rate are asserted. Every other cell is recorded, not tuned for.
"""
from __future__ import annotations
import base64
import codecs
import gzip
import hashlib
import io
import json
import random
import string
import tarfile
import uuid
import zipfile
from hera.privacy.policy import Policy
from hera.privacy.scanners import BLOCKING, default_scanners
from hera.privacy.scanners.pii import luhn_ok

KNOWN_MISSES = {
    "reversed": "patterns are matched left to right; reversed text is never un-reversed",
    "split_concat": 'pieces joined by `" + "` are never re-assembled before matching',
    "rot13": "no rotation or substitution cipher is undone",
    "hex": "hex-encoded text is not decoded (only base64, URL and \\u/\\x escapes are)",
    "gzip_b64": "compressed content is not decompressed, even one layer down",
}
DECODED_SECRET_TRANSFORMS = ("base64", "urlenc", "unicode_escape", "zero_width")
TRANSFORMS = ("plain", "base64", "urlenc", "unicode_escape", "zero_width", "spaced", "leet",
              "reversed", "split_concat", "rot13", "hex", "gzip_b64")
TERM_ONLY = {"spaced", "leet"}

rng = random.Random(20261115)
B62 = string.ascii_letters + string.digits
UPPER_DIGITS = string.ascii_uppercase + string.digits


def tok(n: int, alphabet: str = B62) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


def digits(n: int) -> str:
    return "".join(rng.choice(string.digits) for _ in range(n))


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def luhn_number(prefix: str, length: int) -> str:
    body = prefix + digits(length - len(prefix) - 1)
    return next(body + c for c in string.digits if luhn_ok(body + c))


# -- transformations (bytes -> bytes) -------------------------------------------------------
def _txt(b: bytes) -> str:
    return b.decode("latin-1")


TRANSFORM_FUNCS = {
    "plain": lambda b: b,
    "base64": lambda b: base64.b64encode(b),
    "urlenc": lambda b: "".join(f"%{x:02X}" for x in b).encode(),
    "unicode_escape": lambda b: "".join(f"\\u{ord(c):04x}" for c in _txt(b)).encode(),
    "zero_width": lambda b: "​".join(_txt(b)[i:i + 4] for i in range(0, len(b), 4)).encode("utf-8"),
    "spaced": lambda b: " ".join(_txt(b)).encode("latin-1"),
    "leet": lambda b: _txt(b).translate(str.maketrans("oieast", "013457")).encode("latin-1"),
    "reversed": lambda b: b[::-1],
    "split_concat": lambda b: ('"' + '" + "'.join(_txt(b)[i:i + 6] for i in range(0, len(b), 6)) + '"').encode(),
    "rot13": lambda b: codecs.encode(_txt(b), "rot13").encode("latin-1"),
    "hex": lambda b: b.hex().encode(),
    "gzip_b64": lambda b: base64.b64encode(gzip.compress(b)),
}


def wrap(s: str) -> bytes:
    return f"note: {s} end".encode()


# -- positives: (expected finding, raw bytes) ----------------------------------------------
def secret_formats():
    jwt = b64url(b'{"alg":"HS256","typ":"JWT"}') + "." + b64url(b'{"sub":"invented-user-1"}') + "." + tok(43, B62 + "_-")
    pem_body = "\n".join(base64.b64encode(rng.randbytes(48)).decode() for _ in range(3))
    items = [
        ("secret:aws_access_key", "AKIA" + tok(16, UPPER_DIGITS)),
        ("secret:aws_access_key", "ASIA" + tok(16, UPPER_DIGITS)),
        ("secret:github_token", "ghp_" + tok(36)),
        ("secret:github_token", "github_pat_" + tok(22) + "_" + tok(59)),
        ("secret:slack_token", "xoxb-" + digits(12) + "-" + digits(12) + "-" + tok(24)),
        ("secret:stripe_key", "sk_live_" + tok(24)),
        ("secret:google_api_key", "AIza" + tok(35, B62 + "_-")),
        ("secret:openai_key", "sk-proj-" + tok(40, B62 + "_-")),
        ("secret:anthropic_key", "sk-ant-api03-" + tok(80, B62 + "_-")),
        ("secret:jwt", jwt),
        ("secret:private_key", "-----BEGIN RSA PRIVATE KEY-----\n" + pem_body),
        ("secret:generic_assignment", 'password = "' + tok(14) + '"'),
        ("secret:generic_assignment", "API_KEY=" + tok(20)),
    ]
    return [(n, wrap(v)) for n, v in items]


def entropy_tokens():
    return [("secret:high_entropy", wrap(tok(n, a))) for n, a in (
        (32, B62), (40, B62), (48, B62), (64, B62), (44, B62 + "+/="), (43, B62 + "_-"),
        (56, B62), (36, B62 + "+/"), (72, B62 + "_-"), (33, B62))]


def emails():
    return [("pii:email", wrap(e)) for e in (
        "ines.varga@quillmere.io", "m.okafor+billing@brantlow.co.uk", "tomas_r@fennick.ca",
        "zhu.wei@tarvos.net", "p.lindqvist@orrery-labs.se", "a.haddad@kestrel.example.fr".replace("example.", ""),
        "j.santos@veldmark.com.br", "n.kowalczyk@pinnacle-mill.pl", "ola@trellisworks.dev")]


def phones():
    return [("pii:phone", wrap(p)) for p in (
        "(212) 555-0275", "416-555-0275", "647.555.0275", "+1 437 555 0275", "+44 20 7946 0958",
        "+81 3-5555-0275", "+49 30 5555 0275", "+63 917 555 0275", "+33 1 55 55 02 75", "1-905-555-0275")]


def addresses():
    return [("pii:postal_address", wrap(a)) for a in (
        "1420 Marlow Street, Springfield", "22 Harbour Road", "305 Oak Blvd", "78 Elm Lane",
        "9 Cedar Way", "1600 Pine Avenue Toronto ON M5V 2T6", "7 5th Ave", "ship to M5V 2T6",
        "postal code: SW1A 1AA", "mailing address ZIP 94107")]


def cards():
    nums = [luhn_number("4", 16), luhn_number("51", 16), luhn_number("37", 15), luhn_number("6011", 16),
            luhn_number("4", 13), luhn_number("55", 16), luhn_number("4", 19), luhn_number("35", 16)]
    fmt = [lambda n: n, lambda n: " ".join(n[i:i + 4] for i in range(0, len(n), 4)),
           lambda n: "-".join(n[i:i + 4] for i in range(0, len(n), 4))]
    return [("pii:payment_card", wrap("card " + fmt[i % 3](n))) for i, n in enumerate(nums)]


def trackings():
    return [("pii:tracking_number", wrap(t)) for t in (
        "1Z" + tok(16, UPPER_DIGITS), "94" + digits(20), "92" + digits(20), "93" + digits(18),
        "95" + digits(19), "tracking " + digits(12), "FedEx " + digits(15), "shipment: " + digits(12),
        "1Z" + tok(16, UPPER_DIGITS))]


def order_ids():
    return [("pii:order_id", wrap(o)) for o in (
        "Order #A1B" + tok(5, UPPER_DIGITS), "order_id: " + digits(6), "invoice INV-" + digits(5),
        "Order no. " + digits(5) + "-" + digits(2), "order " + digits(8), "Order ID: 7F3K" + digits(3),
        "ORDER# 555" + digits(2) + "ab", "Invoice: " + digits(7))]


def structured():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "invented")
    tbuf = io.BytesIO()
    with tarfile.open(fileobj=tbuf, mode="w") as t:
        info = tarfile.TarInfo("a.txt")
        info.size = 3
        t.addfile(info, io.BytesIO(b"abc"))
    rows = [{"Name": "Ines V", "Email": "i@quillmere.io", "City": "X"}, {"Name": "T R", "Email": "t@fennick.ca", "City": "Y"}]
    return [
        ("data:sqlite", b"SQLite format 3\x00" + b"\x00" * 84),
        ("data:sql_dump", b"INSERT INTO t VALUES (1,'a');\nINSERT INTO t VALUES (2,'b');\nINSERT INTO t VALUES (3,'c');\n"),
        ("data:sql_dump", b"-- PostgreSQL database dump\n-- Dumped from database version 15\n"),
        ("data:sql_dump", b"CREATE TABLE t (a int);\nINSERT INTO t VALUES (1);\n"),
        ("data:archive", buf.getvalue()),
        ("data:archive", gzip.compress(b"invented content " * 5)),
        ("data:archive", tbuf.getvalue()),
        ("data:env_file", ("APP_NAME=demo\nDEBUG=1\nSECRET_KEY=" + tok(12) + "\n").encode()),
        ("data:env_file", ("export DATABASE_URL=postgres://u@h/d\nPORT=80\nMODE=x\n").encode()),
        ("data:tabular_personal", b"first_name,last_name,email\nIna,V,i@quillmere.io\nTom,R,t@fennick.ca\n"),
        ("data:tabular_personal", json.dumps(rows).encode()),
        ("data:tabular_personal", b'{"customer": "a", "phone": "1"}\n{"customer": "b", "phone": "2"}\n'),
    ]


TERMS = [
    {"term": "Zorbulon", "kind": "project", "match": "exact", "action": "block"},
    {"term": "Quenthorp Ltd", "kind": "client", "match": "exact", "action": "block"},
    {"term": "Mirelark", "kind": "project", "match": "exact", "action": "review"},
    {"term": "brantlow-internal", "kind": "host", "match": "case_insensitive", "action": "block"},
    {"term": "Velmora Group", "kind": "client", "match": "case_insensitive", "action": "block"},
    {"term": "tarvosdb", "kind": "host", "match": "case_insensitive", "action": "review"},
    {"term": "Fennick", "kind": "person", "match": "word", "action": "block"},
    {"term": "Orrery", "kind": "project", "match": "word", "action": "block"},
    {"term": "kestrelforge", "kind": "project", "match": "word", "action": "signal"},
    {"term": "Pinnacle Mill", "kind": "client", "match": "fuzzy", "action": "block"},
    {"term": "Ulvaxis", "kind": "project", "match": "fuzzy", "action": "block"},
    {"term": "Okafor Holdings", "kind": "client", "match": "fuzzy", "action": "review"},
]
POLICY = Policy(terms=TERMS)


def terms_positives():
    out = []
    for i, t in enumerate(TERMS):
        text = f"Please look at the {t['term']} notes today"
        out.append((f"term:{t['action']}#{i}", text.encode()))
    return out


CATEGORIES = {
    "secret_formats": secret_formats(), "entropy": entropy_tokens(), "email": emails(),
    "phone": phones(), "address": addresses(), "card": cards(), "tracking": trackings(),
    "order_id": order_ids(), "structured": structured(), "terms": terms_positives(),
}


# -- benign look-alikes ---------------------------------------------------------------------
def benign_corpus() -> list[bytes]:
    r = random.Random(99)
    hexs = lambda n: "".join(r.choice("0123456789abcdef") for _ in range(n))
    b64 = lambda n: base64.b64encode(r.randbytes(n)).decode()
    items = [
        # uuids, hashes, commit ids
        *(f"id: {uuid.UUID(int=r.getrandbits(128), version=4)}" for _ in range(4)),
        *(f"sha1: {hashlib.sha1(str(i).encode()).hexdigest()}" for i in range(3)),
        *(f"sha256: {hashlib.sha256(str(i).encode()).hexdigest()}" for i in range(3)),
        f"md5 {hashlib.md5(b'x').hexdigest()}",
        f"commit {hexs(40)}\nAuthor: build bot", f"git checkout {hexs(40)}", f"FROM image@sha256:{hexs(64)}",
        f"key=value&id={uuid.UUID(int=r.getrandbits(128))}&commit={hexs(40)}",
        # example-domain emails
        "contact: user@example.com", "cc: a@example.org, b@example.net", "mail dev@test.example",
        "smtp user@localhost", "bounce@mail.example.com", "x@host.invalid",
        # documentation phone numbers
        "call (416) 555-0123", "tel +1 202 555 0143", "fax 212-555-0199", "+1 647 555 0100",
        # version strings, ips, timestamps, ids
        "version 1.2.3.4", "numpy==1.26.4 and v10.20.30", "Python 3.11.9 (main, Oct 2026)", "ip 192.168.100.200",
        "2026-11-14T12:30:45Z", "ts=1731585045123", "built 20261114123045", "pi = 3.14159265358979",
        "ISBN 978-3-16-148410-0", "build 2026-11-14 run 4821",
        # lockfile integrity lines
        f'"integrity": "sha512-{b64(64)}"', f'"integrity": "sha256-{b64(32)}"', f'"integrity": "sha384-{b64(48)}"',
        f'    resolved "https://registry.invalid/pkg/-/pkg-1.0.0.tgz#{hexs(40)}"',
        # minified js / code / css
        "!function(e,t){var n=e.length,r=0;for(;r<n;r++)t(e[r],r)}(window,function(a,b){return a+b});",
        "var a=document.getElementById('app'),b=a.querySelectorAll('.item');b.forEach(function(c){c.classList.add('on')});",
        ".container-fluid-row-wrapper-inner-content-block-header { margin: 0 auto; }",
        "const getUserAccountBalanceFromRemoteServiceProvider = () => null;",
        "token = tokenize(text)", 'password = getpass.getpass()', 'api_key = os.environ["API_KEY"]',
        "password: changeme", 'token: "your_token_here"', "API_KEY=xxxxxxxxxxxxxxxx", 'password = "<password>"',
        "secret: ${SECRET_NAME}", "path /usr/local/lib/python3.11/site-packages/numpy/core/_multiarray_umath.py",
        # ordinary prose
        "In order to continue, please place the token on the table.",
        "The token budget for this prompt is 4096 tokens.", "Sort the rows in descending order by date.",
        "The order of operations matters when you evaluate the expression.",
        "We shipped 12 releases in 2024 and it took 5 minutes to drive home.",
        "Take the first 10 ways to improve and drive the way forward.",
        "Luhn-failing digits: 4111 1111 1111 1112", "Dr. Smith said the way is Final Draft 3.",
        "A paragraph of plain prose with no identifiers, numbers or addresses at all.",
        "SGVsbG8gd29ybGQsIHRoaXMgaXMgYSBiZW5pZ24gc3RyaW5nIGZvciB0ZXN0aW5n",     # base64 of benign prose
        "Fennicks and orreries are common nouns; zorbulonic is not a word here.",  # word-mode near misses
    ]
    return [i.encode() for i in items]


def findings_for(data: bytes, scanners) -> list[str]:
    out: list[str] = []
    for s in scanners:
        out.extend(s(data))
    return out


def detected(expected: str, found: list[str]) -> bool:
    return expected in found or f"{expected} (decoded)" in found


def measure():
    scanners = default_scanners(POLICY)
    table: dict[str, dict[str, tuple[int, int]]] = {}
    for cat, samples in CATEGORIES.items():
        row = {}
        for tname in TRANSFORMS:
            if tname in TERM_ONLY and cat != "terms":
                continue
            hits = sum(detected(exp, findings_for(TRANSFORM_FUNCS[tname](raw), scanners))
                       for exp, raw in samples)
            row[tname] = (hits, len(samples))
        table[cat] = row
    benign = benign_corpus()
    blocked = [i for i, b in enumerate(benign) if BLOCKING(findings_for(b, scanners))]
    return table, len(benign), blocked, benign


def render(table, n_benign, blocked) -> str:
    widths = {t: max(len(t), 4) + 1 for t in TRANSFORMS}
    lines = ["", "RECALL per (category, transformation); n = samples per cell",
             f"{'category':<15}{'n':>3}" + "".join(f"{t:>{widths[t]}}" for t in TRANSFORMS)]
    for cat, row in table.items():
        n = next(iter(row.values()))[1]
        cells = "".join(f"{(format(row[t][0] / row[t][1], '.2f') if t in row else 'n/a'):>{widths[t]}}"
                        for t in TRANSFORMS)
        lines.append(f"{cat:<15}{n:>3}{cells}")
    lines.append(f"false-block rate on benign: {len(blocked)}/{n_benign} = {len(blocked) / n_benign:.3f}")
    lines.append("known misses (recorded, not asserted): " + ", ".join(KNOWN_MISSES))
    return "\n".join(lines)


def test_corpus_sizes():
    for cat, samples in CATEGORIES.items():
        assert len(samples) >= 8, cat
    assert len(benign_corpus()) >= 40
    assert set(KNOWN_MISSES) == {"reversed", "split_concat", "rot13", "hex", "gzip_b64"}
    assert all(KNOWN_MISSES.values())


def test_seeded_corpus_recall_and_false_blocks():
    table, n_benign, blocked, benign = measure()
    report = render(table, n_benign, blocked)
    print(report)                                   # visible with `pytest -s`
    for i in blocked:
        print("  false block:", benign[i][:70], findings_for(benign[i], default_scanners(POLICY)))
    for cat, row in table.items():
        hits, n = row["plain"]
        assert hits / n >= 0.9, f"plain recall for {cat}: {hits}/{n}"
    for cat in ("secret_formats", "entropy"):
        for t in DECODED_SECRET_TRANSFORMS:
            hits, n = table[cat][t]
            assert hits / n >= 0.75, f"{t} recall for {cat}: {hits}/{n}"
    assert len(blocked) / n_benign <= 0.10, f"false-block rate {len(blocked)}/{n_benign}"


def test_corpus_findings_never_contain_corpus_text():
    scanners = default_scanners(POLICY)
    for samples in CATEGORIES.values():
        for _exp, raw in samples:
            for f in findings_for(raw, scanners):
                assert f.split(" ")[0].split(":")[0] in {"secret", "pii", "data", "term"}
                assert len(f) < 40
