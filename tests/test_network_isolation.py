"""Only hera/privacy/gateway.py may import a network library."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "hera"
GATEWAY = SRC / "privacy" / "gateway.py"
# hera/modelhost.py may import http.client and socket, and nothing else on the banned list: it
# only ever connects to 127.0.0.1 (health checks of the child it started itself, and a probe that
# refuses to start when something already listens on the port). tests/test_modelhost.py checks that
# every URL and connection it builds is on 127.0.0.1. It never sends content, only GET /v1/models.
LOOPBACK_ONLY = {"modelhost.py": {"http.client", "socket"}}
BANNED = {"urllib.request", "urllib3", "http.client", "http.server", "socket", "ssl", "requests",
          "httpx", "aiohttp", "websockets", "ftplib", "smtplib"}


def banned_imports(tree: ast.AST) -> list[str]:
    hits = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        for n in names:
            if any(n == b or n.startswith(b + ".") for b in BANNED):
                hits.append(n)
    return hits


def test_only_gateway_imports_network_libraries():
    offenders = {}
    for p in SRC.rglob("*.py"):
        if p == GATEWAY:
            continue
        hits = banned_imports(ast.parse(p.read_text(encoding="utf-8")))
        if p.parent == SRC:
            hits = [h for h in hits if h not in LOOPBACK_ONLY.get(p.name, ())]
        if hits:
            offenders[str(p.relative_to(SRC))] = hits
    assert offenders == {}


def test_detector_catches_all_import_forms():
    for code in ("import urllib.request", "from urllib import request", "from urllib.request import urlopen",
                 "import socket", "from http import client", "import requests as r"):
        assert banned_imports(ast.parse(code)), code
    assert not banned_imports(ast.parse("import urllib.parse\nfrom http import HTTPStatus"))


def test_gateway_does_import_urllib():
    assert "urllib.request" in banned_imports(ast.parse(GATEWAY.read_text(encoding="utf-8")))


def test_the_loopback_allowance_is_narrow():
    hits = banned_imports(ast.parse((SRC / "modelhost.py").read_text(encoding="utf-8")))
    assert set(hits) <= LOOPBACK_ONLY["modelhost.py"] | {"http.client.HTTPConnection"} and hits
    assert set(LOOPBACK_ONLY) == {"modelhost.py"}
