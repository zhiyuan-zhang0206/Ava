"""MemoryStore unit tests — the numpy backend's storage core.

Pure in-memory/npz logic, no HTTP: upsert/delete/meta/search semantics must
match the milvus backend's contract exactly (same row vocabulary, same
path aggregation, same ordering), because the whole point of the numpy
backend is to be the exact reconciliation baseline.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from services.memory_search.store import MemoryStore

_DIM = 8
_FP = "test:gemini:dim=8"


def _store(tmp_path: Path, *, dim: int = _DIM, fingerprint: str = _FP) -> MemoryStore:
    return MemoryStore(tmp_path / "vectors.npz", dim=dim, fingerprint=fingerprint)


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(_DIM).astype(np.float32)


def _row(
    path: str,
    mtime: float,
    content_hash: str,
    seed: int,
    kind: str = "body",
    chunk_idx: int = 0,
) -> tuple[str, float, str, np.ndarray, str, int]:
    return (path, mtime, content_hash, _vec(seed), kind, chunk_idx)


def _upsert_sequentially(
    store: MemoryStore, rows: list[tuple[str, float, str, np.ndarray, str, int]]
) -> None:
    for path, mtime, hash_, vector, kind, chunk_idx in rows:
        store.upsert(path, mtime, hash_, vector, kind=kind, chunk_idx=chunk_idx)


def test_upsert_all_meta_aggregates_per_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert("/a.md", 1.0, "ha", _vec(0), kind="body", chunk_idx=0)
    store.upsert("/a.md", 1.0, "ha", _vec(1), kind="body", chunk_idx=1)
    store.upsert("/b.md", 2.0, "hb", _vec(2), kind="body", chunk_idx=0)
    assert store.all_meta() == {"/a.md": (1.0, "ha", _FP), "/b.md": (2.0, "hb", _FP)}
    assert len(store) == 3


def test_upsert_overwrite_same_pk(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert("/a.md", 1.0, "h1", _vec(0), kind="body", chunk_idx=0)
    store.upsert("/a.md", 2.0, "h2", _vec(1), kind="body", chunk_idx=0)
    assert store.all_meta() == {"/a.md": (2.0, "h2", _FP)}
    assert len(store) == 1  # overwritten, not appended


@pytest.mark.parametrize(
    ("initial_rows", "rows"),
    [
        ([], [_row("/a.md", 1.0, "ha", 0), _row("/b.md", 2.0, "hb", 1)]),
        (
            [_row("/a.md", 1.0, "old", 0)],
            [_row("/a.md", 3.0, "new", 2), _row("/b.md", 2.0, "hb", 1)],
        ),
        (
            [],
            [_row("/a.md", 1.0, "old", 0), _row("/a.md", 2.0, "new", 1)],
        ),
        (
            [],
            [
                _row("/a.md", 1.0, "ha", 0, "desc", 0),
                _row("/a.md", 1.0, "ha", 1, "body", 0),
                _row("/a.md", 1.0, "ha", 2, "body", 1),
            ],
        ),
    ],
    ids=["all-new", "mixed-new-overwrite", "duplicate-pk", "desc-and-body"],
)
def test_upsert_many_matches_sequential_upserts(
    tmp_path: Path,
    initial_rows: list[tuple[str, float, str, np.ndarray, str, int]],
    rows: list[tuple[str, float, str, np.ndarray, str, int]],
) -> None:
    sequential = MemoryStore(tmp_path / "sequential.npz", dim=_DIM, fingerprint=_FP)
    batched = MemoryStore(tmp_path / "batched.npz", dim=_DIM, fingerprint=_FP)
    _upsert_sequentially(sequential, initial_rows)
    _upsert_sequentially(batched, initial_rows)

    _upsert_sequentially(sequential, rows)
    batched.upsert_many(rows)

    query = _vec(99)
    assert batched.all_meta() == sequential.all_meta()
    assert len(batched) == len(sequential)
    assert batched.search_topk(query, 10) == sequential.search_topk(query, 10)


def test_upsert_many_rejects_bad_row_without_mutation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_many([_row("/present.md", 1.0, "original", 0)])
    before = (store.all_meta(), len(store), store.search_topk(_vec(0), 10))

    with pytest.raises(ValueError, match=r"embedding shape \(7,\) != \(8,\)"):
        store.upsert_many(
            [
                _row("/would-be-new.md", 2.0, "new", 1),
                ("/bad.md", 3.0, "bad", np.zeros(7, dtype=np.float32), "body", 0),
            ]
        )

    assert (store.all_meta(), len(store), store.search_topk(_vec(0), 10)) == before


def test_upsert_many_empty_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_many([_row("/present.md", 1.0, "original", 0)])
    before = (store.all_meta(), len(store), store.search_topk(_vec(0), 10))
    store.upsert_many([])
    assert (store.all_meta(), len(store), store.search_topk(_vec(0), 10)) == before


def test_upsert_many_grows_matrix_once_for_new_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import services.memory_search.store as store_module

    new_rows = [_row(f"/{idx}.md", 1.0, str(idx), idx) for idx in range(5)]
    update_rows = [_row(f"/{idx}.md", 2.0, f"new-{idx}", idx + 10) for idx in range(5)]
    calls: list[str] = []
    original_vstack = store_module.np.vstack
    original_concatenate = store_module.np.concatenate

    def _vstack(*args: object, **kwargs: object) -> np.ndarray:
        calls.append("vstack")
        return original_vstack(*args, **kwargs)  # type: ignore[arg-type]

    def _concatenate(*args: object, **kwargs: object) -> np.ndarray:
        calls.append("concatenate")
        return original_concatenate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store_module.np, "vstack", _vstack)
    monkeypatch.setattr(store_module.np, "concatenate", _concatenate)
    store = _store(tmp_path)
    store.upsert_many(new_rows)
    assert calls == ["concatenate"]

    calls.clear()
    store.upsert_many(update_rows)
    assert calls == []


def test_delete_removes_all_chunks_of_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for idx in range(3):
        store.upsert("/a.md", 1.0, "ha", _vec(idx), kind="body", chunk_idx=idx)
    store.upsert("/b.md", 2.0, "hb", _vec(9), kind="body", chunk_idx=0)
    store.delete("/a.md")
    assert store.all_meta() == {"/b.md": (2.0, "hb", _FP)}


def test_delete_missing_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.delete("/never_existed.md")  # must not raise


def test_search_topk_exact_cosine_order_and_aggregation(tmp_path: Path) -> None:
    """Best chunk per path wins; ordering is cosine-descending — the exact
    counterpart of milvus's distance-ascending contract."""
    ones = np.ones(_DIM, dtype=np.float32)
    store = _store(tmp_path)
    store.upsert("/a.md", 1.0, "ha", ones, kind="body", chunk_idx=0)
    store.upsert("/a.md", 1.0, "ha", 0.9 * ones, kind="body", chunk_idx=1)
    store.upsert("/b.md", 2.0, "hb", -ones, kind="body", chunk_idx=0)
    assert store.search_topk(ones, k=5) == ["/a.md", "/b.md"]
    assert store.search_topk(ones, k=1) == ["/a.md"]


def test_search_topk_empty_store(tmp_path: Path) -> None:
    assert _store(tmp_path).search_topk(_vec(0), k=5) == []


def test_search_topk_rejects_wrong_dim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="shape"):
        store.search_topk(np.zeros(7, dtype=np.float32), k=1)


def test_upsert_rejects_wrong_dim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="shape"):
        store.upsert("/a.md", 1.0, "h", np.zeros(7, dtype=np.float32), kind="body", chunk_idx=0)


def test_last_save_seconds_none_until_first_save(tmp_path: Path) -> None:
    """The stats surface reports no save duration until a save actually
    happened since boot — None, never a fabricated zero."""
    store = _store(tmp_path)
    assert store.last_save_seconds is None
    store.upsert("/a.md", 1.0, "ha", _vec(0), kind="body", chunk_idx=0)
    assert store.last_save_seconds is None  # upsert alone does not persist
    store.save()
    assert store.last_save_seconds is not None
    assert store.last_save_seconds >= 0.0


def test_last_save_seconds_is_the_latest_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every successful save refreshes the recorded duration — the stats
    surface always reads the most recent persistence cost, never the first
    one (write-once would silently hide a growing npz cost)."""
    import services.memory_search.store as store_module

    ticks = iter([0.0, 1.0, 2.0, 5.0])

    class _FakeTime:
        @staticmethod
        def perf_counter() -> float:
            return next(ticks)

    monkeypatch.setattr(store_module, "time", _FakeTime)
    store = _store(tmp_path)
    store.upsert("/a.md", 1.0, "ha", _vec(0), kind="body", chunk_idx=0)
    store.save()
    assert store.last_save_seconds == 1.0
    store.upsert("/b.md", 2.0, "hb", _vec(1), kind="body", chunk_idx=0)
    store.save()
    assert store.last_save_seconds == 3.0


def test_save_load_roundtrip(tmp_path: Path) -> None:
    """The npz survives a full store rebuild — the service's crash recovery."""
    store = _store(tmp_path)
    ones = np.ones(_DIM, dtype=np.float32)
    store.upsert("/a.md", 1.0, "ha", ones, kind="body", chunk_idx=0)
    store.upsert("/a.md", 1.0, "ha", 0.5 * ones, kind="body", chunk_idx=1)
    store.save()

    fresh = _store(tmp_path)
    fresh.load()
    assert fresh.all_meta() == {"/a.md": (1.0, "ha", _FP)}
    assert fresh.search_topk(ones, k=5) == ["/a.md"]
    assert len(fresh) == 2


def test_load_absent_file_starts_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.load()
    assert len(store) == 0
    assert store.all_meta() == {}


def test_load_missing_keys_starts_empty(tmp_path: Path) -> None:
    """A torn npz (some keys absent) is a broken cache — start empty, the
    cold-start reconcile rebuilds it."""
    data_file = tmp_path / "vectors.npz"
    np.savez(data_file, pks=np.array(["x"], dtype=str))  # only one of the keys
    store = _store(tmp_path)
    store.load()
    assert len(store) == 0
    assert store.all_meta() == {}


def test_load_corrupted_file_starts_empty(tmp_path: Path) -> None:
    """A file that does not parse (truncated / foreign bytes) degrades to
    start-empty instead of crashing the daemon into a respawn loop."""
    data_file = tmp_path / "vectors.npz"
    data_file.write_bytes(b"this is not a zip file")
    store = _store(tmp_path)
    store.load()
    assert len(store) == 0
    assert store.all_meta() == {}


def test_load_truncated_zip_starts_empty(tmp_path: Path) -> None:
    """BadZipFile path: a zip header without a real archive."""
    data_file = tmp_path / "vectors.npz"
    data_file.write_bytes(b"PK\x03\x04truncated")
    store = _store(tmp_path)
    store.load()
    assert len(store) == 0
    assert store.all_meta() == {}


def test_load_mid_file_failure_leaves_store_empty(tmp_path: Path) -> None:
    """A file whose keys are all present but one column fails to convert
    (bytes-dtype mtimes holding a non-float) must not half-load: the store
    stays fully empty, never a few columns populated and the rest absent."""
    data_file = tmp_path / "vectors.npz"
    np.savez(
        data_file,
        pks=np.array(["x"], dtype=str),
        paths=np.array(["/a.md"], dtype=str),
        kinds=np.array(["body"], dtype=str),
        chunk_idx=np.array([0], dtype=np.int64),
        mtimes=np.array([b"not-a-float"], dtype="S20"),
        content_hashes=np.array(["h"], dtype=str),
        embedders=np.array(["fp"], dtype=str),
        vectors=np.zeros((1, _DIM), dtype=np.float32),
    )
    store = _store(tmp_path)
    store.load()
    assert len(store) == 0
    assert store.all_meta() == {}
    assert store.search_topk(np.ones(_DIM, dtype=np.float32), 5) == []


def test_load_dim_mismatch_starts_empty(tmp_path: Path) -> None:
    """CTO ② (2026-08-30): an npz whose vectors have another provider's
    width is a stale cache — the store starts empty and the cold-start
    reconcile rebuilds it, instead of serving searches with a mismatched
    matrix (a query at the configured dim would raise, not search)."""
    data_file = tmp_path / "vectors.npz"
    np.savez(
        data_file,
        pks=np.array(["x"], dtype=str),
        paths=np.array(["/a.md"], dtype=str),
        kinds=np.array(["body"], dtype=str),
        chunk_idx=np.array([0], dtype=np.int64),
        mtimes=np.array([1.0], dtype=np.float64),
        content_hashes=np.array(["h"], dtype=str),
        embedders=np.array(["other:provider"], dtype=str),
        vectors=np.zeros((1, 64), dtype=np.float32),
    )
    store = _store(tmp_path)
    store.load()
    assert len(store) == 0
    assert store.all_meta() == {}
    assert store.search_topk(np.zeros(_DIM, dtype=np.float32), 5) == []


def test_fingerprint_roundtrips_through_npz(tmp_path: Path) -> None:
    """The provider fingerprint is persisted per row and survives a store
    rebuild — the reconcile key's third element is durable."""
    store = _store(tmp_path)
    store.upsert("/a.md", 1.0, "ha", _vec(0), kind="body", chunk_idx=0)
    store.save()

    fresh = _store(tmp_path)
    fresh.load()
    assert fresh.all_meta() == {"/a.md": (1.0, "ha", _FP)}
