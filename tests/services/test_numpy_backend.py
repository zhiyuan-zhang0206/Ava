"""NumPyBackend client tests — against a live memory_search service.

A session-scoped uvicorn server on a free loopback port serves the real
app (store + npz in a tmp dir), so the backend's HTTP client is exercised
over a real socket — the same shape the indexer daemon and gateway dial.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Iterator

import httpx
import numpy as np
import pytest
import uvicorn

from services.memory_indexer.backends import probe
from services.memory_indexer.backends.numpy import NumPyBackend
from services.memory_search.app import build_app
from services.memory_search.store import MemoryStore


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_DIM = 8
_FP = "test:gemini:dim=8"


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(_DIM).astype(np.float32)


@pytest.fixture(scope="session")
def memory_search_uri(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A real uvicorn server for the whole session, on a free port."""
    port = _free_port()
    data_file = tmp_path_factory.mktemp("memory-search") / "vectors.npz"
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(MemoryStore(data_file, dim=_DIM, fingerprint=_FP)),
            host="127.0.0.1",
            port=port,
            log_level=None,
            access_log=False,
            log_config=None,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = __import__("time").time() + 10.0
    while not server.started:
        if __import__("time").time() > deadline:
            raise RuntimeError("memory_search test server failed to start")
        __import__("time").sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5.0)


@pytest.fixture
def backend(memory_search_uri: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[NumPyBackend]:
    from shared.config import settings

    monkeypatch.setattr(settings.services, "memory_search_uri", memory_search_uri)
    b = NumPyBackend()
    b.connect()
    try:
        yield b
    finally:
        b.close()


def test_write_read_roundtrip(backend: NumPyBackend) -> None:
    ones = np.ones(_DIM, dtype=np.float32)
    backend.upsert("/a.md", 1.0, "ha", ones, kind="body", chunk_idx=0)
    backend.upsert("/b.md", 2.0, "hb", -ones, kind="body", chunk_idx=0)
    assert backend.all_meta() == {"/a.md": (1.0, "ha", _FP), "/b.md": (2.0, "hb", _FP)}
    assert backend.search_topk(ones, k=5) == ["/a.md", "/b.md"]
    backend.delete("/a.md")
    assert backend.all_meta() == {"/b.md": (2.0, "hb", _FP)}


def test_upsert_many_roundtrip(backend: NumPyBackend) -> None:
    rows = [
        ("/batch-a.md", 1.0, "ha", _vec(0), "body", 0),
        ("/batch-b.md", 2.0, "hb", _vec(1), "body", 0),
    ]
    backend.upsert_many(rows)
    meta = backend.all_meta()
    assert meta["/batch-a.md"] == (1.0, "ha", _FP)
    assert meta["/batch-b.md"] == (2.0, "hb", _FP)
    results = backend.search_topk(_vec(0), k=1000)
    assert {"/batch-a.md", "/batch-b.md"}.issubset(results)
    backend.delete("/batch-a.md")
    backend.delete("/batch-b.md")


def test_upsert_many_uses_one_request_with_batch_timeout() -> None:
    requests: list[httpx.Request] = []

    def _respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    backend = NumPyBackend()
    backend._client = httpx.Client(
        base_url="http://memory-search", transport=httpx.MockTransport(_respond)
    )
    try:
        backend.upsert_many(
            [
                ("/a.md", 1.0, "ha", _vec(0), "body", 0),
                ("/b.md", 2.0, "hb", _vec(1), "body", 0),
            ]
        )
    finally:
        backend.close()

    assert len(requests) == 1
    assert requests[0].url.path == "/upsert_batch"
    assert requests[0].extensions["timeout"]["read"] == 300.0


def test_readonly_upsert_many_connects_but_refuses_write(
    memory_search_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.services, "memory_search_uri", memory_search_uri)
    backend = NumPyBackend(readonly=True)
    backend.connect()
    try:
        with pytest.raises(RuntimeError, match="read-only"):
            backend.upsert_many([("/a.md", 1.0, "ha", _vec(0), "body", 0)])
    finally:
        backend.close()


def test_search_topk_async_matches_sync(backend: NumPyBackend) -> None:
    ones = np.ones(_DIM, dtype=np.float32)
    backend.upsert("/a.md", 1.0, "ha", ones, kind="body", chunk_idx=0)
    backend.upsert("/b.md", 2.0, "hb", -ones, kind="body", chunk_idx=0)
    sync = backend.search_topk(ones, k=5)
    async_result = asyncio.run(backend.search_topk_async(ones, 5, timeout=5.0))
    assert async_result == sync == ["/a.md", "/b.md"]


def test_connect_fails_fast_when_service_down(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.services, "memory_search_uri", f"http://127.0.0.1:{_free_port()}")
    backend = NumPyBackend()
    with pytest.raises(httpx.ConnectError):
        backend.connect()
    backend.close()  # idempotent no-op


def test_requires_connect_before_use() -> None:
    backend = NumPyBackend()
    with pytest.raises(RuntimeError, match="not connected"):
        backend.all_meta()


# ── preflight probes (CTO ruling 2026-08-30 direction ②) ───────────────────


def test_probe_numpy_healthy(memory_search_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.services, "memory_search_uri", memory_search_uri)
    assert probe.probe_backend("numpy").message is None


def test_probe_numpy_unreachable_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A down service is transient (may be booting) but the message must name
    the fix and the switch action."""
    from shared.config import settings

    monkeypatch.setattr(settings.services, "memory_search_uri", f"http://127.0.0.1:{_free_port()}")
    result = probe.probe_backend("numpy")
    assert not result.fatal  # booting is transient — the retry loop owns the wait
    assert "memory_search service is not reachable" in (result.message or "")
    assert "AVA_MEMORY_SEARCH_BACKEND=numpy" in (result.message or "")


def test_probe_milvus_unreachable_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.services, "milvus_uri", f"http://127.0.0.1:{_free_port()}")
    result = probe.probe_backend("milvus")
    assert not result.fatal
    assert "milvus is not reachable" in (result.message or "")
    assert "AVA_MEMORY_SEARCH_BACKEND=numpy" in (result.message or "")


def test_probe_unknown_backend_is_fatal() -> None:
    result = probe.probe_backend("qdrant")
    assert result.fatal
    assert "unknown memory search backend" in (result.message or "")
