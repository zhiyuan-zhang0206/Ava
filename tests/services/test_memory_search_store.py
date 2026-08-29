"""MemoryStore unit tests — the numpy backend's storage core.

Pure in-memory/npz logic, no HTTP: upsert/delete/meta/search semantics must
match the milvus backend's contract exactly (same row vocabulary, same
path aggregation, same ordering), because the whole point of the numpy
backend is to be the exact reconciliation baseline.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from services.memory_indexer import embedder
from services.memory_search.store import MemoryStore


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(embedder.DIM).astype(np.float32)


def test_upsert_all_meta_aggregates_per_path(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "vectors.npz")
    store.upsert("/a.md", 1.0, "ha", _vec(0), kind="body", chunk_idx=0)
    store.upsert("/a.md", 1.0, "ha", _vec(1), kind="body", chunk_idx=1)
    store.upsert("/b.md", 2.0, "hb", _vec(2), kind="body", chunk_idx=0)
    assert store.all_meta() == {"/a.md": (1.0, "ha"), "/b.md": (2.0, "hb")}
    assert len(store) == 3


def test_upsert_overwrite_same_pk(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "vectors.npz")
    store.upsert("/a.md", 1.0, "h1", _vec(0), kind="body", chunk_idx=0)
    store.upsert("/a.md", 2.0, "h2", _vec(1), kind="body", chunk_idx=0)
    assert store.all_meta() == {"/a.md": (2.0, "h2")}
    assert len(store) == 1  # overwritten, not appended


def test_delete_removes_all_chunks_of_path(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "vectors.npz")
    for idx in range(3):
        store.upsert("/a.md", 1.0, "ha", _vec(idx), kind="body", chunk_idx=idx)
    store.upsert("/b.md", 2.0, "hb", _vec(9), kind="body", chunk_idx=0)
    store.delete("/a.md")
    assert store.all_meta() == {"/b.md": (2.0, "hb")}


def test_delete_missing_noop(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "vectors.npz")
    store.delete("/never_existed.md")  # must not raise


def test_search_topk_exact_cosine_order_and_aggregation(tmp_path: Path) -> None:
    """Best chunk per path wins; ordering is cosine-descending — the exact
    counterpart of milvus's distance-ascending contract."""
    ones = np.ones(embedder.DIM, dtype=np.float32)
    store = MemoryStore(tmp_path / "vectors.npz")
    store.upsert("/a.md", 1.0, "ha", ones, kind="body", chunk_idx=0)
    store.upsert("/a.md", 1.0, "ha", 0.9 * ones, kind="body", chunk_idx=1)
    store.upsert("/b.md", 2.0, "hb", -ones, kind="body", chunk_idx=0)
    assert store.search_topk(ones, k=5) == ["/a.md", "/b.md"]
    assert store.search_topk(ones, k=1) == ["/a.md"]


def test_search_topk_empty_store(tmp_path: Path) -> None:
    assert MemoryStore(tmp_path / "vectors.npz").search_topk(_vec(0), k=5) == []


def test_search_topk_rejects_wrong_dim(tmp_path: Path) -> None:
    import pytest

    store = MemoryStore(tmp_path / "vectors.npz")
    with pytest.raises(ValueError, match="shape"):
        store.search_topk(np.zeros(7, dtype=np.float32), k=1)


def test_upsert_rejects_wrong_dim(tmp_path: Path) -> None:
    import pytest

    store = MemoryStore(tmp_path / "vectors.npz")
    with pytest.raises(ValueError, match="shape"):
        store.upsert("/a.md", 1.0, "h", np.zeros(7, dtype=np.float32), kind="body", chunk_idx=0)


def test_save_load_roundtrip(tmp_path: Path) -> None:
    """The npz survives a full store rebuild — the service's crash recovery."""
    data_file = tmp_path / "vectors.npz"
    store = MemoryStore(data_file)
    ones = np.ones(embedder.DIM, dtype=np.float32)
    store.upsert("/a.md", 1.0, "ha", ones, kind="body", chunk_idx=0)
    store.upsert("/a.md", 1.0, "ha", 0.5 * ones, kind="body", chunk_idx=1)
    store.save()

    fresh = MemoryStore(data_file)
    fresh.load()
    assert fresh.all_meta() == {"/a.md": (1.0, "ha")}
    assert fresh.search_topk(ones, k=5) == ["/a.md"]
    assert len(fresh) == 2


def test_load_absent_file_starts_empty(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "missing.npz")
    store.load()
    assert len(store) == 0
    assert store.all_meta() == {}


def test_load_missing_keys_starts_empty(tmp_path: Path) -> None:
    """A torn npz (some keys absent) is a broken cache — start empty, the
    cold-start reconcile rebuilds it."""
    data_file = tmp_path / "vectors.npz"
    np.savez(data_file, pks=np.array(["x"], dtype=str))  # only one of the keys
    store = MemoryStore(data_file)
    store.load()
    assert len(store) == 0
    assert store.all_meta() == {}


def test_load_corrupted_file_starts_empty(tmp_path: Path) -> None:
    """A file that does not parse (truncated / foreign bytes) degrades to
    start-empty instead of crashing the daemon into a respawn loop."""
    data_file = tmp_path / "vectors.npz"
    data_file.write_bytes(b"this is not a zip file")
    store = MemoryStore(data_file)
    store.load()
    assert len(store) == 0
    assert store.all_meta() == {}


def test_load_truncated_zip_starts_empty(tmp_path: Path) -> None:
    """BadZipFile path: a zip header without a real archive."""
    data_file = tmp_path / "vectors.npz"
    data_file.write_bytes(b"PK\x03\x04truncated")
    store = MemoryStore(data_file)
    store.load()
    assert len(store) == 0
    assert store.all_meta() == {}
