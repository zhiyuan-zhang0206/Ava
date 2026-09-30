"""services/agent_ops/outbox_flusher.py — the resident deferred-delivery loop.

The loop must make one immediate pass (so a daemon restart right after a
recovery backfills at once), stay off a quiesced unit (the stop window must
not borrow the pool it just released), and refuse to start on a broken initial
config read instead of guessing a cadence.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest

from base.agents.messages import delivery_outbox as outbox
from base.deploy.maintenance import admission
from services.agent_ops import outbox_flusher


def _limits(interval: float) -> outbox.DeliveryOutboxLimits:
    return outbox.DeliveryOutboxLimits(
        enabled=True,
        retry_backoff_steps=(30.0, 60.0, 300.0, 900.0),
        budget_seconds=43200.0,
        abandoned_retention_days=30,
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
    monkeypatch.setattr(admission, "quiesced", lambda: False)

    task = asyncio.create_task(outbox_flusher._run(object(), 3600.0))  # type: ignore[arg-type]
    try:
        await _poll(lambda: bool(calls))
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(calls) == 1  # the immediate pass; the 1h wait never elapses


async def test_a_flush_pass_logs_its_summary(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The loop logs through a stdlib logger, so the summary must be `%`-formatted: a `{}`
    template with positional arguments raises while the record is formatted and the line
    is lost — the only record of a dead-letter redelivery pass."""
    caplog.set_level(logging.INFO, logger=outbox_flusher._log.name)
    calls: list[object] = []

    def _fake_flush(pool: Any) -> outbox.FlushReport:
        calls.append(pool)
        return outbox.FlushReport(delivered=1, deferred=2, expired=3)

    monkeypatch.setattr(outbox, "flush", _fake_flush)
    monkeypatch.setattr(outbox, "limits", lambda: _limits(3600.0))
    monkeypatch.setattr(admission, "quiesced", lambda: False)

    task = asyncio.create_task(outbox_flusher._run(object(), 3600.0))  # type: ignore[arg-type]
    try:
        await _poll(lambda: bool(calls))
        await _poll(lambda: bool(caplog.records))
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [r.getMessage() for r in caplog.records] == [
        "[delivery-outbox] flush pass: delivered=1 buffered=0 abandoned=0 "
        "deferred=2 unreadable=0 expired=3"
    ]


async def test_a_failed_interval_read_keeps_the_previous_wait_and_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger=outbox_flusher._log.name)

    def _boom() -> outbox.DeliveryOutboxLimits:
        raise RuntimeError("config unavailable")

    def _empty_flush(pool: Any) -> outbox.FlushReport:
        return outbox.FlushReport()

    monkeypatch.setattr(outbox, "flush", _empty_flush)
    monkeypatch.setattr(outbox, "limits", _boom)
    monkeypatch.setattr(admission, "quiesced", lambda: False)

    task = asyncio.create_task(outbox_flusher._run(object(), 0.01))  # type: ignore[arg-type]
    try:
        await _poll(lambda: bool(caplog.records))
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert caplog.records[0].getMessage() == (
        "[delivery-outbox] tick interval read failed; keeping 0.01s"
    )


async def test_run_defers_while_quiesced(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    state = {"quiesced": True}

    def _fake_flush(pool: Any) -> outbox.FlushReport:
        calls.append(pool)
        return outbox.FlushReport()

    monkeypatch.setattr(outbox, "flush", _fake_flush)
    monkeypatch.setattr(outbox, "limits", lambda: _limits(0.01))
    monkeypatch.setattr(admission, "quiesced", lambda: state["quiesced"])

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
