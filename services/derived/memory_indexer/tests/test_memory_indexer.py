"""Memory indexer unit tests — daemon reconcile + chunk commit logic.

The embedding provider is mocked (a `_FakeProvider`); storage is the real
`MemoryStore` behind an in-process adapter (`store_backend.StoreBackend`), so
the daemon's reconcile, tail-cleanup and provider-switch behavior runs against
the production numpy storage core without the HTTP hop. The store's own
semantics are pinned in `services/derived/memory_search/tests/test_memory_search_store.py`.

The provider contract (Gemini adapter wire behavior) is pinned separately
in `services/derived/memory_indexer/embeddings/tests/test_embeddings.py`; here the focus is the daemon's
reconcile logic, including the provider-fingerprint gate (a provider
switch re-embeds every row even at the same content hash — same dim is
not the same semantic space).
"""

from __future__ import annotations

import asyncio
import queue
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import numpy as np
import pytest

from base.config import settings
from base.daemon import health
from base.daemon.health import Liveness
from base.lm.plugin_providers import build_model_catalog
from services.derived.memory_indexer import config, daemon
from services.derived.memory_indexer.backends.base import MemorySearchBackend, content_hash
from services.derived.memory_indexer.embeddings import factory
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError
from services.derived.memory_indexer.tests.store_backend import StoreBackend

_DIM = 8
_FP = "test:gemini:dim=8"


def _liveness_timeout() -> float:
    return config.liveness_timeout_seconds(
        settings.services.embedding_backend,
        timeout_seconds=settings.services.memory_embed_timeout_seconds,
    )


@pytest.fixture
def store_backend(tmp_path: Path) -> StoreBackend:
    """A fresh in-process backend over a real `MemoryStore` (npz under tmp_path)."""
    return StoreBackend(tmp_path / "index" / "vectors.npz", dim=_DIM, fingerprint=_FP)


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(_DIM).astype(np.float32)


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
    monkeypatch.setattr(daemon, "_memory_root", lambda: tmp_path)


def test_content_hash_deterministic() -> None:
    assert content_hash("hello") == content_hash("hello")
    assert content_hash("hello") != content_hash("world")


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
    store_backend: StoreBackend,
) -> None:
    f1 = tmp_path / "a.md"
    f1.write_text("content A")
    f2 = tmp_path / "b.md"
    f2.write_text("content B")

    provider = _FakeProvider()
    daemon._process_paths(
        store_backend,
        {f1.resolve(), f2.resolve()},
        provider,
        Liveness(_liveness_timeout()),
    )
    assert provider.embed_batch_count == 1
    meta = store_backend.all_meta()
    assert str(f1.resolve()) in meta
    assert str(f2.resolve()) in meta


def test_process_paths_skips_unchanged_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
) -> None:
    f = tmp_path / "a.md"
    f.write_text("content")

    provider = _FakeProvider()
    daemon._process_paths(
        store_backend,
        {f.resolve()},
        provider,
        Liveness(_liveness_timeout()),
    )
    assert provider.embed_batch_count == 1

    daemon._process_paths(
        store_backend,
        {f.resolve()},
        provider,
        Liveness(_liveness_timeout()),
    )
    assert provider.embed_batch_count == 1  # hash unchanged, no re-embed


def test_process_paths_reembeds_on_provider_fingerprint_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
) -> None:
    """CTO ① (2026-08-30): a provider switch must trigger a full rebuild —
    old vectors live in a different semantic space even at the same dim, so
    they cannot be mixed with new ones (same content hash is irrelevant)."""
    f = tmp_path / "a.md"
    f.write_text("content")

    first = _FakeProvider()
    daemon._process_paths(
        store_backend,
        {f.resolve()},
        first,
        Liveness(_liveness_timeout()),
    )
    assert first.embed_batch_count == 1

    # Simulate the switch: same content, same mtime, new provider fingerprint —
    # a fresh daemon run would build the backend for the new provider too.
    switched = _FakeProvider(fingerprint="another-provider:dim=8")
    switched_backend = store_backend.reopen("another-provider:dim=8")
    daemon._process_paths(
        switched_backend,
        {f.resolve()},
        switched,
        Liveness(_liveness_timeout()),
    )
    assert switched.embed_batch_count == 1  # re-embedded despite unchanged hash
    meta = switched_backend.all_meta()
    assert meta[str(f.resolve())][2] == "another-provider:dim=8"


def test_process_paths_deletes_missing_files(tmp_path: Path, store_backend: StoreBackend) -> None:
    """Path enters dirty set but file not on disk — index row is deleted."""
    ghost = str(
        tmp_path / "ghost_nonexistent.md"
    )  # under tmp_path, definitely does not exist (never written)
    store_backend.upsert(ghost, 1.0, "h", _vec(0), kind="body", chunk_idx=0)
    daemon._process_paths(
        store_backend,
        {Path(ghost)},
        _FakeProvider(),
        Liveness(_liveness_timeout()),
    )
    assert store_backend.all_meta() == {}


def test_event_handler_pushes_md_paths_only() -> None:
    dirty: queue.Queue[Path] = queue.Queue()
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
    dirty: queue.Queue[Path] = queue.Queue()
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
    store_backend: StoreBackend,
) -> None:
    """Rows outside the watched root are pruned even when the file still
    exists on disk — the stale authoring-checkout leftovers that surface
    as duplicate search hits (e.g. user-profile.md ×2)."""
    watched = tmp_path / "watched"
    watched.mkdir()
    monkeypatch.setattr(daemon, "_memory_root", lambda: watched)

    foreign_dir = tmp_path / "foreign"
    foreign_dir.mkdir()
    foreign = foreign_dir / "note.md"  # exists on disk, outside watched root
    foreign.write_text("content")

    store_backend.upsert(str(foreign), 1.0, "h", _vec(0), kind="body", chunk_idx=0)
    daemon._process_paths(
        store_backend,
        {foreign},
        _FakeProvider(),
        Liveness(_liveness_timeout()),
    )
    assert store_backend.all_meta() == {}


def test_cold_start_reconcile_prunes_foreign_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
) -> None:
    """Cold-start reconcile deletes rows whose path is outside the watched
    root — the durable fix for the 11 stale authoring-checkout entries."""
    root = tmp_path / "watched"
    root.mkdir()
    watched = root / "a.md"
    watched.write_text("watched content")
    foreign = tmp_path / "foreign.md"  # exists on disk, outside root
    foreign.write_text("foreign content")

    monkeypatch.setattr(daemon, "_memory_root", lambda: root)
    # Both rows pre-exist in the index (e.g. from an era before the
    # gateway-checkout split).
    backend = store_backend
    backend.upsert(str(watched.resolve()), 1.0, "h", _vec(0), kind="body", chunk_idx=0)
    backend.upsert(str(foreign.resolve()), 1.0, "h", _vec(0), kind="body", chunk_idx=0)

    daemon._reconcile(
        backend,
        _FakeProvider(),
        Liveness(_liveness_timeout()),
    )
    meta = store_backend.all_meta()
    assert str(foreign.resolve()) not in meta
    assert str(watched.resolve()) in meta


def test_cold_start_reconcile_reembeds_on_provider_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
) -> None:
    """CTO ①: the provider fingerprint is part of the reconcile key — a row
    built by another provider is dirty at cold start even at the same mtime
    and hash, so the switch wipes the index by re-embedding everything."""
    root = tmp_path / "watched"
    root.mkdir()
    watched = root / "a.md"
    watched.write_text("watched content")

    monkeypatch.setattr(daemon, "_memory_root", lambda: root)
    backend = store_backend
    watched_mtime = watched.stat().st_mtime
    backend.upsert(str(watched.resolve()), watched_mtime, "h", _vec(0), kind="body", chunk_idx=0)
    # mtime matches disk, hash matches — but the fingerprint is the old provider's.
    assert backend.all_meta() == {str(watched.resolve()): (watched_mtime, "h", _FP)}

    switched = _FakeProvider(fingerprint="other:provider")
    switched_backend = store_backend.reopen("other:provider")
    daemon._reconcile(
        switched_backend,
        switched,
        Liveness(_liveness_timeout()),
    )
    # The row was re-embedded with the new fingerprint.
    meta = switched_backend.all_meta()
    assert str(watched.resolve()) in meta
    assert meta[str(watched.resolve())][2] == "other:provider"


class _RecordingBackend(MemorySearchBackend):
    """In-memory writes isolate beat placement from RPC and filesystem latency."""

    name = "recording"

    def __init__(self, spend: Callable[[str], None] = lambda _op: None) -> None:
        self.rows: dict[tuple[str, str, int], tuple[float, str, str]] = {}
        self.calls: list[str] = []
        self.spend = spend

    def connect(self) -> None:
        raise AssertionError("processing must use the already connected backend")

    def close(self) -> None:
        raise AssertionError("processing must leave backend lifecycle to its caller")

    def upsert(
        self,
        path: str,
        mtime: float,
        content_hash: str,
        embedding: np.ndarray,
        *,
        kind: str,
        chunk_idx: int,
    ) -> None:
        self.upsert_many([(path, mtime, content_hash, embedding, kind, chunk_idx)])

    def search_topk(self, query_vector: np.ndarray, k: int) -> list[str]:
        raise AssertionError("indexing must not search")

    async def search_topk_async(
        self, query_vector: np.ndarray, k: int, *, timeout: float
    ) -> list[str]:
        raise AssertionError("indexing must not search")

    def all_meta(self) -> dict[str, tuple[float, str, str]]:
        return {path: meta for (path, _, _), meta in self.rows.items()}

    def delete(self, path: str) -> None:
        self.calls.append("delete")
        self.spend("delete")
        self.rows = {key: meta for key, meta in self.rows.items() if key[0] != path}

    def delete_stale_rows(self, entries: Sequence[tuple[str, dict[str, int]]]) -> None:
        self.calls.append("delete_stale_rows")
        self.spend("delete_stale_rows")
        for path, limits in entries:
            self.rows = {
                key: meta
                for key, meta in self.rows.items()
                if key[0] != path or (key[1] in limits and key[2] < limits[key[1]])
            }

    def upsert_many(self, rows: Sequence[tuple[str, float, str, np.ndarray, str, int]]) -> None:
        self.calls.append("upsert_many")
        self.spend("upsert_many")
        for path, mtime, hash_, _, kind, idx in rows:
            self.rows[path, kind, idx] = (mtime, hash_, _FP)


def test_process_paths_beats_per_embed_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every batch gets its own beat, even when their total exceeds the ceiling."""
    now = 0.0
    monkeypatch.setattr(health, "time", SimpleNamespace(monotonic=lambda: now))
    liveness = Liveness(_liveness_timeout())
    batch_duration = 100.0
    monkeypatch.setattr(daemon, "_BATCH_SIZE", 2)
    paths: set[Path] = set()
    for i in range(10 * daemon._BATCH_SIZE):
        note = tmp_path / f"note-{i:03d}.md"
        note.write_text(f"content {i}", encoding="utf-8")
        paths.add(note.resolve())

    class SlowProvider(_FakeProvider):
        def embed_batch(self, texts: list[str]) -> np.ndarray:
            nonlocal now
            now += batch_duration
            assert liveness.is_alive(), "liveness stale during embed batch"
            assert liveness.stale_for() == batch_duration, "previous batch compounded with embed"
            return super().embed_batch(texts)

    backend = _RecordingBackend()
    provider = SlowProvider()
    daemon._process_paths(backend, paths, provider, liveness)

    assert now > _liveness_timeout()
    assert provider.embed_batch_count == 10
    assert liveness.is_alive()
    assert set(backend.all_meta()) == {str(path) for path in paths}


@pytest.mark.parametrize("fail_last_batch", [False, True])
def test_process_paths_beats_during_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_last_batch: bool
) -> None:
    """The final embed, cleanup and upsert cannot share a beat-free interval."""
    now = 0.0
    monkeypatch.setattr(health, "time", SimpleNamespace(monotonic=lambda: now))
    liveness = Liveness(_liveness_timeout())
    durations = {"delete_stale_rows": 5.0, "upsert_many": 300.0}

    def spend(op: str) -> None:
        nonlocal now
        now += durations[op]
        assert liveness.is_alive(), f"liveness stale during {op}"
        assert liveness.stale_for() == durations[op], f"previous call compounded with {op}"

    class SlowFinalProvider(_FakeProvider):
        def embed_batch(self, texts: list[str]) -> np.ndarray:
            nonlocal now
            now += factory.worst_case_batch_seconds(
                settings.services.embedding_backend,
                timeout_seconds=settings.services.memory_embed_timeout_seconds,
            )
            assert liveness.is_alive()
            if fail_last_batch and self.embed_batch_count == 1:
                raise EmbeddingAPIError("last batch failed")
            return super().embed_batch(texts)

    monkeypatch.setattr(daemon, "_BATCH_SIZE", 1)
    paths = {tmp_path / name for name in ("a.md", "b.md")}
    for path in paths:
        path.write_text("body")
    backend = _RecordingBackend(spend)
    provider = SlowFinalProvider()
    if fail_last_batch:
        with pytest.raises(EmbeddingAPIError, match="last batch failed"):
            daemon._process_paths(backend, paths, provider, liveness)
        assert set(backend.all_meta()) == {str(tmp_path / "a.md")}
    else:
        daemon._process_paths(backend, paths, provider, liveness)
        assert set(backend.all_meta()) == {str(path) for path in paths}
    assert backend.calls == ["delete_stale_rows", "upsert_many"]


def test_process_paths_beats_per_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A delete-only drain batch may exceed the ceiling while each delete is timely."""
    now = 0.0
    monkeypatch.setattr(health, "time", SimpleNamespace(monotonic=lambda: now))
    liveness = Liveness(_liveness_timeout())

    def spend(op: str) -> None:
        nonlocal now
        assert op == "delete"
        now += 3.0
        assert liveness.is_alive(), "liveness stale during accumulated deletes"

    backend = _RecordingBackend(spend)
    paths = {tmp_path / f"deleted-{i}.md" for i in range(400)}
    backend.rows = {(str(path), "body", 0): (0.0, "old", _FP) for path in paths}
    provider = _FakeProvider()
    daemon._process_paths(backend, paths, provider, liveness)
    assert now > _liveness_timeout()
    assert backend.all_meta() == {}
    assert backend.calls == ["delete"] * len(paths)
    assert provider.embed_batch_count == 0


@pytest.mark.parametrize("provider_budget", [0.0, 10.0, 3600.0, 7200.0])
def test_liveness_timeout_covers_worst_embed_batch(
    monkeypatch: pytest.MonkeyPatch, provider_budget: float
) -> None:
    def batch_budget(name: str, *, timeout_seconds: float) -> float:
        del name, timeout_seconds
        return provider_budget

    monkeypatch.setattr(factory, "worst_case_batch_seconds", batch_budget)
    ceiling = _liveness_timeout()
    assert ceiling >= config._LIVENESS_TIMEOUT_FLOOR_S
    assert ceiling >= provider_budget + config._LIVENESS_SAFETY_MARGIN_S


def test_factory_worst_case_registry_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in factory._PROVIDERS:
        monkeypatch.setattr(settings.services, "embedding_backend", name)
        assert (
            factory.worst_case_batch_seconds(
                settings.services.embedding_backend,
                timeout_seconds=settings.services.memory_embed_timeout_seconds,
            )
            > 0
        ), name

    unknown = "unknown-provider"
    monkeypatch.setattr(settings.services, "embedding_backend", unknown)
    with pytest.raises(ValueError, match="unknown embedding provider") as provider_error:
        factory.get_provider_named(
            unknown,
            catalog=build_model_catalog(),
            timeout_reader=lambda: settings.services.memory_embed_timeout_seconds,
            api_key_reader=lambda: None,
        )
    with pytest.raises(ValueError) as budget_error:
        factory.worst_case_batch_seconds(
            settings.services.embedding_backend,
            timeout_seconds=settings.services.memory_embed_timeout_seconds,
        )
    assert str(budget_error.value) == str(provider_error.value)


def test_cold_start_reconcile_beats_liveness_across_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
) -> None:
    """Regression (watchdog kill loop, 2026-09-19): the cold-start rebuild
    works the dirty set in file-granular chunks, beating liveness between them.
    Before the fix, a rebuild lasting longer than the liveness ceiling kept
    /healthz at 'loop: stale', so the watchdog restarted the daemon
    mid-rebuild and the rebuild restarted from zero every ~3 minutes — it
    never converged."""
    root = tmp_path / "watched"
    root.mkdir()
    files: list[Path] = []
    for i in range(7):
        note = root / f"note-{i}.md"
        note.write_text(f"content {i}")
        files.append(note)
    monkeypatch.setattr(daemon, "_memory_root", lambda: root)
    monkeypatch.setattr(daemon, "_RECONCILE_CHUNK_PATHS", 2)  # 7 files -> 4 chunks

    chunk_sizes: list[int] = []
    visited: list[str] = []

    def slow_process_paths(
        backend: Any, paths: set[Path], provider: Any, liveness: Liveness
    ) -> None:
        chunk_sizes.append(len(paths))
        visited.extend(sorted(p.name for p in paths))
        time.sleep(0.2)

    monkeypatch.setattr(daemon, "_process_paths", slow_process_paths)

    liveness = Liveness(0.4)
    started = time.monotonic()
    assert daemon._reconcile(store_backend, _FakeProvider(), liveness) is True
    elapsed = time.monotonic() - started

    # Chunked by file: call count and sizes exact, every file visited once.
    assert chunk_sizes == [2, 2, 2, 1]
    assert visited == [note.name for note in files]
    # The rebuild outlived the liveness ceiling (4 chunks ≥ 0.8s > 0.4s)...
    assert elapsed > 0.4
    # ...yet the beats between chunks kept /healthz from ever reading stale.
    assert liveness.is_alive()
    assert liveness.stale_for() < 0.4


# ── chunk splitting (recall-v2: description + body chunks) ───────────────


def _long_note(paragraphs: int) -> str:
    return "---\ntype: Memory\ndescription: hand off to 402\n---\n\n" + "\n\n".join(
        f"paragraph-{i} " + "word " * 80 for i in range(paragraphs)
    )


def _assert_retry_idle(retry: daemon._ReconcileRetrySchedule) -> None:
    assert not retry.pending
    assert not retry.due()
    assert retry.retry_in_s() is None


def _assert_one_ladder_rung(
    retry: daemon._ReconcileRetrySchedule, clock: SimpleNamespace, delay: float
) -> None:
    """One incomplete signal arms the rung; repeated drain failures cannot postpone it."""
    assert retry.record_incomplete() == delay
    assert retry.pending
    assert not retry.due()
    assert retry.retry_in_s() == delay
    clock.now += delay - 0.25
    assert retry.ensure_scheduled() is None
    assert retry.retry_in_s() == 0.25
    assert not retry.due()
    clock.now += 0.25
    assert retry.due()
    clock.now += 1.0
    assert retry.retry_in_s() == 0.0


class _FlakyProvider(_FakeProvider):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise EmbeddingAPIError("429 Too Many Requests")
        return super().embed_batch(texts)


async def _cancel_quietly(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _assert_gap_closed(
    backend: StoreBackend,
    files: set[Path],
    retry: daemon._ReconcileRetrySchedule,
    liveness: Liveness,
    reconcile: Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert liveness.is_alive()
    assert set(await asyncio.to_thread(backend.all_meta)) == {str(p) for p in files}
    assert not retry.pending
    assert retry.retry_in_s() is None
    passes = reconcile.call_count
    assert passes == 4  # startup, two incomplete retries, then completion
    await asyncio.sleep(0.15)  # no extra pass after the cap-sized wait elapses
    assert reconcile.call_count == passes
    assert "gap closed; retries cleared" in caplog.text
    assert "reconcile incomplete; retry scheduled in 0.1s" in caplog.text


__all__ = ["_FlakyProvider"]
