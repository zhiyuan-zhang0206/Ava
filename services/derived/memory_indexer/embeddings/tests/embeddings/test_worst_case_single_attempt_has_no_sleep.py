"""Embeddings cases: worst case single attempt has no sleep."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any

import httpx
import numpy as np
import pytest

from base.config import settings
from base.host.net.resilience import ExponentialBackoff, Policy
from services.derived.memory_indexer.embeddings import factory, gemini
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError
from services.derived.memory_indexer.embeddings.gemini import DIM, GeminiEmbeddingProvider
from services.derived.memory_indexer.embeddings.tests.test_embeddings import (
    _dummy_gemini_key as _dummy_gemini_key,
)
from services.derived.memory_indexer.embeddings.tests.test_embeddings import (
    _embedding_server,
    _provider,
)
from services.derived.memory_indexer.embeddings.tests.test_embeddings import (
    _load_provider_plugins as _load_provider_plugins,
)
from services.derived.memory_indexer.embeddings.tests.test_embeddings import (
    pytestmark as pytestmark,
)
from services.derived.memory_indexer.embeddings.tests.test_embeddings import (
    trickle_server as trickle_server,
)


def test_worst_case_single_attempt_has_no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    from unittest.mock import Mock

    from base.config import settings
    from services.derived.memory_indexer.embeddings import gemini

    backoff = Mock(side_effect=AssertionError("one attempt must not evaluate backoff"))
    monkeypatch.setattr(
        gemini, "_EMBED_POLICY", replace(gemini._EMBED_POLICY, max_attempts=1, backoff=backoff)
    )
    assert gemini.worst_case_batch_seconds() == settings.services.memory_embed_timeout_seconds
    backoff.assert_not_called()


def test_factory_default_is_gemini() -> None:
    """The unset switch yields the Gemini provider — behavior unchanged."""
    from base.config import settings

    assert settings.services.embedding_backend == "gemini"
    assert isinstance(factory.get_provider(), GeminiEmbeddingProvider)


def test_factory_unknown_backend_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognized AVA_EMBEDDING_BACKEND must not silently fall back to
    gemini — a typo would keep the old provider while the operator believes
    the switch happened."""
    from base.config import settings

    monkeypatch.setattr(settings.services, "embedding_backend", "openai")
    with pytest.raises(ValueError, match="unknown embedding provider"):
        factory.get_provider()


def test_factory_provider_named_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_EMBEDDING_BACKEND=gemini yields the Gemini adapter."""
    from base.config import settings

    monkeypatch.setattr(settings.services, "embedding_backend", "gemini")
    assert isinstance(factory.get_provider_named("gemini"), GeminiEmbeddingProvider)


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("attempts", [1, 3])
def test_embed_trickle_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
    trickle_server: tuple[str, list[float]],
    mode: str,
    attempts: int,
) -> None:
    """Bound one attempt AND retry exhaustion on real sockets, despite timely reads."""
    from base.host.net import resilience

    endpoint, requests = trickle_server
    timeout = 0.25
    monkeypatch.setattr(gemini, "_ENDPOINT", endpoint)
    monkeypatch.setattr(settings.services, "memory_embed_timeout_seconds", timeout)
    monkeypatch.setattr(resilience, "retry_sleep", time.sleep)
    monkeypatch.setattr(resilience, "retry_asleep", asyncio.sleep)
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
    from base.host.net import resilience

    timeout = 0.2
    monkeypatch.setattr(settings.services, "memory_embed_timeout_seconds", timeout)
    monkeypatch.setattr(resilience, "retry_sleep", time.sleep)
    monkeypatch.setattr(resilience, "retry_asleep", asyncio.sleep)
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
    from base.host.net import resilience

    timeout = 0.2
    monkeypatch.setattr(settings.services, "memory_embed_timeout_seconds", timeout)
    monkeypatch.setattr(resilience, "retry_sleep", time.sleep)
    monkeypatch.setattr(resilience, "retry_asleep", asyncio.sleep)
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
    monkeypatch: pytest.MonkeyPatch, retry_waits: list[float], mode: str, status: int
) -> None:
    """Real HTTPX rejects error headers immediately, preserving classification and delay."""
    from base.host.net import resilience

    classified: list[Exception] = []

    def classify(exc: Exception) -> bool:
        classified.append(exc)
        return resilience.http_classifier(exc)

    policy = Policy(
        max_attempts=2,
        backoff=ExponentialBackoff(base=0.001, factor=1, cap=0.001),
        jitter_span=0,
        classify=classify,
    )
    monkeypatch.setattr(settings.services, "memory_embed_timeout_seconds", 0.3)
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
            assert retry_waits == []
        else:
            result = invoke()
            np.testing.assert_array_equal(result, np.ones((1, DIM), dtype=np.float32))
            assert len(requests) == 2
            assert retry_waits == [7.0]  # Retry-After wins over the tiny policy backoff.
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
