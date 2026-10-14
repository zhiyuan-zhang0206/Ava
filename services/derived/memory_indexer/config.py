"""Narrow live configuration readers owned by the memory-indexer composition root."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from base.config import ConfigBoot
from services.derived.memory_indexer.embeddings import factory


@dataclass(frozen=True)
class MemoryIndexerConfig:
    """Tuning of the indexing loops; the defaults are the production values."""

    loop_interval_s: float = 1.0
    """Seconds the main loop sleeps between two drains of the dirty queue."""

    reconcile_chunk_paths: int = 64
    """Dirty files per chunk of the startup reconcile.

    The reconcile beats before and after each chunk: a full rebuild can outlive the liveness
    ceiling. Chunks bound preparation and local work; `process_paths` also beats before external
    calls within each chunk.
    """

    batch_size: int = 32
    """Texts per embedding call: `embed_content` accepts several inputs, so batching amortizes round-trips."""

    def __post_init__(self) -> None:
        if self.loop_interval_s <= 0:
            raise ValueError("loop_interval_s must be positive")
        if self.reconcile_chunk_paths < 1:
            raise ValueError("reconcile_chunk_paths must be at least 1")
        if self.batch_size < 1:
            raise ValueError("batch_size must be at least 1")


@dataclass(frozen=True)
class MemoryIndexerInputs:
    """Readers retain one boot owner without reading or freezing its values."""

    embedding_name: Callable[[], str]
    embed_timeout: Callable[[], float]
    api_key: Callable[[], str | None]
    backend_name: Callable[[], str]
    search_uri: Callable[[], str]

    @classmethod
    def from_boot(cls, boot: ConfigBoot) -> MemoryIndexerInputs:
        """Bind each reader to the root's owner; construction performs no reads."""

        def api_key() -> str | None:
            value = boot.view.lm.gemini_api_key
            return None if value is None else value.get_secret_value()

        return cls(
            embedding_name=lambda: boot.view.services.embedding_backend,
            embed_timeout=lambda: boot.view.services.memory_embed_timeout_seconds,
            api_key=api_key,
            backend_name=lambda: boot.view.services.memory_search_backend,
            search_uri=lambda: boot.view.services.memory_search_uri,
        )


# Derive the ceiling from one provider batch's full retry budget: a single
# legitimate call can exceed 180s, and several shorter calls can compound.
# _process_paths beats before each provider/backend call, including commits
# and deletes, so calls cannot compound in one gap (default batch budget 606s).
# A false kill costs a rebuild; later true-wedge detection costs staleness
# only, since search keeps reading the existing index.
LIVENESS_TIMEOUT_FLOOR_S = 180.0  # Historic ceiling; preserve other loop branches' slack.
# Covers executor scheduling, local processing, and loop resumption. Commit
# calls beat separately; NumPy's 300s upsert allowance fits the default 636s.
LIVENESS_SAFETY_MARGIN_S = 30.0


def liveness_timeout_seconds(name: str, *, timeout_seconds: float) -> float:
    """Cover one selected provider batch plus local scheduling and commit slack."""
    return max(
        LIVENESS_TIMEOUT_FLOOR_S,
        factory.worst_case_batch_seconds(name, timeout_seconds=timeout_seconds)
        + LIVENESS_SAFETY_MARGIN_S,
    )
