"""One switch selects the embedding provider.

`get_provider()` is the single entry point: the indexer daemon (document
batch path) and the gateway search endpoint (async query path) both take
their provider from here, keyed by the name supplied by the composition root
(`AVA_EMBEDDING_BACKEND`, default `gemini`). Unknown names fail fast — an
unrecognized value raises ValueError naming the known providers instead of
silently falling back to Gemini (a typo would otherwise keep the old
provider while the operator believes the switch happened).

Each call returns a fresh provider holding the service root's catalog; construction does no
network I/O. Every registry entry pairs its constructor with a document-batch
retry budget, exposed by `worst_case_batch_seconds()` for daemon liveness.
`get_descriptor()` exposes dim/fingerprint for storage and health probes without
constructing a billing-capable provider or loading a catalog.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from base.lm.catalog import ModelCatalog
from services.derived.memory_indexer.embeddings import gemini
from services.derived.memory_indexer.embeddings.base import EmbeddingProvider


@dataclass(frozen=True)
class EmbeddingDescriptor:
    """The provider's vector space, available without a runtime catalog."""

    name: str
    dim: int
    fingerprint: str


_ProviderEntry = tuple[
    Callable[[ModelCatalog, Callable[[], float], Callable[[], str | None]], EmbeddingProvider],
    Callable[[float], float],
    EmbeddingDescriptor,
]
_PROVIDERS: dict[str, _ProviderEntry] = {
    gemini.GeminiEmbeddingProvider.name: (
        lambda catalog, timeout_reader, api_key_reader: gemini.GeminiEmbeddingProvider(
            catalog, timeout_reader=timeout_reader, api_key_reader=api_key_reader
        ),
        gemini.worst_case_batch_seconds,
        EmbeddingDescriptor(
            gemini.GeminiEmbeddingProvider.name,
            gemini.GeminiEmbeddingProvider.dim,
            gemini.GeminiEmbeddingProvider.fingerprint,
        ),
    ),
}


def _provider_entry(name: str) -> _ProviderEntry:
    try:
        return _PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(_PROVIDERS))
        raise ValueError(f"unknown embedding provider {name!r} (known: {known})") from None


def get_provider_named(
    name: str,
    *,
    catalog: ModelCatalog,
    timeout_reader: Callable[[], float],
    api_key_reader: Callable[[], str | None],
) -> EmbeddingProvider:
    """Construct a provider by name; unknown names fail fast."""
    ctor, _, _ = _provider_entry(name)
    return ctor(catalog, timeout_reader, api_key_reader)


def get_provider(
    name: str,
    *,
    catalog: ModelCatalog,
    timeout_reader: Callable[[], float],
    api_key_reader: Callable[[], str | None],
) -> EmbeddingProvider:
    """Construct the configured provider
    (`settings.services.embedding_backend`, env `AVA_EMBEDDING_BACKEND`)."""
    return get_provider_named(
        name, catalog=catalog, timeout_reader=timeout_reader, api_key_reader=api_key_reader
    )


def worst_case_batch_seconds(name: str, *, timeout_seconds: float) -> float:
    """Return the configured provider's full document-batch retry budget."""
    _, batch_seconds, _ = _provider_entry(name)
    return batch_seconds(timeout_seconds)


def get_descriptor(name: str) -> EmbeddingDescriptor:
    """Read the configured provider's metadata without constructing a provider."""
    _, _, descriptor = _provider_entry(name)
    return descriptor
