"""The stop window: the indexer's drain loop leaves the dirty queue alone while the unit is
quiesced — the pgvector backend borrows the pool for every batch."""

from __future__ import annotations

import asyncio
import queue
from pathlib import Path
from unittest.mock import Mock

import pytest

from base.cluster.machine import MachineIdentity
from base.daemon.health import Liveness
from base.deploy.maintenance import admission
from services.derived.memory_indexer import daemon
from tests.fixtures.unit.identity import machine_identity as machine_identity


@pytest.mark.parametrize("quiesced", [True, False])
async def test_a_quiesced_unit_processes_no_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    quiesced: bool,
    *,
    machine_identity: MachineIdentity,
) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)
    monkeypatch.setattr(daemon, "_LOOP_INTERVAL_S", 0.01)
    batches: list[set[Path]] = []

    def record_batch(
        _backend: object,
        batch: set[Path],
        _provider: object,
        _liveness: object,
        *,
        machine: MachineIdentity,
    ) -> None:
        batches.append(batch)

    monkeypatch.setattr(daemon, "_process_paths", record_batch)
    dirty: queue.Queue[Path] = queue.Queue()
    dirty.put(tmp_path / "note.md")
    task = asyncio.create_task(
        daemon._drain_loop(Mock(), dirty, Liveness(60.0), Mock(), machine=machine_identity)
    )
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert bool(batches) is (not quiesced)
    assert dirty.empty() is (not quiesced), "a quiesced unit leaves dirty paths for the resume"
