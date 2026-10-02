"""The stop window: the indexer's drain loop leaves the dirty queue alone while the unit is
quiesced — the pgvector backend borrows the pool for every batch."""

from __future__ import annotations

import asyncio
import queue
from pathlib import Path
from unittest.mock import Mock

import pytest

from base.daemon.health import Liveness
from base.deploy.maintenance import admission
from services.memory_indexer import daemon


@pytest.mark.parametrize("quiesced", [True, False])
async def test_a_quiesced_unit_processes_no_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, quiesced: bool
) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)
    monkeypatch.setattr(daemon, "_LOOP_INTERVAL_S", 0.01)
    batches: list[set[Path]] = []
    monkeypatch.setattr(daemon, "_process_paths", lambda _b, batch, _p, _l: batches.append(batch))
    dirty: queue.Queue[Path] = queue.Queue()
    dirty.put(tmp_path / "note.md")
    retry = daemon._ReconcileRetrySchedule(base_s=1.0, cap_s=2.0)
    task = asyncio.create_task(daemon._drain_loop(Mock(), dirty, Liveness(60.0), Mock(), retry))
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert bool(batches) is (not quiesced)
    assert dirty.empty() is (not quiesced), "a quiesced unit leaves dirty paths for the resume"
