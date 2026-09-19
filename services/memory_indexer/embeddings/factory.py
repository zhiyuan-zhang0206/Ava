"""One switch selects the embedding provider.

`get_provider()` is the single entry point: the indexer daemon (document
batch path) and the gateway search endpoint (async query path) both take
their provider from here, keyed by `settings.services.embedding_backend`
(`AVA_EMBEDDING_BACKEND`, default `gemini`). Unknown names fail fast — an
unrecognized value raises ValueError naming the known providers instead of
silently falling back to Gemini (a typo would otherwise keep the old
provider while the operator believes the switch happened).

Each call returns a fresh, stateless provider; construction does no
network I/O. Every registry entry pairs its constructor with a document-batch
retry budget, exposed by `worst_case_batch_seconds()` for daemon liveness.
"""

from __future__ import annotations

from collections.abc import Callable

from services.memory_indexer.embeddings import gemini
from services.memory_indexer.embeddings.base import EmbeddingProvider
from shared.config import settings

_ProviderEntry = tuple[Callable[[], EmbeddingProvider], Callable[[], float]]
_PROVIDERS: dict[str, _ProviderEntry] = {
    gemini.GeminiEmbeddingProvider.name: (
        gemini.GeminiEmbeddingProvider,
        gemini.worst_case_batch_seconds,
    ),
}


def _provider_entry(name: str) -> _ProviderEntry:
    try:
        return _PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(_PROVIDERS))
        raise ValueError(f"unknown embedding provider {name!r} (known: {known})") from None


def get_provider_named(name: str) -> EmbeddingProvider:
    """Construct a provider by name; unknown names fail fast."""
    ctor, _ = _provider_entry(name)
    return ctor()


def get_provider() -> EmbeddingProvider:
    """Construct the configured provider
    (`settings.services.embedding_backend`, env `AVA_EMBEDDING_BACKEND`)."""
    return get_provider_named(settings.services.embedding_backend)


def worst_case_batch_seconds() -> float:
    """Return the configured provider's full document-batch retry budget."""
    _, batch_seconds = _provider_entry(settings.services.embedding_backend)
    return batch_seconds()
