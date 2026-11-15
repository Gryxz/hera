import pytest
from hera.store import db


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def hera_home(tmp_path, monkeypatch):
    from hera import paths
    monkeypatch.setenv("HERA_HOME", str(tmp_path / "hera"))
    paths.ensure_layout()
    return tmp_path / "hera"


@pytest.fixture
def toy(hera_home, tmp_path):
    from toyrepo import make_toy
    return make_toy(tmp_path)
