"""Tests for Prompt 4.1 (fixes BUG-06): operator password hashing, JWT
issuing/verification (app/auth.py), the operators table (storage.py), the
POST /auth/login endpoint, and the `python -m app.create_operator` CLI.
No live API/model calls anywhere in this file.
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app import config, create_operator, main, storage
from app.auth import create_token, hash_password, require_operator, verify_password
from app.main import app
from app.ratelimit import TokenBucketLimiter

client = TestClient(app)


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    storage.init_db()


# --- Password hashing --------------------------------------------------


def test_hash_password_roundtrips_with_verify_password():
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", hashed)
    assert not verify_password("wrong-password", hashed)


def test_hash_password_never_stores_plaintext():
    assert "correct-horse-battery-staple" not in hash_password("correct-horse-battery-staple")


# --- storage.operators ---------------------------------------------------


def test_create_and_look_up_operator():
    operator_id = storage.create_operator("jane", hash_password("pw"))
    assert isinstance(operator_id, int)

    looked_up = storage.get_operator_by_username("jane")
    assert looked_up is not None
    assert looked_up["id"] == operator_id
    assert looked_up["username"] == "jane"
    assert verify_password("pw", looked_up["password_hash"])


def test_get_operator_by_username_returns_none_for_unknown_username():
    assert storage.get_operator_by_username("nobody") is None


def test_create_operator_duplicate_username_raises_integrity_error():
    storage.create_operator("jane", hash_password("pw1"))
    with pytest.raises(IntegrityError):
        storage.create_operator("jane", hash_password("pw2"))


# --- JWT issuing/verification (app.auth) ----------------------------------


def test_create_token_is_decodable_by_require_operator():
    token = create_token(operator_id=7, username="jane")
    operator = require_operator(f"Bearer {token}")
    assert operator.id == 7
    assert operator.operator_id == "jane"


def test_require_operator_rejects_missing_header():
    with pytest.raises(Exception) as excinfo:
        require_operator(None)
    assert excinfo.value.status_code == 401


def test_require_operator_rejects_malformed_header():
    with pytest.raises(Exception) as excinfo:
        require_operator("not-a-bearer-token")
    assert excinfo.value.status_code == 401


def test_require_operator_rejects_tampered_token():
    token = create_token(operator_id=7, username="jane")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(Exception) as excinfo:
        require_operator(f"Bearer {tampered}")
    assert excinfo.value.status_code == 401


def test_require_operator_rejects_expired_token():
    now = datetime.now(UTC)
    expired_payload = {
        "sub": "7",
        "username": "jane",
        "iat": now - timedelta(hours=9),
        "exp": now - timedelta(hours=1),
    }
    expired_token = jwt.encode(expired_payload, config.JWT_SECRET, algorithm="HS256")
    with pytest.raises(Exception) as excinfo:
        require_operator(f"Bearer {expired_token}")
    assert excinfo.value.status_code == 401
    assert "expired" in excinfo.value.detail.lower()


def test_require_operator_rejects_token_signed_with_a_different_secret():
    forged = jwt.encode(
        {"sub": "1", "username": "attacker", "iat": datetime.now(UTC), "exp": datetime.now(UTC) + timedelta(hours=1)},
        "a-secret-the-server-never-used-at-all-32-bytes",
        algorithm="HS256",
    )
    with pytest.raises(Exception) as excinfo:
        require_operator(f"Bearer {forged}")
    assert excinfo.value.status_code == 401


# --- POST /auth/login ------------------------------------------------------


def test_login_succeeds_with_correct_credentials():
    storage.create_operator("jane", hash_password("correct-password"))

    response = client.post("/auth/login", json={"username": "jane", "password": "correct-password"})

    assert response.status_code == 200
    body = response.json()
    assert body["operator_id"] == "jane"
    operator = require_operator(f"Bearer {body['token']}")
    assert operator.operator_id == "jane"


def test_login_fails_with_wrong_password():
    storage.create_operator("jane", hash_password("correct-password"))
    response = client.post("/auth/login", json={"username": "jane", "password": "wrong-password"})
    assert response.status_code == 401


def test_login_fails_with_unknown_username():
    response = client.post("/auth/login", json={"username": "nobody", "password": "whatever"})
    assert response.status_code == 401


def test_login_unknown_username_and_wrong_password_give_the_same_error():
    """Same 401 message either way -- a failed login can't be used to
    enumerate which usernames exist."""
    storage.create_operator("jane", hash_password("correct-password"))
    wrong_password = client.post("/auth/login", json={"username": "jane", "password": "nope"})
    unknown_user = client.post("/auth/login", json={"username": "nobody", "password": "nope"})
    assert wrong_password.json()["detail"] == unknown_user.json()["detail"]


def test_login_token_grants_access_to_pending_approvals():
    storage.create_operator("jane", hash_password("correct-password"))
    login = client.post("/auth/login", json={"username": "jane", "password": "correct-password"})
    token = login.json()["token"]

    response = client.get("/pending-approvals", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_login_rate_limited_after_too_many_attempts(monkeypatch):
    # A tiny, never-refilling bucket makes the second attempt exhaust it
    # deterministically, without needing config.RATE_LIMIT_AUTH_LOGIN_CAPACITY
    # (5) real attempts in a test.
    monkeypatch.setattr(main, "_login_limiter", TokenBucketLimiter(capacity=1, refill_per_second=0.0))

    storage.create_operator("jane", hash_password("correct-password"))
    first = client.post("/auth/login", json={"username": "jane", "password": "wrong"})
    assert first.status_code == 401  # budget spent, but this attempt itself still processed

    second = client.post("/auth/login", json={"username": "jane", "password": "correct-password"})
    assert second.status_code == 429  # rate-limited even with the RIGHT password now


# --- python -m app.create_operator CLI --------------------------------------


def test_create_operator_cli_creates_a_working_account(monkeypatch):
    monkeypatch.setattr("sys.argv", ["create_operator", "cli_jane"])
    passwords = iter(["a-good-password", "a-good-password"])
    monkeypatch.setattr("getpass.getpass", lambda *_args, **_kwargs: next(passwords))

    exit_code = create_operator.main()

    assert exit_code == 0
    operator = storage.get_operator_by_username("cli_jane")
    assert operator is not None
    assert verify_password("a-good-password", operator["password_hash"])


def test_create_operator_cli_rejects_mismatched_confirmation(monkeypatch):
    monkeypatch.setattr("sys.argv", ["create_operator", "cli_jane"])
    passwords = iter(["one-password", "a-different-password"])
    monkeypatch.setattr("getpass.getpass", lambda *_args, **_kwargs: next(passwords))

    exit_code = create_operator.main()

    assert exit_code == 1
    assert storage.get_operator_by_username("cli_jane") is None


def test_create_operator_cli_rejects_empty_password(monkeypatch):
    monkeypatch.setattr("sys.argv", ["create_operator", "cli_jane"])
    monkeypatch.setattr("getpass.getpass", lambda *_args, **_kwargs: "")

    exit_code = create_operator.main()

    assert exit_code == 1
    assert storage.get_operator_by_username("cli_jane") is None


def test_create_operator_cli_rejects_duplicate_username(monkeypatch):
    storage.create_operator("cli_jane", hash_password("existing-password"))
    monkeypatch.setattr("sys.argv", ["create_operator", "cli_jane"])
    passwords = iter(["a-good-password", "a-good-password"])
    monkeypatch.setattr("getpass.getpass", lambda *_args, **_kwargs: next(passwords))

    exit_code = create_operator.main()

    assert exit_code == 1
