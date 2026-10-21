"""Regression tests for BUG-14 (see docs/BUG_REPORT.md): an empty
GEMINI_API_KEY in the shell must not silently shadow a real key in .env.
Also covers the Prompt 4.1 (BUG-06) auth/API-key env vars failing fast the
same way GEMINI_API_KEY already does."""

from unittest.mock import patch

import pytest

from app.config import _resolve_gemini_api_key, _resolve_jwt_secret, _resolve_promptguard_api_keys


def test_bug14_empty_env_value_falls_back_to_dotenv(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    with patch("app.config.dotenv_values", return_value={"GEMINI_API_KEY": "real-key-from-dotenv"}):
        assert _resolve_gemini_api_key() == "real-key-from-dotenv"


def test_bug14_missing_env_value_falls_back_to_dotenv(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with patch("app.config.dotenv_values", return_value={"GEMINI_API_KEY": "real-key-from-dotenv"}):
        assert _resolve_gemini_api_key() == "real-key-from-dotenv"


def test_bug14_nonempty_env_value_wins_over_dotenv(monkeypatch):
    """A real platform-set env var (e.g. on Render) must win over a stray
    local .env file -- no blind override=True."""
    monkeypatch.setenv("GEMINI_API_KEY", "real-key-from-shell")
    with patch("app.config.dotenv_values", return_value={"GEMINI_API_KEY": "different-key-from-dotenv-file"}):
        assert _resolve_gemini_api_key() == "real-key-from-shell"


def test_bug14_missing_everywhere_fails_fast(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with patch("app.config.dotenv_values", return_value={}), pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        _resolve_gemini_api_key()


def test_bug14_empty_everywhere_fails_fast(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    with (
        patch("app.config.dotenv_values", return_value={"GEMINI_API_KEY": ""}),
        pytest.raises(RuntimeError, match="GEMINI_API_KEY"),
    ):
        _resolve_gemini_api_key()


# --- Prompt 4.1 (BUG-06): JWT_SECRET / PROMPTGUARD_API_KEYS fail fast -------


def test_jwt_secret_missing_fails_fast(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        _resolve_jwt_secret()


def test_jwt_secret_empty_fails_fast(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        _resolve_jwt_secret()


def test_jwt_secret_present_is_returned(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "a-real-secret")
    assert _resolve_jwt_secret() == "a-real-secret"


def test_promptguard_api_keys_missing_fails_fast(monkeypatch):
    monkeypatch.delenv("PROMPTGUARD_API_KEYS", raising=False)
    with pytest.raises(RuntimeError, match="PROMPTGUARD_API_KEYS"):
        _resolve_promptguard_api_keys()


def test_promptguard_api_keys_empty_fails_fast(monkeypatch):
    monkeypatch.setenv("PROMPTGUARD_API_KEYS", "")
    with pytest.raises(RuntimeError, match="PROMPTGUARD_API_KEYS"):
        _resolve_promptguard_api_keys()


def test_promptguard_api_keys_blank_entries_also_fail_fast(monkeypatch):
    """A value like ",, ," has no real keys in it once stripped -- must
    fail the same way as a genuinely empty/missing value, not silently
    resolve to an empty (and therefore unauthenticatable-by-anyone) set."""
    monkeypatch.setenv("PROMPTGUARD_API_KEYS", " , , ")
    with pytest.raises(RuntimeError, match="PROMPTGUARD_API_KEYS"):
        _resolve_promptguard_api_keys()


def test_promptguard_api_keys_parses_comma_separated_list_and_strips_whitespace(monkeypatch):
    monkeypatch.setenv("PROMPTGUARD_API_KEYS", "key-one, key-two ,key-three")
    assert _resolve_promptguard_api_keys() == {"key-one", "key-two", "key-three"}
