"""The stop window: the heartbeat's check-in, liveness and completion-digest loops skip their
passes while the unit is quiesced, so none borrows a connection from a pool the stop is about
to close."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from psycopg_pool import ConnectionPool

from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.deploy.maintenance import admission
from base.events.live.bus import EventBus
from services.heartbeat import completion_digest, daemon


async def _run_briefly(
    monkeypatch: pytest.MonkeyPatch,
    loop: Callable[[ConnectionPool, LoopProgress], Coroutine[Any, Any, None]],
    pool: object,
    *,
    quiesced: bool,
) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)

    async def short_sleep(_progress: LoopProgress, _total_s: float) -> None:
        await asyncio.sleep(0.01)

    monkeypatch.setattr(daemon, "_sleep_with_liveness", short_sleep)
    task = asyncio.create_task(loop(cast("ConnectionPool", pool), LoopProgress("loop", 60.0)))
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("quiesced", [True, False])
async def test_a_quiesced_unit_sends_no_check_in_pass(
    monkeypatch: pytest.MonkeyPatch, quiesced: bool
) -> None:
    pool = MagicMock()
    await _run_briefly(monkeypatch, daemon._dispatch_loop, pool, quiesced=quiesced)
    assert bool(pool.mock_calls) is (not quiesced)


@pytest.mark.parametrize("quiesced", [True, False])
async def test_a_quiesced_unit_runs_no_liveness_pass(
    monkeypatch: pytest.MonkeyPatch, quiesced: bool
) -> None:
    passes: list[object] = []

    async def record_pass(_db: object, pool: object, _bus: object) -> None:
        passes.append(pool)

    monkeypatch.setattr(daemon, "run_liveness_pass", record_pass)
    bus = cast("EventBus", MagicMock())
    await _run_briefly(
        monkeypatch,
        lambda pool, liveness: daemon._liveness_loop(Database.from_settings(), pool, bus, liveness),
        MagicMock(),
        quiesced=quiesced,
    )
    assert bool(passes) is (not quiesced)


@pytest.mark.parametrize("quiesced", [True, False])
async def test_a_quiesced_unit_flushes_no_completion_digest(
    monkeypatch: pytest.MonkeyPatch, quiesced: bool
) -> None:
    flushes: list[object] = []

    async def record_flush(pool: object, _db: object, _bus: object) -> int:
        flushes.append(pool)
        return 0

    monkeypatch.setattr(completion_digest, "flush_once", record_flush)
    monkeypatch.setattr(completion_digest, "FLUSH_INTERVAL_S", 0.01)
    bus = cast(EventBus, MagicMock())
    await _run_briefly(
        monkeypatch,
        lambda pool, progress: completion_digest.completion_digest_loop(
            pool, Database.from_settings(), bus, progress
        ),
        MagicMock(),
        quiesced=quiesced,
    )
    assert bool(flushes) is (not quiesced)
