"""`POST /api/memory/search` endpoint unit tests — primary direct embed + the configured backend;
relative-path conversion; wire error propagation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot, settings
from base.config.tests.configuration import snapshot_process_config
from base.host.db_authority.api import AcceptanceCache
from base.lm import usage
from base.lm.catalog import ModelCatalog
from gateway.http.config import GatewayConfig, gateway_config_from_boot
from gateway.routers.memory import MemorySearchInputs
from gateway.tests.agents.gateway_auth import gateway_credentials
from services.derived.memory_indexer import rerank
from services.derived.memory_indexer.backends.base import ChunkHit
from services.derived.memory_indexer.embeddings import factory
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError
from tests.fixtures.gateway_config import gateway_test_app, gateway_test_client

_active_app: list[FastAPI] = []


def memory_config(root: Path, *, boot: ConfigBoot | None = None) -> GatewayConfig:
    resolved_boot = snapshot_process_config() if boot is None else boot
    return replace(gateway_config_from_boot(resolved_boot), memory_root=root)


def memory_search_app() -> FastAPI:
    assert _active_app, "memory_search_app_db fixture must be active"
    return _active_app[-1]


def _memory_inputs() -> MemorySearchInputs:
    def api_key() -> str | None:
        value = settings.lm.gemini_api_key
        return None if value is None else value.get_secret_value()

    return MemorySearchInputs(
        embedding_backend=lambda: settings.services.embedding_backend,
        backend=lambda: settings.services.memory_search_backend,
        uri=lambda: settings.services.memory_search_uri,
        embedding_timeout=lambda: settings.services.memory_embed_timeout_seconds,
        api_key=api_key,
        acquire_timeout=lambda: settings.services.memory_search_acquire_timeout_seconds,
        deadline=lambda: settings.services.memory_search_deadline_seconds,
        rerank_model=lambda: settings.services.memory_rerank_model,
        rerank_candidates=lambda: settings.services.memory_rerank_candidates,
        dedup_candidates=lambda: settings.services.memory_dedup_candidates,
    )


def _hit(path: Path, idx: int = 0, kind: str = "body") -> ChunkHit:
    return ChunkHit(str(path), kind, idx, 0.5)


class Judge:
    """The decision model: probability by a marker in the question, default 0.5.

    Records every call so tests can assert what the endpoint asked and with which model."""

    def __init__(self) -> None:
        self.rules: dict[str, float] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.failure: Exception | None = None

    async def __call__(
        self, model: str, state: str, question: Any, *, catalog: ModelCatalog, usage_kind: str
    ) -> float:
        del catalog, usage_kind
        self.calls.append((model, state, question))
        if self.failure is not None:
            raise self.failure
        return next((p for marker, p in self.rules.items() if marker in state), 0.5)


@pytest.fixture(autouse=True)
def judge(monkeypatch: pytest.MonkeyPatch) -> Judge:
    """Replace the decision call; the search endpoint is tested against its contract."""
    fake = Judge()
    monkeypatch.setattr(rerank, "ask_relevance", fake)
    return fake


@pytest.fixture(autouse=True)
def memory_search_app_db(
    model_catalog: ModelCatalog,
    machine_identity: MachineIdentity,
) -> Any:
    # ASGITransport tests skip the lifespan; supply their existing root's inputs. The
    # machine-token acceptance cache is one of them: the bearer check reads it on every
    # request, and without it these tests only pass in a worker where an earlier test's
    # lifespan happened to leave one on the shared app.
    app = gateway_test_app(machine=machine_identity)
    machine_token_acceptance: AcceptanceCache = {}
    app.state.machine_token_acceptance = machine_token_acceptance
    app.state.db = object()
    app.state.catalog = model_catalog
    app.state.machine = machine_identity
    app.state.memory_search_inputs = _memory_inputs()
    _active_app.append(app)
    try:
        yield app
    finally:
        _active_app.clear()


def patch_provider(monkeypatch: pytest.MonkeyPatch, provider: object) -> None:
    def construct(
        name: str,
        *,
        catalog: ModelCatalog,
        timeout_reader: Callable[[], float],
        api_key_reader: Callable[[], str | None],
    ) -> object:
        del name, catalog, timeout_reader, api_key_reader
        return provider

    monkeypatch.setattr(factory, "get_provider", construct)


class StubProvider:
    """Embedding provider stand-in — the handler reads dim/fingerprint and
    calls embed_query_async; a class (not an instance) works because the
    factory's `get_provider()` return value is only attribute-accessed."""

    dim = 768
    fingerprint = "fake:provider:dim=768"

    @staticmethod
    async def embed_query_async(_text: str) -> list[float]:
        return [0.0] * StubProvider.dim

    @staticmethod
    def embed_query(_text: str) -> list[float]:
        return [0.0] * StubProvider.dim

    @staticmethod
    def embed_batch(texts: list[str]) -> list[list[float]]:
        return [[0.0] * StubProvider.dim for _ in texts]


class TestPrimaryPath:
    """Primary node goes directly through embedder + the backend, returns relative paths."""

    def test_primary_returns_relative_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Primary path: stub embedder + index → verify returned paths are relative to memory_root."""
        # create a few fake markdown files in tmp so relative_to doesn't raise
        (tmp_path / "notes").mkdir()
        (tmp_path / "notes" / "foo.md").write_text("x")
        (tmp_path / "bar.md").write_text("y")

        # stub embedder/backend to avoid real Gemini / backend calls
        import services.derived.memory_indexer.backends.factory as _factory

        patch_provider(monkeypatch, StubProvider)

        class _FakeBackend:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def search_topk_async(
                self, _vec: object, _k: int, *, timeout: float
            ) -> list[ChunkHit]:
                assert timeout > 0  # the handler must hand the backend a real deadline
                return [_hit(tmp_path / "notes" / "foo.md"), _hit(tmp_path / "bar.md")]

        monkeypatch.setattr(_factory, "get_backend", _FakeBackend)

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.post(
                "/api/memory/search", json={"query": "test", "k": 5, "purpose": "search"}
            )
        assert resp.status_code == 200
        # must be relative paths relative to memory_root — must not contain tmp_path prefix
        body = resp.json()
        results = body["results"]
        assert set(body) == {"results"}
        assert [item["path"] for item in results] == ["notes/foo.md", "bar.md"]
        assert len(results) == 2
        assert results[0]["path"] == "notes/foo.md"
        assert results[1]["path"] == "bar.md"
        # description is empty because stub files have no YAML frontmatter
        assert results[0]["description"] == ""
        assert results[1]["description"] == ""
        # each result carries the matched chunk and its relevance score
        assert [(item["chunk"], item["score"]) for item in results] == [("x", 0.5), ("y", 0.5)]

    def test_primary_returns_descriptions_from_frontmatter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When files have a YAML frontmatter description, results include it."""
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

        patch_provider(monkeypatch, StubProvider)

        class _FakeBackend:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def search_topk_async(
                self, _vec: object, _k: int, *, timeout: float
            ) -> list[ChunkHit]:
                assert timeout > 0  # the handler must hand the backend a real deadline
                return [
                    _hit(tmp_path / "with_desc.md"),
                    _hit(tmp_path / "no_desc.md"),
                    _hit(tmp_path / "no_frontmatter.md"),
                ]

        monkeypatch.setattr(_factory, "get_backend", _FakeBackend)

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.post(
                "/api/memory/search", json={"query": "test", "k": 3, "purpose": "search"}
            )
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
        assert set(body) == {"results"}

    def test_primary_embedder_failure_raises_indexer_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """embedder API failure → IndexerUnavailable (wire 503)."""

        class _BoomProvider:
            dim = 768
            fingerprint = "fake:provider:dim=768"

            @staticmethod
            async def embed_query_async(_q: str) -> Any:
                raise EmbeddingAPIError("gemini quota exhausted")

        patch_provider(monkeypatch, _BoomProvider)

        with gateway_test_client() as client:
            resp = client.post(
                "/api/memory/search", json={"query": "x", "k": 5, "purpose": "search"}
            )
        assert resp.status_code == 503
        body = resp.json()
        assert body["reason"] == "indexer_unavailable"
        assert "embed" in body["detail"]

    def test_unexpected_embed_failure_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unmodelled failure keeps its identity instead of becoming an outage."""

        failure = FileNotFoundError(2, "No such file or directory")

        class BrokenProvider(StubProvider):
            @staticmethod
            async def embed_query_async(_q: str) -> list[float]:
                raise failure

        patch_provider(monkeypatch, BrokenProvider)
        with gateway_test_client() as client, pytest.raises(FileNotFoundError) as caught:
            client.post("/api/memory/search", json={"query": "x", "k": 5, "purpose": "search"})
        assert caught.value is failure

    def test_primary_backend_failure_raises_indexer_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """backend raises (e.g. connect refused) → IndexerUnavailable (wire 503)."""
        import services.derived.memory_indexer.backends.factory as _factory

        patch_provider(monkeypatch, StubProvider)

        class _BoomBackend:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def search_topk_async(
                self, _vec: object, _k: int, *, timeout: float
            ) -> list[ChunkHit]:
                raise RuntimeError("connection refused 19530")

        monkeypatch.setattr(_factory, "get_backend", _BoomBackend)

        with gateway_test_client() as client:
            resp = client.post(
                "/api/memory/search", json={"query": "x", "k": 5, "purpose": "search"}
            )
        assert resp.status_code == 503
        assert resp.json()["reason"] == "indexer_unavailable"


class TestRerank:
    """The second stage orders the vector candidates by the decision model's probability."""

    def _backend(
        self, monkeypatch: pytest.MonkeyPatch, hits: list[ChunkHit]
    ) -> list[tuple[int, float]]:
        import services.derived.memory_indexer.backends.factory as _factory

        patch_provider(monkeypatch, StubProvider)
        asked: list[tuple[int, float]] = []

        class _FakeBackend:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def search_topk_async(
                self, _vec: object, k: int, *, timeout: float
            ) -> list[ChunkHit]:
                asked.append((k, timeout))
                return hits

        monkeypatch.setattr(_factory, "get_backend", _FakeBackend)
        return asked

    def _pool(self, tmp_path: Path) -> None:
        for name in ("a", "b", "c"):
            (tmp_path / f"{name}.md").write_text(
                f"---\ntype: Memory\ndescription: about {name}\ntags:\n- type/reference\n---\n\npassage {name}\n",
                encoding="utf-8",
            )

    def test_results_follow_the_decision_probability_not_vector_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, judge: Judge
    ) -> None:
        self._pool(tmp_path)
        self._backend(monkeypatch, [_hit(tmp_path / n) for n in ("a.md", "b.md", "c.md")])
        judge.rules = {"passage a": 0.1, "passage b": 0.6, "passage c": 0.95}

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.post(
                "/api/memory/search", json={"query": "what about c", "k": 2, "purpose": "search"}
            )

        assert resp.status_code == 200
        results = resp.json()["results"]
        assert [(r["path"], r["score"]) for r in results] == [("c.md", 0.95), ("b.md", 0.6)]
        assert results[0]["description"] == "about c"
        assert results[0]["tags"] == ["type/reference"]
        assert results[0]["chunk"] == "passage c"
        # one decision request per candidate, on the configured model, about the query
        assert len(judge.calls) == 3
        assert {model for model, _, _ in judge.calls} == {settings.services.memory_rerank_model}
        assert all("what about c" in state for _, state, _ in judge.calls)

    def test_purpose_selects_the_relevance_question(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, judge: Judge
    ) -> None:
        self._pool(tmp_path)
        self._backend(monkeypatch, [_hit(tmp_path / "a.md")])
        questions: dict[str, Any] = {}
        for purpose in ("recall", "search", "dedup"):
            judge.calls.clear()
            with gateway_test_client(config=memory_config(tmp_path)) as client:
                resp = client.post(
                    "/api/memory/search", json={"query": "q", "k": 1, "purpose": purpose}
                )
            assert resp.status_code == 200
            questions[purpose] = judge.calls[0][2]
        assert len(set(questions.values())) == 3

    def test_backend_is_asked_for_the_configured_candidate_count(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._pool(tmp_path)
        asked = self._backend(monkeypatch, [])
        monkeypatch.setattr(settings.services, "memory_rerank_candidates", 40)

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            assert client.post(
                "/api/memory/search", json={"query": "q", "k": 5, "purpose": "search"}
            ).json() == {"results": []}
            client.post("/api/memory/search", json={"query": "q", "k": 60, "purpose": "search"})
        # a `k` above the candidate count does not widen the retrieval
        assert [k for k, _timeout in asked] == [40, 40]

    def test_dedup_asks_for_its_own_candidate_count(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._pool(tmp_path)
        asked = self._backend(monkeypatch, [])
        monkeypatch.setattr(settings.services, "memory_rerank_candidates", 40)
        monkeypatch.setattr(settings.services, "memory_dedup_candidates", 7)

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            for purpose, k in (("dedup", 5), ("dedup", 9), ("recall", 5), ("search", 5)):
                client.post("/api/memory/search", json={"query": "q", "k": k, "purpose": purpose})
        # dedup ranks its own count whatever `k` is; recall and search keep the shared one
        assert [k for k, _timeout in asked] == [7, 7, 40, 40]

    def test_dedup_candidate_count_defaults_to_five(self) -> None:
        assert settings.services.memory_dedup_candidates == 5

    def test_stale_vector_hits_are_dropped_before_ranking(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, judge: Judge
    ) -> None:
        self._pool(tmp_path)
        self._backend(
            monkeypatch,
            [_hit(tmp_path / "gone.md"), _hit(tmp_path / "a.md", idx=9), _hit(tmp_path / "b.md")],
        )
        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.post(
                "/api/memory/search", json={"query": "q", "k": 5, "purpose": "search"}
            )
        assert [r["path"] for r in resp.json()["results"]] == ["b.md"]
        assert len(judge.calls) == 1

    def test_decision_failure_is_a_clear_503_not_vector_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, judge: Judge
    ) -> None:
        from base.api_contracts.agent_contract import IndexerUnavailable

        self._pool(tmp_path)
        self._backend(monkeypatch, [_hit(tmp_path / "a.md")])
        judge.failure = IndexerUnavailable("memory rerank failed (jev): upstream 503")

        with gateway_test_client(config=memory_config(tmp_path)) as client:
            resp = client.post(
                "/api/memory/search", json={"query": "q", "k": 5, "purpose": "search"}
            )

        assert resp.status_code == 503
        body = resp.json()
        assert body["reason"] == "indexer_unavailable"
        assert "memory rerank failed" in body["detail"]
        assert "results" not in body


class TestRequestValidation:
    """Schema validation — query must not be empty, k range."""

    def test_empty_query_rejected(self) -> None:
        with gateway_test_client() as client:
            resp = client.post(
                "/api/memory/search", json={"query": "", "k": 5, "purpose": "search"}
            )
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        "body", [{"query": "x", "k": 5}, {"query": "x", "k": 5, "purpose": "audit"}]
    )
    def test_missing_or_unknown_purpose_rejected(self, body: dict[str, Any]) -> None:
        with gateway_test_client() as client:
            resp = client.post("/api/memory/search", json=body)
        assert resp.status_code == 422

    def test_blank_query_rejected(self) -> None:
        with gateway_test_client() as client:
            resp = client.post(
                "/api/memory/search", json={"query": "  \n ", "k": 5, "purpose": "search"}
            )
        assert resp.status_code == 422

    def test_k_out_of_range_rejected(self) -> None:
        with gateway_test_client() as client:
            resp = client.post(
                "/api/memory/search", json={"query": "x", "k": 0, "purpose": "search"}
            )
        assert resp.status_code == 422
        with gateway_test_client() as client:
            resp = client.post(
                "/api/memory/search", json={"query": "x", "k": 101, "purpose": "search"}
            )
        assert resp.status_code == 422


# --- a stalled backend must degrade the endpoint, not pin it ---

SEARCH_PERMITS = 2  # the stub's per-test semaphore size (the knob is a setting)
_TEST_DEADLINE_S = 0.5


async def never_returns(*_args: object, **_kwargs: object) -> list[ChunkHit]:
    """A backend call that accepts the request and then never answers.

    Takes its arguments loosely on purpose: the point is the handler's
    behaviour around the call, not the call's signature.
    """
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


def stub_search_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    search: Any,
    permits: int = SEARCH_PERMITS,
) -> asyncio.Semaphore:
    """Point the search handler at a stubbed backend and a short deadline.

    Returns the fresh per-test semaphore so tests can assert on permit state.
    """
    import services.derived.memory_indexer.backends.factory as _factory
    from base.config import settings

    app = memory_search_app()
    app.state.gateway_config = replace(app.state.gateway_config, memory_root=tmp_path)
    (tmp_path / "a.md").write_text("---\ntype: Memory\n---\nx\n", encoding="utf-8")

    # A fresh semaphore per test. asyncio.Semaphore binds itself to the event
    # loop of its first *contended* acquire and rejects every other loop after
    # that, and pytest-asyncio hands each test its own loop — so sharing one
    # across two contended tests would fail on the second. The handler reads it
    # from `app.state.memory_search_gate`, so the stub installs a fresh instance there.
    fresh = asyncio.Semaphore(permits)
    app.state.memory_search_gate = fresh
    monkeypatch.setattr(settings.services, "memory_search_deadline_seconds", _TEST_DEADLINE_S)

    class _StubBackend:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def search_topk_async(self, _v: object, _k: int, *, timeout: float) -> list[ChunkHit]:
            return await search(_v, _k, timeout=timeout)

    patch_provider(monkeypatch, StubProvider)
    monkeypatch.setattr(_factory, "get_backend", _StubBackend)
    return fresh


def asgi_client() -> httpx.AsyncClient:
    """Drive the app on the *test's* own event loop, so concurrent requests
    contend for the handler's semaphore the way they do in the gateway.
    `TestClient` would run them on its own portal loop instead."""
    app = memory_search_app()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=gateway_credentials(app.state.gateway_config),
    )


async def _search(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post("/api/memory/search", json={"query": "x", "k": 1, "purpose": "search"})


async def assert_semaphore_locked(sem: asyncio.Semaphore, timeout_s: float = 5.0) -> None:
    """Wait for concurrent requests to acquire every memory-search permit."""
    deadline = time.monotonic() + timeout_s
    while not sem.locked() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert sem.locked(), "search holders never acquired every permit"


@pytest.mark.parametrize("error_type", [RuntimeError, TypeError])
async def test_query_usage_failure_propagates_without_network_replay(
    monkeypatch: pytest.MonkeyPatch, model_catalog: ModelCatalog, error_type: type[Exception]
) -> None:
    from types import SimpleNamespace

    from fastapi import Request
    from pydantic import SecretStr

    from gateway.routers import memory
    from gateway.schemas.memory import MemorySearchRequest
    from services.derived.memory_indexer.embeddings.gemini import DIM
    from services.derived.memory_indexer.embeddings.tests.test_embeddings import AsyncClient

    fake = AsyncClient(vectors=[[1.0] * DIM], prompt_token_count=123)
    monkeypatch.setattr(httpx, "AsyncClient", fake)
    monkeypatch.setattr(settings.lm, "gemini_api_key", SecretStr("test-key"))
    failure = error_type("accounting invariant failed")
    seen: list[object] = []

    def fail_usage(*, catalog: object, **_kwargs: object) -> None:
        seen.append(catalog)
        raise failure

    monkeypatch.setattr(usage, "log_usage_fields", fail_usage)
    state = SimpleNamespace(
        catalog=model_catalog,
        memory_search_gate=asyncio.Semaphore(1),
        memory_search_inputs=_memory_inputs(),
    )

    async def stays_connected() -> dict[str, str]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    request = Request({"type": "http", "app": SimpleNamespace(state=state)}, stays_connected)
    with pytest.raises(error_type) as caught:
        await memory.post_memory_search(
            request, MemorySearchRequest(query="hello", k=1, purpose="search")
        )
    assert caught.value is failure
    assert seen == [model_catalog]
    assert seen[0] is model_catalog
    assert fake.call_count == 1
    assert state.memory_search_gate._value == 1
