"""NumPy memory search backend — HTTP client over the local memory_search service.

The storage lives in the standalone `services/memory_search/` process
(loopback HTTP on 19531), so the indexer daemon (write path) and the
gateway (read path) both talk to it exactly like they talk to the milvus
daemon — one process owns the in-memory matrix + npz, no cross-process
shared state.

`connect()` opens the sync client the daemon's batched writes use and
probes the service (a real GET /meta — the retry loop in
`services.memory_indexer.daemon` calls connect() until the service is up).
The gateway's `search_topk_async` opens a per-call async client bounded by
the caller's deadline — the same per-request lifecycle as the milvus
backend.
"""

from __future__ import annotations

import httpx
import numpy as np

from services.memory_indexer.backends.base import KIND_BODY
from shared.config import settings


class NumPyBackend:
    """Protocol-compliant backend over the memory_search HTTP service."""

    name = "numpy"

    def __init__(self) -> None:
        self._client: httpx.Client | None = None

    @property
    def _uri(self) -> str:
        return settings.services.memory_search_uri

    def connect(self) -> None:
        """Open the sync client + prove the service answers (a real GET
        /meta, not a TCP connect — a foreign process on the port fails here
        and the daemon's retry loop keeps waiting)."""
        client = httpx.Client(base_url=self._uri, timeout=5.0)
        try:
            client.get("/meta").raise_for_status()
        except Exception:
            client.close()
            raise
        self._client = client

    def close(self) -> None:
        """Close the client when one exists; idempotent."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def _require_client(self) -> httpx.Client:
        if self._client is None:
            raise RuntimeError(f"{self.name} backend not connected — call connect() first")
        return self._client

    def upsert(
        self,
        path: str,
        mtime: float,
        content_hash: str,
        embedding: np.ndarray,
        *,
        kind: str = KIND_BODY,
        chunk_idx: int = 0,
    ) -> None:
        """Write / update one chunk row — see `backends.base`."""
        resp = self._require_client().post(
            "/upsert",
            json={
                "path": path,
                "mtime": float(mtime),
                "content_hash": content_hash,
                "kind": kind,
                "chunk_idx": int(chunk_idx),
                "vector": embedding.astype(np.float32).tolist(),
            },
        )
        resp.raise_for_status()

    def delete(self, path: str) -> None:
        """Delete every chunk row of `path`; the service no-ops when absent."""
        self._require_client().post("/delete", json={"path": path}).raise_for_status()

    def all_meta(self) -> dict[str, tuple[float, str, str]]:
        """Per-path (mtime, content_hash, provider_fingerprint) — see
        `backends.base`."""
        resp = self._require_client().get("/meta")
        resp.raise_for_status()
        return {
            path: (float(mtime), str(hash_), str(fingerprint))
            for path, (mtime, hash_, fingerprint) in resp.json().items()
        }

    def search_topk(self, query_vector: np.ndarray, k: int) -> list[str]:
        """Exact cosine top-k paths — see `backends.base`."""
        resp = self._require_client().post(
            "/search",
            json={"vector": query_vector.astype(np.float32).tolist(), "k": k},
        )
        resp.raise_for_status()
        return [str(p) for p in resp.json()["paths"]]

    async def search_topk_async(
        self, query_vector: np.ndarray, k: int, *, timeout: float
    ) -> list[str]:
        """Async twin of `search_topk` — a per-call async client bounded by
        the caller's deadline, closed in `finally` (same lifecycle as the
        milvus backend's async path)."""
        async with httpx.AsyncClient(base_url=self._uri, timeout=timeout) as client:
            resp = await client.post(
                "/search",
                json={"vector": query_vector.astype(np.float32).tolist(), "k": k},
            )
            resp.raise_for_status()
            return [str(p) for p in resp.json()["paths"]]
