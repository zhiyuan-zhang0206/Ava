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
from shared.config import settings

_BACKENDS: dict[str, Callable[[], MemorySearchBackend]] = {
    MilvusBackend.name: MilvusBackend,
}


def get_backend() -> MemorySearchBackend:
    """Construct the configured backend.

    Unknown names fail fast — an unrecognized `AVA_MEMORY_SEARCH_BACKEND`
    must not silently fall back to milvus (a typo would otherwise keep the
    old storage while the operator believes the switch happened).
    """
    name = settings.services.memory_search_backend
    try:
        ctor = _BACKENDS[name]
    except KeyError:
        known = ", ".join(sorted(_BACKENDS))
        raise ValueError(f"unknown memory search backend {name!r} (known: {known})") from None
    return ctor()
