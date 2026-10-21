"""Tests for app/llm.py: the provider layer itself (caching, retry/backoff,
fallback, normalization). Everything here mocks `app.llm._gemini_client` —
the lowest seam inside the Gemini backend — so these tests exercise the
REAL caching/retry/fallback/pacing logic in app/llm.py (unlike
test_agent.py-style tests that mock the public generate_with_tools/judge/
embed_many functions directly, bypassing all of this). No live API calls.
"""

from unittest.mock import MagicMock, patch

import pytest
from google.genai import errors

from app import config, llm, storage
from app.llm import GenerateResult, Message, ModelUnavailable, ToolCall, ToolDeclaration, ToolResult


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "test.db"))
    storage.init_db()


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Pacing and retry backoff both sleep via `app.llm._sleep` — patch it
    to a no-op so these tests run instantly instead of waiting out real
    13s pacing gaps or exponential backoff delays."""
    monkeypatch.setattr(llm, "_sleep", lambda seconds: None)
    llm._last_call_at.clear()


def _client_returning(generate_content=None, embed_content=None) -> MagicMock:
    client = MagicMock()
    if generate_content is not None:
        client.models.generate_content.side_effect = generate_content
    if embed_content is not None:
        client.models.embed_content.side_effect = embed_content
    return client


# --- generate_with_tools: normalization -------------------------------------


def test_generate_with_tools_normalizes_response_to_tool_calls():
    raw_call = MagicMock(id="c1", args={"folder": "inbox"})
    raw_call.name = "read_email"  # MagicMock(name=...) sets the mock's repr, not an attribute
    response = MagicMock(text=None, function_calls=[raw_call])

    with patch("app.llm._gemini_client", return_value=_client_returning(generate_content=[response])):
        result = llm.generate_with_tools(
            [Message(role="user", text="hi")],
            [ToolDeclaration(name="read_email", description="d", parameters={"type": "object", "properties": {}})],
        )

    assert result == GenerateResult(text=None, tool_calls=[ToolCall(id="c1", name="read_email", args={"folder": "inbox"})])


def test_generate_with_tools_sends_tool_results_as_one_message_per_turn():
    """Round-trips a ToolResult-bearing Message through the Gemini content
    translation and back — the request actually sent must carry both the
    model's prior tool_calls turn and the combined tool_results turn."""
    response = MagicMock(text="done", function_calls=None)
    client = _client_returning(generate_content=[response])

    messages = [
        Message(role="user", text="go"),
        Message(role="model", tool_calls=(ToolCall(id="c1", name="read_email", args={}),)),
        Message(role="user", tool_results=(ToolResult(call_id="c1", name="read_email", output={"result": []}),)),
    ]

    with patch("app.llm._gemini_client", return_value=client):
        llm.generate_with_tools(messages, [])

    sent_contents = client.models.generate_content.call_args.kwargs["contents"]
    assert len(sent_contents) == 3
    assert sent_contents[1].parts[0].function_call.name == "read_email"
    assert sent_contents[2].parts[0].function_response.response == {"result": []}


# --- judge: caching ----------------------------------------------------


def test_judge_without_cache_key_never_caches():
    response = MagicMock(text="0.42")
    client = _client_returning(generate_content=[response, response])

    with patch("app.llm._gemini_client", return_value=client):
        first = llm.judge("prompt")
        second = llm.judge("prompt")

    assert first == second == "0.42"
    assert client.models.generate_content.call_count == 2


def test_judge_caches_by_cache_key_not_by_the_literal_prompt():
    """BUG-07 compatibility: drift_engine wraps the same classified text in
    a fresh random boundary tag every call, so the rendered prompt is
    never literally identical twice. Caching must key on the stable
    `cache_key` (the raw text), not `prompt` -- two calls with DIFFERENT
    prompts but the SAME cache_key must hit the cache on the second."""
    response = MagicMock(text="0.9")
    client = _client_returning(generate_content=[response])

    with patch("app.llm._gemini_client", return_value=client):
        first = llm.judge("<tag_abc123>malicious text</tag_abc123>", cache_key="malicious text")
        second = llm.judge("<tag_def456>malicious text</tag_def456>", cache_key="malicious text")

    assert first == second == "0.9"
    assert client.models.generate_content.call_count == 1, "second call with a different prompt must hit the cache"


def test_judge_cache_hit_survives_lru_clear_via_db(tmp_path, monkeypatch):
    """The DB-backed half of the cache is the source of truth -- clearing
    only the in-memory LRU (as happens between test runs, or could happen
    on a process restart with a persistent DB) must not force a re-call."""
    response = MagicMock(text="0.3")
    client = _client_returning(generate_content=[response])

    with patch("app.llm._gemini_client", return_value=client):
        llm.judge("some prompt", cache_key="stable text")
        llm.clear_cache()
        result = llm.judge("a totally different prompt rendering", cache_key="stable text")

    assert result == "0.3"
    assert client.models.generate_content.call_count == 1


# --- embed_many: batching + partial cache hits ------------------------------


def test_embed_many_makes_one_batched_call_for_several_misses():
    response = MagicMock(embeddings=[MagicMock(values=[1.0, 0.0]), MagicMock(values=[0.0, 1.0])])
    client = _client_returning(embed_content=[response])

    with patch("app.llm._gemini_client", return_value=client):
        vectors = llm.embed_many(["text a", "text b"])

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    client.models.embed_content.assert_called_once_with(model=config.EMBEDDING_MODEL, contents=["text a", "text b"])


def test_embed_many_only_calls_for_cache_misses_and_preserves_order():
    first_response = MagicMock(embeddings=[MagicMock(values=[9.0, 9.0])])
    second_response = MagicMock(embeddings=[MagicMock(values=[1.0, 1.0]), MagicMock(values=[2.0, 2.0])])
    client = _client_returning(embed_content=[first_response, second_response])

    with patch("app.llm._gemini_client", return_value=client):
        llm.embed_many(["cached text"])  # primes the cache
        vectors = llm.embed_many(["new a", "cached text", "new b"])

    assert vectors == [[1.0, 1.0], [9.0, 9.0], [2.0, 2.0]]
    # Second embed_many call must only ask the provider for the two MISSES,
    # in one batched call -- not three, and not one per miss.
    assert client.models.embed_content.call_count == 2
    second_call_contents = client.models.embed_content.call_args_list[1].kwargs["contents"]
    assert second_call_contents == ["new a", "new b"]


def test_embed_many_empty_input_makes_no_call():
    client = _client_returning(embed_content=[])
    with patch("app.llm._gemini_client", return_value=client):
        assert llm.embed_many([]) == []
    client.models.embed_content.assert_not_called()


# --- retry/backoff + ModelUnavailable ---------------------------------------


def test_retryable_error_is_retried_then_succeeds():
    retryable = errors.ClientError(429, {"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}})
    success = MagicMock(text="0.5")
    client = _client_returning(generate_content=[retryable, success])

    with patch("app.llm._gemini_client", return_value=client):
        result = llm.judge("prompt")

    assert result == "0.5"
    assert client.models.generate_content.call_count == 2


def test_retries_exhausted_raises_model_unavailable():
    overloaded = errors.ServerError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
    client = _client_returning(generate_content=[overloaded] * (config.MODEL_MAX_RETRIES + 1))

    with patch("app.llm._gemini_client", return_value=client), pytest.raises(ModelUnavailable):
        llm.judge("prompt")

    assert client.models.generate_content.call_count == config.MODEL_MAX_RETRIES + 1


def test_non_retryable_error_propagates_immediately_without_retrying():
    bad_request = errors.ClientError(400, {"error": {"message": "bad request", "status": "INVALID_ARGUMENT"}})
    client = _client_returning(generate_content=[bad_request])

    with patch("app.llm._gemini_client", return_value=client), pytest.raises(errors.ClientError):
        llm.judge("prompt")

    assert client.models.generate_content.call_count == 1, "a non-retryable error must not be retried"


def test_fallback_provider_not_registered_is_skipped_gracefully(monkeypatch):
    """FALLBACK_PROVIDER pointing at a provider app/llm.py doesn't know
    about yet (e.g. "groq", before tonight's addition) must not crash --
    it's just skipped, and the primary's own ModelUnavailable surfaces."""
    monkeypatch.setattr(config, "FALLBACK_PROVIDER", "groq")
    overloaded = errors.ServerError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
    client = _client_returning(generate_content=[overloaded] * (config.MODEL_MAX_RETRIES + 1))

    with patch("app.llm._gemini_client", return_value=client), pytest.raises(ModelUnavailable):
        llm.judge("prompt")


def test_cache_miss_result_is_not_lost_when_second_text_in_batch_fails():
    """embed_many's cache-populate step only runs after a successful
    batched call -- if the call itself raises, nothing is partially
    cached under a wrong value."""
    overloaded = errors.ServerError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
    client = _client_returning(embed_content=[overloaded] * (config.MODEL_MAX_RETRIES + 1))

    with patch("app.llm._gemini_client", return_value=client), pytest.raises(ModelUnavailable):
        llm.embed_many(["a", "b"])

    assert storage.get_cached_llm_value(llm._cache_key(config.EMBEDDING_MODEL, "a")) is None
    assert storage.get_cached_llm_value(llm._cache_key(config.EMBEDDING_MODEL, "b")) is None
