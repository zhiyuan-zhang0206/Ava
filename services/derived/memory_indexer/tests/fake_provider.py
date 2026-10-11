"""A constant-vector stand-in for the embedding provider, shared by the memory indexer's tests."""

from __future__ import annotations

import numpy as np

DIM = 8
FINGERPRINT = "test:gemini:dim=8"


class FakeProvider:
    """Stand-in for `EmbeddingProvider` — records embed calls, returns
    constant vectors; `fingerprint` is settable so tests can simulate a
    provider switch."""

    def __init__(self, *, fingerprint: str = FINGERPRINT, dim: int = DIM) -> None:
        self.name = "fake"
        self.dim = dim
        self.fingerprint = fingerprint
        self.embed_batch_count = 0

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        self.embed_batch_count += 1
        return np.array([[float(len(t))] * self.dim for t in texts], dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return np.zeros(self.dim, dtype=np.float32)

    async def embed_query_async(self, text: str) -> np.ndarray:
        return np.zeros(self.dim, dtype=np.float32)
