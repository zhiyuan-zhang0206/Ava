"""Memory indexer unit tests — daemon reconcile + the milvus storage engine.

The embedding provider is mocked (a `_FakeProvider`); the storage tests use
a session-scoped milvus-lite standalone server (`milvus_client` fixture in
tests/conftest.py). The milvus-lite standalone server starts in ~3s, one
shared per session; tests drop the collection between them for isolation.
Same backing as prod (standalone server), no in-process mixing.

The provider contract (Gemini adapter wire behavior) is pinned separately
in `tests/services/test_embeddings.py`; here the focus is the daemon's
reconcile logic, including the provider-fingerprint gate (a provider
switch re-embeds every row even at the same content hash — same dim is
not the same semantic space).
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from services.memory_indexer import daemon
from services.memory_indexer.backends.base import content_hash
from services.memory_indexer.backends.milvus import (
    _COLLECTION,
    _EXPECTED_FIELDS,
    MilvusBackend,
    _schema_current,
)
from services.memory_indexer.embeddings.base import EmbeddingAPIError

_DIM = 8
_FP = "test:gemini:dim=8"


def _backend(client: Any) -> MilvusBackend:
    """Wrap the raw milvus fixture client in the backend adapter — the
    daemon talks to backends, not raw clients."""
    return MilvusBackend(dim=_DIM, fingerprint=_FP, client=client)


class _FakeProvider:
    """Stand-in for `EmbeddingProvider` — records embed calls, returns
    constant vectors; `fingerprint` is settable so tests can simulate a
    provider switch."""

    def __init__(self, *, fingerprint: str = _FP, dim: int = _DIM) -> None:
        self.name = "fake"
        self.dim = dim
        self.fingerprint = fingerprint
        self.embed_batch_count = 0

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        self.embed_batch_count += 1
        return np.array([[float(len(t))] * self.dim for t in texts], dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return np.zeros(self.dim, dtype=np.float32)

    async def embed_query_async(self, text: str) -> np.ndarray:
        return np.zeros(self.dim, dtype=np.float32)


@pytest.fixture(autouse=True)
def _watched_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the daemon's watched root at this test's sandbox.

    `_process_paths` now prunes any path outside the watched root (stale
    authoring-checkout leftovers), so tests that embed files under
    `tmp_path` must make `tmp_path` the root — otherwise every file they
    write would count as foreign and be deleted.
    """
    monkeypatch.setattr(daemon, "_MEMORY_ROOT", tmp_path)


def test_content_hash_deterministic() -> None:
    assert content_hash("hello") == content_hash("hello")
    assert content_hash("hello") != content_hash("world")


# ── milvus storage engine (through the backend adapter) ───────────────────


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(_DIM).astype(np.float32)


def test_index_connect_creates_collection(milvus_client) -> None:
    """The fixture's connect() idempotently created the collection at the
    test dim; `_schema_current` agrees."""
    assert milvus_client.has_collection(_COLLECTION)  # pyright: ignore[reportUnknownMemberType]
    assert _schema_current(milvus_client, _DIM)  # pyright: ignore[reportUnknownArgumentType]


def test_upsert_then_all_meta(milvus_client) -> None:
    backend = _backend(milvus_client)
    backend.upsert("/a/b.md", 1.0, "hash1", _vec(0), kind="body", chunk_idx=0)
    backend.upsert("/c/d.md", 2.0, "hash2", _vec(1), kind="body", chunk_idx=0)
    assert backend.all_meta() == {
        "/a/b.md": (1.0, "hash1", _FP),
        "/c/d.md": (2.0, "hash2", _FP),
    }


def test_upsert_overwrite(milvus_client) -> None:
    """Same path = update (by primary key path), not insert."""
    backend = _backend(milvus_client)
    backend.upsert("/a.md", 1.0, "hash1", _vec(0), kind="body", chunk_idx=0)
    backend.upsert("/a.md", 2.0, "hash2", _vec(1), kind="body", chunk_idx=0)
    assert backend.all_meta() == {"/a.md": (2.0, "hash2", _FP)}


def test_upsert_many_inserts_all_rows_and_overwrites(milvus_client) -> None:
    backend = _backend(milvus_client)
    backend.upsert_many(
        [
            ("/a.md", 1.0, "old", _vec(0), "body", 0),
            ("/b.md", 2.0, "hb", _vec(1), "body", 0),
            ("/a.md", 3.0, "new", _vec(2), "body", 0),
        ]
    )
    assert backend.all_meta() == {"/a.md": (3.0, "new", _FP), "/b.md": (2.0, "hb", _FP)}
    rows = milvus_client.query(  # pyright: ignore[reportUnknownMemberType]
        collection_name=_COLLECTION, filter="", output_fields=["pk"], limit=100
    )
    assert len(rows) == 2  # pyright: ignore[reportUnknownArgumentType]


def test_readonly_milvus_upsert_many_raises(milvus_client) -> None:
    backend = MilvusBackend(
        dim=_DIM,
        fingerprint=_FP,
        client=milvus_client,  # pyright: ignore[reportUnknownArgumentType]
        readonly=True,
    )
    with pytest.raises(RuntimeError, match="read-only"):
        backend.upsert_many([("/a.md", 1.0, "ha", _vec(0), "body", 0)])


def test_delete(milvus_client) -> None:
    backend = _backend(milvus_client)
    backend.upsert("/a.md", 1.0, "h", _vec(0), kind="body", chunk_idx=0)
    backend.delete("/a.md")
    assert backend.all_meta() == {}


def test_delete_missing_noop(milvus_client) -> None:
    _backend(milvus_client).delete("/never_existed.md")  # no raise


def test_search_topk_returns_sorted_by_cosine(milvus_client) -> None:
    """Insert 3 known vectors, query matches one of them, that path ranks first."""
    backend = _backend(milvus_client)
    target = np.ones(_DIM, dtype=np.float32)
    orthogonal = np.zeros(_DIM, dtype=np.float32)
    orthogonal[0] = 1.0
    opposite = -np.ones(_DIM, dtype=np.float32)

    backend.upsert("/target.md", 1.0, "h1", target, kind="body", chunk_idx=0)
    backend.upsert("/orthogonal.md", 2.0, "h2", orthogonal, kind="body", chunk_idx=0)
    backend.upsert("/opposite.md", 3.0, "h3", opposite, kind="body", chunk_idx=0)

    results = backend.search_topk(target, k=3)
    assert results[0] == "/target.md"
    assert results[-1] == "/opposite.md"


def test_search_topk_empty_collection(milvus_client) -> None:
    assert _backend(milvus_client).search_topk(_vec(0), k=5) == []


def test_search_topk_respects_k(milvus_client) -> None:
    backend = _backend(milvus_client)
    for i in range(10):
        backend.upsert(f"/{i}.md", float(i), f"h{i}", _vec(i), kind="body", chunk_idx=0)
    assert len(backend.search_topk(_vec(0), k=3)) == 3


def test_upsert_chunk_rows_meta_stays_per_path(milvus_client) -> None:
    backend = _backend(milvus_client)
    for idx in range(3):
        backend.upsert("/a.md", 1.0, "ha", _vec(idx), kind="body", chunk_idx=idx)
    backend.upsert("/a.md", 1.0, "ha", _vec(9), kind="desc", chunk_idx=0)
    backend.upsert("/b.md", 2.0, "hb", _vec(0), kind="body", chunk_idx=0)
    assert backend.all_meta() == {
        "/a.md": (1.0, "ha", _FP),
        "/b.md": (2.0, "hb", _FP),
    }


def test_search_topk_aggregates_chunks_by_path(milvus_client) -> None:
    """Chunk rows of one path collapse to a single hit; the best chunk per
    path decides the rank."""
    backend = _backend(milvus_client)
    ones = np.ones(_DIM, dtype=np.float32)
    backend.upsert("/a.md", 1.0, "ha", ones, kind="body", chunk_idx=0)
    backend.upsert("/a.md", 1.0, "ha", 0.9 * ones, kind="body", chunk_idx=1)
    backend.upsert("/a.md", 1.0, "ha", 0.5 * ones, kind="body", chunk_idx=2)
    backend.upsert("/b.md", 2.0, "hb", -ones, kind="body", chunk_idx=0)
    results = backend.search_topk(ones, k=5)
    assert results == ["/a.md", "/b.md"]  # no duplicate paths


def test_delete_removes_all_chunks_of_path(milvus_client) -> None:
    backend = _backend(milvus_client)
    for idx in range(3):
        backend.upsert("/a.md", 1.0, "ha", _vec(idx), kind="body", chunk_idx=idx)
    backend.upsert("/a.md", 1.0, "ha", _vec(9), kind="desc", chunk_idx=0)
    backend.upsert("/b.md", 2.0, "hb", _vec(0), kind="body", chunk_idx=0)
    backend.delete("/a.md")
    assert backend.all_meta() == {"/b.md": (2.0, "hb", _FP)}
    assert backend.search_topk(_vec(0), k=5) == ["/b.md"]


def test_connect_migrates_legacy_schema(milvus_client) -> None:
    """A legacy single-row-per-file collection (path PK, no kind/chunk_idx)
    is dropped and recreated by connect(); its rows are gone — cold-start
    rebuilds them chunked."""
    from pymilvus import DataType

    client = milvus_client
    client.drop_collection(_COLLECTION)  # pyright: ignore[reportUnknownMemberType]
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("path", DataType.VARCHAR, is_primary=True, max_length=1024)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("mtime", DataType.DOUBLE)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("content_hash", DataType.VARCHAR, max_length=128)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=_DIM)  # pyright: ignore[reportUnknownMemberType]
    legacy_idx = client.prepare_index_params()  # pyright: ignore[reportUnknownMemberType]
    legacy_idx.add_index(field_name="vector", metric_type="COSINE", index_type="AUTOINDEX")  # pyright: ignore[reportUnknownMemberType]
    client.create_collection(  # pyright: ignore[reportUnknownMemberType]
        collection_name=_COLLECTION, schema=schema, index_params=legacy_idx
    )
    client.insert(  # pyright: ignore[reportUnknownMemberType]
        collection_name=_COLLECTION,
        data=[
            {
                "path": "/old.md",
                "mtime": 1.0,
                "content_hash": "h",
                "vector": [0.0] * _DIM,
            }
        ],
    )

    fresh = MilvusBackend(dim=_DIM, fingerprint=_FP)
    fresh.connect()
    try:
        info = fresh._require_client().describe_collection(collection_name=_COLLECTION)  # pyright: ignore[reportUnknownMemberType]
        fields = info["fields"] if isinstance(info, dict) else getattr(info, "fields", [])  # pyright: ignore[reportUnknownArgumentType]
        names = {f["name"] for f in fields}
        assert names >= _EXPECTED_FIELDS
        assert fresh.all_meta() == {}  # legacy row dropped with the collection
    finally:
        fresh.close()


def test_schema_current_detects_legacy_layout(milvus_client) -> None:
    """_schema_current is True on the chunked schema, False on the legacy one."""
    assert _schema_current(milvus_client, _DIM)  # pyright: ignore[reportUnknownArgumentType]

    from pymilvus import DataType

    client = milvus_client
    client.drop_collection(_COLLECTION)  # pyright: ignore[reportUnknownMemberType]
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("path", DataType.VARCHAR, is_primary=True, max_length=1024)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("mtime", DataType.DOUBLE)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("content_hash", DataType.VARCHAR, max_length=128)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=_DIM)  # pyright: ignore[reportUnknownMemberType]
    idx = client.prepare_index_params()  # pyright: ignore[reportUnknownMemberType]
    idx.add_index(field_name="vector", metric_type="COSINE", index_type="AUTOINDEX")  # pyright: ignore[reportUnknownMemberType]
    client.create_collection(collection_name=_COLLECTION, schema=schema, index_params=idx)  # pyright: ignore[reportUnknownMemberType]
    assert not _schema_current(client, _DIM)  # pyright: ignore[reportUnknownArgumentType]


# ── daemon helpers ──────────────────────────────────────────────────────


def test_scan_disk_finds_md_only(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.md").write_text("c")

    result = daemon._scan_disk(tmp_path)
    assert set(result.keys()) == {(tmp_path / "a.md").resolve(), (sub / "c.md").resolve()}


def test_scan_disk_skips_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "real.md"
    real.write_text("a")
    link = tmp_path / "link.md"
    link.symlink_to(real)

    result = daemon._scan_disk(tmp_path)
    assert link.resolve() not in result or set(result.keys()) == {real.resolve()}


def test_scan_disk_missing_root_returns_empty(tmp_path: Path) -> None:
    result = daemon._scan_disk(tmp_path / "nonexistent")
    assert result == {}


def test_process_paths_embeds_new_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    milvus_client,
) -> None:
    f1 = tmp_path / "a.md"
    f1.write_text("content A")
    f2 = tmp_path / "b.md"
    f2.write_text("content B")

    provider = _FakeProvider()
    daemon._process_paths(_backend(milvus_client), {f1.resolve(), f2.resolve()}, provider)
    assert provider.embed_batch_count == 1
    meta = _backend(milvus_client).all_meta()
    assert str(f1.resolve()) in meta
    assert str(f2.resolve()) in meta


def test_process_paths_skips_unchanged_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    milvus_client,
) -> None:
    f = tmp_path / "a.md"
    f.write_text("content")

    provider = _FakeProvider()
    daemon._process_paths(_backend(milvus_client), {f.resolve()}, provider)
    assert provider.embed_batch_count == 1

    daemon._process_paths(_backend(milvus_client), {f.resolve()}, provider)
    assert provider.embed_batch_count == 1  # hash unchanged, no re-embed


def test_process_paths_reembeds_on_provider_fingerprint_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    milvus_client,
) -> None:
    """CTO ① (2026-08-30): a provider switch must trigger a full rebuild —
    old vectors live in a different semantic space even at the same dim, so
    they cannot be mixed with new ones (same content hash is irrelevant)."""
    f = tmp_path / "a.md"
    f.write_text("content")

    first = _FakeProvider()
    daemon._process_paths(_backend(milvus_client), {f.resolve()}, first)
    assert first.embed_batch_count == 1

    # Simulate the switch: same content, same mtime, new provider fingerprint —
    # a fresh daemon run would build the backend for the new provider too.
    switched = _FakeProvider(fingerprint="another-provider:dim=8")
    switched_backend = MilvusBackend(
        dim=_DIM,
        fingerprint="another-provider:dim=8",
        client=milvus_client,  # pyright: ignore[reportUnknownArgumentType]
    )
    daemon._process_paths(switched_backend, {f.resolve()}, switched)
    assert switched.embed_batch_count == 1  # re-embedded despite unchanged hash
    meta = switched_backend.all_meta()
    assert meta[str(f.resolve())][2] == "another-provider:dim=8"


def test_process_paths_deletes_missing_files(tmp_path: Path, milvus_client) -> None:
    """Path enters dirty set but file not on disk — index row is deleted."""
    ghost = str(
        tmp_path / "ghost_nonexistent.md"
    )  # under tmp_path, definitely does not exist (never written)
    _backend(milvus_client).upsert(ghost, 1.0, "h", _vec(0), kind="body", chunk_idx=0)
    daemon._process_paths(_backend(milvus_client), {Path(ghost)}, _FakeProvider())
    assert _backend(milvus_client).all_meta() == {}


def test_event_handler_pushes_md_paths_only() -> None:
    import queue as q

    dirty: q.Queue[Path] = q.Queue()
    handler = daemon._MarkdownEventHandler(dirty)

    class E:
        def __init__(self, src: str, *, is_dir: bool = False) -> None:
            self.src_path = src
            self.is_directory = is_dir

    handler.on_created(E("/a.md"))  # type: ignore[arg-type]
    handler.on_modified(E("/b.txt"))  # type: ignore[arg-type]
    handler.on_deleted(E("/c.md"))  # type: ignore[arg-type]
    handler.on_created(E("/dir", is_dir=True))  # type: ignore[arg-type]

    pushed = []
    while not dirty.empty():
        pushed.append(dirty.get_nowait())  # pyright: ignore[reportUnknownMemberType]
    assert pushed == [Path("/a.md"), Path("/c.md")]


def test_event_handler_on_moved_pushes_both_ends() -> None:
    import queue as q

    dirty: q.Queue[Path] = q.Queue()
    handler = daemon._MarkdownEventHandler(dirty)

    class MoveEvent:
        src_path = "/old.md"
        dest_path = "/new.md"
        is_directory = False

    handler.on_moved(MoveEvent())  # type: ignore[arg-type]
    pushed = []
    while not dirty.empty():
        pushed.append(dirty.get_nowait())  # pyright: ignore[reportUnknownMemberType]
    assert pushed == [Path("/old.md"), Path("/new.md")]


def test_process_paths_deletes_foreign_paths_even_when_file_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    milvus_client,
) -> None:
    """Rows outside the watched root are pruned even when the file still
    exists on disk — the stale authoring-checkout leftovers that surface
    as duplicate search hits (e.g. user-profile.md ×2)."""
    watched = tmp_path / "watched"
    watched.mkdir()
    monkeypatch.setattr(daemon, "_MEMORY_ROOT", watched)

    foreign_dir = tmp_path / "foreign"
    foreign_dir.mkdir()
    foreign = foreign_dir / "note.md"  # exists on disk, outside watched root
    foreign.write_text("content")

    _backend(milvus_client).upsert(str(foreign), 1.0, "h", _vec(0), kind="body", chunk_idx=0)
    daemon._process_paths(_backend(milvus_client), {foreign}, _FakeProvider())
    assert _backend(milvus_client).all_meta() == {}


def test_cold_start_reconcile_prunes_foreign_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    milvus_client,
) -> None:
    """Cold-start reconcile deletes rows whose path is outside the watched
    root — the durable fix for the 11 stale authoring-checkout entries."""
    root = tmp_path / "watched"
    root.mkdir()
    watched = root / "a.md"
    watched.write_text("watched content")
    foreign = tmp_path / "foreign.md"  # exists on disk, outside root
    foreign.write_text("foreign content")

    monkeypatch.setattr(daemon, "_MEMORY_ROOT", root)
    # Both rows pre-exist in the index (e.g. from an era before the
    # gateway-checkout split).
    backend = _backend(milvus_client)
    backend.upsert(str(watched.resolve()), 1.0, "h", _vec(0), kind="body", chunk_idx=0)
    backend.upsert(str(foreign.resolve()), 1.0, "h", _vec(0), kind="body", chunk_idx=0)

    daemon._cold_start_reconcile(backend, _FakeProvider())
    meta = _backend(milvus_client).all_meta()
    assert str(foreign.resolve()) not in meta
    assert str(watched.resolve()) in meta


def test_cold_start_reconcile_reembeds_on_provider_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    milvus_client,
) -> None:
    """CTO ①: the provider fingerprint is part of the reconcile key — a row
    built by another provider is dirty at cold start even at the same mtime
    and hash, so the switch wipes the index by re-embedding everything."""
    root = tmp_path / "watched"
    root.mkdir()
    watched = root / "a.md"
    watched.write_text("watched content")

    monkeypatch.setattr(daemon, "_MEMORY_ROOT", root)
    backend = _backend(milvus_client)
    watched_mtime = watched.stat().st_mtime
    backend.upsert(str(watched.resolve()), watched_mtime, "h", _vec(0), kind="body", chunk_idx=0)
    # mtime matches disk, hash matches — but the fingerprint is the old provider's.
    assert backend.all_meta() == {str(watched.resolve()): (watched_mtime, "h", _FP)}

    switched = _FakeProvider(fingerprint="other:provider")
    switched_backend = MilvusBackend(
        dim=_DIM,
        fingerprint="other:provider",
        client=milvus_client,  # pyright: ignore[reportUnknownArgumentType]
    )
    daemon._cold_start_reconcile(switched_backend, switched)
    # The row was re-embedded with the new fingerprint.
    meta = switched_backend.all_meta()
    assert str(watched.resolve()) in meta
    assert meta[str(watched.resolve())][2] == "other:provider"


def test_refresh_gateway_checkout_fast_forwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Safety net: pulls origin/main into the gateway checkout and logs
    loudly when HEAD moved (a post-merge refresh was missed)."""
    import logging

    subprocess.run(  # noqa: S603 — fixed argv, test sandbox
        ["git", "init", "-q", str(tmp_path)], check=True
    )
    subprocess.run(  # noqa: S603 — fixed argv, test sandbox
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(  # noqa: S603 — fixed argv, test sandbox
        ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True
    )
    (tmp_path / "a.md").write_text("x")
    subprocess.run(  # noqa: S603 — fixed argv, test sandbox
        ["git", "-C", str(tmp_path), "add", "-A"], check=True
    )
    subprocess.run(  # noqa: S603 — fixed argv, test sandbox
        ["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True
    )
    before = subprocess.check_output(  # noqa: S603 — fixed argv, test sandbox
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()

    from shared import memory_repo

    monkeypatch.setattr(memory_repo, "gateway_memory_dir", lambda: tmp_path)
    monkeypatch.setattr(memory_repo, "pull_main", lambda: "abc1234")

    with caplog.at_level(logging.INFO, logger="services.memory_indexer.daemon"):
        daemon._refresh_gateway_checkout()
    assert "fast-forwarded" in caplog.text
    assert before != "abc1234"


def test_refresh_gateway_checkout_failure_logs_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed pull is logged at ERROR and never raised — the drain loop
    retries next cycle instead of letting the daemon die."""
    import logging

    from shared import memory_repo

    monkeypatch.setattr(memory_repo, "gateway_memory_dir", lambda: tmp_path)

    def _boom() -> str:
        raise RuntimeError("network down")

    monkeypatch.setattr(memory_repo, "pull_main", _boom)

    with caplog.at_level(logging.ERROR, logger="services.memory_indexer.daemon"):
        daemon._refresh_gateway_checkout()  # must not raise
    assert "refresh failed" in caplog.text


# ── chunk splitting (recall-v2: description + body chunks) ───────────────


def test_split_note_extracts_description_and_body() -> None:
    content = (
        "---\ntype: Memory\ndescription: A note about the user's health\n"
        "---\n\n# Health\n\nbody content"
    )
    desc, body = daemon._split_note(content)
    assert desc == "A note about the user's health"
    assert "# Health" in body
    assert "description" not in body


def test_split_note_no_frontmatter_returns_full_body() -> None:
    content = "# Just a heading\n\nNo YAML."
    desc, body = daemon._split_note(content)
    assert desc is None
    assert body == content


def test_split_note_blank_description_is_none() -> None:
    desc, body = daemon._split_note("---\ndescription: \n---\n\nbody")
    assert desc is None
    assert body == "\nbody"  # the shared parser keeps the blank line after the fence


def test_chunk_body_short_text_single_chunk() -> None:
    assert daemon._chunk_body("short body") == ["short body"]
    assert daemon._chunk_body("  \n\n  ") == []


def test_chunk_body_splits_at_paragraph_boundaries() -> None:
    paras = [f"paragraph-{i} " + "word " * 80 for i in range(6)]  # 412 chars each
    body = "\n\n".join(paras)
    chunks = daemon._chunk_body(body, max_chars=1800, overlap_chars=200)
    assert len(chunks) == 2
    assert all(len(c) <= 1800 for c in chunks)
    assert chunks[0].startswith("paragraph-0")
    assert chunks[1].startswith("paragraph-4")  # 4×412 = 1654 fits; 5×412 would not
    # every paragraph survives whole (chunking strips paragraph whitespace)
    assert all(p.strip() in "".join(chunks) for p in paras)


def test_chunk_body_overlap_carries_trailing_paragraphs() -> None:
    paras = [f"paragraph-{i} " + "word " * 80 for i in range(6)]
    body = "\n\n".join(paras)
    chunks = daemon._chunk_body(body, max_chars=1400, overlap_chars=600)
    # the previous chunk's tail paragraph re-opens the next chunk
    assert chunks[1].startswith("paragraph-2")
    assert "paragraph-2" in chunks[0]


def test_chunk_body_hard_splits_oversized_paragraph() -> None:
    para = "x" * 3000
    chunks = daemon._chunk_body(para, max_chars=1000, overlap_chars=100)
    assert chunks == ["x" * 1000, "x" * 1000, "x" * 1000, "x" * 300]


def test_file_rows_desc_plus_body_chunks() -> None:
    content = "---\ntype: Memory\ndescription: hand off to 402\n---\n\n" + "\n\n".join(
        f"paragraph-{i} " + "word " * 80 for i in range(6)
    )
    rows = daemon._file_rows(content)
    assert rows[0] == ("desc", 0, "hand off to 402")
    assert [k for k, _, _ in rows] == ["desc", "body", "body"]


def test_file_rows_no_frontmatter_no_desc() -> None:
    assert daemon._file_rows("# heading\n\nshort body") == [("body", 0, "# heading\n\nshort body")]


def test_process_paths_indexes_desc_and_body_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    milvus_client,
) -> None:
    """One file with a description + a long body lands as 1 desc row + N body
    chunks; all_meta still reports the file once."""
    f = tmp_path / "long.md"
    f.write_text(
        "---\ntype: Memory\ndescription: hand off to 402\n---\n\n"
        + "\n\n".join(f"paragraph-{i} " + "word " * 80 for i in range(6)),
        encoding="utf-8",
    )

    daemon._process_paths(_backend(milvus_client), {f.resolve()}, _FakeProvider())
    assert str(f.resolve()) in _backend(milvus_client).all_meta()
    rows = milvus_client.query(  # pyright: ignore[reportUnknownMemberType]
        collection_name=_COLLECTION,
        filter=f'path == "{f.resolve()!s}"',
        output_fields=["kind", "chunk_idx"],
        limit=100,
    )
    kinds = sorted((r["kind"], r["chunk_idx"]) for r in rows)  # pyright: ignore[reportUnknownArgumentType]
    assert kinds == [("body", 0), ("body", 1), ("desc", 0)]


def test_process_paths_calls_upsert_many_once_across_embed_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    milvus_client,
) -> None:
    class _RecordingBackend(MilvusBackend):
        def __init__(self) -> None:
            super().__init__(dim=_DIM, fingerprint=_FP, client=milvus_client)  # pyright: ignore[reportUnknownArgumentType]
            self.calls: list[list[tuple[str, float, str, np.ndarray, str, int]]] = []

        def upsert_many(self, rows: Sequence[tuple[str, float, str, np.ndarray, str, int]]) -> None:
            self.calls.append(list(rows))
            super().upsert_many(rows)

    first = tmp_path / "first.md"
    first.write_text("---\ndescription: first description\n---\nfirst body", encoding="utf-8")
    second = tmp_path / "second.md"
    second.write_text("second body", encoding="utf-8")
    monkeypatch.setattr(daemon, "_BATCH_SIZE", 2)
    backend = _RecordingBackend()

    daemon._process_paths(backend, {first.resolve(), second.resolve()}, _FakeProvider())

    assert len(backend.calls) == 1
    assert len(backend.calls[0]) == 3
    assert {row[0] for row in backend.calls[0]} == {str(first.resolve()), str(second.resolve())}


def test_process_paths_flushes_embedded_rows_before_embedding_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    milvus_client,
) -> None:
    class _FailSecondBatchProvider(_FakeProvider):
        def embed_batch(self, texts: list[str]) -> np.ndarray:
            if self.embed_batch_count == 1:
                raise EmbeddingAPIError("second batch failed")
            return super().embed_batch(texts)

    class _RecordingBackend(MilvusBackend):
        def __init__(self) -> None:
            super().__init__(dim=_DIM, fingerprint=_FP, client=milvus_client)  # pyright: ignore[reportUnknownArgumentType]
            self.calls: list[list[tuple[str, float, str, np.ndarray, str, int]]] = []

        def upsert_many(self, rows: Sequence[tuple[str, float, str, np.ndarray, str, int]]) -> None:
            self.calls.append(list(rows))
            super().upsert_many(rows)

    note = tmp_path / "note.md"
    note.write_text("---\ndescription: description\n---\nbody", encoding="utf-8")
    monkeypatch.setattr(daemon, "_BATCH_SIZE", 1)
    backend = _RecordingBackend()

    with pytest.raises(EmbeddingAPIError, match="second batch failed"):
        daemon._process_paths(backend, {note.resolve()}, _FailSecondBatchProvider())

    assert len(backend.calls) == 1
    assert len(backend.calls[0]) == 1
    assert backend.all_meta() == {
        str(note.resolve()): (note.stat().st_mtime, content_hash(note.read_text()), _FP)
    }
