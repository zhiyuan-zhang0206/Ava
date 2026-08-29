"""One switch selects the memory search storage backend.

`get_backend()` is the single entry point: the indexer daemon (write
path) and the gateway search endpoint (read path) both take their backend
from here, keyed by `settings.services.memory_search_backend`
(`AVA_MEMORY_SEARCH_BACKEND`, default `milvus`). Switching storage is one
env var + a restart; the cold-start reconcile rebuilds the index on the
new backend without hand-copying data.

Each call returns a fresh, unconnected backend; the caller owns connect /
close (the daemon holds one for its lifetime; the gateway's async search
connects and closes per request).
"""

from __future__ import annotations

from collections.abc import Callable

from services.memory_indexer.backends.base import MemorySearchBackend
from services.memory_indexer.backends.milvus import MilvusBackend
from services.memory_indexer.backends.numpy import NumPyBackend
from shared.config import settings

_BACKENDS: dict[str, Callable[[], MemorySearchBackend]] = {
    MilvusBackend.name: MilvusBackend,
    NumPyBackend.name: NumPyBackend,
}


def get_backend_named(name: str) -> MemorySearchBackend:
    """Construct a backend by name — the one dispatch path; unknown names
    fail fast (an unrecognized value must not silently fall back to milvus:
    a typo would otherwise keep the old storage while the operator believes
    the switch happened)."""
    try:
        ctor = _BACKENDS[name]
    except KeyError:
        known = ", ".join(sorted(_BACKENDS))
        raise ValueError(f"unknown memory search backend {name!r} (known: {known})") from None
    return ctor()


def get_backend() -> MemorySearchBackend:
    """Construct the configured backend
    (`settings.services.memory_search_backend`, env
    `AVA_MEMORY_SEARCH_BACKEND`)."""
    return get_backend_named(settings.services.memory_search_backend)
