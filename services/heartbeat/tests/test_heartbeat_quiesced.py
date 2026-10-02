"""The stop window: the heartbeat's check-in and liveness loops skip their passes while the
unit is quiesced, so neither borrows a connection from a pool the stop is about to close."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast
from unittest.mock import MagicMock

import pytest
from psycopg_pool import ConnectionPool

from base.daemon.health import Liveness
from base.deploy.maintenance import admission
from services.heartbeat import daemon


async def _run_briefly(
    monkeypatch: pytest.MonkeyPatch,
    loop: Callable[[ConnectionPool, Liveness], Awaitable[None]],
    pool: object,
    *,
    quiesced: bool,
) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)

    async def short_sleep(_liveness: Liveness, _total_s: float) -> None:
        await asyncio.sleep(0.01)

    monkeypatch.setattr(daemon, "_sleep_with_liveness", short_sleep)
    task = asyncio.create_task(loop(cast("ConnectionPool", pool), Liveness(60.0)))
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

    async def record_pass(pool: object) -> None:
        passes.append(pool)

    monkeypatch.setattr(daemon, "run_liveness_pass", record_pass)
    await _run_briefly(monkeypatch, daemon._liveness_loop, MagicMock(), quiesced=quiesced)
    assert bool(passes) is (not quiesced)
