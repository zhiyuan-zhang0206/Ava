"""Embedding provider contract tests — the abstraction's hard gate.

The Gemini adapter's wire behavior is pinned exactly as it was before the
abstraction (endpoint, payload shape, auth header, per-site retry
policies, dim, shape validation) — these tests ARE the statement that
"Gemini adapter behavior is unchanged". The HTTP call is mocked
(`httpx.AsyncClient`), with a dummy API key. Deadline guards additionally
use a local trickling HTTP server.

Also pins the factory switch: `AVA_EMBEDDING_BACKEND` dispatch, unknown
values fail fast, and the provider declares the vector space (`dim` +
`fingerprint`) the storage layer consumes.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import socket
import threading
import time
from collections.abc import AsyncGenerator, Generator, Iterator
from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from typing import Any

import httpx
import numpy as np
import pytest

from services.memory_indexer.embeddings import factory, gemini
from services.memory_indexer.embeddings.base import EmbeddingAPIError
from services.memory_indexer.embeddings.gemini import (
    _EMBED_POLICY,
    _ENDPOINT,
    _MODEL_ID,
    _QUERY_EMBED_POLICY,
    DIM,
    GeminiEmbeddingProvider,
)
from shared.config import settings
from shared.lm._plugin_providers import ensure_provider_plugins_loaded
from shared.resilience import ExponentialBackoff, Policy


def _provider() -> GeminiEmbeddingProvider:
    return GeminiEmbeddingProvider()


class _FakeResponse(httpx.Response):
    """Buffered response with the real HTTPX status contract."""

    def __init__(self, payload: dict[str, Any], *, status_code: int = 200) -> None:
        super().__init__(
            status_code,
            json=payload,
            request=httpx.Request("POST", _ENDPOINT),
        )


class _AsyncClient:
    """Records attempts across client instances, including sync retry loops."""

    def __init__(
        self,
        vectors: list[list[float]] | None = None,
        *,
        raises_times: int = 0,
        status_code: int = 200,
        prompt_token_count: int | None = None,
        response_body: dict[str, Any] | None = None,
    ) -> None:
        self._vectors = vectors or []
        self._raises_remaining = raises_times
        self._status_code = status_code
        self._prompt_token_count = prompt_token_count
        self._response_body = response_body
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []
        self.timeout = 0.0

    def __call__(self, *, timeout: float) -> _AsyncClient:
        self.timeout = timeout
        return self

    async def __aenter__(self) -> _AsyncClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    @asynccontextmanager
    async def stream(
        self, method: str, url: str, *, json: dict[str, Any], headers: dict[str, str]
    ) -> AsyncGenerator[httpx.Response]:
        assert method == "POST"
        self.call_count += 1
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": self.timeout})
        if self._raises_remaining > 0:
            self._raises_remaining -= 1
            raise httpx.ConnectError("simulated network failure")
        body: dict[str, Any] = {"embeddings": [{"values": v} for v in self._vectors]}
        if self._prompt_token_count is not None:
            body["usageMetadata"] = {"promptTokenCount": self._prompt_token_count}
        if self._response_body is not None:
            body = self._response_body
        yield _FakeResponse(body, status_code=self._status_code)


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: _AsyncClient) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", fake)


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


@pytest.fixture(scope="module", autouse=True)
def _load_provider_plugins() -> None:
    """Load the provider plugins so `vendor_of_model` can resolve a vendor.

    The billing-span assertions need one: `_log_usage` skips span emission
    when `vendor_of_model` returns None, and a bare registry resolves
    nothing — so without this the file only passed when an earlier test
    module in the same worker happened to load the plugins (#4031).
    """
    ensure_provider_plugins_loaded()


@pytest.fixture(autouse=True)
def _dummy_gemini_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a key so `_api_key()` does not short-circuit the provider.

    These tests mock the HTTP call (`httpx.AsyncClient`), not auth — the key
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
    fake = _AsyncClient(vectors=[[1.0] * DIM, [2.0] * DIM])
    _patch_client(monkeypatch, fake)
    result = _provider().embed_batch(["text1", "text2"])
    assert result.shape == (2, DIM)
    assert result.dtype == np.float32
    body = fake.calls[-1]["json"]
    assert [r["taskType"] for r in body["requests"]] == ["RETRIEVAL_DOCUMENT"] * 2


def test_embed_query_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _AsyncClient(vectors=[[1.0] * DIM])
    _patch_client(monkeypatch, fake)
    result = _provider().embed_query("hello")
    assert result.shape == (DIM,)
    body = fake.calls[-1]["json"]
    assert body["requests"][0]["taskType"] == "RETRIEVAL_QUERY"


def test_embed_batch_emits_priced_billing_span(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    fake = _AsyncClient(vectors=[[1.0] * DIM], prompt_token_count=123)
    _patch_client(monkeypatch, fake)
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
    fake = _AsyncClient(vectors=[[1.0] * DIM], prompt_token_count=123)
    _patch_client(monkeypatch, fake)
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
    fake = _AsyncClient(vectors=[[1.0] * DIM])
    _patch_client(monkeypatch, fake)
    tracer = _enable_tracing(monkeypatch)

    _provider().embed_batch(["hello"])

    _assert_priced_embedding_span(tracer, tok_in=0)
    [record] = [record for record in loguru_records if record["extra"].get("event") == "llm_usage"]
    assert record["extra"]["usage_kind"] == "embedding"
    assert "unpriced" not in record["extra"]
    assert record["extra"]["cost_usd"] == 0.0


def test_embed_query_async_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Async queries use the same client seam and query payload as sync queries."""
    fake = _AsyncClient(vectors=[[1.0] * DIM])
    _patch_client(monkeypatch, fake)
    result = asyncio.run(_provider().embed_query_async("hello"))
    assert result.shape == (DIM,)
    call = fake.calls[-1]
    assert call["url"] == _ENDPOINT
    assert call["headers"]["x-goog-api-key"]
    body = call["json"]
    assert body["requests"][0]["taskType"] == "RETRIEVAL_QUERY"
    assert body["requests"][0]["outputDimensionality"] == DIM


def test_embed_query_async_emits_priced_billing_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _AsyncClient(vectors=[[1.0] * DIM], prompt_token_count=123)
    _patch_client(monkeypatch, fake)
    tracer = _enable_tracing(monkeypatch)

    result = asyncio.run(_provider().embed_query_async("hello"))

    assert result.shape == (DIM,)
    _assert_priced_embedding_span(tracer, tok_in=123)


def test_embed_request_payload_and_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire contract: endpoint, api-key header, per-text request shape."""
    fake = _AsyncClient(vectors=[[1.0] * DIM])
    _patch_client(monkeypatch, fake)
    _provider().embed_batch(["hello world"])
    call = fake.calls[-1]
    assert call["url"] == _ENDPOINT
    assert call["headers"]["x-goog-api-key"]  # non-empty key forwarded
    req = call["json"]["requests"][0]
    assert req["model"] == f"models/{_MODEL_ID}"
    assert req["outputDimensionality"] == DIM
    assert req["content"]["parts"][0]["text"] == "hello world"


def test_embed_batch_empty_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _AsyncClient(vectors=[])
    _patch_client(monkeypatch, fake)
    tracer = _enable_tracing(monkeypatch)
    result = _provider().embed_batch([])
    assert result.shape == (0, DIM)
    assert fake.call_count == 0
    assert tracer.spans == []


def test_embed_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    fake = _AsyncClient(vectors=[[1.0] * DIM], raises_times=2)
    _patch_client(monkeypatch, fake)
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
    fake = _AsyncClient(vectors=[[1.0] * DIM], raises_times=100)
    _patch_client(monkeypatch, fake)
    tracer = _enable_tracing(monkeypatch)
    with pytest.raises(EmbeddingAPIError, match="failed after"):
        _provider().embed_batch(["hello"])
    assert tracer.spans == []  # a failed call emits no billing span


def test_embed_survives_billing_emit_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A billing-emit exception is swallowed — the embed call still completes
    (module contract: billing can never affect the call it observes)."""
    fake = _AsyncClient(vectors=[[1.0] * DIM], prompt_token_count=123)
    _patch_client(monkeypatch, fake)
    _enable_tracing(monkeypatch)

    def _boom(**kwargs: object) -> None:
        raise RuntimeError("billing exploded")

    monkeypatch.setattr("shared.lm.billing.emit_billing_event", _boom)

    result = _provider().embed_batch(["hello"])

    assert result.shape == (1, DIM)


def test_embed_http_error_status_retries_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-2xx (raise_for_status) is a retryable failure; exhausting retries raises."""
    fake = _AsyncClient(vectors=[[1.0] * DIM], status_code=500)
    _patch_client(monkeypatch, fake)
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

    fake = _AsyncClient(raises_times=100)
    _patch_client(monkeypatch, fake)
    with pytest.raises(EmbeddingAPIError, match="failed after 2 attempts"):
        asyncio.run(_provider().embed_query_async("hello"))
    assert fake.call_count == _QUERY_EMBED_POLICY.max_attempts


def test_embed_4xx_fails_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deterministic 4xx is permanent: one attempt, then EmbeddingAPIError
    (R2-D classify; the pre-R2 loop wasted its whole budget retrying 400/403 —
    audit 06 Q4)."""

    fake = _AsyncClient(status_code=400)
    _patch_client(monkeypatch, fake)
    with pytest.raises(EmbeddingAPIError, match="Gemini embed HTTP 400"):
        _provider().embed_query("hello")
    assert fake.call_count == 1  # 4xx -> permanent -> single attempt


def test_embed_timeout_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-request timeout comes from config (AVA_EMBED_TIMEOUT_SECONDS,
    task #698 G8); the retry policy is a module constant (R2-D)."""
    from shared.config import settings

    monkeypatch.setattr(settings.services, "memory_embed_timeout_seconds", 12.5)
    fake = _AsyncClient(vectors=[[1.0] * DIM], raises_times=1)
    _patch_client(monkeypatch, fake)

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
        "AsyncClient",
        lambda *_a, **_k: pytest.fail("must not construct a client without an API key"),  # pyright: ignore[reportUnknownArgumentType]
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
    fake = _AsyncClient(vectors=[[1.0] * DIM] * 3)
    _patch_client(monkeypatch, fake)
    with pytest.raises(EmbeddingAPIError, match="unexpected shape"):
        _provider().embed_batch(["a", "b"])


def test_embed_malformed_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Embedding entries missing `values` -> EmbeddingAPIError, not a crash."""

    fake = _AsyncClient(response_body={"embeddings": [{"nope": []}]})
    _patch_client(monkeypatch, fake)
    with pytest.raises(EmbeddingAPIError, match="malformed"):
        _provider().embed_query("hello")


def test_sync_embed_rejects_running_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail with caller guidance before creating a coroutine or a client."""

    def unexpected_client(**kwargs: Any) -> None:
        pytest.fail("running-loop guard must precede client construction")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)

    async def invoke() -> None:
        with pytest.raises(RuntimeError, match="sync embedding provider API") as error:
            gemini._embed(["hello"], "RETRIEVAL_DOCUMENT")
        # EmbeddingAPIError is a RuntimeError; preserve the existing wrapper.
        assert isinstance(error.value, EmbeddingAPIError)
        assert type(error.value.__cause__) is RuntimeError
        message = str(error.value)
        assert "asyncio.to_thread or an executor" in message
        assert "embed_query_async" in message
        assert "batch embedding stays sync" in message

    asyncio.run(invoke())


# ── factory switch ────────────────────────────────────────────────────────


def test_worst_case_single_attempt_has_no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    from unittest.mock import Mock

    from services.memory_indexer.embeddings import gemini
    from shared.config import settings

    backoff = Mock(side_effect=AssertionError("one attempt must not evaluate backoff"))
    monkeypatch.setattr(
        gemini, "_EMBED_POLICY", replace(gemini._EMBED_POLICY, max_attempts=1, backoff=backoff)
    )
    assert gemini.worst_case_batch_seconds() == settings.services.memory_embed_timeout_seconds
    backoff.assert_not_called()


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


@contextmanager
def _embedding_server(
    *,
    compressed: bool = False,
    chunked: bool = False,
    drip: bool = True,
    framing: str | None = None,
    error_status: int | None = None,
) -> Generator[tuple[str, list[float]]]:
    """Drip body or HTTP framing while every read gap stays below the timeout."""
    if framing is not None:
        chunked = True
    started: list[float] = []
    stop = threading.Event()
    body = json.dumps({"embeddings": [{"values": [1.0] * DIM}]}).encode()
    if compressed:
        buffer = BytesIO()
        with gzip.GzipFile(filename="x" * 80, mode="wb", fileobj=buffer, mtime=0) as member:
            member.write(body)
        body = buffer.getvalue()
        # Raw reads cover the long gzip filename without producing decoded bytes.
        prefix_size, read_gap = 90, 0.025
    else:
        body = b" " * 40 + body
        prefix_size, read_gap = 40, 0.05
    chunks = (
        [body[i : i + 1] for i in range(prefix_size)] + [body[prefix_size:]]
        if drip
        else [body[i : i + 11] for i in range(0, len(body), 11)]
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            started.append(time.monotonic())
            if error_status is not None and len(started) == 1:
                self.send_response(error_status)
                self.send_header("Content-Length", "40")
                self.send_header("Retry-After", "7")
                self.end_headers()
                try:
                    for _ in range(40):
                        self.wfile.write(b"e")
                        self.wfile.flush()
                        if stop.wait(0.05):
                            return
                except (BrokenPipeError, ConnectionResetError):
                    return  # Header-phase rejection closes the unread error body.
                return
            self.send_response(200)
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                if framing is not None:
                    size = f"{len(body):x}".encode()
                    if framing == "extension":
                        wire = [size + b";name="] + [b"x"] * 40
                        wire.append(b"\r\n" + body + b"\r\n0\r\n\r\n")
                    elif framing == "trailer":
                        wire = [size + b"\r\n" + body + b"\r\n0\r\nX-Test: "]
                        wire += [b"y"] * 40 + [b"\r\n\r\n"]
                    else:
                        raise AssertionError(f"unknown framing: {framing}")
                    for fragment in wire:
                        self.wfile.write(fragment)
                        self.wfile.flush()
                        if stop.wait(read_gap):
                            return
                    return
                for chunk in chunks:
                    if chunked:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    else:
                        self.wfile.write(chunk)
                    self.wfile.flush()
                    if drip and stop.wait(read_gap):
                        return
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return  # Expected when the client cancels the attempt.

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/embed", started
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.fixture
def trickle_server() -> Iterator[tuple[str, list[float]]]:
    with _embedding_server() as server:
        yield server


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("attempts", [1, 3])
def test_embed_trickle_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
    trickle_server: tuple[str, list[float]],
    mode: str,
    attempts: int,
) -> None:
    """Bound one attempt AND retry exhaustion on real sockets, despite timely reads."""
    from shared import resilience

    endpoint, requests = trickle_server
    timeout = 0.25
    monkeypatch.setattr(gemini, "_ENDPOINT", endpoint)
    monkeypatch.setattr(settings.services, "memory_embed_timeout_seconds", timeout)
    monkeypatch.setattr(resilience, "_sleep", time.sleep)
    monkeypatch.setattr(resilience, "_asleep", asyncio.sleep)
    policy = Policy(
        max_attempts=attempts,
        backoff=ExponentialBackoff(base=0.001, factor=1, cap=0.001),
        jitter_span=0,
    )
    started = time.monotonic()
    with pytest.raises(EmbeddingAPIError, match="exceeded the deadline") as error:
        if mode == "sync":
            gemini._embed(["hello"], "RETRIEVAL_DOCUMENT", policy=policy)
        else:
            asyncio.run(gemini._embed_async(["hello"], "RETRIEVAL_QUERY", policy=policy))
    elapsed = time.monotonic() - started
    assert isinstance(error.value.__cause__, httpx.ReadTimeout)
    assert len(requests) == attempts
    assert attempts * timeout <= elapsed < attempts * (timeout + 0.2)
    # Even all retries finish before one complete 2s trickle response.
    assert elapsed < 2


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("attempts", [1, 3])
def test_embed_compressed_trickle_deadline(
    monkeypatch: pytest.MonkeyPatch, attempts: int, mode: str
) -> None:
    """Gzip metadata cannot hide body reads from the deadline or retry budget."""
    from shared import resilience

    timeout = 0.2
    monkeypatch.setattr(settings.services, "memory_embed_timeout_seconds", timeout)
    monkeypatch.setattr(resilience, "_sleep", time.sleep)
    monkeypatch.setattr(resilience, "_asleep", asyncio.sleep)
    policy = Policy(
        max_attempts=attempts,
        backoff=ExponentialBackoff(base=0.001, factor=1, cap=0.001),
        jitter_span=0,
    )
    with _embedding_server(compressed=True) as (endpoint, requests):
        monkeypatch.setattr(gemini, "_ENDPOINT", endpoint)
        started = time.monotonic()
        with pytest.raises(EmbeddingAPIError, match="exceeded the deadline") as error:
            if mode == "sync":
                gemini._embed(["hello"], "RETRIEVAL_DOCUMENT", policy=policy)
            else:
                asyncio.run(gemini._embed_async(["hello"], "RETRIEVAL_QUERY", policy=policy))
        elapsed = time.monotonic() - started
        assert isinstance(error.value.__cause__, httpx.ReadTimeout)
        assert len(requests) == attempts
        assert attempts * timeout <= elapsed < attempts * (timeout + 0.2)
        # All retries must end before the first gzip filename finishes dripping (2.25s).
        assert elapsed < 2


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("framing", ["extension", "trailer"])
@pytest.mark.parametrize("attempts", [1, 3])
def test_embed_framing_drip_deadline(
    monkeypatch: pytest.MonkeyPatch, mode: str, framing: str, attempts: int
) -> None:
    """Chunk extensions and trailers cannot hide timely reads from cancellation."""
    from shared import resilience

    timeout = 0.2
    monkeypatch.setattr(settings.services, "memory_embed_timeout_seconds", timeout)
    monkeypatch.setattr(resilience, "_sleep", time.sleep)
    monkeypatch.setattr(resilience, "_asleep", asyncio.sleep)
    policy = Policy(
        max_attempts=attempts,
        backoff=ExponentialBackoff(base=0.001, factor=1, cap=0.001),
        jitter_span=0,
    )
    with _embedding_server(framing=framing) as (endpoint, requests):
        monkeypatch.setattr(gemini, "_ENDPOINT", endpoint)
        started = time.monotonic()
        with pytest.raises(EmbeddingAPIError, match="exceeded the deadline") as error:
            if mode == "sync":
                gemini._embed(["hello"], "RETRIEVAL_DOCUMENT", policy=policy)
            else:
                asyncio.run(gemini._embed_async(["hello"], "RETRIEVAL_QUERY", policy=policy))
        elapsed = time.monotonic() - started
        assert isinstance(error.value.__cause__, httpx.ReadTimeout)
        assert len(requests) == attempts
        assert attempts * timeout <= elapsed < attempts * (timeout + 0.2)
        assert elapsed < 2  # Cancel all attempts before one framing drip completes.


@pytest.mark.parametrize("chunked", [False, True])
def test_embed_gzip_response_round_trip(monkeypatch: pytest.MonkeyPatch, chunked: bool) -> None:
    """HTTPX decoding preserves gzip, including chunked transfer."""
    with _embedding_server(compressed=True, chunked=chunked, drip=False) as (endpoint, requests):
        monkeypatch.setattr(gemini, "_ENDPOINT", endpoint)
        result = _provider().embed_batch(["hello"])
        np.testing.assert_array_equal(result, np.ones((1, DIM), dtype=np.float32))
        assert len(requests) == 1


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("status", [400, 429])
def test_embed_slow_error_body_preserves_status(
    monkeypatch: pytest.MonkeyPatch, mode: str, status: int
) -> None:
    """Real HTTPX rejects error headers immediately, preserving classification and delay."""
    from shared import resilience

    sleeps: list[float] = []
    classified: list[Exception] = []

    def classify(exc: Exception) -> bool:
        classified.append(exc)
        return resilience.http_classifier(exc)

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    policy = Policy(
        max_attempts=2,
        backoff=ExponentialBackoff(base=0.001, factor=1, cap=0.001),
        jitter_span=0,
        classify=classify,
    )
    monkeypatch.setattr(settings.services, "memory_embed_timeout_seconds", 0.3)
    monkeypatch.setattr(resilience, "_sleep", sleeps.append)
    monkeypatch.setattr(resilience, "_asleep", record_sleep)
    with _embedding_server(error_status=status, drip=False) as (endpoint, requests):
        monkeypatch.setattr(gemini, "_ENDPOINT", endpoint)

        def invoke() -> np.ndarray:
            if mode == "sync":
                return gemini._embed(["hello"], "RETRIEVAL_DOCUMENT", policy=policy)
            return asyncio.run(gemini._embed_async(["hello"], "RETRIEVAL_QUERY", policy=policy))

        started = time.monotonic()
        if status == 400:
            with pytest.raises(EmbeddingAPIError, match="Gemini embed HTTP 400") as error:
                invoke()
            assert isinstance(error.value.__cause__, httpx.HTTPStatusError)
            assert len(requests) == 1
            assert sleeps == []
        else:
            result = invoke()
            np.testing.assert_array_equal(result, np.ones((1, DIM), dtype=np.float32))
            assert len(requests) == 2
            assert sleeps == [7.0]  # Retry-After wins over the tiny policy backoff.
        elapsed = time.monotonic() - started
        assert elapsed < 0.25  # The error body would take 2s; even one timeout is 0.3s.
        assert len(classified) == 1
        status_error = classified[0]
        assert isinstance(status_error, httpx.HTTPStatusError)
        assert status_error.response.status_code == status
        assert resilience.extract_retry_after(status_error) == 7.0
        assert not status_error.response.is_stream_consumed


def test_sync_embed_slow_resolver_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sync caller returns at the deadline while its DNS thread finishes later."""
    real_getaddrinfo = socket.getaddrinfo
    finished = threading.Event()
    resolver_calls: list[object] = []

    def slow_getaddrinfo(host: object, *args: Any, **kwargs: Any) -> Any:
        resolver_calls.append(host)
        try:
            time.sleep(0.5)
            return real_getaddrinfo("127.0.0.1", *args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setattr(socket, "getaddrinfo", slow_getaddrinfo)
    monkeypatch.setattr(settings.services, "memory_embed_timeout_seconds", 0.1)
    with _embedding_server(drip=False) as (endpoint, requests):
        monkeypatch.setattr(gemini, "_ENDPOINT", endpoint.replace("127.0.0.1", "resolver.test"))
        started = time.monotonic()
        try:
            with pytest.raises(EmbeddingAPIError, match="exceeded the deadline") as error:
                gemini._embed(["hello"], "RETRIEVAL_DOCUMENT", policy=Policy(max_attempts=1))
            elapsed = time.monotonic() - started
            assert isinstance(error.value.__cause__, httpx.ReadTimeout)
            assert len(resolver_calls) == 1
            assert not requests  # Cancellation occurs before opening the local socket.
            assert 0.1 <= elapsed < 0.3
            assert not finished.is_set()
        finally:
            # Join our finite stub before its monkeypatch and server are torn down.
            assert finished.wait(timeout=2)
