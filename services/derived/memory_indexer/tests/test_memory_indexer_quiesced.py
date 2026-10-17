from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.daemon.health import Liveness
from base.deploy.maintenance import admission
from services.derived.memory_indexer import daemon
from services.derived.memory_indexer.backends.base import ChunkHit
from services.derived.memory_indexer.config import MemoryIndexerConfig
from services.derived.memory_indexer.patrol_status import PatrolStatus


class _EmptyBackend:
    name = "empty"

    def connect(self) -> None:
        raise AssertionError("patrol must use the already connected backend")

    def close(self) -> None:
        raise AssertionError("patrol must leave backend lifecycle to its caller")

    def upsert(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("the pool is empty")

    def upsert_many(self, rows: object) -> None:
        raise AssertionError("the pool is empty")

    def delete(self, path: str) -> None:
        raise AssertionError("the index is empty")

    def delete_stale_rows(self, entries: object) -> None:
        raise AssertionError("the index is empty")

    def all_meta(self) -> dict[str, tuple[float, str, str]]:
        return {}

    def search_topk(self, query_vector: np.ndarray, k: int) -> list[ChunkHit]:
        raise AssertionError("indexing must not search")

    async def search_topk_async(
        self, query_vector: np.ndarray, k: int, *, timeout: float
    ) -> list[ChunkHit]:
        raise AssertionError("indexing must not search")


@pytest.mark.parametrize("quiesced", [True, False])
async def test_a_quiesced_unit_runs_no_patrol_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    quiesced: bool,
    *,
    machine_identity: MachineIdentity,
) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)
    indexer_config = MemoryIndexerConfig(loop_interval_s=0.01, patrol_interval_s=1.0)
    status = PatrolStatus(retry_limit=indexer_config.retry_limit)
    task = asyncio.create_task(
        daemon.patrol_loop(
            _EmptyBackend(),
            Mock(fingerprint="test"),
            Liveness(60.0),
            machine=machine_identity,
            config=indexer_config,
            status=status,
            clock=lambda: 0.0,
            wait=asyncio.sleep,
            root=tmp_path,
        )
    )
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert (status.lag()["last_success_at"] is not None) is (not quiesced)
