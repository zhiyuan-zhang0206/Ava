"""services/agent_ops/outbox_flusher.py — the resident deferred-delivery loop.

The loop makes one immediate pass (so a daemon restart right after a recovery
backfills at once), stays off a quiesced unit (the stop window must not borrow the
pool it just released), follows the live flush interval, beats per record so a long
pass is not read as a wedge, and ends the process — through the ops server's
`TaskGroup` — when a round raises."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest

from base.agents.messages import delivery_outbox as outbox
from base.daemon import round_loop
from base.daemon.endpoints import ServiceEndpoints
from base.daemon.loop_health import LivenessGroup, LoopProgress
from base.deploy.maintenance import admission
from services.agent_ops import daemon, outbox_flusher


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


def _progress() -> LoopProgress:
    return LoopProgress("delivery-outbox", outbox_flusher.INITIAL_LIVENESS_TIMEOUT_S)


async def _poll(predicate: Callable[[], bool], *, attempts: int = 300) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    interval: float,
    flush: Callable[..., outbox.FlushReport],
    quiesced: Callable[[], bool] = lambda: False,
) -> None:
    monkeypatch.setattr(outbox, "flush", flush)
    monkeypatch.setattr(outbox, "limits", lambda: _limits(interval))
    monkeypatch.setattr(admission, "quiesced", quiesced)


async def test_the_loop_performs_an_immediate_round_then_paces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def flush(pool: Any, **_kw: object) -> outbox.FlushReport:
        calls.append(pool)
        return outbox.FlushReport(delivered=1)

    _patch(monkeypatch, interval=3600.0, flush=flush)

    task = asyncio.create_task(outbox_flusher.outbox_loop(object(), _progress()))  # type: ignore[arg-type]
    try:
        await _poll(lambda: bool(calls))
        await asyncio.sleep(0.05)
    finally:
        await _stop(task)
    assert len(calls) == 1  # the immediate round; the 1h wait never elapses


async def test_a_flush_pass_logs_its_summary(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The loop logs through a stdlib logger, so the summary must be `%`-formatted: a `{}`
    template with positional arguments raises while the record is formatted and the line
    is lost — the only record of a dead-letter redelivery pass."""
    caplog.set_level(logging.INFO, logger=outbox_flusher._log.name)

    def flush(pool: Any, **_kw: object) -> outbox.FlushReport:
        return outbox.FlushReport(delivered=1, deferred=2, expired=3)

    _patch(monkeypatch, interval=3600.0, flush=flush)

    task = asyncio.create_task(outbox_flusher.outbox_loop(object(), _progress()))  # type: ignore[arg-type]
    try:
        await _poll(lambda: bool(caplog.records))
    finally:
        await _stop(task)
    assert [r.getMessage() for r in caplog.records] == [
        "[delivery-outbox] flush pass: delivered=1 buffered=0 abandoned=0 "
        "deferred=2 unreadable=0 expired=3"
    ]


async def test_the_loop_defers_while_quiesced(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    state = {"quiesced": True}

    def flush(pool: Any, **_kw: object) -> outbox.FlushReport:
        calls.append(pool)
        return outbox.FlushReport()

    _patch(monkeypatch, interval=0.01, flush=flush, quiesced=lambda: state["quiesced"])

    task = asyncio.create_task(outbox_flusher.outbox_loop(object(), _progress()))  # type: ignore[arg-type]
    try:
        await asyncio.sleep(0.05)
        assert calls == []  # quiesced: records (and the pool) stay untouched
        state["quiesced"] = False
        await _poll(lambda: bool(calls))
    finally:
        await _stop(task)


async def test_the_wait_follows_the_live_flush_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator changes `AVA_DELIVERY_OUTBOX_FLUSH_INTERVAL_SECONDS` live: the
    next wait is the interval the round read, not the one the loop started with."""
    waits: list[float] = []
    knobs = {"interval": 0.01}

    async def sleep(_progress: object, total_s: float) -> None:
        waits.append(total_s)
        knobs["interval"] = 0.02
        await asyncio.sleep(0)

    def flush(pool: Any, **_kw: object) -> outbox.FlushReport:
        return outbox.FlushReport()

    monkeypatch.setattr(outbox, "flush", flush)
    monkeypatch.setattr(outbox, "limits", lambda: _limits(knobs["interval"]))
    monkeypatch.setattr(admission, "quiesced", lambda: False)
    monkeypatch.setattr(round_loop, "sleep_with_progress", sleep)

    task = asyncio.create_task(outbox_flusher.outbox_loop(object(), _progress()))  # type: ignore[arg-type]
    try:
        await _poll(lambda: len(waits) >= 2)
    finally:
        await _stop(task)
    assert waits[:2] == [0.01, 0.02]


async def test_liveness_follows_the_pass_not_the_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass over many records outlives one tick: each record beats the loop, and
    the wedge threshold is sized from the live knobs (three connection waits per
    record plus slack), never the idle interval."""
    progress = _progress()
    beats: list[int] = []
    seen_timeout: list[float] = []

    def flush(pool: Any, *, on_record: Callable[[], None], **_kw: object) -> outbox.FlushReport:
        seen_timeout.append(progress.timeout_s)
        for _ in range(3):
            on_record()
            beats.append(1)
        return outbox.FlushReport()

    _patch(monkeypatch, interval=30.0, flush=flush)

    task = asyncio.create_task(outbox_flusher.outbox_loop(object(), progress))  # type: ignore[arg-type]
    try:
        await _poll(lambda: len(beats) == 3)
    finally:
        await _stop(task)
    assert seen_timeout == [outbox_flusher.liveness_timeout_s(30.0)] == [4 * 30.0 + 60.0]


async def test_a_failing_round_ends_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A flush that raises (not a failed delivery, which the pass records and
    survives) ends the loop; nothing logs and carries on."""

    def flush(pool: Any, **_kw: object) -> outbox.FlushReport:
        raise RuntimeError("journal unreadable")

    _patch(monkeypatch, interval=0.01, flush=flush)

    with pytest.raises(RuntimeError, match="journal unreadable"):
        await outbox_flusher.outbox_loop(object(), _progress())  # type: ignore[arg-type]


async def test_an_unreadable_config_ends_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> outbox.DeliveryOutboxLimits:
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(outbox, "limits", boom)

    with pytest.raises(RuntimeError, match="config unavailable"):
        await outbox_flusher.outbox_loop(object(), _progress())  # type: ignore[arg-type]


async def test_a_crashing_outbox_loop_ends_the_ops_server_and_releases_its_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ops server and the outbox loop share one TaskGroup: when the loop raises,
    the server stops serving, `_main` leaves with the error (the supervisor restarts
    the unit) and the pool, the health server and the pidfile are released."""
    from base.deploy.schema import migrations

    events: list[str] = []
    served_cancelled = asyncio.Event()

    class _Pool:
        def close(self) -> None:
            events.append("pool")

    class _Server:
        async def __aenter__(self) -> _Server:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def serve_forever(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                served_cancelled.set()
                raise

    async def start_health(name: str, port: int, **kw: object) -> _Server:
        assert isinstance(kw["liveness"], LivenessGroup)
        assert set(kw["liveness"].snapshot()) == {"delivery-outbox"}  # type: ignore[union-attr]
        return _Server()

    async def stop_health(_server: object) -> None:
        events.append("health")

    async def crashing_loop(_pool: object, _progress: object) -> None:
        await asyncio.sleep(0.01)
        raise RuntimeError("outbox loop crashed")

    monkeypatch.setattr(daemon, "_is_running", lambda: False)
    monkeypatch.setattr(daemon, "_write_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "_remove_pidfile", lambda: events.append("pidfile"))

    def schema_current(_url: str) -> None:
        return None

    monkeypatch.setattr(migrations, "assert_schema_current", schema_current)
    monkeypatch.setattr(daemon, "_open_db_pool", _Pool)
    monkeypatch.setattr(daemon, "_ops_acceptance", lambda: None)

    def bind_host(_acceptance: object) -> str:
        return "127.0.0.1"

    monkeypatch.setattr(daemon, "_ops_bind_host", bind_host)
    monkeypatch.setattr(daemon, "start_health_server", start_health)
    monkeypatch.setattr(daemon, "stop_health_server", stop_health)
    monkeypatch.setattr(daemon, "_register_boot", lambda: None)
    monkeypatch.setattr(daemon.outbox_flusher, "outbox_loop", crashing_loop)
    assert (
        ServiceEndpoints.from_settings().of("ops").health_port
    )  # the port table still names the ops slot

    with pytest.raises(ExceptionGroup) as raised:
        await daemon._main()

    assert [str(exc) for exc in raised.value.exceptions] == ["outbox loop crashed"]
    assert served_cancelled.is_set()
    assert sorted(events) == ["health", "pidfile", "pool"]
