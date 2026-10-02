"""The stop window: the gateway's database-backed flush loops skip their passes while the
unit is quiesced, so neither borrows a connection from a pool the stop is about to close."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast
from unittest.mock import MagicMock

import pytest
from psycopg_pool import ConnectionPool

from base.deploy.maintenance import admission
from gateway.agents import completion_notice_flusher, max_id_gauge

_Loop = Callable[[ConnectionPool], Awaitable[None]]


@pytest.mark.parametrize("quiesced", [True, False])
@pytest.mark.parametrize(
    ("module", "loop", "interval"),
    [
        (max_id_gauge, max_id_gauge.max_agent_id_flusher, "FLUSH_INTERVAL_S"),
        (
            completion_notice_flusher,
            completion_notice_flusher.completion_notice_flusher,
            "FLUSH_INTERVAL_S",
        ),
    ],
    ids=["max-id-gauge", "completion-digest"],
)
async def test_a_quiesced_unit_borrows_no_connection(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    loop: _Loop,
    interval: str,
    quiesced: bool,
) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)
    monkeypatch.setattr(module, interval, 0.01)
    pool = MagicMock()
    task = asyncio.create_task(loop(cast("ConnectionPool", pool)))
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert bool(pool.mock_calls) is (not quiesced)
