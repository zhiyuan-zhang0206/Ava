"""Shared native queries outlive HTTP waiters and remain owned through shutdown."""

import asyncio
import threading
from typing import Any

import pytest
from opentelemetry.sdk.trace import Span, TracerProvider
from opentelemetry.trace import StatusCode, get_current_span, use_span

from gateway.inspect._cache import InspectCacheFullError, InspectQueryCache


async def test_cancelled_leader_keeps_one_shared_native_load_until_close() -> None:
    cache = InspectQueryCache[str, object](max_entries=2, max_inflight=2, max_concurrent_loads=1)
    started, release = threading.Event(), threading.Event()
    expected = object()
    calls = 0

    def load() -> object:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=5)
        return expected

    leader = asyncio.create_task(cache.get_or_load_async("same", load, ttl_s=0, now=lambda: 0))
    follower = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        with pytest.raises(InspectCacheFullError):
            await cache.get_or_load_async("other", load, ttl_s=0, now=lambda: 0)
        follower = asyncio.create_task(
            cache.get_or_load_async("same", load, ttl_s=0, now=lambda: 0)
        )
        await asyncio.sleep(0)
        release.set()
        assert await follower is expected
        assert calls == 1
        # Production's zero TTL does not retain a successful snapshot.
        assert await cache.get_or_load_async("same", load, ttl_s=0, now=lambda: 0) is expected
        assert calls == 2
    finally:
        release.set()
        await cache.aclose()
        await asyncio.gather(*[task for task in (leader, follower) if task], return_exceptions=True)


async def test_cancelled_close_waits_for_physical_completion_and_stops_admission() -> None:
    cache = InspectQueryCache[str, str](max_entries=2, max_inflight=1)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def load() -> str:
        started.set()
        assert release.wait(timeout=5)
        finished.set()
        return "done"

    request = asyncio.create_task(cache.get_or_load_async("sql", load, ttl_s=0, now=lambda: 0))
    close = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        close = asyncio.create_task(cache.aclose())
        await asyncio.sleep(0)
        for _ in range(2):
            close.cancel()
            assert not (await asyncio.wait({close}, timeout=0.01))[0]
        assert not finished.is_set()
        with pytest.raises(RuntimeError, match="owner is closed"):
            await cache.get_or_load_async("sql", load, ttl_s=0, now=lambda: 0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await close
        assert finished.is_set()
    finally:
        release.set()
        await cache.aclose()
        if close is not None:
            await asyncio.gather(request, close, return_exceptions=True)
        else:
            await asyncio.gather(request, return_exceptions=True)


async def test_orphaned_native_error_reaches_existing_loop_diagnostics() -> None:
    cache = InspectQueryCache[str, str](max_entries=2, max_inflight=1)
    started, release = threading.Event(), threading.Event()
    failure = RuntimeError("native query failed after HTTP cancellation")
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    reports: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reports.append(context))

    def load() -> str:
        started.set()
        assert release.wait(timeout=5)
        raise failure

    request = asyncio.create_task(
        cache.get_or_load_async("agent:7/24h", load, ttl_s=0, now=lambda: 0)
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        release.set()
        await cache.aclose()
        assert len(reports) == 1
        assert reports[0]["exception"] is failure
        assert failure.__traceback__ is not None
        assert reports[0]["operation"] == "inspect_statistics"
        assert reports[0]["key"] == "agent:7/24h"
    finally:
        release.set()
        await cache.aclose()
        loop.set_exception_handler(previous)


async def test_native_query_keeps_parent_span_without_mutating_its_lifetime() -> None:
    cache = InspectQueryCache[str, object](max_entries=2, max_inflight=1)
    provider = TracerProvider()
    parent = provider.get_tracer(__name__).start_span("inspect request")
    assert isinstance(parent, Span)
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    reports: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    failure = RuntimeError("query error leaves the parent span unchanged")

    def broken() -> object:
        assert get_current_span() is parent
        raise failure

    try:
        with use_span(parent, end_on_exit=False):
            assert (
                await cache.get_or_load_async("success", get_current_span, ttl_s=0, now=lambda: 0)
                is parent
            )
            with pytest.raises(RuntimeError) as raised:
                await cache.get_or_load_async("failure", broken, ttl_s=0, now=lambda: 0)
            assert raised.value is failure
            assert get_current_span() is parent
            assert parent.is_recording()
            assert parent.status.status_code is StatusCode.UNSET
            assert parent.events == ()
        assert parent.is_recording()
        assert len(reports) == 1
        assert reports[0]["exception"] is failure
    finally:
        await cache.aclose()
        parent.end()
        provider.shutdown()
        loop.set_exception_handler(previous)


async def test_native_failure_reaches_leader_and_follower_without_cancelling_owner() -> None:
    cache = InspectQueryCache[str, str](max_entries=2, max_inflight=1)
    started, release = threading.Event(), threading.Event()
    failure = RuntimeError("invalid SQL result")
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, _context: None)

    def load() -> str:
        started.set()
        assert release.wait(timeout=5)
        raise failure

    try:
        async with asyncio.TaskGroup() as unrelated_service:
            live = unrelated_service.create_task(asyncio.sleep(5))
            leader = asyncio.create_task(
                cache.get_or_load_async("sql", load, ttl_s=0, now=lambda: 0)
            )
            assert await asyncio.to_thread(started.wait, 1)
            follower = asyncio.create_task(
                cache.get_or_load_async(
                    "sql", lambda: pytest.fail("duplicate SQL"), ttl_s=0, now=lambda: 0
                )
            )
            await asyncio.sleep(0)
            release.set()
            for waiter in (leader, follower):
                with pytest.raises(RuntimeError) as raised:
                    await waiter
                assert raised.value is failure
            assert not live.done()
            assert (
                await cache.get_or_load_async("sql", lambda: "healthy", ttl_s=0, now=lambda: 0)
                == "healthy"
            )
            live.cancel()
    finally:
        release.set()
        await cache.aclose()
        loop.set_exception_handler(previous)
