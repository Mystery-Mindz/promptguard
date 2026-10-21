import pytest

from app import llm


@pytest.fixture(autouse=True)
def _clear_llm_in_memory_cache():
    """`app.llm`'s in-memory LRU cache is a module-level global shared by
    the whole test process — without this, a value cached (and a mock
    called) by one test would silently serve a stale hit to a later test
    expecting its own mock to be called. The DB-backed half of the cache
    already resets per-test naturally wherever a test points
    `config.DATABASE_PATH` at a fresh temp file."""
    llm.clear_cache()
    yield
    llm.clear_cache()
