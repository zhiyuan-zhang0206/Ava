from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.daemon.health import Liveness
from services.derived.memory_indexer import daemon
from services.derived.memory_indexer.config import MemoryIndexerConfig
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError
from services.derived.memory_indexer.patrol_status import PatrolStatus, retry_delay_s
from services.derived.memory_indexer.tests.fake_provider import DIM, FakeProvider
from services.derived.memory_search.tests.store_backend import StoreBackend
from services.derived.memory_search.tests.test_memory_indexer import (
    liveness_timeout,
)
from services.derived.memory_search.tests.test_memory_indexer import (
    memory_watched_root as memory_watched_root,
)
from services.derived.memory_search.tests.test_memory_indexer import (
    store_backend as store_backend,
)


class _Clock:
    def __init__(self, *, stop_at: float, on_tick: Callable[[float], None] | None = None) -> None:
        self.now = 0.0
        self._stop_at = stop_at
        self._on_tick = on_tick

    def monotonic(self) -> float:
        return self.now

    async def wait(self, delay: float) -> None:
        self.now += delay
        if self._on_tick is not None:
            self._on_tick(self.now)
        if self.now > self._stop_at:
            raise asyncio.CancelledError


class _RecordingStatus(PatrolStatus):
    def __init__(self, *, retry_limit: int, clock: _Clock) -> None:
        super().__init__(retry_limit=retry_limit)
        self.passes: list[tuple[float, bool]] = []
        self._clock = clock

    def failed(self) -> None:
        super().failed()
        self.passes.append((self._clock.now, False))

    def succeeded(self) -> None:
        super().succeeded()
        self.passes.append((self._clock.now, True))


async def _run_patrol(
    backend: StoreBackend,
    provider: FakeProvider,
    *,
    config: MemoryIndexerConfig,
    status: _RecordingStatus,
    clock: _Clock,
    machine: MachineIdentity,
) -> None:
    with pytest.raises(asyncio.CancelledError):
        await daemon.patrol_loop(
            backend,
            provider,
            Liveness(liveness_timeout()),
            machine=machine,
            config=config,
            status=status,
            clock=clock.monotonic,
            wait=clock.wait,
        )


class _DownProvider(FakeProvider):
    def embed_batch(self, texts: list[str]) -> np.ndarray:
        raise EmbeddingAPIError("provider down")


class _FailsOnceProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        if self.failures:
            self.failures -= 1
            raise EmbeddingAPIError("provider hiccup")
        return super().embed_batch(texts)


async def test_a_changed_note_waits_for_the_next_patrol_without_a_file_event(
    tmp_path: Path,
    store_backend: StoreBackend,
    *,
    machine_identity: MachineIdentity,
) -> None:
    config = MemoryIndexerConfig(loop_interval_s=1.0, patrol_interval_s=100.0)
    note = tmp_path / "note.md"

    def on_tick(now: float) -> None:
        if now == 2.0:
            note.write_text("a note written between patrols")
        if now == 50.0:
            assert store_backend.all_meta() == {}, "no pass has run since the note arrived"

    clock = _Clock(stop_at=102.0, on_tick=on_tick)
    status = _RecordingStatus(retry_limit=config.retry_limit, clock=clock)
    await _run_patrol(
        store_backend,
        FakeProvider(),
        config=config,
        status=status,
        clock=clock,
        machine=machine_identity,
    )

    assert status.passes == [(1.0, True), (101.0, True)]
    assert list(store_backend.all_meta()) == [str(note.resolve())]
    assert status.pending == 0


async def test_a_failed_pass_retries_with_backoff_then_marks_over_limit(
    tmp_path: Path,
    store_backend: StoreBackend,
    *,
    machine_identity: MachineIdentity,
) -> None:
    (tmp_path / "note.md").write_text("note")
    config = MemoryIndexerConfig(
        loop_interval_s=1.0, patrol_interval_s=900.0, retry_backoff_s=60.0, retry_limit=3
    )
    clock = _Clock(stop_at=1330.0)
    status = _RecordingStatus(retry_limit=config.retry_limit, clock=clock)
    await _run_patrol(
        store_backend,
        _DownProvider(),
        config=config,
        status=status,
        clock=clock,
        machine=machine_identity,
    )

    assert status.passes == [
        (1.0, False),
        (61.0, False),
        (181.0, False),
        (421.0, False),
        (1321.0, False),
    ]
    index = status.lag()
    assert index["pending_paths"] == 1
    assert index["consecutive_failures"] == 5
    assert index["over_limit"] is True
    assert index["last_success_at"] is None


async def test_health_lag_clears_once_a_retry_succeeds(
    tmp_path: Path,
    store_backend: StoreBackend,
    *,
    machine_identity: MachineIdentity,
) -> None:
    note = tmp_path / "note.md"
    note.write_text("note")
    config = MemoryIndexerConfig(
        loop_interval_s=1.0, patrol_interval_s=100.0, retry_backoff_s=60.0, retry_limit=3
    )
    clock = _Clock(stop_at=70.0)
    status = _RecordingStatus(retry_limit=config.retry_limit, clock=clock)
    await _run_patrol(
        store_backend,
        _FailsOnceProvider(),
        config=config,
        status=status,
        clock=clock,
        machine=machine_identity,
    )

    assert status.passes == [(1.0, False), (61.0, True)]
    index = status.lag()
    assert index["pending_paths"] == 0
    assert index["consecutive_failures"] == 0
    assert index["over_limit"] is False
    assert index["last_success_at"] is not None
    assert list(store_backend.all_meta()) == [str(note.resolve())]


def test_retry_delay_doubles_and_never_exceeds_the_patrol_interval() -> None:
    config = MemoryIndexerConfig(retry_backoff_s=60.0, patrol_interval_s=900.0, retry_limit=5)
    assert [retry_delay_s(config, failures) for failures in (1, 2, 3, 4)] == [
        60.0,
        120.0,
        240.0,
        480.0,
    ]
    capped = MemoryIndexerConfig(retry_backoff_s=60.0, patrol_interval_s=200.0, retry_limit=5)
    assert [retry_delay_s(capped, failures) for failures in (1, 2, 3)] == [
        60.0,
        120.0,
        200.0,
    ]


def test_over_limit_only_marks_and_keeps_the_lag_pending() -> None:
    status = PatrolStatus(retry_limit=2)
    status.begin(3)
    for _ in range(2):
        status.failed()
        assert status.over_limit is False
    status.failed()
    assert status.over_limit is True
    assert status.pending == 3
    status.succeeded()
    assert (status.over_limit, status.consecutive_failures, status.pending) == (False, 0, 0)


def test_patrol_compares_content_not_mtime(
    tmp_path: Path,
    store_backend: StoreBackend,
    *,
    machine_identity: MachineIdentity,
) -> None:
    note = tmp_path / "note.md"
    note.write_text("original content")
    original_mtime = note.stat().st_mtime
    daemon.process_paths(
        store_backend,
        {note.resolve()},
        FakeProvider(),
        Liveness(liveness_timeout()),
        machine=machine_identity,
        config=MemoryIndexerConfig(),
    )

    config = MemoryIndexerConfig()

    def stale() -> set[Path]:
        return daemon.dirty_paths(
            daemon.scan_disk(tmp_path, config=config), store_backend.all_meta(), FakeProvider()
        )

    os.utime(note, (original_mtime + 3600, original_mtime + 3600))
    assert stale() == set(), "an mtime-only change must not re-embed"

    note.write_text("changed content")
    os.utime(note, (original_mtime, original_mtime))
    assert stale() == {note.resolve()}, "a content change with the old mtime must re-embed"


def test_navigation_files_stay_out_of_the_index(
    tmp_path: Path,
    store_backend: StoreBackend,
    *,
    machine_identity: MachineIdentity,
) -> None:
    (tmp_path / "MEMORY.md").write_text("- [note](note.md) — description")
    sub = tmp_path / "sub"
    sub.mkdir()
    index_file = sub / "index.md"
    index_file.write_text("- [note](../note.md) — description")
    note = tmp_path / "note.md"
    note.write_text("note body")

    config = MemoryIndexerConfig()
    assert set(daemon.scan_disk(tmp_path, config=config)) == {note.resolve()}
    store_backend.upsert(
        str(index_file.resolve()),
        1.0,
        "stale",
        np.zeros(DIM, dtype=np.float32),
        kind="body",
        chunk_idx=0,
    )
    dirty = daemon.dirty_paths(
        daemon.scan_disk(tmp_path, config=config), store_backend.all_meta(), FakeProvider()
    )
    assert index_file.resolve() in dirty, "an existing row for a navigation file is pruned"
    assert (tmp_path / "MEMORY.md").resolve() not in dirty, "the scan never offers one"
    daemon.process_paths(
        store_backend,
        {index_file.resolve()},
        FakeProvider(),
        Liveness(liveness_timeout()),
        machine=machine_identity,
        config=MemoryIndexerConfig(),
    )
    assert store_backend.all_meta() == {}
