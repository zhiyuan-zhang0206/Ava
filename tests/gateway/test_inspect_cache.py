"""Admission and cancellation invariants for shared in-flight queries."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway.routers._inspect_cache import InspectCacheFullError, InspectQueryCache


def test_inspect_cache_admission_bounds_concurrent_loads() -> None:
    """A distinct-key leader is rejected at capacity, while its follower shares the load."""
    cache = InspectQueryCache[str, str](
        max_entries=8,
        max_inflight=8,
        max_concurrent_loads=1,
    )
    started = threading.Event()
    release = threading.Event()

    def blocking_load() -> str:
        started.set()
        assert release.wait(timeout=2)
        return "first"

    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(
            cache.get_or_load,
            "first",
            blocking_load,
            ttl_s=10,
            now=lambda: 0,
        )
        assert started.wait(timeout=1)
        follower = executor.submit(
            cache.get_or_load,
            "first",
            lambda: pytest.fail("a follower must not load"),
            ttl_s=10,
            now=lambda: 0,
        )
        with pytest.raises(InspectCacheFullError):
            cache.get_or_load("second", lambda: "second", ttl_s=10, now=lambda: 0)
        release.set()
        assert leader.result(timeout=1) == "first"
        assert follower.result(timeout=1) == "first"

    assert cache.get_or_load("second", lambda: "second", ttl_s=10, now=lambda: 0) == "second"


def test_inspect_singleflight_cancellation_releases_key_for_retry() -> None:
    """A cancelled leader never leaves its key in the in-flight map."""
    cache = InspectQueryCache[str, object](max_entries=2, max_inflight=1)
    calls = 0
    expected = object()

    def load() -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError
        return expected

    with pytest.raises(asyncio.CancelledError):
        cache.get_or_load("same", load, ttl_s=10, now=lambda: 0)
    assert cache.get_or_load("same", load, ttl_s=10, now=lambda: 0) is expected
    assert calls == 2


@pytest.mark.asyncio
async def test_inspect_async_followers_timeout_without_retaining_executor_workers() -> None:
    """Timed-out followers wait on the shared Future without blocking threads."""
    cache = InspectQueryCache[str, object](max_entries=2, max_inflight=1)
    started = threading.Event()
    release = threading.Event()
    expected = object()
    calls = 0

    def blocking_load() -> object:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2)
        return expected

    leader = asyncio.create_task(
        cache.get_or_load_async("same", blocking_load, ttl_s=10, now=lambda: 0)
    )
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.001)
    assert started.is_set()

    followers = [
        asyncio.create_task(
            asyncio.wait_for(
                cache.get_or_load_async(
                    "same",
                    lambda: pytest.fail("a follower must not load"),
                    ttl_s=10,
                    now=lambda: 0,
                ),
                timeout=0.01,
            )
        )
        for _ in range(64)
    ]
    results = await asyncio.gather(*followers, return_exceptions=True)
    assert all(isinstance(result, TimeoutError) for result in results)

    # A to_thread probe still starts immediately. An implementation that puts
    # every follower's Future.result() in the shared executor exhausts all of
    # its workers here until the leader is released.
    assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), timeout=1) == "free"
    assert calls == 1

    release.set()
    assert await asyncio.wait_for(leader, timeout=1) is expected
    assert (
        await cache.get_or_load_async(
            "same", lambda: pytest.fail("late result must be cached"), ttl_s=10, now=lambda: 0
        )
        is expected
    )


@pytest.mark.asyncio
async def test_inspect_async_leader_deadline_fails_followers_fast() -> None:
    """A deadline failure reaches all followers and releases the key for a retry."""
    cache = InspectQueryCache[str, object](max_entries=2, max_inflight=1)
    started = threading.Event()
    release = threading.Event()
    expected = object()
    calls = 0

    def deadline_expired_load() -> object:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2)
        raise TimeoutError("inspect deadline expired")

    leader = asyncio.create_task(
        cache.get_or_load_async("same", deadline_expired_load, ttl_s=10, now=lambda: 0)
    )
    assert await asyncio.to_thread(started.wait, 1)
    follower = asyncio.create_task(
        cache.get_or_load_async(
            "same",
            lambda: pytest.fail("a follower must not load"),
            ttl_s=10,
            now=lambda: 0,
        )
    )
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(TimeoutError, match="inspect deadline expired"):
        await leader
    with pytest.raises(TimeoutError, match="inspect deadline expired"):
        await follower
    assert calls == 1
    assert (
        await cache.get_or_load_async("same", lambda: expected, ttl_s=10, now=lambda: 0) is expected
    )


def test_inspect_cache_bounds_values_and_distinct_inflight_keys() -> None:
    """Both retained snapshots and active distinct-key loaders stay bounded."""
    cache = InspectQueryCache[str, str](max_entries=2, max_inflight=1)
    loads: dict[str, int] = {}

    def load(key: str) -> str:
        loads[key] = loads.get(key, 0) + 1
        return f"{key}-{loads[key]}"

    assert cache.get_or_load("a", lambda: load("a"), ttl_s=10, now=lambda: 0) == "a-1"
    assert cache.get_or_load("b", lambda: load("b"), ttl_s=10, now=lambda: 0) == "b-1"
    assert cache.get_or_load("c", lambda: load("c"), ttl_s=10, now=lambda: 0) == "c-1"
    # Insertion-order tie breaking evicts the oldest equal-expiry value.
    assert cache.get_or_load("a", lambda: load("a"), ttl_s=10, now=lambda: 0) == "a-2"

    started = threading.Event()
    release = threading.Event()

    def blocking_load() -> str:
        started.set()
        assert release.wait(timeout=2)
        return "held"

    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(
            cache.get_or_load, "holder", blocking_load, ttl_s=10, now=lambda: 0
        )
        assert started.wait(timeout=1)
        with pytest.raises(InspectCacheFullError):
            cache.get_or_load("overflow", lambda: "no", ttl_s=10, now=lambda: 0)
        release.set()
        assert holder.result(timeout=1) == "held"
