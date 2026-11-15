import base64
import uuid
import pytest
from hera.privacy.scanners.secrets import scan_secrets, shannon_entropy

U = "ABCDEFGHJKLMNPQRSTUVWXYZ"
# Invented values, assembled at runtime so no key-shaped literal sits in the repository.
AWS = "AKIA" + "TESTFAKE0000KEY1"
GH = "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5"
RANDOM40 = "q7Zk2Xv9Lm4Rt8Wc1Np6Yd3Hs5Bg0Fj" + "Ue4Ka1Ox"


@pytest.mark.parametrize("text,name", [
    (AWS, "aws_access_key"),
    ("ASIA" + "TESTFAKE0000KEY2", "aws_access_key"),
    (GH, "github_token"),
    ("gho_" + "x9Y8w7V6u5T4s3R2q1P0o9N8m7L6k5J4i3H2", "github_token"),
    ("github_pat_" + "11TESTFAKE0000aaaaaa_bbbbbbbbbbbbbbbbbbbbbbbb", "github_token"),
    ("xoxb-" + "123456789012-123456789012-" + "tEsTfAkEtOkEn0000aaaa", "slack_token"),
    ("sk_live_" + "TESTFAKE0000aaaabbbb1234", "stripe_key"),
    ("rk_live_" + "TESTFAKE0000aaaabbbb1234", "stripe_key"),
    ("sk_test_" + "TESTFAKE0000aaaabbbb1234", "stripe_key"),
    ("AIza" + "SyTESTFAKE0000aaaabbbbccccddddeeeee", "google_api_key"),
    ("sk-" + "proj-TESTFAKE0000aaaabbbbccccdddd", "openai_key"),
    ("sk-ant-" + "api03-TESTFAKE0000aaaabbbbccccdddd", "anthropic_key"),
    ("eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiJ0ZXN0LXVzZXIifQ." + "TESTFAKEsignature0000aaaa", "jwt"),
    ("-----BEGIN RSA PRIVATE KEY-----\nTESTFAKE\n", "private_key"),
    ("-----BEGIN PRIVATE KEY-----", "private_key"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", "private_key"),
])
def test_known_formats(text, name):
    assert f"secret:{name}" in scan_secrets(f"value {text} end".encode())


@pytest.mark.parametrize("text", [
    'password = "hunter2hunter"', "passwd: Tr0ub4dor&3x", "secret=abcd1234efgh",
    "API_KEY=ab12cd34ef56", "api-key: 'zzzz9999yyyy'", "auth_token = 'a1b2c3d4e5f6'",
    "client_secret: s3cr3tValue99",
])
def test_generic_assignment(text):
    assert "secret:generic_assignment" in scan_secrets(text.encode())


@pytest.mark.parametrize("text", [
    "password =", 'password = ""', "password: changeme", "secret = 'example-secret-value'",
    "token: your_token_here", "api_key = YOUR-API-KEY-HERE", "password=xxxxxxxxxx",
    'secret: "<secret value>"', "token: ${SECRET_TOKEN}", "api_key: {{ api_key_value }}",
    "password = 'aaaaaaaaaaaa'", "password: short", "token = tokenize(text)",
    'api_key = os.environ["API_KEY"]', "password = self.password_value", "token = config.token_name",
])
def test_generic_assignment_exceptions(text):
    assert scan_secrets(text.encode()) == []


def test_entropy_rule():
    assert shannon_entropy(RANDOM40) >= 4.0
    assert scan_secrets(f"blob {RANDOM40}".encode()) == ["secret:high_entropy"]
    assert scan_secrets(b"x" * 40) == []
    assert scan_secrets(b"this_is_a_very_long_snake_case_identifier_name") == []


@pytest.mark.parametrize("benign", [
    "a94a8fe5ccb19ba61c4c0873d391e987982fbbd3",                               # sha1 / commit id
    "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",       # sha256
    "d41d8cd98f00b204e9800998ecf8427e",                                       # md5
    str(uuid.UUID(int=0x1234567890ABCDEF1234567890ABCDEF)),
    "sha512-" + base64.b64encode(bytes(range(64))).decode(),
    "sha256-" + base64.b64encode(bytes(range(32, 64))).decode(),
    "sha384-" + base64.b64encode(bytes(range(48))).decode(),
])
def test_entropy_exceptions(benign):
    assert scan_secrets(f'"integrity": "{benign}" id={benign}'.encode()) == []


def test_lockfile_integrity_string_is_exempt_but_bare_base64_is_not():
    blob = base64.b64encode(bytes((i * 37 + 11) % 256 for i in range(64))).decode()
    assert scan_secrets(f'"integrity": "sha512-{blob}"'.encode()) == []
    assert "secret:high_entropy" in scan_secrets(f"digest {blob}".encode())


def test_zero_width_characters_are_stripped():
    split = AWS[:6] + "​" + AWS[6:12] + "﻿" + AWS[12:]
    assert "secret:aws_access_key" in scan_secrets(split.encode())


def test_decoded_layers_are_suffixed():
    note = f"note: {GH} end"
    b64 = base64.b64encode(note.encode()).decode()
    assert "secret:github_token (decoded)" in scan_secrets(b64.encode())
    pct = "".join(f"%{b:02X}" for b in AWS.encode())
    assert scan_secrets(pct.encode()) == ["secret:aws_access_key (decoded)"]
    esc = "".join(f"\\u{ord(c):04x}" for c in AWS)
    assert scan_secrets(esc.encode()) == ["secret:aws_access_key (decoded)"]
    hexesc = "".join(f"\\x{ord(c):02x}" for c in AWS)
    assert scan_secrets(hexesc.encode()) == ["secret:aws_access_key (decoded)"]


def test_base64_of_binary_is_not_decoded_into_findings():
    blob = base64.b64encode(bytes(range(256))[:60]).decode()
    assert all("(decoded)" not in f for f in scan_secrets(f"x {blob}".encode()))


def test_findings_never_contain_matched_text():
    for text in (AWS, GH, RANDOM40, 'password = "hunter2hunter"'):
        for f in scan_secrets(text.encode()):
            assert text not in f and f.startswith("secret:")


def test_empty_and_binary_input():
    assert scan_secrets(b"") == []
    assert isinstance(scan_secrets(bytes(range(256))), list)
