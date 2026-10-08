"""Tests for the `publish_best_effort` / `publish_best_effort_sync` primitives.

These are the single never-raise wrapper for every fire-and-forget live-UI /
lifecycle event publish (base/live_announce, base/labels,
gateway/routers/pages, ops/lifecycle all route through them). The invariant
they enforce: pub/sub is only a latency optimization, so a publish failure must
never propagate into (crash / roll back) the caller — it returns None and logs,
classified like the `base/db/__init__.py:publish_inbound_wake` template (NOPERM /
ResponseError → WARNING, transient → DEBUG).
"""

from __future__ import annotations

from typing import Any

import pytest
import redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import NoPermissionError, ResponseError

from base.config import settings
from base.events.live import redis_client
from base.events.live.bus import EventBus
from base.events.live.tests.fakes import patch_async_redis, patch_sync_redis

_bus = EventBus.from_settings()


@pytest.fixture(autouse=True)
def _skip_acl_backoff_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    global _bus  # noqa: PLW0603 — a bus built from the settings the session has by now
    _bus = EventBus.from_settings()
    """Exercise the terminal best-effort contract without waiting 45.5 seconds.

    Retry cadence and its bound are asserted in test_redis_client; these tests
    own warning classification after retries have been exhausted.
    """

    async def _no_async_wait(_delay: float) -> None:
        return None

    def _no_sync_wait(_delay: float) -> None:
        return None

    def _max_jitter(delay_cap: float) -> float:
        return delay_cap

    monkeypatch.setattr(redis_client, "_sleep_sync", _no_sync_wait)
    monkeypatch.setattr(redis_client, "_sleep_async", _no_async_wait)
    monkeypatch.setattr(redis_client, "_auth_retry_jitter", _max_jitter)


class _BoomSyncClient:
    """A sync redis stand-in whose publish raises — asserts the wrapper swallows."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def publish(self, _channel: str, _payload: str) -> int:
        raise self._exc

    def close(self) -> None:  # closed in the wrapper's finally
        pass


class _BoomAsyncClient:
    """An async redis stand-in whose publish raises."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def publish(self, _channel: str, _payload: str) -> int:
        raise self._exc


class TestPublishBestEffortSync:
    def test_returns_receiver_count_on_real_redis(self) -> None:
        """A successful publish returns the receiver count (0 with nobody subscribed)
        — an int, never None."""
        n = _bus.publish_best_effort_sync("{}", context="test")
        assert n == 0

    def test_never_raises_and_debug_logs_on_transient(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        """A transient failure (redis down) is swallowed → None, logged at DEBUG."""
        patch_sync_redis(monkeypatch, lambda: _BoomSyncClient(RedisConnectionError("down")))
        result = _bus.publish_best_effort_sync("{}", channel="ava:x", context="unit")
        assert result is None
        hits = [r for r in loguru_records if "skipped" in r["message"] and "ava:x" in r["message"]]
        assert hits, "expected a best-effort DEBUG skip line"
        assert all(r["level"].no < 30 for r in hits), "transient failure must be DEBUG, not WARNING"  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    def test_non_transport_failure_warns_with_traceback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        """An exception that is not a redis/transport error is a bug in the publish path:
        swallowed → None (best-effort), but WARNING with the traceback, never DEBUG."""
        patch_sync_redis(monkeypatch, lambda: _BoomSyncClient(AttributeError("bug")))
        result = _bus.publish_best_effort_sync("{}", channel="ava:bug", context="unit")
        assert result is None
        hits = [r for r in loguru_records if "failed unexpectedly" in r["message"]]
        assert hits
        assert all(r["level"].no >= 30 and r["exception"] is not None for r in hits)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    def test_never_raises_and_warns_on_noperm(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        """A ResponseError (redis NOPERM — ACL misconfig) is swallowed → None, but
        logged at WARNING because it silently disables live updates fleet-wide."""
        patch_sync_redis(monkeypatch, lambda: _BoomSyncClient(NoPermissionError("NOPERM")))
        result = _bus.publish_best_effort_sync("{}", channel="ava:noperm-sync", context="unit")
        assert result is None
        assert any(
            "rejected by redis" in r["message"] and r["level"].no >= 30  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            for r in loguru_records
        ), "a NOPERM publish must be logged at WARNING"

    def test_noperm_warning_is_rate_limited(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        """A persistent NOPERM outage warns once per channel then drops repeats to
        DEBUG — one event funnels the whole fleet through here, so the WARNING must
        not flood."""
        patch_sync_redis(monkeypatch, lambda: _BoomSyncClient(NoPermissionError("NOPERM")))
        channel = "ava:noperm-throttle"
        for _ in range(4):
            assert _bus.publish_best_effort_sync("{}", channel=channel, context="unit") is None
        warnings = [
            r
            for r in loguru_records
            if "rejected by redis" in r["message"] and r["level"].no >= 30  # pyright: ignore[reportUnknownMemberType]
        ]
        assert len(warnings) == 1, f"expected exactly one WARNING, got {len(warnings)}"  # pyright: ignore[reportUnknownArgumentType]
        assert any("rate-limited" in r["message"] and r["level"].no < 30 for r in loguru_records), (  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            "suppressed repeats must drop to a DEBUG rate-limited line"
        )


class TestPublishBestEffortAsync:
    async def test_returns_receiver_count_on_real_redis(self) -> None:
        n = await _bus.publish_best_effort("{}", context="test")
        assert n == 0

    async def test_never_raises_and_debug_logs_on_transient(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        patch_async_redis(monkeypatch, lambda: _BoomAsyncClient(RedisConnectionError("down")))
        result = await _bus.publish_best_effort("{}", channel="ava:x", context="unit")
        assert result is None
        assert any("skipped" in r["message"] and r["level"].no < 30 for r in loguru_records), (  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            "expected a best-effort DEBUG skip line"
        )

    async def test_never_raises_and_warns_on_noperm(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        patch_async_redis(monkeypatch, lambda: _BoomAsyncClient(NoPermissionError("NOPERM")))
        result = await _bus.publish_best_effort("{}", channel="ava:noperm-async", context="unit")
        assert result is None
        assert any(
            "rejected by redis" in r["message"] and r["level"].no >= 30  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            for r in loguru_records
        )

    async def test_reports_live_receiver_count(self) -> None:
        """With a real subscriber on the channel, the count is the number of
        receivers — the signal any 0-receiver guard relies on."""
        import redis.asyncio as aredis

        channel = f"{settings.data_plane.events_channel}:count"
        client = aredis.Redis.from_url(settings.data_plane.redis_url, decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
        pubsub = client.pubsub()  # pyright: ignore[reportUnknownMemberType]
        await pubsub.subscribe(channel)
        await pubsub.get_message(timeout=1.0)  # drain the subscribe confirmation
        try:
            n = await _bus.publish_best_effort("{}", channel=channel, context="test")
            assert n == 1
        finally:
            await pubsub.unsubscribe(channel)  # pyright: ignore[reportUnknownMemberType]
            await pubsub.aclose()
            await client.aclose()


def test_zero_receivers_distinguished_from_failure() -> None:
    """0 (delivered to nobody) and None (publish failed) are distinct return
    values — the contract callers that check receiver counts rely on. A real
    publish with no subscriber is 0, not None."""
    with redis.Redis.from_url(settings.data_plane.redis_url) as _r:  # pyright: ignore[reportUnknownMemberType]
        pass  # sanity: redis reachable
    assert _bus.publish_best_effort_sync("{}", channel="ava:nobody") == 0


async def test_warning_cadence_is_shared_by_bus_publishers_and_isolated_across_buses(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    """Each bus shares sync/async warning cadence, while a new owner reports afresh."""
    now = 100.0
    monkeypatch.setattr(redis_client.time, "monotonic", lambda: now)
    failure = ResponseError("NOPERM")
    patch_sync_redis(monkeypatch, lambda: _BoomSyncClient(failure))
    patch_async_redis(monkeypatch, lambda: _BoomAsyncClient(failure))
    first = EventBus.from_settings()
    second = EventBus.from_settings()
    channel = "ava:owner-cadence"

    assert first.publish_best_effort_sync("{}", channel=channel) is None
    assert await first.publish_best_effort("{}", channel=channel) is None
    assert await second.publish_best_effort("{}", channel=channel) is None
    now += 59.0
    assert first.publish_best_effort_sync("{}", channel=channel) is None
    now += 1.0
    assert await first.publish_best_effort("{}", channel=channel) is None
    assert first.publish_best_effort_sync("{}", channel=f"{channel}:other") is None
    patch_sync_redis(monkeypatch, lambda: _BoomSyncClient(AttributeError("bug")))
    assert first.publish_best_effort_sync("{}", channel=channel) is None
    assert first.publish_best_effort_sync("{}", channel=channel) is None

    assert [r["level"].name for r in loguru_records] == [
        "WARNING",
        "DEBUG",
        "WARNING",
        "DEBUG",
        "WARNING",
        "WARNING",
        "WARNING",
    ]
    assert [r["exception"] is not None for r in loguru_records] == [False] * 6 + [True]
    assert sum("rate-limited" in r["message"] for r in loguru_records) == 2
    assert "failed unexpectedly" in loguru_records[-1]["message"]
