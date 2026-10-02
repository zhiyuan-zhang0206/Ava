"""The stop window: the page server's reconcile loop skips its pass while the unit is
quiesced, so it borrows no connection from a pool the stop is about to close."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import MagicMock

import pytest
from psycopg_pool import ConnectionPool

from base.daemon.health import Liveness
from base.deploy.maintenance import admission
from services.page_server import daemon


@pytest.mark.parametrize("quiesced", [True, False])
async def test_a_quiesced_unit_runs_no_reconcile_pass(
    monkeypatch: pytest.MonkeyPatch, quiesced: bool
) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)
    monkeypatch.setattr(daemon, "reachable_host", lambda: "127.0.0.1")
    monkeypatch.setattr(daemon, "machine_name", lambda: "test-machine")
    passes: list[object] = []

    def record_pass(*args: object) -> None:
        passes.append(args)

    monkeypatch.setattr(daemon, "_reconcile_once", record_pass)
    real_sleep = asyncio.sleep

    async def short_sleep(_seconds: float) -> None:
        await real_sleep(0.01)

    monkeypatch.setattr(daemon.asyncio, "sleep", short_sleep)
    pool = MagicMock()
    task = asyncio.create_task(daemon._reconcile_loop(cast("ConnectionPool", pool), Liveness(60.0)))
    try:
        await real_sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert bool(passes) is (not quiesced)
