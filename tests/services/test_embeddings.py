"""Embedding provider contract tests — the abstraction's hard gate.

The Gemini adapter's wire behavior is pinned exactly as it was before the
abstraction (endpoint, payload shape, auth header, per-site retry
policies, dim, shape validation) — these tests ARE the statement that
"Gemini adapter behavior is unchanged". The HTTP call is mocked
(`httpx.post` / `httpx.AsyncClient`), so no network and no API key beyond
a dummy.

Also pins the factory switch: `AVA_EMBEDDING_BACKEND` dispatch, unknown
values fail fast, and the provider declares the vector space (`dim` +
`fingerprint`) the storage layer consumes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import numpy as np
import pytest

from services.memory_indexer.embeddings import factory
from services.memory_indexer.embeddings.base import EmbeddingAPIError
from services.memory_indexer.embeddings.gemini import (
    _EMBED_POLICY,
    _ENDPOINT,
    _MODEL_ID,
    _QUERY_EMBED_POLICY,
    DIM,
    GeminiEmbeddingProvider,
)


def _provider() -> GeminiEmbeddingProvider:
    return GeminiEmbeddingProvider()


class _FakeResponse:
    """Minimal stand-in for httpx.Response: raise_for_status + json."""

    def __init__(self, payload: dict[str, Any], *, status_ok: bool = True) -> None:
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self) -> None:
        if not self._status_ok:
            raise httpx.HTTPStatusError(
                "500 Server Error",
                request=httpx.Request("POST", _ENDPOINT),
                response=httpx.Response(500),
            )

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakePost:
    """Records each httpx.post call; returns embeddings or raises (in call
    order) to simulate transient failures. `vectors` are the per-text
    embedding rows the batch response carries back."""

    def __init__(
        self,
        vectors: list[list[float]] | None = None,
        *,
        raises_times: int = 0,
        status_ok: bool = True,
        prompt_token_count: int | None = None,
    ) -> None:
        self._vectors = vectors or []
        self._raises_remaining = raises_times
        self._status_ok = status_ok
        self._prompt_token_count = prompt_token_count
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> _FakeResponse:
        self.call_count += 1
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if self._raises_remaining > 0:
            self._raises_remaining -= 1
            raise httpx.ConnectError("simulated network failure")
        embeddings = [{"values": v} for v in self._vectors]
        body: dict[str, Any] = {"embeddings": embeddings}
        if self._prompt_token_count is not None:
            body["usageMetadata"] = {"promptTokenCount": self._prompt_token_count}
        return _FakeResponse(body, status_ok=self._status_ok)


def _patch_post(monkeypatch: pytest.MonkeyPatch, fake: _FakePost) -> None:
    monkeypatch.setattr(httpx, "post", fake)


class _RecordedSpan:
    def __init__(self, name: str, start_time: int | None) -> None:
        self.name = name
        self.start_time = start_time
        self.attributes: dict[str, Any] = {}
        self.ended = False

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def end(self) -> None:
        self.ended = True


class _RecordingTracer:
    def __init__(self) -> None:
        self.spans: list[_RecordedSpan] = []

    def start_span(self, name: str, *, start_time: int | None = None) -> _RecordedSpan:
        span = _RecordedSpan(name, start_time)
        self.spans.append(span)
        return span


def _enable_tracing(monkeypatch: pytest.MonkeyPatch) -> _RecordingTracer:
    from shared import trace as trace_mod

    tracer = _RecordingTracer()
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setitem(trace_mod._state, "initialized", True)
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]
    return tracer


def _embedding_cost(tok_in: int) -> float:
    """Cost of an embedding call under the catalog's gemini-embedding-2 entry.

    Text input $0.20/1M tokens; embeddings have no output and no cache, so
    the billed cost is tok_in × 0.20 / 1M, rounded the way emit_billing_event
    rounds it (6 decimals)."""
    from shared.lm.pricing import quote

    priced = quote(_MODEL_ID, tok_in, 0, 0)
    assert priced is not None  # gemini-embedding-2 is registered in the catalog
    return priced.cost_usd


def _assert_priced_embedding_span(tracer: _RecordingTracer, *, tok_in: int) -> None:
    assert len(tracer.spans) == 1
    span = tracer.spans[0]
    assert span.name == "ava.billing.call"
    assert span.ended is True
    assert span.attributes["ava.billing.vendor"] == "google"
    assert span.attributes["ava.billing.model"] == _MODEL_ID
    assert span.attributes["ava.billing.tokens_in"] == tok_in
    assert span.attributes["ava.billing.tokens_out"] == 0
    assert span.attributes["ava.billing.usage_kind"] == "embedding"
    assert span.attributes["ava.billing.cost"] == round(_embedding_cost(tok_in), 6)
    assert "ava.billing.unpriced" not in span.attributes


def _assert_unpriced_embedding_span(tracer: _RecordingTracer, *, tok_in: int) -> None:
    """Pins the unpriced fallback path for a model absent from the catalog."""
    # Read the module attribute, not the test file's import snapshot, so a
    # monkeypatched _MODEL_ID (the unknown-model case) is what we assert on.
    from services.memory_indexer.embeddings import gemini

    assert len(tracer.spans) == 1
    span = tracer.spans[0]
    assert span.name == "ava.billing.call"
    assert span.ended is True
    assert span.attributes["ava.billing.vendor"] == "google"
    assert span.attributes["ava.billing.model"] == gemini._MODEL_ID
    assert span.attributes["ava.billing.tokens_in"] == tok_in
    assert span.attributes["ava.billing.usage_kind"] == "embedding"
    assert span.attributes["ava.billing.cost"] == 0.0
    assert span.attributes["ava.billing.unpriced"] is True


@pytest.fixture(autouse=True)
def _dummy_gemini_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a key so `_api_key()` does not short-circuit the provider.

    These tests mock the HTTP call (`httpx.post`), not auth — the key
    check runs before it, so without a key every success-path test would
    raise `EmbeddingAPIError` instead of exercising the request. CI has no
    GEMINI_API_KEY; running locally the prod `.env` leaked one in and hid
    the dependency. `test_embed_no_api_key_raises` overrides this back to
    None to keep the missing-key branch a tested behavior.
    """
    from pydantic import SecretStr

    from shared.config import settings

    monkeypatch.setattr(settings.lm, "gemini_api_key", SecretStr("test-gemini-key"))


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize shared.resilience backoff sleeps so retry-path tests do
    not hang; the retry loop itself is still exercised (call counts). The
    provider's policy is a module constant (R2-D), no longer settings-driven.
    `_asleep` must be a REAL coroutine function: `aretry` awaits it, so a sync
    lambda turns every async retry into `TypeError: object NoneType can't be
    used in 'await' expression`."""
    monkeypatch.setattr("shared.resilience._sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]

    async def _no_asleep(_s: float) -> None:
        return None

    monkeypatch.setattr("shared.resilience._asleep", _no_asleep)


# ── provider surface (the contract) ───────────────────────────────────────


def test_provider_declares_vector_space() -> None:
    """dim + fingerprint are the provider's declaration of its vector space
    — the storage layer's schema width and the reconcile key."""
    provider = _provider()
    assert provider.name == "gemini"
    assert provider.dim == DIM == 3072
    assert provider.fingerprint == f"gemini:{_MODEL_ID}:dim=3072"
    assert provider.fingerprint.startswith("gemini:")


def test_embed_batch_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePost(vectors=[[1.0] * DIM, [2.0] * DIM])
    _patch_post(monkeypatch, fake)
    result = _provider().embed_batch(["text1", "text2"])
    assert result.shape == (2, DIM)
    assert result.dtype == np.float32
    body = fake.calls[-1]["json"]
    assert [r["taskType"] for r in body["requests"]] == ["RETRIEVAL_DOCUMENT"] * 2


def test_embed_query_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePost(vectors=[[1.0] * DIM])
    _patch_post(monkeypatch, fake)
    result = _provider().embed_query("hello")
    assert result.shape == (DIM,)
    body = fake.calls[-1]["json"]
    assert body["requests"][0]["taskType"] == "RETRIEVAL_QUERY"


def test_embed_batch_emits_priced_billing_span(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    fake = _FakePost(vectors=[[1.0] * DIM], prompt_token_count=123)
    _patch_post(monkeypatch, fake)
    tracer = _enable_tracing(monkeypatch)

    _provider().embed_batch(["hello"])

    _assert_priced_embedding_span(tracer, tok_in=123)
    [record] = [record for record in loguru_records if record["extra"].get("event") == "llm_usage"]
    assert record["extra"]["usage_kind"] == "embedding"
    assert "unpriced" not in record["extra"]
    assert record["extra"]["cost_usd"] == _embedding_cost(123)


def test_embed_batch_unknown_model_emits_unpriced_billing_span(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    """Pins the unpriced fallback: a model absent from the pricing catalog
    still emits a billing span, flagged unpriced with cost 0 (task #2493 —
    gemini-embedding-2 entering the catalog flipped the other tests from this
    path to the priced one; keep one case proving the fallback survives)."""
    from services.memory_indexer.embeddings import gemini

    monkeypatch.setattr(gemini, "_MODEL_ID", "gemini-unknown-embedding-model")
    fake = _FakePost(vectors=[[1.0] * DIM], prompt_token_count=123)
    _patch_post(monkeypatch, fake)
    tracer = _enable_tracing(monkeypatch)

    _provider().embed_batch(["hello"])

    _assert_unpriced_embedding_span(tracer, tok_in=123)
    [record] = [record for record in loguru_records if record["extra"].get("event") == "llm_usage"]
    assert record["extra"]["usage_kind"] == "embedding"
    assert record["extra"]["unpriced"] == 1


def test_embed_batch_emits_priced_accounting_without_usage_metadata(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    fake = _FakePost(vectors=[[1.0] * DIM])
    _patch_post(monkeypatch, fake)
    tracer = _enable_tracing(monkeypatch)

    _provider().embed_batch(["hello"])

    _assert_priced_embedding_span(tracer, tok_in=0)
    [record] = [record for record in loguru_records if record["extra"].get("event") == "llm_usage"]
    assert record["extra"]["usage_kind"] == "embedding"
    assert "unpriced" not in record["extra"]
    assert record["extra"]["cost_usd"] == 0.0


def test_embed_query_async_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The async path uses `httpx.AsyncClient` (not the sync `httpx.post`),
    so it needs its own client fake — the wire contract it produces is the
    same payload as the sync query path."""

    class _OkAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _OkAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(
            self, url: str, *, json: dict[str, Any], headers: dict[str, str]
        ) -> _FakeResponse:
            return _FakeResponse(
                {"embeddings": [{"values": [1.0] * DIM} for _ in json["requests"]]}
            )

    calls: list[dict[str, Any]] = []

    class _RecordingClient(_OkAsyncClient):
        async def post(
            self, url: str, *, json: dict[str, Any], headers: dict[str, str]
        ) -> _FakeResponse:
            calls.append({"url": url, "json": json, "headers": headers})
            return await _OkAsyncClient.post(self, url, json=json, headers=headers)

    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    result = asyncio.run(_provider().embed_query_async("hello"))
    assert result.shape == (DIM,)
    body = calls[-1]["json"]
    assert calls[-1]["url"] == _ENDPOINT
    assert calls[-1]["headers"]["x-goog-api-key"]
    assert body["requests"][0]["taskType"] == "RETRIEVAL_QUERY"
    assert body["requests"][0]["outputDimensionality"] == DIM


def test_embed_query_async_emits_priced_billing_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _AsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _AsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(
            self, url: str, *, json: dict[str, Any], headers: dict[str, str]
        ) -> _FakeResponse:
            return _FakeResponse(
                {
                    "embeddings": [{"values": [1.0] * DIM} for _ in json["requests"]],
                    "usageMetadata": {"promptTokenCount": 123},
                }
            )

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    tracer = _enable_tracing(monkeypatch)

    result = asyncio.run(_provider().embed_query_async("hello"))

    assert result.shape == (DIM,)
    _assert_priced_embedding_span(tracer, tok_in=123)


def test_embed_request_payload_and_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire contract: endpoint, api-key header, per-text request shape."""
    fake = _FakePost(vectors=[[1.0] * DIM])
    _patch_post(monkeypatch, fake)
    _provider().embed_batch(["hello world"])
    call = fake.calls[-1]
    assert call["url"] == _ENDPOINT
    assert call["headers"]["x-goog-api-key"]  # non-empty key forwarded
    req = call["json"]["requests"][0]
    assert req["model"] == f"models/{_MODEL_ID}"
    assert req["outputDimensionality"] == DIM
    assert req["content"]["parts"][0]["text"] == "hello world"


def test_embed_batch_empty_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePost(vectors=[])
    _patch_post(monkeypatch, fake)
    tracer = _enable_tracing(monkeypatch)
    result = _provider().embed_batch([])
    assert result.shape == (0, DIM)
    assert fake.call_count == 0
    assert tracer.spans == []


def test_embed_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    fake = _FakePost(vectors=[[1.0] * DIM], raises_times=2)
    _patch_post(monkeypatch, fake)
    tracer = _enable_tracing(monkeypatch)
    result = _provider().embed_batch(["hello"])
    assert result.shape == (1, DIM)
    assert fake.call_count == 3
    _assert_priced_embedding_span(tracer, tok_in=0)
    [record] = [record for record in loguru_records if record["extra"].get("event") == "llm_usage"]
    assert record["extra"]["usage_kind"] == "embedding"
    assert "unpriced" not in record["extra"]
    assert record["extra"]["cost_usd"] == 0.0


def test_embed_raises_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePost(vectors=[[1.0] * DIM], raises_times=100)
    _patch_post(monkeypatch, fake)
    tracer = _enable_tracing(monkeypatch)
    with pytest.raises(EmbeddingAPIError, match="failed after"):
        _provider().embed_batch(["hello"])
    assert tracer.spans == []  # a failed call emits no billing span


def test_embed_survives_billing_emit_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A billing-emit exception is swallowed — the embed call still completes
    (module contract: billing can never affect the call it observes)."""
    fake = _FakePost(vectors=[[1.0] * DIM], prompt_token_count=123)
    _patch_post(monkeypatch, fake)
    _enable_tracing(monkeypatch)

    def _boom(**kwargs: object) -> None:
        raise RuntimeError("billing exploded")

    monkeypatch.setattr("shared.lm.billing.emit_billing_event", _boom)

    result = _provider().embed_batch(["hello"])

    assert result.shape == (1, DIM)


def test_embed_http_error_status_retries_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-2xx (raise_for_status) is a retryable failure; exhausting retries raises."""
    fake = _FakePost(vectors=[[1.0] * DIM], status_ok=False)
    _patch_post(monkeypatch, fake)
    with pytest.raises(EmbeddingAPIError, match="failed after"):
        _provider().embed_query("hello")

    assert fake.call_count == _QUERY_EMBED_POLICY.max_attempts


def test_query_embed_policy_is_lighter_than_document_policy() -> None:
    """Query embeds and indexer document embeds answer to different masters
    (task #2003/B): a search query sits inside the gateway's own search
    deadline, so the indexer's 4-attempt schedule (1->2->4->8s) could spend
    the whole budget retrying
    a 429 the caller (passive recall / an explicit ava.memory.search) would
    simply watch expire — and the caller retries the *search*, not the embed."""
    assert _QUERY_EMBED_POLICY.max_attempts < _EMBED_POLICY.max_attempts


def test_embed_query_async_uses_the_query_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway's async query embed retries on the lighter query schedule
    (2 attempts), not the indexer's 4: a 429 during a fleet wake must not burn
    the search deadline on retries."""

    class _FailingClient:
        """AsyncClient stand-in whose POST always raises a transient error.

        A class-level counter: the client instance is constructed inside
        `embed_query_async`, so the test cannot reach the instance — and a
        class (not a lambda) keeps pyright happy about `AsyncClient`'s type.
        """

        post_calls: int = 0

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FailingClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> None:
            _FailingClient.post_calls += 1
            raise httpx.ConnectError("simulated network failure")

    monkeypatch.setattr(httpx, "AsyncClient", _FailingClient)
    with pytest.raises(EmbeddingAPIError, match="failed after 2 attempts"):
        asyncio.run(_provider().embed_query_async("hello"))
    assert _FailingClient.post_calls == _QUERY_EMBED_POLICY.max_attempts


def test_embed_4xx_fails_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deterministic 4xx is permanent: one attempt, then EmbeddingAPIError
    (R2-D classify; the pre-R2 loop wasted its whole budget retrying 400/403 —
    audit 06 Q4)."""

    class _FourHundred:
        def raise_for_status(self) -> None:
            raise httpx.HTTPStatusError(
                "400 Bad Request",
                request=httpx.Request("POST", _ENDPOINT),
                response=httpx.Response(400),
            )

        def json(
            self,
        ) -> dict:  # pragma: no cover — never reached
            return {}

    calls: list[str] = []

    def _post(url: str, *, json: dict, headers: dict, timeout: float) -> _FourHundred:
        calls.append(url)
        return _FourHundred()

    monkeypatch.setattr(httpx, "post", _post)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(EmbeddingAPIError, match="failed after"):
        _provider().embed_query("hello")
    assert len(calls) == 1  # 4xx → permanent → single attempt


def test_embed_timeout_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-request timeout comes from config (AVA_EMBED_TIMEOUT_SECONDS,
    task #698 G8); the retry policy is a module constant (R2-D)."""
    from shared.config import settings

    monkeypatch.setattr(settings.services, "memory_embed_timeout_seconds", 12.5)
    fake = _FakePost(vectors=[[1.0] * DIM], raises_times=1)
    _patch_post(monkeypatch, fake)

    _provider().embed_batch(["hello"])

    assert fake.call_count == 2  # 1 failure + 1 retry
    for call in fake.calls:
        assert call["timeout"] == 12.5


def test_embed_no_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing key -> EmbeddingAPIError with actionable guidance, before any POST.

    Overrides the autouse dummy key back to None so this branch stays a
    tested behavior even though every other provider test injects a key.
    """
    from shared.config import settings

    monkeypatch.setattr(settings.lm, "gemini_api_key", None)
    # No key must mean no network attempt — this makes that observable.
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *_a, **_k: pytest.fail("must not POST without an API key"),  # pyright: ignore[reportUnknownArgumentType]
    )
    with pytest.raises(EmbeddingAPIError) as exc_info:
        _provider().embed_query("hello")
    message = str(exc_info.value)
    assert "GEMINI_API_KEY" in message
    assert ".env" in message  # actionable: points the operator at where to set it


def test_embed_async_client_construction_failure_wraps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AsyncClient construction errors wrap into EmbeddingAPIError (#971).

    The client is built outside the retry loop; if construction itself
    fails (bad timeout config, transport setup), the module contract
    still holds: only EmbeddingAPIError escapes.
    """

    class _Boom:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise httpx.ConnectError("client init failed")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    with pytest.raises(EmbeddingAPIError, match="client init failed"):
        asyncio.run(_provider().embed_query_async("hello"))


def test_embed_response_shape_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePost(vectors=[[1.0] * DIM] * 3)
    _patch_post(monkeypatch, fake)
    with pytest.raises(EmbeddingAPIError, match="unexpected shape"):
        _provider().embed_batch(["a", "b"])


def test_embed_malformed_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Embedding entries missing `values` -> EmbeddingAPIError, not a crash."""

    def bad_post(url: str, *, json: Any, headers: Any, timeout: float) -> _FakeResponse:
        return _FakeResponse({"embeddings": [{"nope": []}]})

    monkeypatch.setattr(httpx, "post", bad_post)
    with pytest.raises(EmbeddingAPIError, match="malformed"):
        _provider().embed_query("hello")


# ── factory switch ────────────────────────────────────────────────────────


def test_factory_default_is_gemini() -> None:
    """The unset switch yields the Gemini provider — behavior unchanged."""
    from shared.config import settings

    assert settings.services.embedding_backend == "gemini"
    assert isinstance(factory.get_provider(), GeminiEmbeddingProvider)


def test_factory_unknown_backend_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognized AVA_EMBEDDING_BACKEND must not silently fall back to
    gemini — a typo would keep the old provider while the operator believes
    the switch happened."""
    from shared.config import settings

    monkeypatch.setattr(settings.services, "embedding_backend", "openai")
    with pytest.raises(ValueError, match="unknown embedding provider"):
        factory.get_provider()


def test_factory_provider_named_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_EMBEDDING_BACKEND=gemini yields the Gemini adapter."""
    from shared.config import settings

    monkeypatch.setattr(settings.services, "embedding_backend", "gemini")
    assert isinstance(factory.get_provider_named("gemini"), GeminiEmbeddingProvider)
