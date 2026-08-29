"""Backend abstraction unit tests — factory dispatch + MilvusBackend adapter.

The milvus storage primitives themselves are covered by
`test_memory_indexer.py` (through the legacy index module surface); here
the adapter's delegation, lifecycle, and the async deadline plumbing are
pinned, plus the factory's fail-fast switch.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from services.memory_indexer import embedder
from services.memory_indexer.backends import factory
from services.memory_indexer.backends.base import KIND_DESC, pk_of
from services.memory_indexer.backends.milvus import MilvusBackend


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(embedder.DIM).astype(np.float32)


# ── factory ──────────────────────────────────────────────────────────────


def test_factory_and_probe_registries_stay_in_sync() -> None:
    """Every backend has both a constructor and a preflight probe — the
    daemon's fail-fast preflight must not silently skip a backend."""
    from services.memory_indexer.backends import probe

    assert set(factory._BACKENDS) == set(probe._PROBES)


def test_factory_default_is_milvus() -> None:
    """The unset switch yields the milvus backend — behavior unchanged."""
    from shared.config import settings

    assert settings.services.memory_search_backend == "milvus"
    assert isinstance(factory.get_backend(), MilvusBackend)


def test_factory_numpy_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_MEMORY_SEARCH_BACKEND=numpy yields the NumPyBackend."""
    from services.memory_indexer.backends.numpy import NumPyBackend
    from shared.config import settings

    monkeypatch.setattr(settings.services, "memory_search_backend", "numpy")
    assert isinstance(factory.get_backend(), NumPyBackend)


def test_factory_pgvector_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_MEMORY_SEARCH_BACKEND=pgvector yields the PGVectorBackend."""
    from services.memory_indexer.backends.pgvector import PGVectorBackend
    from shared.config import settings

    monkeypatch.setattr(settings.services, "memory_search_backend", "pgvector")
    assert isinstance(factory.get_backend(), PGVectorBackend)


def test_factory_unknown_backend_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognized AVA_MEMORY_SEARCH_BACKEND must not silently fall
    back to milvus — a typo would keep the old storage while the operator
    believes the switch happened."""
    from shared.config import settings

    monkeypatch.setattr(settings.services, "memory_search_backend", "qdrant")
    with pytest.raises(ValueError, match="unknown memory search backend"):
        factory.get_backend()


# ── row vocabulary ───────────────────────────────────────────────────────


def test_pk_of_roundtrips_path_kind_chunk() -> None:
    assert pk_of("/a/b.md", KIND_DESC, 0) == "/a/b.md\x1fdesc\x1f0"
    assert pk_of("/a/b.md", "body", 3) == "/a/b.md\x1fbody\x1f3"


# ── MilvusBackend adapter ─────────────────────────────────────────────────


def test_backend_requires_connect_before_use() -> None:
    """Using an unconnected backend is a caller bug — fail fast, not a
    confusing pymilvus error deeper in."""
    backend = MilvusBackend()
    with pytest.raises(RuntimeError, match="not connected"):
        backend.all_meta()


def test_backend_close_idempotent() -> None:
    closed: list[str] = []

    class _FakeClient:
        def close(self) -> None:
            closed.append("x")

    backend = MilvusBackend(client=_FakeClient())  # pyright: ignore[reportArgumentType]
    backend.close()
    backend.close()  # second close must be a no-op
    assert closed == ["x"]


def test_backend_write_read_delegation(milvus_client) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """The adapter over a live client: upsert / all_meta / search_topk /
    delete behave exactly like the legacy module functions (same engine)."""
    backend = MilvusBackend(client=milvus_client)  # pyright: ignore[reportUnknownArgumentType]
    target = np.ones(embedder.DIM, dtype=np.float32)
    backend.upsert("/a.md", 1.0, "ha", target, kind="body", chunk_idx=0)
    backend.upsert("/a.md", 1.0, "ha", 0.5 * target, kind="body", chunk_idx=1)
    backend.upsert("/b.md", 2.0, "hb", -target, kind="body", chunk_idx=0)
    assert backend.all_meta() == {"/a.md": (1.0, "ha"), "/b.md": (2.0, "hb")}
    assert backend.search_topk(target, k=5) == ["/a.md", "/b.md"]  # chunk aggregation, best wins
    backend.delete("/a.md")
    assert backend.all_meta() == {"/b.md": (2.0, "hb")}


def test_backend_search_topk_async_passes_timeout_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter keeps the gateway's per-request async lifecycle: connect
    and search both receive the caller's deadline and the client closes —
    the 2026-08-03 unbounded-await guard, now owned by the backend instead
    of the gateway handler."""
    from services.memory_indexer.backends import milvus

    seen: dict[str, list[float]] = {"connect": [], "search": []}
    closed = asyncio.Event()

    class _FakeAsyncClient:
        async def close(self) -> None:
            closed.set()

    async def _fake_connect(*, timeout: float) -> _FakeAsyncClient:
        seen["connect"].append(timeout)
        return _FakeAsyncClient()

    async def _fake_search(client: object, vec: object, k: int, *, timeout: float) -> list[str]:
        seen["search"].append(timeout)
        return ["/a.md"]

    monkeypatch.setattr(milvus, "_connect_async", _fake_connect)
    monkeypatch.setattr(milvus, "_search_topk_async", _fake_search)

    backend = MilvusBackend()
    result = asyncio.run(backend.search_topk_async(_vec(0), 3, timeout=2.5))
    assert result == ["/a.md"]
    assert seen["connect"] == [2.5]
    assert seen["search"] == [2.5]
    assert closed.is_set()


def test_backend_search_topk_async_closes_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing search still closes its client — the connection cannot
    leak and pin the process-global pymilvus lock."""
    from services.memory_indexer.backends import milvus

    closed = asyncio.Event()

    class _FakeAsyncClient:
        async def close(self) -> None:
            closed.set()

    async def _fake_connect(*, timeout: float) -> _FakeAsyncClient:
        return _FakeAsyncClient()

    async def _boom(client: object, vec: object, k: int, *, timeout: float) -> list[str]:
        raise RuntimeError("milvus exploded")

    monkeypatch.setattr(milvus, "_connect_async", _fake_connect)
    monkeypatch.setattr(milvus, "_search_topk_async", _boom)

    backend = MilvusBackend()
    with pytest.raises(RuntimeError, match="milvus exploded"):
        asyncio.run(backend.search_topk_async(_vec(0), 3, timeout=2.5))
    assert closed.is_set()


def test_backend_connect_sets_client_and_close_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect() installs the client it created; close() releases it."""
    from services.memory_indexer.backends import milvus

    closed: list[str] = []

    class _FakeClient:
        def close(self) -> None:
            closed.append("x")

    monkeypatch.setattr(milvus, "_connect", _FakeClient)
    backend = MilvusBackend()
    backend.connect()
    assert isinstance(backend._client, _FakeClient)  # pyright: ignore[reportPrivateUsage]
    backend.close()
    assert closed == ["x"]
    assert backend._client is None  # pyright: ignore[reportPrivateUsage]
