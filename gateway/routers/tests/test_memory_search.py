"""`POST /api/memory/search` endpoint unit tests — primary direct embed + the configured backend;
relative-path conversion; wire error propagation."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError


@pytest.fixture(autouse=True)
def _app_db(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the app lifespan sets app.state.db; ASGITransport tests never run it.
    monkeypatch.setattr(app.state, "db", object(), raising=False)


class _StubProvider:
    """Embedding provider stand-in — the handler reads dim/fingerprint and
    calls embed_query_async; a class (not an instance) works because the
    factory's `get_provider()` return value is only attribute-accessed."""

    dim = 768
    fingerprint = "fake:provider:dim=768"

    @staticmethod
    async def embed_query_async(_text: str) -> list[float]:
        return [0.0] * _StubProvider.dim

    @staticmethod
    def embed_query(_text: str) -> list[float]:
        return [0.0] * _StubProvider.dim

    @staticmethod
    def embed_batch(texts: list[str]) -> list[list[float]]:
        return [[0.0] * _StubProvider.dim for _ in texts]


class TestPrimaryPath:
    """Primary node goes directly through embedder + the backend, returns relative paths."""

    def test_primary_returns_relative_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Primary path: stub embedder + index → verify returned paths are relative to memory_root."""

        # Use gateway_memory_dir() as the memory root for relative path resolution
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        # create a few fake markdown files in tmp so relative_to doesn't raise
        (tmp_path / "notes").mkdir()
        (tmp_path / "notes" / "foo.md").write_text("x")
        (tmp_path / "bar.md").write_text("y")

        # stub embedder/backend to avoid real Gemini / backend calls
        import services.derived.memory_indexer.backends.factory as _factory
        import services.derived.memory_indexer.embeddings.factory as _embedding_factory

        monkeypatch.setattr(_embedding_factory, "get_provider", _StubProvider)

        class _FakeBackend:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def search_topk_async(
                self, _vec: object, _k: int, *, timeout: float
            ) -> list[str]:
                assert timeout > 0  # the handler must hand the backend a real deadline
                return [
                    str(tmp_path / "notes" / "foo.md"),
                    str(tmp_path / "bar.md"),
                ]

        monkeypatch.setattr(_factory, "get_backend", _FakeBackend)

        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "test", "k": 5})
        assert resp.status_code == 200
        # must be relative paths relative to memory_root — must not contain tmp_path prefix
        body = resp.json()
        paths = body["paths"]
        results = body["results"]
        assert paths == ["notes/foo.md", "bar.md"]
        assert len(results) == 2
        assert results[0]["path"] == "notes/foo.md"
        assert results[1]["path"] == "bar.md"
        # description is empty because stub files have no YAML frontmatter
        assert results[0]["description"] == ""
        assert results[1]["description"] == ""

    def test_primary_returns_descriptions_from_frontmatter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When files have a YAML frontmatter description, results include it."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "with_desc.md").write_text(
            """---
type: Memory
description: A note about the user's health
title: Health Overview
---

# Health
""",
            encoding="utf-8",
        )
        (tmp_path / "no_desc.md").write_text(
            """---
type: Memory
title: No Description
---

# No desc
""",
            encoding="utf-8",
        )
        (tmp_path / "no_frontmatter.md").write_text("# Just a heading\n\nNo YAML.")

        import services.derived.memory_indexer.backends.factory as _factory
        import services.derived.memory_indexer.embeddings.factory as _embedding_factory

        monkeypatch.setattr(_embedding_factory, "get_provider", _StubProvider)

        class _FakeBackend:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def search_topk_async(
                self, _vec: object, _k: int, *, timeout: float
            ) -> list[str]:
                assert timeout > 0  # the handler must hand the backend a real deadline
                return [
                    str(tmp_path / "with_desc.md"),
                    str(tmp_path / "no_desc.md"),
                    str(tmp_path / "no_frontmatter.md"),
                ]

        monkeypatch.setattr(_factory, "get_backend", _FakeBackend)

        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "test", "k": 3})
        assert resp.status_code == 200
        body = resp.json()
        results = body["results"]
        assert len(results) == 3
        assert results[0]["path"] == "with_desc.md"
        assert results[0]["description"] == "A note about the user's health"
        assert results[1]["path"] == "no_desc.md"
        assert results[1]["description"] == ""
        assert results[2]["path"] == "no_frontmatter.md"
        assert results[2]["description"] == ""
        # paths still only return paths (backward compat)
        assert body["paths"] == ["with_desc.md", "no_desc.md", "no_frontmatter.md"]

    def test_primary_embedder_failure_raises_indexer_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """embedder API failure → IndexerUnavailable (wire 503)."""
        import services.derived.memory_indexer.embeddings.factory as _embedding_factory

        class _BoomProvider:
            dim = 768
            fingerprint = "fake:provider:dim=768"

            @staticmethod
            async def embed_query_async(_q: str) -> Any:
                raise EmbeddingAPIError("gemini quota exhausted")

        monkeypatch.setattr(_embedding_factory, "get_provider", _BoomProvider)

        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "x", "k": 5})
        assert resp.status_code == 503
        body = resp.json()
        assert body["reason"] == "indexer_unavailable"
        assert "embed" in body["detail"]

    def test_unexpected_embed_failure_also_raises_indexer_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An embed failure that is not an `EmbeddingAPIError` is still an
        outage, not an unmodelled error.

        The embed phase used to catch only `EmbeddingAPIError`, so anything else
        escaped as a bare 500 whose body has no wire `reason` — the SDK cannot
        rebuild `IndexerUnavailable` from that, so a caller that handles the
        outage still saw a raw HTTP error. That is how agent 405 died on
        2026-08-07: the gateway was running out of a deleted worktree's venv and
        the embed client raised `FileNotFoundError` on the missing certifi
        cacert. The backend phase below already caught broadly; this makes the
        two symmetric.
        """
        import services.derived.memory_indexer.embeddings.factory as _embedding_factory

        class _BoomProvider:
            dim = 768
            fingerprint = "fake:provider:dim=768"

            @staticmethod
            async def embed_query_async(_q: str) -> Any:
                raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(_embedding_factory, "get_provider", _BoomProvider)

        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "x", "k": 5})
        assert resp.status_code == 503
        assert resp.json()["reason"] == "indexer_unavailable"

    def test_primary_backend_failure_raises_indexer_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """backend raises (e.g. connect refused) → IndexerUnavailable (wire 503)."""
        import services.derived.memory_indexer.backends.factory as _factory
        import services.derived.memory_indexer.embeddings.factory as _embedding_factory

        monkeypatch.setattr(_embedding_factory, "get_provider", _StubProvider)

        class _BoomBackend:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def search_topk_async(
                self, _vec: object, _k: int, *, timeout: float
            ) -> list[str]:
                raise RuntimeError("connection refused 19530")

        monkeypatch.setattr(_factory, "get_backend", _BoomBackend)

        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "x", "k": 5})
        assert resp.status_code == 503
        assert resp.json()["reason"] == "indexer_unavailable"


class TestRequestValidation:
    """Schema validation — query must not be empty, k range."""

    def test_empty_query_rejected(self) -> None:
        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "", "k": 5})
        assert resp.status_code == 422

    def test_k_out_of_range_rejected(self) -> None:
        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "x", "k": 0})
        assert resp.status_code == 422
        with TestClient(app) as client:
            resp = client.post("/api/memory/search", json={"query": "x", "k": 101})
        assert resp.status_code == 422


# ── graph grouping vs the type vocabulary ──


# ── _extract_meta equivalence (audit #2448 Phase 2) ──


def _legacy_extract_meta(path: Path) -> tuple[str, list[str]]:
    """The pre-#2448 `memory._extract_meta` — reference implementation."""
    import yaml as _yaml

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "", []
    if not text.startswith("---\n"):
        return "", []
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return "", []
    try:
        fm = _yaml.safe_load(parts[1])
    except _yaml.YAMLError:
        return "", []
    if not isinstance(fm, dict):
        return "", []
    desc = fm.get("description")  # pyright: ignore[reportUnknownMemberType]
    description = desc.strip() if isinstance(desc, str) and desc.strip() else ""
    raw_tags = fm.get("tags")  # pyright: ignore[reportUnknownMemberType]
    tags = [t for t in raw_tags if isinstance(t, str)] if isinstance(raw_tags, list) else []
    return description, tags


# --- a stalled backend must degrade the endpoint, not pin it ---

_SEARCH_PERMITS = 2  # the stub's per-test semaphore size (the knob is a setting)
_TEST_DEADLINE_S = 0.5


async def _never_returns(*_args: object, **_kwargs: object) -> list[str]:
    """A backend call that accepts the request and then never answers.

    Takes its arguments loosely on purpose: the point is the handler's
    behaviour around the call, not the call's signature.
    """
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


def _stub_search_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    search: Any,
    permits: int = _SEARCH_PERMITS,
) -> asyncio.Semaphore:
    """Point the search handler at a stubbed backend and a short deadline.

    Returns the fresh per-test semaphore so tests can assert on permit state.
    """
    import gateway.routers.memory as _gw_memory
    import services.derived.memory_indexer.backends.factory as _factory
    import services.derived.memory_indexer.embeddings.factory as _embedding_factory
    from base.config import settings

    monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
    (tmp_path / "a.md").write_text("---\ntype: Memory\n---\nx\n", encoding="utf-8")

    # A fresh semaphore per test. asyncio.Semaphore binds itself to the event
    # loop of its first *contended* acquire and rejects every other loop after
    # that, and pytest-asyncio hands each test its own loop — so sharing one
    # across two contended tests would fail on the second. The handler reads it
    # from `app.state.memory_search_gate`, so the stub installs a fresh instance there.
    fresh = asyncio.Semaphore(permits)
    monkeypatch.setattr(app.state, "memory_search_gate", fresh, raising=False)
    monkeypatch.setattr(settings.services, "memory_search_deadline_seconds", _TEST_DEADLINE_S)

    class _StubBackend:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def search_topk_async(self, _v: object, _k: int, *, timeout: float) -> list[str]:
            return await search(_v, _k, timeout=timeout)

    monkeypatch.setattr(_embedding_factory, "get_provider", _StubProvider)
    monkeypatch.setattr(_factory, "get_backend", _StubBackend)
    return fresh


def _asgi_client() -> httpx.AsyncClient:
    """Drive the app on the *test's* own event loop, so concurrent requests
    contend for the handler's semaphore the way they do in the gateway.
    `TestClient` would run them on its own portal loop instead."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _search(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post("/api/memory/search", json={"query": "x", "k": 1})


async def _assert_semaphore_locked(sem: asyncio.Semaphore, timeout_s: float = 5.0) -> None:
    """Wait for concurrent requests to acquire every memory-search permit."""
    deadline = time.monotonic() + timeout_s
    while not sem.locked() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert sem.locked(), "search holders never acquired every permit"
