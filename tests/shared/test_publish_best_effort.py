"""Tests for the `publish_best_effort` / `publish_best_effort_sync` primitives.

These are the single never-raise wrapper for every fire-and-forget live-UI /
lifecycle event publish (shared/live_announce, shared/labels,
gateway/routers/pages, ops/ops_lifecycle all route through them). The invariant
they enforce: pub/sub is only a latency optimization, so a publish failure must
never propagate into (crash / roll back) the caller — it returns None and logs,
classified like the `shared/db.py:publish_inbound_wake` template (NOPERM /
ResponseError → WARNING, transient → DEBUG).
"""

from __future__ import annotations

import pytest
import redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import NoPermissionError

from shared import redis_client
from shared.config import settings
from shared.redis_client import publish_best_effort, publish_best_effort_sync


@pytest.fixture(autouse=True)
def _skip_acl_backoff_waits(monkeypatch: pytest.MonkeyPatch) -> None:
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
        n = publish_best_effort_sync(settings.data_plane.events_channel, "{}", context="test")
        assert n == 0

    def test_never_raises_and_debug_logs_on_transient(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        """A transient failure (redis down) is swallowed → None, logged at DEBUG."""
        monkeypatch.setattr(
            redis_client,
            "sync_redis",
            lambda **_: _BoomSyncClient(RedisConnectionError("down")),  # pyright: ignore[reportUnknownArgumentType]
        )
        result = publish_best_effort_sync("ava:x", "{}", context="unit")
        assert result is None
        hits = [r for r in loguru_records if "skipped" in r["message"] and "ava:x" in r["message"]]
        assert hits, "expected a best-effort DEBUG skip line"
        assert all(r["level"].no < 30 for r in hits), "transient failure must be DEBUG, not WARNING"  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    def test_never_raises_and_warns_on_noperm(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        """A ResponseError (redis NOPERM — ACL misconfig) is swallowed → None, but
        logged at WARNING because it silently disables live updates fleet-wide."""
        redis_client._warn_last.clear()  # deterministic first-warn (see throttle)
        monkeypatch.setattr(
            redis_client,
            "sync_redis",
            lambda **_: _BoomSyncClient(NoPermissionError("NOPERM")),  # pyright: ignore[reportUnknownArgumentType]
        )
        result = publish_best_effort_sync("ava:noperm-sync", "{}", context="unit")
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
        redis_client._warn_last.clear()
        monkeypatch.setattr(
            redis_client,
            "sync_redis",
            lambda **_: _BoomSyncClient(NoPermissionError("NOPERM")),  # pyright: ignore[reportUnknownArgumentType]
        )
        channel = "ava:noperm-throttle"
        for _ in range(4):
            assert publish_best_effort_sync(channel, "{}", context="unit") is None
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
        n = await publish_best_effort(settings.data_plane.events_channel, "{}", context="test")
        assert n == 0

    async def test_never_raises_and_debug_logs_on_transient(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        monkeypatch.setattr(
            redis_client,
            "get_async_redis",
            lambda: _BoomAsyncClient(RedisConnectionError("down")),
        )
        result = await publish_best_effort("ava:x", "{}", context="unit")
        assert result is None
        assert any("skipped" in r["message"] and r["level"].no < 30 for r in loguru_records), (  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            "expected a best-effort DEBUG skip line"
        )

    async def test_never_raises_and_warns_on_noperm(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        redis_client._warn_last.clear()  # deterministic first-warn (see throttle)
        monkeypatch.setattr(
            redis_client,
            "get_async_redis",
            lambda: _BoomAsyncClient(NoPermissionError("NOPERM")),
        )
        result = await publish_best_effort("ava:noperm-async", "{}", context="unit")
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
            n = await publish_best_effort(channel, "{}", context="test")
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
    assert publish_best_effort_sync("ava:nobody", "{}") == 0
