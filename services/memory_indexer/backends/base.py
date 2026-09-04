"""The memory search backend contract + the indexer-layer row vocabulary.

The protocol is the CTO-frozen interface (memory search backend
abstraction, 2026-08-30): every backend implements exactly these eight
methods, so switching storage is one env var + a restart — the cold-start
reconcile rebuilds the index on the new backend without hand-copying data.

The chunk/pk helpers live here because every backend shares them: they
are the indexer layer's row vocabulary (chunking itself stays in the
daemon, embedding in `services.memory_indexer.embeddings`), not any one
backend's storage detail.

The embedding vector space is backend-constructor-injected, never
imported: `backends.factory.get_backend(dim=..., fingerprint=...)` hands
each backend the provider's `dim` (schema width) and `fingerprint`
(identifies the semantic space; stored per row so a provider switch
triggers a full re-embed). Backends therefore contain no provider
constant and no import of the embeddings package.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Protocol

import numpy as np

KIND_DESC = "desc"
"""kind value for the frontmatter-description row of a file."""
KIND_BODY = "body"
"""kind value for a body-chunk row of a file."""

# Unit separator — cannot appear in filesystem paths, so (path, kind, idx)
# round-trips through the folded pk unambiguously.
_SEP = "\x1f"

_PK_MAX_LENGTH = 2048
"""pk = path (≤1024) + sep + kind (≤16) + sep + chunk_idx — 2048 fits the worst case."""


def content_hash(text: str) -> str:
    """sha256 hex digest of utf-8 bytes. Secondary gate when mtime is
    unreliable (e.g. touch without content change); avoids re-embedding
    unchanged content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pk_of(path: str, kind: str, chunk_idx: int) -> str:
    """The single row key encoding (path, kind, chunk_idx).

    milvus-lite 3.x allows only one primary-key field, so the triple folds
    into a VARCHAR key joined with a unit separator — deterministic and
    reversible, unlike a hash. All chunk rows of one path share the `path`
    prefix, which is what delete-by-path keys on. Backends whose storage
    keys on the triple reuse it for the same reason.
    """
    return f"{path}{_SEP}{kind}{_SEP}{chunk_idx}"


class MemorySearchBackend(Protocol):
    """Storage contract behind the memory search index (CTO-frozen 2026-08-30).

    The write path is the indexer daemon's (`upsert` / `upsert_many` / `delete` /
    `all_meta`); the read path is the gateway's (`search_topk` returns
    top-k **paths**, aggregated over chunk rows). Backends may implement
    the async variant with internal thread pooling — no true-async
    requirement beyond never blocking a caller's event loop. The factory
    owns construction; callers only `connect` / use / `close`.
    """

    name: str  # "milvus" | "numpy" | "pgvector"

    # Lifecycle (factory hands out instances; callers only use them).
    def connect(self) -> None: ...
    def close(self) -> None: ...

    # The vector space is fixed at construction (factory injection): every
    # backend knows the row width (`dim`) and stamps each row with the
    # provider `fingerprint` it was built for, so `all_meta` can return it
    # and the daemon can detect a provider switch.

    # Write path (indexer daemon).
    def upsert(
        self,
        path: str,
        mtime: float,
        content_hash: str,
        embedding: np.ndarray,
        *,
        kind: str,
        chunk_idx: int,
    ) -> None: ...

    def upsert_many(self, rows: Sequence[tuple[str, float, str, np.ndarray, str, int]]) -> None:
        """Apply the same per-row semantics as `upsert`, in order.

        A backend may batch the rows into one storage write or round trip.
        Empty rows are a no-op.
        """
        ...

    def delete(self, path: str) -> None: ...

    def all_meta(
        self,
    ) -> dict[str, tuple[float, str, str]]:
        """Per-path (mtime, content_hash, provider_fingerprint) — the
        reconcile key. A fingerprint that differs from the configured
        provider's means the row belongs to another semantic space and must
        be re-embedded."""
        ...

    # Read path (gateway).
    def search_topk(self, query_vector: np.ndarray, k: int) -> list[str]: ...

    async def search_topk_async(
        self, query_vector: np.ndarray, k: int, *, timeout: float
    ) -> list[str]: ...
