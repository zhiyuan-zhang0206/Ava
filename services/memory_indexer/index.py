"""Legacy compat surface for the pre-abstraction module API.

All storage logic moved to `services.memory_indexer.backends.milvus`
(module primitives + the `MilvusBackend` class) behind
`backends.factory.get_backend()` — new code goes through the factory,
never these functions. This module re-exports the old public surface so
the regression suite (`tests/services/test_memory_indexer.py`,
`tests/conftest.py`'s milvus fixture) keeps pinning the same functions it
always has. Delete this file once those tests migrate to the backend
classes.
"""

from __future__ import annotations

import numpy as np
from pymilvus import AsyncMilvusClient, MilvusClient

from services.memory_indexer.backends import milvus as _milvus
from services.memory_indexer.backends.base import KIND_BODY, KIND_DESC, content_hash, pk_of

# Schema internals pinned by the legacy regression suite (raw queries and
# schema-migration tests). Aliased, not re-implemented — one source of truth.
_COLLECTION = _milvus._COLLECTION
_EXPECTED_FIELDS = _milvus._EXPECTED_FIELDS

# The shared row vocabulary is re-exported here too — the daemon used to
# import it from this module and the tests still do.
__all__ = [
    "KIND_BODY",
    "KIND_DESC",
    "all_meta",
    "connect",
    "connect_async",
    "content_hash",
    "delete",
    "pk_of",
    "search_topk",
    "search_topk_async",
    "server_uri",
    "upsert",
]


def server_uri() -> str:
    """Milvus server URI — see `backends.milvus.server_uri`."""
    return _milvus.server_uri()


def connect() -> MilvusClient:
    """Connect + ensure collection + load — see `backends.milvus._connect`."""
    return _milvus._connect()


def upsert(
    client: MilvusClient,
    path: str,
    mtime: float,
    hash_: str,
    embedding: np.ndarray,
    *,
    kind: str = KIND_BODY,
    chunk_idx: int = 0,
) -> None:
    """Legacy wrapper over `backends.milvus._upsert`."""
    _milvus._upsert(client, path, mtime, hash_, embedding, kind=kind, chunk_idx=chunk_idx)


def delete(client: MilvusClient, path: str) -> None:
    """Legacy wrapper over `backends.milvus._delete`."""
    _milvus._delete(client, path)


def all_meta(client: MilvusClient) -> dict[str, tuple[float, str]]:
    """Legacy wrapper over `backends.milvus._all_meta`."""
    return _milvus._all_meta(client)


async def connect_async(*, timeout: float) -> AsyncMilvusClient:
    """Legacy wrapper over `backends.milvus._connect_async`."""
    return await _milvus._connect_async(timeout=timeout)


async def search_topk_async(
    client: AsyncMilvusClient, query_vector: np.ndarray, k: int, *, timeout: float
) -> list[str]:
    """Legacy wrapper over `backends.milvus._search_topk_async`."""
    return await _milvus._search_topk_async(client, query_vector, k, timeout=timeout)


def search_topk(client: MilvusClient, query_vector: np.ndarray, k: int) -> list[str]:
    """Legacy wrapper over `backends.milvus._search_topk`."""
    return _milvus._search_topk(client, query_vector, k)


def _schema_current(client: MilvusClient) -> bool:
    """Legacy wrapper over `backends.milvus._schema_current` (schema-migration tests)."""
    return _milvus._schema_current(client)
