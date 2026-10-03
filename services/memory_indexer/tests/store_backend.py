"""An in-process `MemorySearchBackend` over the real `MemoryStore`.

The daemon tests run the indexer's reconcile / chunk-commit logic against the
production numpy storage core (`services.memory_search.store.MemoryStore`)
without the HTTP hop: the adapter only adds the lifecycle and async members the
protocol requires, plus a read-only row view for assertions the protocol does
not expose.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np

from services.memory_indexer.backends.base import KIND_BODY
from services.memory_search.store import MemoryStore


class StoreBackend:
    """Protocol-compliant backend that delegates every call to a `MemoryStore`."""

    name = "store"

    def __init__(self, data_file: Path, *, dim: int, fingerprint: str) -> None:
        self._data_file = data_file
        self._dim = dim
        self.store = MemoryStore(data_file, dim=dim, fingerprint=fingerprint)

    def reopen(self, fingerprint: str) -> StoreBackend:
        """A fresh backend over the persisted npz, built for another provider —
        what a service restart under a switched provider looks like."""
        self.store.save()
        reopened = StoreBackend(self._data_file, dim=self._dim, fingerprint=fingerprint)
        reopened.store.load()
        return reopened

    def rows(self, path: Path) -> set[tuple[str, int]]:
        """The (kind, chunk_idx) of every row stored for `path` (resolved, as
        the daemon keys rows)."""
        target = str(path.resolve())
        return {
            (kind, idx)
            for row_path, kind, idx in zip(
                self.store._paths, self.store._kinds, self.store._chunk_idx, strict=True
            )
            if row_path == target
        }

    def connect(self) -> None:
        """No-op: the store lives in this process."""

    def close(self) -> None:
        """No-op: nothing to release."""

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
        self.store.upsert(path, mtime, content_hash, embedding, kind=kind, chunk_idx=chunk_idx)

    def upsert_many(self, rows: Sequence[tuple[str, float, str, np.ndarray, str, int]]) -> None:
        self.store.upsert_many(rows)

    def delete(self, path: str) -> None:
        self.store.delete(path)

    def delete_stale_rows(self, entries: Sequence[tuple[str, dict[str, int]]]) -> None:
        self.store.delete_stale_rows(entries)

    def all_meta(self) -> dict[str, tuple[float, str, str]]:
        return self.store.all_meta()

    def search_topk(self, query_vector: np.ndarray, k: int) -> list[str]:
        return self.store.search_topk(query_vector, k)

    async def search_topk_async(
        self, query_vector: np.ndarray, k: int, *, timeout: float
    ) -> list[str]:
        return self.store.search_topk(query_vector, k)
