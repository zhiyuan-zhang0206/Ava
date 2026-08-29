"""Memory search API endpoint tests — the HTTP contract NumPyBackend dials.

FastAPI TestClient against the real app + store (no socket), so the wire
contract is pinned: request/response shapes, status codes, and the
persist-before-ack guarantee.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from services.memory_indexer import embedder
from services.memory_search.app import build_app
from services.memory_search.store import MemoryStore


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(embedder.DIM).astype(np.float32)


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(MemoryStore(tmp_path / "vectors.npz")))


def test_healthz(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/healthz").json() == {"status": "ok"}


def test_upsert_delete_meta_roundtrip(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert (
            client.post(
                "/upsert",
                json={
                    "path": "/a.md",
                    "mtime": 1.0,
                    "content_hash": "ha",
                    "kind": "body",
                    "chunk_idx": 0,
                    "vector": _vec(0).tolist(),
                },
            ).status_code
            == 200
        )
        assert client.get("/meta").json() == {"/a.md": [1.0, "ha"]}
        assert client.post("/delete", json={"path": "/a.md"}).status_code == 200
        assert client.get("/meta").json() == {}


def test_search_returns_ordered_paths(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        ones = np.ones(embedder.DIM, dtype=np.float32)
        for path, vector in [("/a.md", ones), ("/b.md", -ones)]:
            assert (
                client.post(
                    "/upsert",
                    json={
                        "path": path,
                        "mtime": 1.0,
                        "content_hash": "h",
                        "kind": "body",
                        "chunk_idx": 0,
                        "vector": vector.tolist(),
                    },
                ).status_code
                == 200
            )
        body = client.post("/search", json={"vector": ones.tolist(), "k": 5}).json()
        assert body == {"paths": ["/a.md", "/b.md"]}


def test_upsert_persists_before_ack(tmp_path: Path) -> None:
    """A row acked by the API must survive a store rebuild from disk."""
    with _client(tmp_path) as client:
        ones = np.ones(embedder.DIM, dtype=np.float32)
        assert (
            client.post(
                "/upsert",
                json={
                    "path": "/a.md",
                    "mtime": 1.0,
                    "content_hash": "ha",
                    "kind": "body",
                    "chunk_idx": 0,
                    "vector": ones.tolist(),
                },
            ).status_code
            == 200
        )
    fresh = MemoryStore(tmp_path / "vectors.npz")
    fresh.load()
    assert fresh.all_meta() == {"/a.md": (1.0, "ha")}
    assert fresh.search_topk(ones, k=5) == ["/a.md"]


def test_upsert_rejects_wrong_dim_vector(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.post(
            "/upsert",
            json={
                "path": "/a.md",
                "mtime": 1.0,
                "content_hash": "h",
                "kind": "body",
                "chunk_idx": 0,
                "vector": [0.0] * 7,
            },
        )
        assert resp.status_code == 422


def test_upsert_rejects_oversized_vector(tmp_path: Path) -> None:
    """A vector longer than DIM is rejected by the model bound before numpy
    ever allocates (loopback hardening — the wire models carry size caps)."""
    with _client(tmp_path) as client:
        resp = client.post(
            "/upsert",
            json={
                "path": "/a.md",
                "mtime": 1.0,
                "content_hash": "h",
                "kind": "body",
                "chunk_idx": 0,
                "vector": [0.0] * (embedder.DIM + 1),
            },
        )
        assert resp.status_code == 422


def test_search_rejects_oversized_vector_and_bad_k(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert (
            client.post("/search", json={"vector": [0.0] * (embedder.DIM + 1), "k": 5}).status_code
            == 422
        )
        assert (
            client.post("/search", json={"vector": [0.0] * embedder.DIM, "k": 0}).status_code == 422
        )
        assert (
            client.post("/search", json={"vector": [0.0] * embedder.DIM, "k": 1001}).status_code
            == 422
        )
