"""Turn dispatcher cases: subscription recovery."""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from base.events.live.bus import EventBus
from base.events.live.tests.fakes import patch_open_async_redis
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from services.agent_runner.agent_host import dispatcher
from services.agent_runner.agent_host.dispatcher import InboundWakeDispatcher
from services.agent_runner.agent_host.tests.turn_dispatcher.helpers import (
    QueueingPubSub,
    patch_redis,
)
from services.agent_runner.agent_host.tests.turn_dispatcher.scan_setup import ScanScheduler


class TestSubscriptionRecovery:
    async def test_db_down_scan_keeps_the_redis_subscription_open(
        self, monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords
    ) -> None:
        """The durable backstop may be down while the Redis wake path remains healthy."""
        pubsub = QueueingPubSub()
        clients = patch_redis(monkeypatch, pubsub)
        failures_seen = asyncio.Event()
        scans = 0

        async def _pending() -> list[int]:
            nonlocal scans
            scans += 1
            if scans >= 3:
                failures_seen.set()
            raise RuntimeError("database unavailable")

        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
            ScanScheduler(),
            pending_scan=_pending,
            scan_interval_s=0.02,
            max_scan_backoff_s=0.04,
            subscription_read_timeout_s=0.005,
            reconnect_delay_s=0.0,
        )
        task = asyncio.create_task(disp.run())
        try:
            await asyncio.wait_for(failures_seen.wait(), timeout=1.0)

            failures = loguru_records.events("host_dispatcher_scan_failed")
            assert all(record["level"].name == "WARNING" for record in failures)
            failure_events = [record["extra"] for record in failures]
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
        pubsub = QueueingPubSub()
        clients = patch_redis(monkeypatch, pubsub)
        first_failure = asyncio.Event()
        scheduler = ScanScheduler()

        async def _pending() -> list[int]:
            first_failure.set()
            raise RuntimeError("database unavailable")

        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
            scheduler,
            pending_scan=_pending,
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
        pubsub = QueueingPubSub()
        clients = patch_redis(monkeypatch, pubsub)
        recovered_twice = asyncio.Event()
        scans = 0
        successful_scans = 0
        successful_scan_times: list[float] = []

        async def _pending() -> list[int]:
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


class _ScriptedPubSub:
    """One subscription whose outcome is scripted: fail to subscribe, drop after one
    completed read, or stay healthy."""

    def __init__(self, behavior: str) -> None:
        self._behavior = behavior
        self._reads = 0

    async def psubscribe(self, _pattern: str) -> None:
        if self._behavior == "fail":
            raise ConnectionError("redis unreachable")

    async def get_message(self, **_kwargs: object) -> None:
        self._reads += 1
        if self._behavior == "read_then_drop" and self._reads > 1:
            raise ConnectionError("connection reset")
        await asyncio.sleep(0.001)

    async def aclose(self) -> None:
        return None


class _ScriptedRedis:
    def __init__(self, behavior: str) -> None:
        self._behavior = behavior

    def pubsub(self, **_kwargs: object) -> _ScriptedPubSub:
        return _ScriptedPubSub(self._behavior)

    async def aclose(self) -> None:
        return None


class TestReconnectExhaustion:
    """A run of subscription failures ends in an exit for the supervisor, not an
    endless reconnect loop; one completed read starts the count over."""

    async def test_consecutive_failures_raise_and_log_an_error_event(
        self, monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords
    ) -> None:
        opened = 0

        def _open(_url: str, **_kw: object) -> _ScriptedRedis:
            nonlocal opened
            opened += 1
            return _ScriptedRedis("fail")

        patch_open_async_redis(monkeypatch, _open)
        disp = InboundWakeDispatcher(
            EventBus.from_settings(), ScanScheduler(), reconnect_delay_s=0.0
        )

        with pytest.raises(dispatcher.DispatcherReconnectExhaustedError) as raised:
            await asyncio.wait_for(disp.run(), timeout=5.0)

        limit = dispatcher.MAX_CONSECUTIVE_RECONNECT_FAILURES
        assert opened == limit, "the run must stop reconnecting at the limit"
        assert isinstance(raised.value.__cause__, ConnectionError)
        exhausted = loguru_records.events("host_dispatcher_reconnect_exhausted")
        assert len(exhausted) == 1
        assert exhausted[0]["level"].name == "ERROR"
        assert exhausted[0]["extra"]["failures"] == limit

    async def test_a_completed_read_resets_the_failure_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        limit = dispatcher.MAX_CONSECUTIVE_RECONNECT_FAILURES
        # The failures before and after the completed read total well past the
        # limit, but never `limit` in a row: without the reset this run would end.
        script = ["fail"] * (limit - 1) + ["read_then_drop"] + ["fail"] * (limit - 2) + ["healthy"]
        remaining = iter(script)
        healthy_opened = asyncio.Event()

        def _open(_url: str, **_kw: object) -> _ScriptedRedis:
            behavior = next(remaining)
            if behavior == "healthy":
                healthy_opened.set()
            return _ScriptedRedis(behavior)

        patch_open_async_redis(monkeypatch, _open)
        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
            ScanScheduler(),
            subscription_read_timeout_s=0.005,
            subscription_read_deadline_grace_s=0.05,
            reconnect_delay_s=0.0,
        )
        task = asyncio.create_task(disp.run())
        try:
            await asyncio.wait_for(healthy_opened.wait(), timeout=5.0)
            await asyncio.sleep(0.05)
            assert not task.done(), (
                "failures separated by a completed read must not exhaust the run"
            )
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
