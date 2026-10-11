"""Turn dispatcher cases: subscription recovery."""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from base.events.live.bus import EventBus
from base.events.live.tests.fakes import patch_open_async_redis
from services.agent_runner.agent_host import dispatcher
from services.agent_runner.agent_host.dispatcher import InboundWakeDispatcher
from services.agent_runner.agent_host.tests.test_turn_dispatcher import (
    _patch_redis,
    _QueueingPubSub,
)
from services.agent_runner.agent_host.tests.turn_dispatcher.scan_setup import ScanScheduler


class TestSubscriptionRecovery:
    async def test_db_down_scan_keeps_the_redis_subscription_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The durable backstop may be down while the Redis wake path remains healthy."""
        pubsub = _QueueingPubSub()
        clients = _patch_redis(monkeypatch, pubsub)
        failures_seen = asyncio.Event()
        failure_events: list[dict[str, object]] = []
        scans = 0

        def _warning(_message: str, **fields: object) -> None:
            if fields["event"] == "host_dispatcher_scan_failed":
                failure_events.append(fields)

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            nonlocal scans
            scans += 1
            if scans >= 3:
                failures_seen.set()
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(dispatcher.logger, "warning", _warning)
        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
            ScanScheduler(),
            pending_scan=_pending,
            stale_after_s=180.0,
            scan_interval_s=0.02,
            max_scan_backoff_s=0.04,
            subscription_read_timeout_s=0.005,
            reconnect_delay_s=0.0,
        )
        task = asyncio.create_task(disp.run())
        try:
            await asyncio.wait_for(failures_seen.wait(), timeout=1.0)

            assert len(clients) == 1
            assert len(failure_events) >= 3
            assert [event["backoff_s"] for event in failure_events[:3]] == [0.02, 0.04, 0.04]
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_failing_scan_does_not_interrupt_the_pubsub_fast_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DB outage must not create a gap in the subscription's wake delivery."""
        pubsub = _QueueingPubSub()
        clients = _patch_redis(monkeypatch, pubsub)
        first_failure = asyncio.Event()
        scheduler = ScanScheduler()

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            first_failure.set()
            raise RuntimeError("database unavailable")

        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            scan_interval_s=0.02,
            subscription_read_timeout_s=0.005,
            reconnect_delay_s=0.0,
        )
        task = asyncio.create_task(disp.run())
        try:
            await asyncio.wait_for(first_failure.wait(), timeout=1.0)
            pubsub.messages.put_nowait({"type": "pmessage", "channel": "ava:inbound:23"})
            await asyncio.wait_for(scheduler.woken_event.wait(), timeout=1.0)

            assert scheduler.woken == [23]
            assert len(clients) == 1
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_scan_backoff_recovers_on_the_existing_subscription(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A recovered scan resets to its normal cadence without reconnecting Redis."""
        pubsub = _QueueingPubSub()
        clients = _patch_redis(monkeypatch, pubsub)
        recovered_twice = asyncio.Event()
        scans = 0
        successful_scans = 0
        successful_scan_times: list[float] = []

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            nonlocal scans, successful_scans
            scans += 1
            if scans <= 2:
                raise RuntimeError("database unavailable")
            successful_scans += 1
            successful_scan_times.append(time.monotonic())
            if successful_scans == 2:
                recovered_twice.set()
            return []

        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
            ScanScheduler(),
            pending_scan=_pending,
            stale_after_s=180.0,
            scan_interval_s=0.02,
            subscription_read_timeout_s=0.005,
            reconnect_delay_s=0.0,
        )
        task = asyncio.create_task(disp.run())
        try:
            await asyncio.wait_for(recovered_twice.wait(), timeout=1.0)

            assert scans >= 4
            assert successful_scan_times[1] - successful_scan_times[0] < 0.06
            assert len(clients) == 1
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_scan_restart_required_error_exits_without_reconnecting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale turn that cannot unwind remains a host-level recovery condition."""
        pubsub = _QueueingPubSub()
        clients = _patch_redis(monkeypatch, pubsub)

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            raise dispatcher.HostRestartRequiredError("stale turn did not unwind")

        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
            ScanScheduler(),
            pending_scan=_pending,
            stale_after_s=180.0,
        )

        with pytest.raises(dispatcher.HostRestartRequiredError, match="did not unwind"):
            await disp.run()

        assert len(clients) == 1

    async def test_half_open_subscription_read_is_bounded_and_reconnected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A peer may stay TCP-connected yet never answer a subscription read.

        The dispatcher must close that connection and establish a fresh one,
        rather than letting the hosted pending scan and all notifications stop
        behind a hung `PSUBSCRIBE` read.
        """

        class _HalfOpenPubSub:
            def __init__(self) -> None:
                self.closed = False

            async def psubscribe(self, _pattern: str) -> None:
                return None

            async def get_message(self, **_kwargs: object) -> None:
                await asyncio.Event().wait()

            async def aclose(self) -> None:
                self.closed = True

        class _Redis:
            def __init__(self, pubsub: _HalfOpenPubSub) -> None:
                self._pubsub = pubsub
                self.closed = False

            def pubsub(self, **_kwargs: object) -> _HalfOpenPubSub:
                return self._pubsub

            async def aclose(self) -> None:
                self.closed = True

        first_pubsub = _HalfOpenPubSub()
        second_pubsub = _HalfOpenPubSub()
        first = _Redis(first_pubsub)
        second = _Redis(second_pubsub)
        clients = iter([first, second])
        second_opened = asyncio.Event()

        def _open(_url: str, **_kw: object) -> _Redis:
            client = next(clients)
            if client is second:
                second_opened.set()
            return client

        patch_open_async_redis(monkeypatch, _open)
        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
            ScanScheduler(),
            subscription_read_timeout_s=0.01,
            subscription_read_deadline_grace_s=0.01,
            reconnect_delay_s=0.0,
        )
        task = asyncio.create_task(disp.run())
        try:
            await asyncio.wait_for(second_opened.wait(), timeout=1.0)
            assert first_pubsub.closed, "the half-open pubsub handle must be discarded"
            assert first.closed, "the paired Redis command client must be discarded"
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
