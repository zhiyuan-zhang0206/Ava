"""Memory indexer cases: reconcile embed error truncates and returns false."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from base.cluster.machine import MachineIdentity, MachineRoles
from base.daemon.health import Liveness
from services.derived.memory_indexer import chunking, daemon
from services.derived.memory_indexer.backends.base import content_hash
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError
from services.derived.memory_indexer.tests.fake_provider import DIM, FINGERPRINT, FakeProvider
from services.derived.memory_search.tests.store_backend import StoreBackend
from services.derived.memory_search.tests.test_memory_indexer import (
    liveness_timeout,
    long_note,
)
from services.derived.memory_search.tests.test_memory_indexer import (
    memory_watched_root as memory_watched_root,
)
from services.derived.memory_search.tests.test_memory_indexer import (
    store_backend as store_backend,
)
from tests.fixtures.unit.identity import machine_identity as machine_identity


@pytest.fixture
def owned_config_environment() -> Iterator[None]:
    """Restore environment delivery and the process timezone after a real ConfigBoot."""
    try:
        with patch.dict(os.environ):
            yield
    finally:
        time.tzset()


def test_reconcile_embed_error_truncates_and_returns_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
    caplog: pytest.LogCaptureFixture,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """Error semantics preserved from the pre-chunking reconcile (one
    try/except): an EmbeddingAPIError skips the remaining chunks and is logged
    with its error repr — never raised — so the daemon keeps running and
    the return value signals that a follow-up pass is needed."""
    root = tmp_path / "watched"
    root.mkdir()
    for i in range(6):
        (root / f"note-{i}.md").write_text(f"content {i}")

    def memory_root_for_roles(*, roles: MachineRoles) -> Path:
        assert roles == machine_identity.role()
        return root

    monkeypatch.setattr(daemon, "memory_root", memory_root_for_roles)
    monkeypatch.setattr(daemon, "RECONCILE_CHUNK_PATHS", 2)  # 6 files -> 3 chunks

    calls: list[int] = []

    def failing_process_paths(
        backend: Any,
        paths: set[Path],
        provider: Any,
        liveness: Liveness,
        *,
        machine: MachineIdentity,
    ) -> None:
        calls.append(len(paths))
        raise EmbeddingAPIError("embed boom")

    monkeypatch.setattr(daemon, "process_paths", failing_process_paths)

    liveness = Liveness(600.0)
    with caplog.at_level(logging.ERROR, logger="services.derived.memory_indexer.daemon"):
        assert (
            daemon._reconcile(store_backend, FakeProvider(), liveness, machine=machine_identity)
            is False
        )

    assert calls == [2]  # first chunk failed -> remaining chunks never ran (as before)
    expected = (
        f"reconcile embed failed: {EmbeddingAPIError('embed boom')!r}"
        " — index remains incomplete; explicit rebuild required"
    )
    assert expected in caplog.text
    assert liveness.is_alive()


def test_split_note_extracts_description_and_body() -> None:
    content = (
        "---\ntype: Memory\ndescription: A note about the user's health\n"
        "---\n\n# Health\n\nbody content"
    )
    desc, body = chunking.split_note(content)
    assert desc == "A note about the user's health"
    assert "# Health" in body
    assert "description" not in body


def test_split_note_no_frontmatter_returns_full_body() -> None:
    content = "# Just a heading\n\nNo YAML."
    desc, body = chunking.split_note(content)
    assert desc is None
    assert body == content


def test_split_note_blank_description_is_none() -> None:
    desc, body = chunking.split_note("---\ndescription: \n---\n\nbody")
    assert desc is None
    assert body == "\nbody"  # the shared parser keeps the blank line after the fence


def test_chunk_body_short_text_single_chunk() -> None:
    assert chunking.chunk_body("short body") == ["short body"]
    assert chunking.chunk_body("  \n\n  ") == []


def test_chunk_body_splits_at_paragraph_boundaries() -> None:
    paras = [f"paragraph-{i} " + "word " * 80 for i in range(6)]  # 412 chars each
    body = "\n\n".join(paras)
    chunks = chunking.chunk_body(body, max_chars=1800, overlap_chars=200)
    assert len(chunks) == 2
    assert all(len(c) <= 1800 for c in chunks)
    assert chunks[0].startswith("paragraph-0")
    assert chunks[1].startswith("paragraph-4")  # 4×412 = 1654 fits; 5×412 would not
    # every paragraph survives whole (chunking strips paragraph whitespace)
    assert all(p.strip() in "".join(chunks) for p in paras)


def test_chunk_body_overlap_carries_trailing_paragraphs() -> None:
    paras = [f"paragraph-{i} " + "word " * 80 for i in range(6)]
    body = "\n\n".join(paras)
    chunks = chunking.chunk_body(body, max_chars=1400, overlap_chars=600)
    # the previous chunk's tail paragraph re-opens the next chunk
    assert chunks[1].startswith("paragraph-2")
    assert "paragraph-2" in chunks[0]


def test_chunk_body_hard_splits_oversized_paragraph() -> None:
    para = "x" * 3000
    chunks = chunking.chunk_body(para, max_chars=1000, overlap_chars=100)
    assert chunks == ["x" * 1000, "x" * 1000, "x" * 1000, "x" * 300]


def test_file_rows_desc_plus_body_chunks() -> None:
    content = "---\ntype: Memory\ndescription: hand off to 402\n---\n\n" + "\n\n".join(
        f"paragraph-{i} " + "word " * 80 for i in range(6)
    )
    rows = chunking._file_rows(content)
    assert rows[0] == ("desc", 0, "hand off to 402")
    assert [k for k, _, _ in rows] == ["desc", "body", "body"]


def test_file_rows_no_frontmatter_no_desc() -> None:
    assert chunking._file_rows("# heading\n\nshort body") == [
        ("body", 0, "# heading\n\nshort body")
    ]


def test_process_paths_indexes_desc_and_body_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """One file with a description + a long body lands as 1 desc row + N body
    chunks; all_meta still reports the file once."""
    f = tmp_path / "long.md"
    f.write_text(
        "---\ntype: Memory\ndescription: hand off to 402\n---\n\n"
        + "\n\n".join(f"paragraph-{i} " + "word " * 80 for i in range(6)),
        encoding="utf-8",
    )

    daemon.process_paths(
        store_backend,
        {f.resolve()},
        FakeProvider(),
        Liveness(liveness_timeout()),
        machine=machine_identity,
    )
    assert str(f.resolve()) in store_backend.all_meta()
    kinds = sorted(store_backend.rows(f))
    assert kinds == [("body", 0), ("body", 1), ("desc", 0)]


def test_process_paths_removes_stale_tail_when_file_shrinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """Issue #1946: after a file shrinks, the rows it no longer produces
    (old body tail) are gone — every remaining chunk matches current content."""
    f = tmp_path / "long.md"
    f.write_text(long_note(12), encoding="utf-8")
    backend = store_backend
    daemon.process_paths(
        backend,
        {f.resolve()},
        FakeProvider(),
        Liveness(liveness_timeout()),
        machine=machine_identity,
    )
    assert store_backend.rows(f) == {("desc", 0), ("body", 0), ("body", 1), ("body", 2)}

    f.write_text(long_note(3), encoding="utf-8")
    daemon.process_paths(
        backend,
        {f.resolve()},
        FakeProvider(),
        Liveness(liveness_timeout()),
        machine=machine_identity,
    )
    assert store_backend.rows(f) == {("desc", 0), ("body", 0)}
    assert backend.all_meta()[str(f.resolve())][1] == content_hash(f.read_text())


def test_process_paths_removes_desc_row_when_description_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """A removed description removes its row; the body stays current."""
    f = tmp_path / "note.md"
    f.write_text("---\ndescription: old description\n---\n\nbody text", encoding="utf-8")
    backend = store_backend
    daemon.process_paths(
        backend,
        {f.resolve()},
        FakeProvider(),
        Liveness(liveness_timeout()),
        machine=machine_identity,
    )
    assert store_backend.rows(f) == {("desc", 0), ("body", 0)}

    f.write_text("body text", encoding="utf-8")
    daemon.process_paths(
        backend,
        {f.resolve()},
        FakeProvider(),
        Liveness(liveness_timeout()),
        machine=machine_identity,
    )
    assert store_backend.rows(f) == {("body", 0)}
    assert backend.all_meta()[str(f.resolve())][1] == content_hash(f.read_text())


def test_process_paths_removes_all_rows_when_file_becomes_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """An emptied file leaves no rows behind (issue #1946)."""
    f = tmp_path / "note.md"
    f.write_text("---\ndescription: old description\n---\n\nbody text", encoding="utf-8")
    backend = store_backend
    daemon.process_paths(
        backend,
        {f.resolve()},
        FakeProvider(),
        Liveness(liveness_timeout()),
        machine=machine_identity,
    )
    assert store_backend.rows(f) == {("desc", 0), ("body", 0)}

    f.write_text("", encoding="utf-8")
    daemon.process_paths(
        backend,
        {f.resolve()},
        FakeProvider(),
        Liveness(liveness_timeout()),
        machine=machine_identity,
    )
    assert store_backend.rows(f) == set()
    assert backend.all_meta() == {}


def test_process_paths_calls_upsert_many_once_across_embed_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    class _RecordingBackend(StoreBackend):
        def __init__(self) -> None:
            super().__init__(
                tmp_path / "recording" / "vectors.npz", dim=DIM, fingerprint=FINGERPRINT
            )
            self.calls: list[list[tuple[str, float, str, np.ndarray, str, int]]] = []

        def upsert_many(self, rows: Sequence[tuple[str, float, str, np.ndarray, str, int]]) -> None:
            self.calls.append(list(rows))
            super().upsert_many(rows)

    first = tmp_path / "first.md"
    first.write_text("---\ndescription: first description\n---\nfirst body", encoding="utf-8")
    second = tmp_path / "second.md"
    second.write_text("second body", encoding="utf-8")
    monkeypatch.setattr(daemon, "BATCH_SIZE", 2)
    backend = _RecordingBackend()

    daemon.process_paths(
        backend,
        {first.resolve(), second.resolve()},
        FakeProvider(),
        Liveness(liveness_timeout()),
        machine=machine_identity,
    )

    assert len(backend.calls) == 1
    assert len(backend.calls[0]) == 3
    assert {row[0] for row in backend.calls[0]} == {str(first.resolve()), str(second.resolve())}


def test_partial_embedding_failure_keeps_old_rows_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """A file whose embedding fails part way keeps its previous rows whole —
    all-old is the recoverable consistent state; the hash stays mismatching so
    the next fs event or follow-up reconcile retries it (issue #1946)."""

    class _FailSecondBatchProvider(FakeProvider):
        def embed_batch(self, texts: list[str]) -> np.ndarray:
            if self.embed_batch_count == 1:
                raise EmbeddingAPIError("second batch failed")
            return super().embed_batch(texts)

    class _RecordingBackend(StoreBackend):
        def __init__(self) -> None:
            super().__init__(
                tmp_path / "recording" / "vectors.npz", dim=DIM, fingerprint=FINGERPRINT
            )
            self.calls: list[list[tuple[str, float, str, np.ndarray, str, int]]] = []

        def upsert_many(self, rows: Sequence[tuple[str, float, str, np.ndarray, str, int]]) -> None:
            self.calls.append(list(rows))
            super().upsert_many(rows)

    note = tmp_path / "note.md"
    note.write_text("---\ndescription: description\n---\nbody", encoding="utf-8")
    monkeypatch.setattr(daemon, "BATCH_SIZE", 1)
    backend = _RecordingBackend()
    daemon.process_paths(
        backend,
        {note.resolve()},
        FakeProvider(),
        Liveness(liveness_timeout()),
        machine=machine_identity,
    )
    old_meta = backend.all_meta()

    note.write_text("---\ndescription: new description\n---\nnew body", encoding="utf-8")
    backend.calls.clear()
    with pytest.raises(EmbeddingAPIError, match="second batch failed"):
        daemon.process_paths(
            backend,
            {note.resolve()},
            _FailSecondBatchProvider(),
            Liveness(liveness_timeout()),
            machine=machine_identity,
        )

    # Nothing was written for the partially-embedded file; the old rows stand.
    assert backend.calls == []
    assert backend.all_meta() == old_meta


def test_complete_file_still_commits_when_another_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """Files whose rows are ALL embedded commit even when a sibling file
    fails mid-embedding (issue #1946)."""

    class _FailOnSecondFileProvider(FakeProvider):
        def embed_batch(self, texts: list[str]) -> np.ndarray:
            if self.embed_batch_count == 2:
                raise EmbeddingAPIError("third batch failed")
            return super().embed_batch(texts)

    monkeypatch.setattr(daemon, "BATCH_SIZE", 1)
    backend = store_backend
    complete = tmp_path / "complete.md"
    complete.write_text("complete body", encoding="utf-8")
    partial = tmp_path / "partial.md"
    partial.write_text("---\ndescription: p\n---\nbody", encoding="utf-8")

    with pytest.raises(EmbeddingAPIError, match="third batch failed"):
        daemon.process_paths(
            backend,
            {complete.resolve(), partial.resolve()},
            _FailOnSecondFileProvider(),
            Liveness(liveness_timeout()),
            machine=machine_identity,
        )

    meta = backend.all_meta()
    assert str(complete.resolve()) in meta
    assert str(partial.resolve()) not in meta


async def test_failed_drain_batch_stays_incomplete_until_a_new_file_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
    *,
    machine_identity: MachineIdentity,
) -> None:
    import asyncio
    import queue

    file = tmp_path / "failed.md"
    file.write_text("old content")
    daemon.process_paths(
        store_backend, {file.resolve()}, FakeProvider(), Liveness(600), machine=machine_identity
    )
    old_meta = store_backend.all_meta()[str(file.resolve())]
    file.write_text("new content")
    dirty: queue.Queue[Path] = queue.Queue()
    dirty.put(file)
    ticks = 0

    class FailingOnce(FakeProvider):
        def embed_batch(self, texts: list[str]) -> np.ndarray:
            if ticks == 1:
                raise EmbeddingAPIError("temporarily unavailable")
            return super().embed_batch(texts)

    async def poll(_delay: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 3:
            assert store_backend.all_meta()[str(file.resolve())] == old_meta
            dirty.put(file)  # A genuine new event is allowed to update the index.
        if ticks == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(daemon.asyncio, "sleep", poll)
    with pytest.raises(asyncio.CancelledError):
        await daemon.drain_loop(
            store_backend, dirty, Liveness(600), FailingOnce(), machine=machine_identity
        )
    assert store_backend.all_meta()[str(file.resolve())][1] == content_hash("new content")
