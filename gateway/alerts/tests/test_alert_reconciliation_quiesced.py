"""The stop window: the Grafana reconciliation loop skips its pass while the unit is
quiesced, so it borrows no connection from a pool the stop is about to close."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from base.deploy.maintenance import admission
from gateway.alerts import reconciliation


@pytest.mark.parametrize("quiesced", [True, False])
async def test_a_quiesced_unit_runs_no_reconcile_pass(
    monkeypatch: pytest.MonkeyPatch, quiesced: bool
) -> None:
    monkeypatch.setattr(admission, "quiesced", lambda: quiesced)
    monkeypatch.setattr(reconciliation, "_RECONCILE_INTERVAL_S", 0.01)
    passes: list[object] = []

    async def record_pass(*args: Any) -> int:
        passes.append(args)
        return 0

    monkeypatch.setattr(reconciliation, "_reconcile_once", record_pass)
    stop = asyncio.Event()
    task = asyncio.create_task(
        reconciliation.reconciliation_loop(MagicMock(), MagicMock(), MagicMock(), stop)
    )
    try:
        await asyncio.sleep(0.2)
    finally:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)
    assert bool(passes) is (not quiesced)
