"""One switch selects the embedding provider.

`get_provider()` is the single entry point: the indexer daemon (document
batch path) and the gateway search endpoint (async query path) both take
their provider from here, keyed by `settings.services.embedding_backend`
(`AVA_EMBEDDING_BACKEND`, default `gemini`). Unknown names fail fast — an
unrecognized value raises ValueError naming the known providers instead of
silently falling back to Gemini (a typo would otherwise keep the old
provider while the operator believes the switch happened).

Each call returns a fresh, stateless provider; construction does no
network I/O.
"""

from __future__ import annotations

from collections.abc import Callable

from services.memory_indexer.embeddings.base import EmbeddingProvider
from services.memory_indexer.embeddings.gemini import GeminiEmbeddingProvider
from shared.config import settings

_PROVIDERS: dict[str, Callable[[], EmbeddingProvider]] = {
    GeminiEmbeddingProvider.name: GeminiEmbeddingProvider,
}


def get_provider_named(name: str) -> EmbeddingProvider:
    """Construct a provider by name — the one dispatch path; unknown names
    fail fast (see module docstring)."""
    try:
        ctor = _PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(_PROVIDERS))
        raise ValueError(f"unknown embedding provider {name!r} (known: {known})") from None
    return ctor()


def get_provider() -> EmbeddingProvider:
    """Construct the configured provider
    (`settings.services.embedding_backend`, env `AVA_EMBEDDING_BACKEND`)."""
    return get_provider_named(settings.services.embedding_backend)
