"""Backend abstraction unit tests — factory dispatch, the fail-fast switch, row vocabulary."""

from __future__ import annotations

import numpy as np
import pytest

from base.db import Database
from services.memory_indexer.backends import factory
from services.memory_indexer.backends.base import KIND_DESC, pk_of
from services.memory_indexer.backends.numpy import NumPyBackend
from services.memory_indexer.backends.pgvector import PGVectorBackend

_DIM = 8
_FP = "test:gemini:dim=8"


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(_DIM).astype(np.float32)


# ── factory ──────────────────────────────────────────────────────────────


def test_factory_and_probe_registries_stay_in_sync() -> None:
    """Every backend has both a constructor and a preflight probe — the
    daemon's fail-fast preflight must not silently skip a backend."""
    from services.memory_indexer.backends import probe

    assert set(factory._BACKENDS) == set(probe._PROBES)


def test_factory_default_is_numpy() -> None:
    """The unset switch yields the numpy backend (default since 2026-09-02)."""
    from base.config import settings
    from services.memory_indexer.backends.numpy import NumPyBackend

    assert settings.services.memory_search_backend == "numpy"
    assert isinstance(
        factory.get_backend(Database.from_settings(), dim=_DIM, fingerprint=_FP), NumPyBackend
    )


def test_factory_numpy_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_MEMORY_SEARCH_BACKEND=numpy yields the NumPyBackend."""
    from base.config import settings
    from services.memory_indexer.backends.numpy import NumPyBackend

    monkeypatch.setattr(settings.services, "memory_search_backend", "numpy")
    assert isinstance(
        factory.get_backend(Database.from_settings(), dim=_DIM, fingerprint=_FP), NumPyBackend
    )


def test_factory_pgvector_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_MEMORY_SEARCH_BACKEND=pgvector yields the PGVectorBackend."""
    from base.config import settings
    from services.memory_indexer.backends.pgvector import PGVectorBackend

    monkeypatch.setattr(settings.services, "memory_search_backend", "pgvector")
    assert isinstance(
        factory.get_backend(Database.from_settings(), dim=_DIM, fingerprint=_FP), PGVectorBackend
    )


def test_factory_unknown_backend_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognized AVA_MEMORY_SEARCH_BACKEND must not silently fall
    back to numpy — a typo would keep the old storage while the operator
    believes the switch happened."""
    from base.config import settings

    monkeypatch.setattr(settings.services, "memory_search_backend", "qdrant")
    with pytest.raises(ValueError, match="unknown memory search backend"):
        factory.get_backend(Database.from_settings(), dim=_DIM, fingerprint=_FP)


# ── row vocabulary ───────────────────────────────────────────────────────


def test_pk_of_roundtrips_path_kind_chunk() -> None:
    assert pk_of("/a/b.md", KIND_DESC, 0) == "/a/b.md\x1fdesc\x1f0"
    assert pk_of("/a/b.md", "body", 3) == "/a/b.md\x1fbody\x1f3"


def test_empty_upsert_many_is_noop_before_connect() -> None:
    for name in (PGVectorBackend.name, NumPyBackend.name):
        backend = factory.get_backend_named(
            name, database=Database.from_settings(), dim=_DIM, fingerprint=_FP
        )
        backend.upsert_many([])


def test_readonly_backends_refuse_mutations() -> None:
    """Factory-provided read-only backends reject writes before connect."""
    for name in (PGVectorBackend.name, NumPyBackend.name):
        backend = factory.get_backend_named(
            name, database=Database.from_settings(), dim=_DIM, fingerprint=_FP, readonly=True
        )
        with pytest.raises(RuntimeError, match="read-only"):
            backend.upsert("/a.md", 1.0, "hash", _vec(0), kind="body", chunk_idx=0)
        with pytest.raises(RuntimeError, match="read-only"):
            backend.upsert_many([("/a.md", 1.0, "hash", _vec(0), "body", 0)])
        with pytest.raises(RuntimeError, match="read-only"):
            backend.delete("/a.md")
