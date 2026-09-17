"""services/agent_ops/outbox_flusher.py — the resident deferred-delivery loop.

The loop must make one immediate pass (so a daemon restart right after a
recovery backfills at once), stay off a quiesced unit (the stop window must
not borrow the pool it just released), and refuse to start on a broken initial
config read instead of guessing a cadence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from services.agent_ops import outbox_flusher
from shared import delivery_outbox as outbox
from shared import maintenance


def _limits(interval: float) -> outbox.DeliveryOutboxLimits:
    return outbox.DeliveryOutboxLimits(
        enabled=True,
        retry_backoff_steps=(30.0, 60.0, 300.0, 900.0),
        budget_seconds=43200.0,
        dedup_window_seconds=900.0,
        flush_interval_seconds=interval,
        max_entries=128,
    )


async def _poll(predicate: Callable[[], bool], *, attempts: int = 200) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def test_start_refuses_when_initial_config_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox_flusher._task = None

    def _boom() -> outbox.DeliveryOutboxLimits:
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(outbox, "limits", _boom)
    outbox_flusher.start(object())  # type: ignore[arg-type]
    assert outbox_flusher._task is None


async def test_run_performs_an_immediate_pass_then_paces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def _fake_flush(pool: Any) -> outbox.FlushReport:
        calls.append(pool)
        return outbox.FlushReport(delivered=1)

    monkeypatch.setattr(outbox, "flush", _fake_flush)
    monkeypatch.setattr(outbox, "limits", lambda: _limits(3600.0))
    monkeypatch.setattr(maintenance, "quiesced", lambda: False)

    task = asyncio.create_task(outbox_flusher._run(object(), 3600.0))  # type: ignore[arg-type]
    try:
        await _poll(lambda: bool(calls))
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(calls) == 1  # the immediate pass; the 1h wait never elapses


async def test_run_defers_while_quiesced(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    state = {"quiesced": True}

    def _fake_flush(pool: Any) -> outbox.FlushReport:
        calls.append(pool)
        return outbox.FlushReport()

    monkeypatch.setattr(outbox, "flush", _fake_flush)
    monkeypatch.setattr(outbox, "limits", lambda: _limits(0.01))
    monkeypatch.setattr(maintenance, "quiesced", lambda: state["quiesced"])

    task = asyncio.create_task(outbox_flusher._run(object(), 0.01))  # type: ignore[arg-type]
    try:
        await asyncio.sleep(0.05)
        assert calls == []  # quiesced: records (and the pool) stay untouched
        state["quiesced"] = False
        await _poll(lambda: bool(calls))
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
