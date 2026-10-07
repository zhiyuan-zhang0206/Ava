"""Memory indexer cases: reconcile retry inflight pass cancels promptly."""

from __future__ import annotations

import asyncio
import queue
import time
from typing import Any
from unittest.mock import Mock

import pytest

from base.daemon.health import Liveness
from services.derived.memory_indexer import daemon
from services.derived.memory_indexer.tests.test_memory_indexer import _cancel_quietly, _FakeProvider
from services.derived.memory_indexer.tests.test_memory_indexer import (
    _watched_root as _watched_root,
)
from services.derived.memory_indexer.tests.test_memory_indexer import (
    store_backend as store_backend,
)


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
