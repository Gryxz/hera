import pytest
from hera.store import db


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()
