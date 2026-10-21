import pytest

from app import config, guard, llm, main, storage
from app.auth import create_token, hash_password


@pytest.fixture(autouse=True)
def _clear_llm_in_memory_cache():
    """`app.llm`'s in-memory LRU cache is a module-level global shared by
    the whole test process — without this, a value cached (and a mock
    called) by one test would silently serve a stale hit to a later test
    expecting its own mock to be called. The DB-backed half of the cache
    already resets per-test naturally wherever a test points
    `config.DATABASE_URL` at a fresh temp file."""
    llm.clear_cache()
    yield
    llm.clear_cache()


# Fixed, well-known test keys -- every test calling a /guard/* route
# attaches the first one (see tests/test_guard.py's GUARD_HEADERS),
# regardless of whatever real PROMPTGUARD_API_KEYS value CI or a local
# .env happens to have set at import time. The second exists only so a
# test can prove rate limiting is keyed per-key, not global, by using a
# second VALID key instead of an invalid one (which would just 401).
TEST_PROMPTGUARD_API_KEY = "test-promptguard-key"
TEST_PROMPTGUARD_API_KEY_2 = "test-promptguard-key-2"


@pytest.fixture(autouse=True)
def _fixed_promptguard_api_key(monkeypatch):
    monkeypatch.setattr(config, "PROMPTGUARD_API_KEYS", {TEST_PROMPTGUARD_API_KEY, TEST_PROMPTGUARD_API_KEY_2})


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """`app.guard._check_limiter` and `app.main._login_limiter` are
    module-level globals shared by the whole test process -- without this,
    one test's calls could exhaust another test's rate-limit budget,
    since both track state across requests on purpose (that's the whole
    point of a rate limiter)."""
    guard._check_limiter.reset()
    main._login_limiter.reset()
    yield
    guard._check_limiter.reset()
    main._login_limiter.reset()


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Creates a fresh operator ("test_operator") in the current test's
    temp DB (relies on a `temp_db`-style autouse fixture already pointing
    `config.DATABASE_URL` at it -- autouse fixtures run before fixtures a
    test explicitly requests, same scope) and returns an `Authorization`
    header carrying a valid JWT for them -- for tests that need a logged-
    in operator but aren't specifically testing the login flow itself (see
    test_main.py's `test_login_*` for that).
    """
    operator_id = storage.create_operator("test_operator", hash_password("test-password-123"))
    token = create_token(operator_id, "test_operator")
    return {"Authorization": f"Bearer {token}"}
