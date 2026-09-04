"""The NumPy store — chunk rows in memory + npz persistence.

One `MemoryStore` instance lives for the service process's lifetime. All
rows sit in Python lists + one float32 matrix; mutations rewrite the npz
atomically (tmp file + `os.replace`), so a crash mid-save can never leave
a torn file behind. The service wraps every operation in one asyncio
lock — search included (a full exact scan is microseconds at this scale,
so the lock is never contended meaningfully).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from pathlib import Path
from zipfile import BadZipFile

import numpy as np

from services.memory_indexer.backends.base import KIND_BODY, pk_of

_log = logging.getLogger("services.memory_search.store")

_NPZ_VECTORS = "vectors"
_NPZ_PKS = "pks"
_NPZ_PATHS = "paths"
_NPZ_KINDS = "kinds"
_NPZ_CHUNK_IDX = "chunk_idx"
_NPZ_MTIMES = "mtimes"
_NPZ_HASHES = "content_hashes"
_NPZ_EMBEDDERS = "embedders"

_NPZ_KEYS = frozenset(
    {
        _NPZ_VECTORS,
        _NPZ_PKS,
        _NPZ_PATHS,
        _NPZ_KINDS,
        _NPZ_CHUNK_IDX,
        _NPZ_MTIMES,
        _NPZ_HASHES,
        _NPZ_EMBEDDERS,
    }
)


class MemoryStore:
    """Chunk rows keyed by the shared folded pk — the same row vocabulary
    every backend uses, so reconciliation can compare rows 1:1.

    The embedding vector space is injected (the provider's `dim` and
    `fingerprint`, wired by `services.memory_search.daemon` from
    `embeddings.factory.get_provider()`): the store is the numpy backend's
    storage, and its matrix width IS the provider's dim. A loaded npz whose
    width differs is a stale cache from another provider — the store starts
    empty and the cold-start reconcile rebuilds it.
    """

    def __init__(self, data_file: Path, dim: int, fingerprint: str) -> None:
        self._data_file = data_file
        self._dim = dim
        self._fingerprint = fingerprint
        self._pk_index: dict[str, int] = {}
        self._pks: list[str] = []
        self._paths: list[str] = []
        self._kinds: list[str] = []
        self._chunk_idx: list[int] = []
        self._mtimes: list[float] = []
        self._hashes: list[str] = []
        self._embedders: list[str] = []
        self._matrix = np.empty((0, dim), dtype=np.float32)
        # Duration of the most recent successful save(), None until the first
        # one — the /stats endpoint and the 60s stats flusher expose it so a
        # growing npz cost is visible before it becomes a write stall.
        self._last_save_seconds: float | None = None

    # ── persistence ──────────────────────────────────────────────────────

    def load(self) -> None:
        """Load rows from the npz when it exists; a fresh service starts
        empty and the indexer's cold-start reconcile fills it.

        Four broken-file paths degrade to "start empty" instead of raising —
        a file missing keys, a file that does not parse at all
        (truncated / foreign bytes), a file that half-converts (one
        column fails mid-load), and a vector width that differs from the
        configured provider's dim (another provider's index, or a provider
        switch without a restart of this service). All four are a broken
        cache: the memory pool is the source of truth and the cold-start
        reconcile rebuilds the index, so there is nothing worth crashing the
        daemon for — a raise here would mean the watchdog respawns into the
        same broken file every 60s (the restart-loop shape the keepalive
        policy exists to stop, but only a degradable load makes it a
        non-issue)."""
        if not self._data_file.exists():
            return
        try:
            with np.load(self._data_file) as data:
                missing = _NPZ_KEYS - set(data.files)
                if missing:
                    # A torn/foreign file is a broken cache — start empty and let
                    # the cold-start reconcile rebuild it rather than guess.
                    _log.warning(
                        "[memory_search] %s missing keys %s — starting empty; "
                        "cold-start will rebuild the index",
                        self._data_file,
                        sorted(missing),
                    )
                    return
                pks = [str(p) for p in data[_NPZ_PKS].tolist()]
                paths = [str(p) for p in data[_NPZ_PATHS].tolist()]
                kinds = [str(k) for k in data[_NPZ_KINDS].tolist()]
                chunk_idx = [int(i) for i in data[_NPZ_CHUNK_IDX].tolist()]
                mtimes = [float(m) for m in data[_NPZ_MTIMES].tolist()]
                hashes = [str(h) for h in data[_NPZ_HASHES].tolist()]
                matrix = data[_NPZ_VECTORS].astype(np.float32)
                if matrix.ndim == 1:
                    matrix = matrix.reshape(1, -1)
                if matrix.shape[1] != self._dim:
                    # Another provider's vector space (same dim ≠ same
                    # semantic space, but a different dim cannot even be
                    # searched consistently) — the npz is a stale cache.
                    _log.warning(
                        "[memory_search] %s vector dim %d != configured provider dim %d — "
                        "starting empty; cold-start will rebuild the index",
                        self._data_file,
                        matrix.shape[1],
                        self._dim,
                    )
                    return
                embedders = [str(e) for e in data[_NPZ_EMBEDDERS].tolist()]
                pk_index = {pk: idx for idx, pk in enumerate(pks)}
        except (OSError, ValueError, EOFError, KeyError, BadZipFile) as exc:
            # np.load raises ValueError / EOFError on foreign bytes, BadZipFile
            # (a plain Exception subclass, not OSError) on a truncated zip,
            # OSError on read failures, a malformed array can surface a
            # KeyError on access, and a column that fails conversion raises
            # ValueError — every one of them is the same "broken cache"
            # condition.
            _log.warning(
                "[memory_search] %s does not parse or fully convert (%s: %s) — "
                "starting empty; cold-start will rebuild the index",
                self._data_file,
                type(exc).__name__,
                exc,
            )
            return
        # Every column converted — commit all-or-nothing, so a mid-file
        # failure can never leave a half-loaded store (a few columns
        # populated, the rest empty).
        self._pks = pks
        self._paths = paths
        self._kinds = kinds
        self._chunk_idx = chunk_idx
        self._mtimes = mtimes
        self._hashes = hashes
        self._embedders = embedders
        self._matrix = matrix
        self._pk_index = pk_index
        _log.info("[memory_search] loaded %d rows from %s", len(self._pks), self._data_file)

    def save(self) -> None:
        """Atomically rewrite the npz — tmp file in the same directory +
        `os.replace` (same-filesystem rename is atomic).

        Times the whole write+replace and records it as
        `last_save_seconds` on success only — a failed save (np.savez
        raising) leaves the previous duration untouched, because the
        number operators read is "what did the last completed persistence
        cost", not "what did the last attempt cost"."""
        start = time.perf_counter()
        self._data_file.parent.mkdir(parents=True, exist_ok=True)
        # np.savez appends ".npz" to any name not already ending in it, so the
        # tmp name must end in ".npz" for the replace below to find the file.
        tmp = self._data_file.with_suffix(".tmp.npz")
        np.savez(
            tmp,
            pks=np.array(self._pks, dtype=str),
            paths=np.array(self._paths, dtype=str),
            kinds=np.array(self._kinds, dtype=str),
            chunk_idx=np.array(self._chunk_idx, dtype=np.int64),
            mtimes=np.array(self._mtimes, dtype=np.float64),
            content_hashes=np.array(self._hashes, dtype=str),
            embedders=np.array(self._embedders, dtype=str),
            vectors=self._matrix,
        )
        tmp.replace(self._data_file)
        self._last_save_seconds = time.perf_counter() - start

    # ── mutations ────────────────────────────────────────────────────────

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
        """Write / update one chunk row; re-upserting the same triple
        overwrites in place — identical semantics to the milvus backend."""
        self.upsert_many([(path, mtime, content_hash, embedding, kind, chunk_idx)])

    def upsert_many(self, rows: Sequence[tuple[str, float, str, np.ndarray, str, int]]) -> None:
        """Apply chunk rows in order without persisting.

        Every vector is normalized and dimension-checked before any state
        changes. New rows grow the matrix together, while repeated keys update
        the row reserved by their first occurrence so the last write wins.
        """
        validated_rows: list[tuple[str, float, str, np.ndarray, str, int]] = []
        for path, mtime, content_hash, embedding, kind, chunk_idx in rows:
            vector = embedding.astype(np.float32).reshape(-1)
            if vector.shape != (self._dim,):
                raise ValueError(f"embedding shape {vector.shape} != ({self._dim},)")
            validated_rows.append((path, float(mtime), content_hash, vector, kind, int(chunk_idx)))

        initial_row_count = len(self._pks)
        new_vectors: list[np.ndarray] = []
        for path, mtime, content_hash, vector, kind, chunk_idx in validated_rows:
            pk = pk_of(path, kind, chunk_idx)
            idx = self._pk_index.get(pk)
            if idx is None:
                idx = len(self._pks)
                self._pks.append(pk)
                self._paths.append(path)
                self._kinds.append(kind)
                self._chunk_idx.append(chunk_idx)
                self._mtimes.append(mtime)
                self._hashes.append(content_hash)
                self._embedders.append(self._fingerprint)
                self._pk_index[pk] = idx
                new_vectors.append(vector)
                continue

            self._paths[idx] = path
            self._kinds[idx] = kind
            self._chunk_idx[idx] = chunk_idx
            self._mtimes[idx] = mtime
            self._hashes[idx] = content_hash
            self._embedders[idx] = self._fingerprint
            if idx < initial_row_count:
                self._matrix[idx] = vector
            else:
                new_vectors[idx - initial_row_count] = vector

        if new_vectors:
            self._matrix = np.concatenate((self._matrix, new_vectors))

    def delete(self, path: str) -> None:
        """Delete every chunk row of `path`; no-op when the path is absent."""
        keep_idx = [i for i, p in enumerate(self._paths) if p != path]
        if len(keep_idx) == len(self._paths):
            return
        self._pks = [self._pks[i] for i in keep_idx]
        self._paths = [self._paths[i] for i in keep_idx]
        self._kinds = [self._kinds[i] for i in keep_idx]
        self._chunk_idx = [self._chunk_idx[i] for i in keep_idx]
        self._mtimes = [self._mtimes[i] for i in keep_idx]
        self._hashes = [self._hashes[i] for i in keep_idx]
        self._embedders = [self._embedders[i] for i in keep_idx]
        self._matrix = self._matrix[keep_idx]
        self._pk_index = {pk: idx for idx, pk in enumerate(self._pks)}

    def all_meta(self) -> dict[str, tuple[float, str, str]]:
        """Per-path (mtime, content_hash, provider_fingerprint) — one entry
        per **file**, not per chunk (aggregation keeps the max mtime; same
        contract as milvus). The fingerprint is the reconcile key's third
        element."""
        meta: dict[str, tuple[float, str, str]] = {}
        for path, mtime, hash_, fingerprint in zip(
            self._paths, self._mtimes, self._hashes, self._embedders, strict=True
        ):
            prev = meta.get(path)
            if prev is None or mtime > prev[0]:
                meta[path] = (mtime, hash_, fingerprint)
        return meta

    def search_topk(self, query_vector: np.ndarray, k: int) -> list[str]:
        """Exact cosine top-k **paths**, aggregated over chunk rows.

        One matrix product over every row (microseconds at pool scale), then
        per path keep the best (maximum) cosine — the exact counterpart of the
        milvus backend's minimum-distance aggregation, so both order paths the
        same way. Returns fewer than k when the store has fewer distinct
        paths; empty when the store is empty.
        """
        query = query_vector.astype(np.float32).reshape(-1)
        if query.shape != (self._dim,):
            raise ValueError(f"query shape {query.shape} != ({self._dim},)")
        if self._matrix.shape[0] == 0:
            return []
        q_norm = float(np.linalg.norm(query))
        row_norms = np.linalg.norm(self._matrix, axis=1)
        if q_norm == 0.0:
            return list(dict.fromkeys(self._paths))[:k]  # degenerate query: stable order
        cos = self._matrix @ query / (row_norms * q_norm)
        best: dict[str, float] = {}
        for path, sim in zip(self._paths, cos, strict=True):
            if path not in best or float(sim) > best[path]:
                best[path] = float(sim)
        return sorted(best, key=best.__getitem__, reverse=True)[:k]

    @property
    def last_save_seconds(self) -> float | None:
        """Duration (seconds) of the most recent successful save — None until
        the first save since process start."""
        return self._last_save_seconds

    def __len__(self) -> int:
        return len(self._pks)
