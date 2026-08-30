"""The embedding provider contract.

One protocol (`EmbeddingProvider`), N implementations (Gemini today), one
switch (`embeddings.factory.get_provider()` reading
`AVA_EMBEDDING_BACKEND`). The indexer daemon (document batch path) and the
gateway search endpoint (async query path) both take their provider from
the factory; neither imports a concrete provider directly.

The provider is the single source of truth for the vector space. `dim`
declares the row width (what every storage backend's schema must fit) and
`fingerprint` identifies the semantic space uniquely — a provider switch
changes the space even at the same dim, so the indexer records the
fingerprint per index row and re-embeds any row whose fingerprint differs
(see `services.memory_indexer.daemon` and `backends.base`). Storage
backends receive `dim` + `fingerprint` at construction via
`backends.factory.get_backend(...)`, never by importing provider
constants.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np


class EmbeddingAPIError(RuntimeError):
    """An embedding provider call still failed after N retries.

    The indexer daemon skips the current file and retries on the next
    watch event; `ava.memory.search` wraps this as `IndexerUnavailable`
    and raises to the caller.
    """


class EmbeddingProvider(Protocol):
    """Embedding contract behind the memory index (CTO-frozen 2026-08-30).

    Every implementation declares the vector space it produces (`dim`,
    `fingerprint`) and implements exactly these call shapes — aligned with
    the real usage the abstraction was carved out of:

    - `embed_batch` — the indexer daemon's document embeds
      (`RETRIEVAL_DOCUMENT` semantics); returns (N, dim) float32.
    - `embed_query_async` — the gateway's per-query embeds inside the
      search deadline (`RETRIEVAL_QUERY` semantics); returns (dim,) float32.
    - `embed_query` — single sync query ("RETRIEVAL_QUERY" semantics) for
      ops tooling that embeds one query and compares several backends
      (`scripts/memory_search_reconcile.py`).

    A provider may distinguish document vs query task types internally
    (Gemini uses different projections for retrieval); the contract
    leaves the mapping to the implementation as long as the batch path
    embeds documents and the query paths embed queries.

    The factory owns construction; callers only call.
    """

    name: str
    dim: int
    fingerprint: str

    def embed_batch(self, texts: list[str]) -> np.ndarray: ...
    def embed_query(self, text: str) -> np.ndarray: ...
    async def embed_query_async(self, text: str) -> np.ndarray: ...
