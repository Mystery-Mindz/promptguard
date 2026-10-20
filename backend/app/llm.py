"""The provider layer: the ONLY place in this codebase that calls a model
API. `agent.py` and `drift_engine.py` never import `google.genai` (or any
other provider SDK) directly — they only call the three functions exported
here (`generate_with_tools`, `judge`, `embed_many`), using normalized,
provider-agnostic types (`Message`, `ToolCall`, `ToolResult`,
`ToolDeclaration`, `GenerateResult`).

Today only one provider ("gemini") is registered in `_PROVIDERS` below.
Adding a second provider (planned for tonight) means: writing its own
`_<name>_generate_with_tools` / `_<name>_judge` / `_<name>_embed_many` /
`_<name>_is_retryable` functions, registering them as a new `_Provider` in
`_PROVIDERS`, adding its `(provider, role)` entries to `config.MODEL_NAMES`
and (if its rate limits differ) `config.PACING_SECONDS`, and pointing
`AGENT_PROVIDER`/`JUDGE_PROVIDER`/`EMBEDDING_PROVIDER`/`FALLBACK_PROVIDER`
at it. None of that changes `generate_with_tools`/`judge`/`embed_many`'s
signatures or anything that calls them.

Retries: a 429 ("quota exceeded") or 503 ("overloaded") is treated as
retryable — retried with exponential backoff up to
`config.MODEL_MAX_RETRIES` times. If the primary provider never succeeds,
and `config.FALLBACK_PROVIDER` is set AND registered in `_PROVIDERS`, the
whole call (its own fresh set of retries included) is tried once more on
that provider. If nothing works, `ModelUnavailable` is raised —
`main.py` maps this to a clean HTTP 503.

Caching: `judge()` and `embed_many()` go through an in-memory LRU
(`_lru`) backed by a DB table (`storage.llm_cache`), keyed on
`sha256(model + "\\x00" + text)`. `generate_with_tools()` is never cached
(a multi-turn conversation is never identical twice). For `judge()`, "text"
deliberately means the semantically-stable content being judged — the
caller passes it as `cache_key`, separately from the actual `prompt` text
sent to the model, because `drift_engine.py` wraps that content in a fresh
random anti-injection boundary tag on every call (BUG-07): caching by the
literal (randomized) prompt would never hit twice for the same content.
Caching by the stable `cache_key` instead means repeat analysis of the same
content is a real cache hit with no model call at all, while a cache MISS
still gets the full random-boundary treatment, so BUG-07's protection is
unaffected either way.
"""

import functools
import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from google import genai
from google.genai import errors, types
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from app import config, storage

T = TypeVar("T")

Role = Literal["agent", "judge", "embedding"]


class ModelUnavailable(Exception):
    """Raised when a model call fails even after retries on the primary
    provider, and (if `config.FALLBACK_PROVIDER` is set and registered)
    on the fallback provider too. `main.py` catches this specifically and
    maps it to a clean HTTP 503, instead of a raw 500 or a confusing
    provider-specific traceback reaching the client."""


# --- Normalized, provider-agnostic conversation types -----------------------


@dataclass(frozen=True)
class ToolDeclaration:
    """One tool the model may call. `parameters` is a plain JSON-schema-like
    dict (`{"type": "object", "properties": {...}, "required": [...]}`) —
    provider-agnostic; each provider backend translates it into whatever
    shape its own SDK wants (see `_json_schema_to_gemini_schema`)."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """One tool call the model is requesting."""

    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """The result of actually running one `ToolCall`, to be reported back
    to the model. `output` should be JSON-serializable; a dict is sent as
    the response as-is, anything else is wrapped as `{"result": output}`
    (matching how Gemini's `FunctionResponse.response` must be a dict)."""

    call_id: str
    name: str
    output: Any


@dataclass(frozen=True)
class Message:
    """One turn of a tool-calling conversation, in a provider-agnostic
    shape. A `"user"` message carries either plain `text` (e.g. the
    original goal) or `tool_results` (reporting back what running some
    tool calls produced) — never both. A `"model"` message carries the
    model's own turn: optional `text` plus zero or more `tool_calls` it is
    requesting."""

    role: Literal["user", "model"]
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    tool_results: tuple[ToolResult, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GenerateResult:
    """The normalized result of one `generate_with_tools` call: the
    model's own text (if any) and the tool calls it's requesting (if
    any)."""

    text: str | None
    tool_calls: list[ToolCall]


# --- Provider registry --------------------------------------------------


@dataclass(frozen=True)
class _Provider:
    name: str
    generate_with_tools: Callable[[str, list[Message], list[ToolDeclaration], str | None], GenerateResult]
    judge: Callable[[str, str], str]
    embed_many: Callable[[str, list[str]], list[list[float]]]
    is_retryable: Callable[[BaseException], bool]


# --- Gemini backend -------------------------------------------------------

_gemini_client_instance: genai.Client | None = None


def _gemini_client() -> genai.Client:
    """Returns a shared Gemini API client, creating it the first time this
    is called and reusing it afterward (so the app doesn't open a new
    connection per request). The only place `genai.Client` is constructed
    anywhere in this codebase."""
    global _gemini_client_instance
    if _gemini_client_instance is None:
        _gemini_client_instance = genai.Client(
            api_key=config.GEMINI_API_KEY,
            http_options=types.HttpOptions(timeout=config.GEMINI_CLIENT_TIMEOUT_MS),
        )
    return _gemini_client_instance


_JSON_TYPE_TO_GEMINI = {
    "object": types.Type.OBJECT,
    "string": types.Type.STRING,
    "number": types.Type.NUMBER,
    "integer": types.Type.INTEGER,
    "boolean": types.Type.BOOLEAN,
    "array": types.Type.ARRAY,
}


def _json_schema_to_gemini_schema(schema: dict[str, Any]) -> types.Schema:
    """Converts a provider-agnostic JSON-schema-like dict (as used in
    `ToolDeclaration.parameters`) into Gemini's typed `types.Schema`,
    recursively for nested `object`/`array` schemas."""
    kwargs: dict[str, Any] = {"type": _JSON_TYPE_TO_GEMINI[schema["type"]]}
    if "description" in schema:
        kwargs["description"] = schema["description"]
    if schema["type"] == "object":
        kwargs["properties"] = {
            name: _json_schema_to_gemini_schema(sub_schema) for name, sub_schema in schema.get("properties", {}).items()
        }
        if "required" in schema:
            kwargs["required"] = schema["required"]
    if schema["type"] == "array" and "items" in schema:
        kwargs["items"] = _json_schema_to_gemini_schema(schema["items"])
    return types.Schema(**kwargs)


def _message_to_gemini_content(message: Message) -> types.Content:
    """Converts one normalized `Message` into a Gemini `types.Content`.
    Stateless and rebuilt fresh from the full `messages` history on every
    `generate_with_tools` call — the conversation state lives in the
    caller's `list[Message]`, not in any persistent provider-side object."""
    if message.tool_results:
        parts = [
            types.Part(
                function_response=types.FunctionResponse(
                    id=result.call_id,
                    name=result.name,
                    response=result.output if isinstance(result.output, dict) else {"result": result.output},
                )
            )
            for result in message.tool_results
        ]
        return types.Content(role="user", parts=parts)

    parts = []
    if message.text:
        parts.append(types.Part(text=message.text))
    for call in message.tool_calls:
        parts.append(types.Part(function_call=types.FunctionCall(id=call.id, name=call.name, args=call.args)))
    return types.Content(role=message.role, parts=parts)


def _gemini_generate_with_tools(
    model: str, messages: list[Message], tools: list[ToolDeclaration], system_instruction: str | None
) -> GenerateResult:
    client = _gemini_client()
    contents = [_message_to_gemini_content(m) for m in messages]

    config_kwargs: dict[str, Any] = {}
    if tools:
        config_kwargs["tools"] = [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=t.name,
                        description=t.description,
                        parameters=_json_schema_to_gemini_schema(t.parameters),
                    )
                    for t in tools
                ]
            )
        ]
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction

    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(**config_kwargs),
    )
    tool_calls = [
        ToolCall(id=call.id or "", name=call.name or "", args=dict(call.args or {}))
        for call in (response.function_calls or [])
    ]
    return GenerateResult(text=response.text, tool_calls=tool_calls)


def _gemini_judge(model: str, prompt: str) -> str:
    client = _gemini_client()
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0),
    )
    return response.text or ""


def _gemini_embed_many(model: str, texts: list[str]) -> list[list[float]]:
    client = _gemini_client()
    response = client.models.embed_content(model=model, contents=texts)
    assert response.embeddings, "embed_content returned no embeddings"
    return [list(embedding.values or []) for embedding in response.embeddings]


def _gemini_is_retryable(exc: BaseException) -> bool:
    """429 ("quota exceeded") and 503 ("overloaded") are transient —
    retryable with backoff. Anything else (bad request, auth failure,
    malformed response) is a real problem that retrying won't fix."""
    return isinstance(exc, errors.APIError) and exc.code in (429, 503)


_PROVIDERS: dict[str, _Provider] = {
    "gemini": _Provider(
        name="gemini",
        generate_with_tools=_gemini_generate_with_tools,
        judge=_gemini_judge,
        embed_many=_gemini_embed_many,
        is_retryable=_gemini_is_retryable,
    ),
}


# --- Role -> provider/model resolution --------------------------------------

_ROLE_PROVIDER: dict[Role, str] = {
    "agent": config.AGENT_PROVIDER,
    "judge": config.JUDGE_PROVIDER,
    "embedding": config.EMBEDDING_PROVIDER,
}


def _provider_for(role: Role) -> str:
    return _ROLE_PROVIDER[role]


def _model_for(provider: str, role: Role) -> str:
    try:
        return config.MODEL_NAMES[(provider, role)]
    except KeyError:
        raise ModelUnavailable(f"no model configured for provider={provider!r}, role={role!r}") from None


# --- Pacing + retry/backoff + fallback --------------------------------------

# Patched to a no-op in tests (see tests/conftest.py) so neither pacing nor
# retry backoff actually sleeps. Looked up by name at call time (not bound
# at import time), so patching `app.llm._sleep` takes effect immediately.
_sleep: Callable[[float], None] = time.sleep

_last_call_at: dict[tuple[str, str], float] = {}


def _pace_calls(provider: str, model: str) -> None:
    """Pauses (if needed) so consecutive calls to the same (provider,
    model) stay at least `config.PACING_SECONDS[provider]` seconds apart —
    keeps normal runs under a free-tier rate limit instead of failing into
    repeated 429s."""
    key = (provider, model)
    delay = config.PACING_SECONDS.get(provider, config.DEFAULT_PACING_SECONDS)
    elapsed = time.monotonic() - _last_call_at.get(key, 0.0)
    if elapsed < delay:
        _sleep(delay - elapsed)
    _last_call_at[key] = time.monotonic()


def _call_with_retries(provider_def: _Provider, role: Role, fn: Callable[[], T]) -> T:
    """Calls `fn()`, retrying on `provider_def.is_retryable` errors with
    exponential backoff up to `config.MODEL_MAX_RETRIES` times. Raises
    `ModelUnavailable` (chained from the real error) if every attempt was
    retryable and still failed; a non-retryable error propagates as-is
    immediately, on the first attempt."""
    retrying = Retrying(
        retry=retry_if_exception(provider_def.is_retryable),
        stop=stop_after_attempt(config.MODEL_MAX_RETRIES + 1),
        wait=wait_exponential(
            multiplier=config.MODEL_RETRY_BASE_DELAY_SECONDS,
            max=config.MODEL_RETRY_MAX_DELAY_SECONDS,
        ),
        sleep=_sleep,
        reraise=True,
    )
    try:
        return retrying(fn)
    except Exception as e:
        if provider_def.is_retryable(e):
            raise ModelUnavailable(
                f"{provider_def.name} unavailable for {role} after {config.MODEL_MAX_RETRIES + 1} attempt(s): {e}"
            ) from e
        raise


def _with_fallback(role: Role, call: Callable[[str, str], T]) -> tuple[T, str, str]:
    """Tries `call(provider, model)` on the role's configured primary
    provider (with retries); if that raises `ModelUnavailable` and
    `config.FALLBACK_PROVIDER` is set and registered in `_PROVIDERS`,
    tries it once more on the fallback (with its own fresh retries).
    Raises `ModelUnavailable` if nothing worked. Returns `(result,
    provider_name, model)` — the caller needs to know which provider/model
    actually produced the result to cache it correctly (see `judge`/
    `embed_many` below).
    """
    primary = _provider_for(role)
    candidates = [primary]
    fallback = config.FALLBACK_PROVIDER
    if fallback and fallback != primary and fallback in _PROVIDERS:
        candidates.append(fallback)

    last_error: ModelUnavailable | None = None
    for provider_name in candidates:
        provider_def = _PROVIDERS.get(provider_name)
        if provider_def is None:
            continue
        model = _model_for(provider_name, role)
        _pace_calls(provider_name, model)
        try:
            result = _call_with_retries(provider_def, role, functools.partial(call, provider_name, model))
            return result, provider_name, model
        except ModelUnavailable as e:
            last_error = e
            continue

    raise last_error or ModelUnavailable(f"no model provider registered for role={role!r} (configured: {primary!r})")


# --- Cache (in-memory LRU + DB table, storage.llm_cache) --------------------


class _LRUCache:
    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._data: OrderedDict[str, Any] = OrderedDict()

    def get(self, key: str) -> Any | None:
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        if len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


_lru = _LRUCache(config.LLM_CACHE_MAX_ENTRIES)


def _cache_key(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}\x00{text}".encode()).hexdigest()


def _cache_get(model: str, text: str) -> Any | None:
    key = _cache_key(model, text)
    hit = _lru.get(key)
    if hit is not None:
        return hit
    hit = storage.get_cached_llm_value(key)
    if hit is not None:
        _lru.set(key, hit)
    return hit


def _cache_set(model: str, text: str, value: Any) -> None:
    key = _cache_key(model, text)
    _lru.set(key, value)
    storage.set_cached_llm_value(key, model, value)


def clear_cache() -> None:
    """Clears the in-memory LRU only. Used by test fixtures for isolation —
    the DB-backed half of the cache already resets naturally since tests
    point `config.DATABASE_PATH` at a fresh temp file each time; the LRU is
    a plain module-level global that would otherwise leak a cached value
    (and its mock-counted call) across tests."""
    _lru.clear()


# --- Public API --------------------------------------------------------


def generate_with_tools(
    messages: list[Message], tools: list[ToolDeclaration], *, system_instruction: str | None = None
) -> GenerateResult:
    """Runs one turn of a tool-calling conversation. `messages` is the
    full conversation so far (stateless on this end — the caller owns and
    grows this list across turns). Never cached: a multi-turn conversation
    is never identical twice. Raises `ModelUnavailable` if the configured
    provider (and fallback, if any) can't complete the call."""
    result, _, _ = _with_fallback(
        "agent",
        lambda provider, model: _PROVIDERS[provider].generate_with_tools(model, messages, tools, system_instruction),
    )
    return result


def judge(prompt: str, *, cache_key: str | None = None) -> str:
    """Sends `prompt` to the judge model and returns its raw text reply
    (parsing it into a score/decision is the caller's job, since that's
    domain logic, not transport logic).

    Pass `cache_key` as the STABLE text being judged (not `prompt` itself)
    to enable caching — see this module's docstring for why `prompt` and
    `cache_key` are deliberately different strings. Without `cache_key`,
    every call hits the model with no caching at all.
    """
    if cache_key is not None:
        primary_model = _model_for(_provider_for("judge"), "judge")
        cached = _cache_get(primary_model, cache_key)
        if cached is not None:
            return cached

    result, _, model = _with_fallback("judge", lambda provider, model: _PROVIDERS[provider].judge(model, prompt))

    if cache_key is not None:
        _cache_set(model, cache_key, result)
    return result


def embed_many(texts: list[str]) -> list[list[float]]:
    """Embeds every text in `texts` in as few provider calls as possible:
    cache hits are served with no model call at all, and every cache MISS
    is sent together in exactly ONE batched call to the provider (not one
    call per text) — this is what lets `drift_engine.py` embed a trace's
    goal plus every step's text in a single call. Returns vectors in the
    same order as `texts`.
    """
    if not texts:
        return []

    primary_model = _model_for(_provider_for("embedding"), "embedding")

    results: list[list[float] | None] = []
    miss_indices: list[int] = []
    miss_texts: list[str] = []
    for i, text in enumerate(texts):
        cached = _cache_get(primary_model, text)
        results.append(cached)
        if cached is None:
            miss_indices.append(i)
            miss_texts.append(text)

    if miss_texts:
        computed, _, model = _with_fallback(
            "embedding", lambda provider, model: _PROVIDERS[provider].embed_many(model, miss_texts)
        )
        for idx, text, vector in zip(miss_indices, miss_texts, computed, strict=True):
            results[idx] = vector
            _cache_set(model, text, vector)

    return results  # type: ignore[return-value]  # every slot is filled by now
