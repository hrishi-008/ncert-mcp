import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BOOKS_DB = ROOT / "data" / "books.db"


@pytest.fixture(scope="session")
def books():
    from server.books import Books

    if not BOOKS_DB.exists():
        pytest.skip("data/books.db not built (python -m ingest.build_db)")
    return Books(BOOKS_DB)


@pytest.fixture
def state(tmp_path):
    from server.state import State

    return State(f"sqlite:///{tmp_path / 'state.db'}")
