"""One switch selects the memory search storage backend.

`get_backend()` is the single entry point: the indexer daemon (write
path) and the gateway search endpoint (read path) both take their backend
from here, keyed by `settings.services.memory_search_backend`
(`AVA_MEMORY_SEARCH_BACKEND`, default `milvus`). Switching storage is one
env var + a restart; the cold-start reconcile rebuilds the index on the
new backend.

Each call returns a fresh, unconnected backend; the caller owns connect /
close (the daemon holds one for its lifetime; the gateway's async search
connects and closes per request).

The embedding vector space is injected at construction: callers pass the
provider's `dim` (schema width) and `fingerprint` (semantic-space
identifier stored per row) — a backend never imports a provider constant,
so changing embedding providers is a data change, not a code change.
"""

from __future__ import annotations

from collections.abc import Callable

from services.memory_indexer.backends.base import MemorySearchBackend
from services.memory_indexer.backends.milvus import MilvusBackend
from services.memory_indexer.backends.numpy import NumPyBackend
from services.memory_indexer.backends.pgvector import PGVectorBackend
from shared.config import settings


def _numpy_backend(dim: int, fingerprint: str) -> NumPyBackend:
    """NumPyBackend is a thin HTTP client — the vector space lives in the
    memory_search service process, so dim/fingerprint are accepted for the
    uniform factory signature only and deliberately not used."""
    del dim, fingerprint
    return NumPyBackend()


# Uniform constructor shape `(dim: int, fingerprint: str)`; numpy's backend
# is wrapped above instead of taking dead parameters.
_BACKENDS: dict[str, Callable[[int, str], MemorySearchBackend]] = {
    MilvusBackend.name: lambda dim, fingerprint: MilvusBackend(dim=dim, fingerprint=fingerprint),
    NumPyBackend.name: _numpy_backend,
    PGVectorBackend.name: lambda dim, fingerprint: PGVectorBackend(
        dim=dim, fingerprint=fingerprint
    ),
}


def get_backend_named(name: str, *, dim: int, fingerprint: str) -> MemorySearchBackend:
    """Construct a backend by name — the one dispatch path; unknown names
    fail fast (an unrecognized value must not silently fall back to milvus:
    a typo would otherwise keep the old storage while the operator believes
    the switch happened)."""
    try:
        ctor = _BACKENDS[name]
    except KeyError:
        known = ", ".join(sorted(_BACKENDS))
        raise ValueError(f"unknown memory search backend {name!r} (known: {known})") from None
    return ctor(dim, fingerprint)


def get_backend(dim: int, fingerprint: str) -> MemorySearchBackend:
    """Construct the configured backend
    (`settings.services.memory_search_backend`, env
    `AVA_MEMORY_SEARCH_BACKEND`) for the given embedding vector space."""
    return get_backend_named(
        settings.services.memory_search_backend, dim=dim, fingerprint=fingerprint
    )
