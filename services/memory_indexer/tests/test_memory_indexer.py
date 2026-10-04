"""Memory indexer unit tests — daemon reconcile + chunk commit logic.

The embedding provider is mocked (a `_FakeProvider`); storage is the real
`MemoryStore` behind an in-process adapter (`store_backend.StoreBackend`), so
the daemon's reconcile, tail-cleanup and provider-switch behavior runs against
the production numpy storage core without the HTTP hop. The store's own
semantics are pinned in `services/memory_search/tests/test_memory_search_store.py`.

The provider contract (Gemini adapter wire behavior) is pinned separately
in `services/memory_indexer/embeddings/tests/test_embeddings.py`; here the focus is the daemon's
reconcile logic, including the provider-fingerprint gate (a provider
switch re-embeds every row even at the same content hash — same dim is
not the same semantic space).
"""

from __future__ import annotations

import asyncio
import logging
import queue
import subprocess
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from base.config import settings
from base.daemon import health
from base.daemon.health import Liveness
from base.host.net.resilience import MAX_RETRY_AFTER_RESPECT_S, ExponentialBackoff
from services.memory_indexer import daemon
from services.memory_indexer.backends.base import MemorySearchBackend, content_hash
from services.memory_indexer.embeddings import factory, gemini
from services.memory_indexer.embeddings.base import EmbeddingAPIError
from services.memory_indexer.tests.store_backend import StoreBackend

_DIM = 8
_FP = "test:gemini:dim=8"


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
        Liveness(daemon._liveness_timeout_s()),
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
        store_backend, {f.resolve()}, provider, Liveness(daemon._liveness_timeout_s())
    )
    assert provider.embed_batch_count == 1

    daemon._process_paths(
        store_backend, {f.resolve()}, provider, Liveness(daemon._liveness_timeout_s())
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
        store_backend, {f.resolve()}, first, Liveness(daemon._liveness_timeout_s())
    )
    assert first.embed_batch_count == 1

    # Simulate the switch: same content, same mtime, new provider fingerprint —
    # a fresh daemon run would build the backend for the new provider too.
    switched = _FakeProvider(fingerprint="another-provider:dim=8")
    switched_backend = store_backend.reopen("another-provider:dim=8")
    daemon._process_paths(
        switched_backend, {f.resolve()}, switched, Liveness(daemon._liveness_timeout_s())
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
        Liveness(daemon._liveness_timeout_s()),
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
        store_backend, {foreign}, _FakeProvider(), Liveness(daemon._liveness_timeout_s())
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

    daemon._reconcile(backend, _FakeProvider(), Liveness(daemon._liveness_timeout_s()))
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
    daemon._reconcile(switched_backend, switched, Liveness(daemon._liveness_timeout_s()))
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
    liveness = Liveness(daemon._liveness_timeout_s())
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

    assert now > daemon._liveness_timeout_s()
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
    liveness = Liveness(daemon._liveness_timeout_s())
    durations = {"delete_stale_rows": 5.0, "upsert_many": 300.0}

    def spend(op: str) -> None:
        nonlocal now
        now += durations[op]
        assert liveness.is_alive(), f"liveness stale during {op}"
        assert liveness.stale_for() == durations[op], f"previous call compounded with {op}"

    class SlowFinalProvider(_FakeProvider):
        def embed_batch(self, texts: list[str]) -> np.ndarray:
            nonlocal now
            now += factory.worst_case_batch_seconds()
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
    liveness = Liveness(daemon._liveness_timeout_s())

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
    assert now > daemon._liveness_timeout_s()
    assert backend.all_meta() == {}
    assert backend.calls == ["delete"] * len(paths)
    assert provider.embed_batch_count == 0


def test_liveness_timeout_covers_worst_embed_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.services, "embedding_backend", "gemini")

    def assert_coverage() -> float:
        # Recompute each retry gap and the cancellation deadline per attempt.
        policy = gemini._EMBED_POLICY
        worst_batch = policy.max_attempts * settings.services.memory_embed_timeout_seconds
        worst_batch += sum(
            max(policy.backoff(attempt), MAX_RETRY_AFTER_RESPECT_S) + 2 * policy.jitter_span
            for attempt in range(policy.max_attempts - 1)
        )
        provider_budget = factory.worst_case_batch_seconds()
        ceiling = daemon._liveness_timeout_s()
        assert provider_budget >= worst_batch
        assert ceiling >= daemon._LIVENESS_TIMEOUT_FLOOR_S
        assert ceiling >= provider_budget + daemon._LIVENESS_SAFETY_MARGIN_S
        return ceiling

    original = assert_coverage()
    monkeypatch.setattr(
        gemini,
        "_EMBED_POLICY",
        replace(gemini._EMBED_POLICY, backoff=ExponentialBackoff(base=100, factor=2, cap=1000)),
    )
    assert_coverage()  # Later backoffs exceed Retry-After: wrong indices now fail.
    monkeypatch.setattr(
        settings.services,
        "memory_embed_timeout_seconds",
        settings.services.memory_embed_timeout_seconds + 300.0,
    )
    assert assert_coverage() > max(original, daemon._LIVENESS_TIMEOUT_FLOOR_S)


def test_factory_worst_case_registry_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in factory._PROVIDERS:
        monkeypatch.setattr(settings.services, "embedding_backend", name)
        assert factory.worst_case_batch_seconds() > 0, name

    unknown = "unknown-provider"
    monkeypatch.setattr(settings.services, "embedding_backend", unknown)
    with pytest.raises(ValueError, match="unknown embedding provider") as provider_error:
        factory.get_provider_named(unknown)
    with pytest.raises(ValueError) as budget_error:
        factory.worst_case_batch_seconds()
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


def test_reconcile_embed_error_truncates_and_returns_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Error semantics preserved from the pre-chunking reconcile (one
    try/except): an EmbeddingAPIError skips the remaining chunks and is logged
    with its error repr — never raised — so the daemon keeps running and
    the return value signals that a follow-up pass is needed."""
    root = tmp_path / "watched"
    root.mkdir()
    for i in range(6):
        (root / f"note-{i}.md").write_text(f"content {i}")
    monkeypatch.setattr(daemon, "_memory_root", lambda: root)
    monkeypatch.setattr(daemon, "_RECONCILE_CHUNK_PATHS", 2)  # 6 files -> 3 chunks

    calls: list[int] = []

    def failing_process_paths(
        backend: Any, paths: set[Path], provider: Any, liveness: Liveness
    ) -> None:
        calls.append(len(paths))
        raise EmbeddingAPIError("embed boom")

    monkeypatch.setattr(daemon, "_process_paths", failing_process_paths)

    liveness = Liveness(600.0)
    with caplog.at_level(logging.ERROR, logger="services.memory_indexer.daemon"):
        assert daemon._reconcile(store_backend, _FakeProvider(), liveness) is False

    assert calls == [2]  # first chunk failed -> remaining chunks never ran (as before)
    expected = (
        f"reconcile embed failed: {EmbeddingAPIError('embed boom')!r}"
        " — daemon continues; a follow-up pass will retry"
    )
    assert expected in caplog.text
    assert liveness.is_alive()


def test_refresh_gateway_checkout_fast_forwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Safety net: pulls origin/main into the gateway checkout and logs
    loudly when HEAD moved (a post-merge refresh was missed)."""
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

    from base.deploy.git import memory_repo

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
    from base.deploy.git import memory_repo

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
    store_backend: StoreBackend,
) -> None:
    """One file with a description + a long body lands as 1 desc row + N body
    chunks; all_meta still reports the file once."""
    f = tmp_path / "long.md"
    f.write_text(
        "---\ntype: Memory\ndescription: hand off to 402\n---\n\n"
        + "\n\n".join(f"paragraph-{i} " + "word " * 80 for i in range(6)),
        encoding="utf-8",
    )

    daemon._process_paths(
        store_backend,
        {f.resolve()},
        _FakeProvider(),
        Liveness(daemon._liveness_timeout_s()),
    )
    assert str(f.resolve()) in store_backend.all_meta()
    kinds = sorted(store_backend.rows(f))
    assert kinds == [("body", 0), ("body", 1), ("desc", 0)]


def _long_note(paragraphs: int) -> str:
    return "---\ntype: Memory\ndescription: hand off to 402\n---\n\n" + "\n\n".join(
        f"paragraph-{i} " + "word " * 80 for i in range(paragraphs)
    )


def test_process_paths_removes_stale_tail_when_file_shrinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
) -> None:
    """Issue #1946: after a file shrinks, the rows it no longer produces
    (old body tail) are gone — every remaining chunk matches current content."""
    f = tmp_path / "long.md"
    f.write_text(_long_note(12), encoding="utf-8")
    backend = store_backend
    daemon._process_paths(
        backend, {f.resolve()}, _FakeProvider(), Liveness(daemon._liveness_timeout_s())
    )
    assert store_backend.rows(f) == {("desc", 0), ("body", 0), ("body", 1), ("body", 2)}

    f.write_text(_long_note(3), encoding="utf-8")
    daemon._process_paths(
        backend, {f.resolve()}, _FakeProvider(), Liveness(daemon._liveness_timeout_s())
    )
    assert store_backend.rows(f) == {("desc", 0), ("body", 0)}
    assert backend.all_meta()[str(f.resolve())][1] == content_hash(f.read_text())


def test_process_paths_removes_desc_row_when_description_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
) -> None:
    """A removed description removes its row; the body stays current."""
    f = tmp_path / "note.md"
    f.write_text("---\ndescription: old description\n---\n\nbody text", encoding="utf-8")
    backend = store_backend
    daemon._process_paths(
        backend, {f.resolve()}, _FakeProvider(), Liveness(daemon._liveness_timeout_s())
    )
    assert store_backend.rows(f) == {("desc", 0), ("body", 0)}

    f.write_text("body text", encoding="utf-8")
    daemon._process_paths(
        backend, {f.resolve()}, _FakeProvider(), Liveness(daemon._liveness_timeout_s())
    )
    assert store_backend.rows(f) == {("body", 0)}
    assert backend.all_meta()[str(f.resolve())][1] == content_hash(f.read_text())


def test_process_paths_removes_all_rows_when_file_becomes_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
) -> None:
    """An emptied file leaves no rows behind (issue #1946)."""
    f = tmp_path / "note.md"
    f.write_text("---\ndescription: old description\n---\n\nbody text", encoding="utf-8")
    backend = store_backend
    daemon._process_paths(
        backend, {f.resolve()}, _FakeProvider(), Liveness(daemon._liveness_timeout_s())
    )
    assert store_backend.rows(f) == {("desc", 0), ("body", 0)}

    f.write_text("", encoding="utf-8")
    daemon._process_paths(
        backend, {f.resolve()}, _FakeProvider(), Liveness(daemon._liveness_timeout_s())
    )
    assert store_backend.rows(f) == set()
    assert backend.all_meta() == {}


def test_process_paths_calls_upsert_many_once_across_embed_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RecordingBackend(StoreBackend):
        def __init__(self) -> None:
            super().__init__(tmp_path / "recording" / "vectors.npz", dim=_DIM, fingerprint=_FP)
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

    daemon._process_paths(
        backend,
        {first.resolve(), second.resolve()},
        _FakeProvider(),
        Liveness(daemon._liveness_timeout_s()),
    )

    assert len(backend.calls) == 1
    assert len(backend.calls[0]) == 3
    assert {row[0] for row in backend.calls[0]} == {str(first.resolve()), str(second.resolve())}


def test_partial_embedding_failure_keeps_old_rows_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file whose embedding fails part way keeps its previous rows whole —
    all-old is the recoverable consistent state; the hash stays mismatching so
    the next fs event or follow-up reconcile retries it (issue #1946)."""

    class _FailSecondBatchProvider(_FakeProvider):
        def embed_batch(self, texts: list[str]) -> np.ndarray:
            if self.embed_batch_count == 1:
                raise EmbeddingAPIError("second batch failed")
            return super().embed_batch(texts)

    class _RecordingBackend(StoreBackend):
        def __init__(self) -> None:
            super().__init__(tmp_path / "recording" / "vectors.npz", dim=_DIM, fingerprint=_FP)
            self.calls: list[list[tuple[str, float, str, np.ndarray, str, int]]] = []

        def upsert_many(self, rows: Sequence[tuple[str, float, str, np.ndarray, str, int]]) -> None:
            self.calls.append(list(rows))
            super().upsert_many(rows)

    note = tmp_path / "note.md"
    note.write_text("---\ndescription: description\n---\nbody", encoding="utf-8")
    monkeypatch.setattr(daemon, "_BATCH_SIZE", 1)
    backend = _RecordingBackend()
    daemon._process_paths(
        backend, {note.resolve()}, _FakeProvider(), Liveness(daemon._liveness_timeout_s())
    )
    old_meta = backend.all_meta()

    note.write_text("---\ndescription: new description\n---\nnew body", encoding="utf-8")
    backend.calls.clear()
    with pytest.raises(EmbeddingAPIError, match="second batch failed"):
        daemon._process_paths(
            backend,
            {note.resolve()},
            _FailSecondBatchProvider(),
            Liveness(daemon._liveness_timeout_s()),
        )

    # Nothing was written for the partially-embedded file; the old rows stand.
    assert backend.calls == []
    assert backend.all_meta() == old_meta


def test_complete_file_still_commits_when_another_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
) -> None:
    """Files whose rows are ALL embedded commit even when a sibling file
    fails mid-embedding (issue #1946)."""

    class _FailOnSecondFileProvider(_FakeProvider):
        def embed_batch(self, texts: list[str]) -> np.ndarray:
            if self.embed_batch_count == 2:
                raise EmbeddingAPIError("third batch failed")
            return super().embed_batch(texts)

    monkeypatch.setattr(daemon, "_BATCH_SIZE", 1)
    backend = store_backend
    complete = tmp_path / "complete.md"
    complete.write_text("complete body", encoding="utf-8")
    partial = tmp_path / "partial.md"
    partial.write_text("---\ndescription: p\n---\nbody", encoding="utf-8")

    with pytest.raises(EmbeddingAPIError, match="third batch failed"):
        daemon._process_paths(
            backend,
            {complete.resolve(), partial.resolve()},
            _FailOnSecondFileProvider(),
            Liveness(daemon._liveness_timeout_s()),
        )

    meta = backend.all_meta()
    assert str(complete.resolve()) in meta
    assert str(partial.resolve()) not in meta


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


def test_reconcile_retry_schedule_backoff_and_reset() -> None:
    clock = SimpleNamespace(now=100.0)
    retry = daemon._ReconcileRetrySchedule(base_s=2.0, cap_s=9.0, clock=lambda: clock.now)
    _assert_retry_idle(retry)
    # Each incomplete signal advances the ladder, including an idle drain failure.
    for delay in [2.0, 4.0, 8.0, 9.0, 9.0]:
        _assert_one_ladder_rung(retry, clock, delay)
    # A quota outage may last indefinitely; the capped backoff must not overflow.
    for _ in range(1100):
        assert retry.record_incomplete() == 9.0
    retry.record_pass_complete()
    _assert_retry_idle(retry)
    assert retry.ensure_scheduled() == 2.0  # an idle drain failure takes the base rung
    assert retry.record_incomplete() == 4.0  # its incomplete follow-up takes the next


def test_reconcile_health_payload() -> None:
    now = 0.0
    retry = daemon._ReconcileRetrySchedule(base_s=2.0, cap_s=4.0, clock=lambda: now)
    idle = {"reconcile_pending": False, "reconcile_retry_in_s": None}
    assert daemon._reconcile_health(retry) == idle
    retry.ensure_scheduled()
    assert daemon._reconcile_health(retry) == {
        "reconcile_pending": True,
        "reconcile_retry_in_s": 2.0,
    }
    now = 0.76
    assert daemon._reconcile_health(retry)["reconcile_retry_in_s"] == 1.2
    retry.record_pass_complete()
    assert daemon._reconcile_health(retry) == idle


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


async def test_reconcile_retry_drain_loop_converges_after_embed_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reconcile = Mock(wraps=daemon._reconcile)
    monkeypatch.setattr(daemon, "_reconcile", reconcile)
    # More than one reconcile chunk: recovery must fill the entire original gap.
    files = {tmp_path / f"note-{i:03d}.md" for i in range(70)}
    for note in files:
        note.write_text(f"content of {note.name}")
    monkeypatch.setattr(daemon, "_LOOP_INTERVAL_S", 0.01)
    backend = store_backend
    provider = _FlakyProvider(failures=3)
    liveness = Liveness(1.0)
    retry = daemon._ReconcileRetrySchedule(base_s=0.05, cap_s=0.1)
    assert await asyncio.to_thread(daemon._reconcile, backend, provider, liveness) is False
    retry.record_incomplete()
    with caplog.at_level(logging.INFO, logger="services.memory_indexer.daemon"):
        task = asyncio.create_task(
            daemon._drain_loop(backend, queue.Queue(), liveness, provider, retry)
        )
        try:
            async with asyncio.timeout(5.0):
                while retry.pending:
                    assert not task.done()
                    assert liveness.is_alive()
                    await asyncio.sleep(0.01)
            await _assert_gap_closed(backend, files, retry, liveness, reconcile, caplog)
        finally:
            await _cancel_quietly(task)


async def test_reconcile_retry_drain_failure_arms_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    note = tmp_path / "changed.md"
    note.write_text("new content")
    monkeypatch.setattr(daemon, "_LOOP_INTERVAL_S", 0.01)
    backend = store_backend
    provider = _FlakyProvider(failures=1)
    dirty: queue.Queue[Path] = queue.Queue()
    dirty.put(note)
    liveness = Liveness(1.0)
    retry = daemon._ReconcileRetrySchedule(base_s=0.05, cap_s=0.1)
    task = asyncio.create_task(daemon._drain_loop(backend, dirty, liveness, provider, retry))
    try:
        async with asyncio.timeout(5.0):
            while not retry.pending:
                assert not task.done()
                await asyncio.sleep(0.005)
            assert dirty.empty()
            assert "embed failed for a batch of 1 path(s)" in caplog.text
            assert "follow-up reconcile scheduled" in caplog.text
            while retry.pending:
                assert not task.done()
                assert liveness.is_alive()
                await asyncio.sleep(0.01)
        assert liveness.is_alive()
        assert set(await asyncio.to_thread(backend.all_meta)) == {str(note)}
        assert provider.attempts == 2
        assert not retry.pending
    finally:
        await _cancel_quietly(task)


async def test_reconcile_retry_beats_between_failed_pass_and_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = {"reconcile": 0, "batch": 0}

    def slow_reconcile(*args: Any) -> bool:
        time.sleep(0.4)
        completed["reconcile"] += 1
        return False

    def slow_batch(*args: Any) -> None:
        time.sleep(0.4)
        completed["batch"] += 1
        raise EmbeddingAPIError("429 Too Many Requests")

    monkeypatch.setattr(daemon, "_reconcile", slow_reconcile)
    monkeypatch.setattr(daemon, "_process_paths", slow_batch)
    monkeypatch.setattr(daemon, "_LOOP_INTERVAL_S", 0.01)
    dirty: queue.Queue[Path] = queue.Queue()
    dirty.put(tmp_path / "note.md")
    retry = daemon._ReconcileRetrySchedule(base_s=0.01, cap_s=0.1)
    retry.record_incomplete()
    # Either operation fits below the ceiling, but their combined tails do not.
    liveness = Liveness(0.6)
    task = asyncio.create_task(daemon._drain_loop(Mock(), dirty, liveness, _FakeProvider(), retry))
    started = time.monotonic()
    try:
        while time.monotonic() - started < 1.3:
            await asyncio.sleep(0.01)
            assert not task.done()
            assert liveness.is_alive(), completed
        assert completed["reconcile"] >= 1
        assert completed["batch"] == 1
    finally:
        await _cancel_quietly(task)


async def test_run_unknown_provider_fails_before_health_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    start_health_server = AsyncMock()
    monkeypatch.setattr(daemon, "_is_running", lambda: False)
    monkeypatch.setattr(daemon, "_write_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "start_health_server", start_health_server)
    monkeypatch.setattr(settings.services, "embedding_backend", "unknown-provider")

    with pytest.raises(SystemExit) as exc:
        await daemon.run()

    assert exc.value.code == 1
    assert "FATAL: unknown embedding provider 'unknown-provider'" in capsys.readouterr().err
    start_health_server.assert_not_awaited()


async def test_run_arms_retry_when_startup_reconcile_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from services.memory_indexer.backends.probe import ProbeResult

    backend = Mock()
    backend.name = "fake"
    provider = _FakeProvider()
    reconcile = Mock(return_value=False)
    drain = AsyncMock()
    monkeypatch.setattr(daemon, "_is_running", lambda: False)
    monkeypatch.setattr(daemon, "_write_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "_remove_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "start_health_server", AsyncMock())
    monkeypatch.setattr(daemon, "stop_health_server", AsyncMock())
    monkeypatch.setattr(daemon, "_endpoint", lambda: Mock(health_port=0))
    monkeypatch.setattr(daemon, "get_provider", lambda: provider)
    monkeypatch.setattr(daemon, "probe_backend", Mock(return_value=ProbeResult(message=None)))
    monkeypatch.setattr(daemon, "_connect_backend_with_retry", AsyncMock(return_value=backend))
    monkeypatch.setattr(daemon, "Observer", Mock())
    monkeypatch.setattr(daemon, "_reconcile", reconcile)
    monkeypatch.setattr(daemon, "_drain_loop", drain)
    monkeypatch.setattr(
        daemon.settings.services, "memory_indexer_reconcile_retry_backoff_seconds", 7.0
    )

    with caplog.at_level(logging.WARNING, logger="services.memory_indexer.daemon"):
        await daemon.run()

    reconcile.assert_called_once()
    assert reconcile.call_args.args[:2] == (backend, provider)
    drain.assert_awaited_once()
    assert drain.await_args is not None
    retry = drain.await_args.kwargs["retry"]
    assert isinstance(retry, daemon._ReconcileRetrySchedule)
    assert retry.pending
    retry_in = retry.retry_in_s()
    assert retry_in is not None and 0 < retry_in <= 7.0
    assert "startup reconcile incomplete; retry scheduled in 7.0s" in caplog.text


async def test_reconcile_retry_wait_keeps_liveness_and_cancels_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon, "_LOOP_INTERVAL_S", 0.01)
    # Wait far longer than the heartbeat ceiling while keeping the test bounded.
    retry = daemon._ReconcileRetrySchedule(base_s=2.0, cap_s=2.0)
    retry.record_incomplete()
    liveness = Liveness(0.15)
    backend = Mock()
    task = asyncio.create_task(
        daemon._drain_loop(backend, queue.Queue(), liveness, _FakeProvider(), retry)
    )
    started = time.monotonic()
    try:
        while time.monotonic() - started < 0.4:
            await asyncio.sleep(0.01)
            assert liveness.is_alive()
            assert retry.pending
            assert not task.done()
        backend.all_meta.assert_not_called()  # still waiting, no pass ran yet
    finally:
        cancelled_at = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)
        assert time.monotonic() - cancelled_at < 0.5


async def test_reconcile_retry_inflight_pass_cancels_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Event

    entered, release, finished = Event(), Event(), Event()

    def slow_reconcile(*args: Any) -> bool:
        entered.set()
        try:
            assert release.wait(timeout=2.0)
            return True
        finally:
            finished.set()

    monkeypatch.setattr(daemon, "_reconcile", slow_reconcile)
    monkeypatch.setattr(daemon, "_LOOP_INTERVAL_S", 0.01)
    retry = daemon._ReconcileRetrySchedule(base_s=0.01, cap_s=0.01)
    retry.record_incomplete()
    task = asyncio.create_task(
        daemon._drain_loop(Mock(), queue.Queue(), Liveness(1.0), _FakeProvider(), retry)
    )
    try:
        async with asyncio.timeout(1.0):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        assert not finished.is_set()  # the event loop is free while the worker waits
        cancelled_at = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)
        assert time.monotonic() - cancelled_at < 0.5
        assert not finished.is_set()  # cancellation does not stop the executor thread
    finally:
        release.set()
        await _cancel_quietly(task)
        assert await asyncio.to_thread(finished.wait, 1.0)
