"""Regression tests for BUG-14 (see docs/BUG_REPORT.md): an empty
GEMINI_API_KEY in the shell must not silently shadow a real key in .env."""

from unittest.mock import patch

import pytest

from app.config import _resolve_gemini_api_key


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
