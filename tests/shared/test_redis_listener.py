"""Tests for `RedisInboundListener` — Redis pub/sub inbound wake listener.

Mirrors the wait/wake pattern of `tests/agent/test_db.py`'s `wait_for_inbound`
tests, over Redis pub/sub.

Channel isolation + ACL awareness: the inbound channel is cluster-scoped
(`<prefix>:inbound:<agent_id>`, `shared.cluster.inbound_channel`), and a dev
cluster's redis ACL user is scoped to `&<prefix>:*`. The `TestAclScopedChannel`
suite pins both halves against a real ACL user so the "hardcoded `ava:inbound`
is NOPERM outside `main`" bug is regression-tested at the ACL boundary, and the
listener's degrade-instead-of-crash response is exercised.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
import redis.asyncio as aredis
from redis.exceptions import NoPermissionError

from shared.cluster import inbound_channel, redis_channel_prefix
from shared.config import settings
from shared.redis_listener import RedisInboundListener, WakeFailure, WakeState


async def _publish_inbound(agent_id: int, inbound_id: int = 42) -> None:
    """Publish a wake-up to the agent's (cluster-scoped) Redis channel."""
    from shared.redis_client import sync_redis

    r = sync_redis()
    try:
        r.publish(inbound_channel(agent_id), str(inbound_id))  # pyright: ignore[reportUnknownMemberType]
    finally:
        r.close()


class TestRedisInboundListener:
    """RedisInboundListener basic behavior tests."""

    async def test_ensure_listening_idempotent(self) -> None:
        """`ensure_listening` is safe to call repeatedly."""
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=9999)
        try:
            await listener.ensure_listening()
            await listener.ensure_listening()  # Idempotent — no error
        finally:
            await listener.close()

    async def test_wait_one_wakes_on_publish(self) -> None:
        """`wait_one` returns immediately after Redis publish."""
        agent_id = 9998
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=agent_id)
        try:
            # Start wait_one in background
            started = time.monotonic()
            wait_task = asyncio.create_task(listener.wait_one(timeout=10.0))

            # Give it a moment to subscribe, then publish
            await asyncio.sleep(0.1)
            await _publish_inbound(agent_id)

            await asyncio.wait_for(wait_task, timeout=5.0)
            elapsed = time.monotonic() - started
            # Should wake quickly (well under 10s timeout)
            assert elapsed < 5.0, f"wait_one took {elapsed:.1f}s — publish didn't wake?"
        finally:
            await listener.close()

    async def test_wait_one_times_out(self) -> None:
        """`wait_one` returns after timeout (no publish)."""
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=9997)
        try:
            started = time.monotonic()
            await listener.wait_one(timeout=0.5)
            elapsed = time.monotonic() - started
            # Should return around timeout (with some tolerance)
            assert elapsed < 2.0, f"wait_one took {elapsed:.1f}s for 0.5s timeout"
        finally:
            await listener.close()

    async def test_wait_one_ignores_other_agent(self) -> None:
        """Agent only receives messages from its own channel."""
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=9996)
        try:
            wait_task = asyncio.create_task(listener.wait_one(timeout=3.0))

            # Publish to a DIFFERENT agent's channel
            await _publish_inbound(agent_id=9995)  # Different agent!

            # The wait must NOT wake from the wrong channel. wait_one returns
            # None on BOTH timeout and message (see its docstring), so the old
            # shape (3.0s wait_one inside 5.0s wait_for) passed for the buggy
            # wake-immediately behavior AND the correct time-out behavior
            # alike — a test that could not fail. Assert the task is still
            # pending after a generous window (a wrong-channel wake returns in
            # milliseconds), then bound the legitimate time-out with the outer
            # wait_for so a hang fails the test instead of wedging it
            # (audit round-2 cc-docs-tests P1).
            await asyncio.sleep(0.5)
            assert not wait_task.done(), "listener woke on another agent's publish"
            await asyncio.wait_for(wait_task, timeout=5.0)
        finally:
            await listener.close()

    async def test_subscribes_to_prefixed_channel(self) -> None:
        """The listener subscribes to `inbound_channel(agent_id)`, not a bare
        `ava:inbound:*` — the property the per-cluster ACL relies on."""
        agent_id = 9993
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=agent_id)
        try:
            expected = inbound_channel(agent_id)
            assert listener._channel == expected
            assert expected.endswith(f":inbound:{agent_id}")
            assert expected.startswith(f"{redis_channel_prefix()}:")
        finally:
            await listener.close()

    async def test_reconnect_after_close(self) -> None:
        """`wait_one` auto-reconnects after connection close."""
        agent_id = 9994
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=agent_id)
        try:
            await listener.ensure_listening()
            # Force close the underlying connection
            await listener.close()

            # Next wait_one should transparently reconnect
            wait_task = asyncio.create_task(listener.wait_one(timeout=10.0))
            await asyncio.sleep(0.1)
            await _publish_inbound(agent_id)
            await asyncio.wait_for(wait_task, timeout=5.0)
        finally:
            await listener.close()

    async def test_close_idempotent(self) -> None:
        """close() called before open / called multiple times does not raise — agent finally + eval cleanup
        relies on it being safely no-op (redis version of pre-migration test_db.py::test_close_is_idempotent)."""
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=9992)
        # never opened → close is a no-op
        await listener.close()
        await listener.close()
        # open once, then double close
        await listener.ensure_listening()
        await listener.close()
        await listener.close()

    async def test_concurrent_wait_one_serialised_by_lock(self) -> None:
        """Two concurrent `wait_one` calls do not race on the shared pubsub
        connection.  Before the fix, the second waiter would reach `_consume_one`
        (`pubsub.get_message`) while the first was still blocked in its own
        `_consume_one` on the same connection, so both read the same asyncio
        StreamReader at once — hitting ``RuntimeError: readuntil() called while
        another coroutine is already waiting for incoming data``.  (The
        `_ensure_subscribed` ping uses a separate pooled connection, so the
        collision is get_message vs get_message, not ping vs get_message.)

        With the lock, the second ``wait_one`` blocks until the first releases it,
        keeping the pubsub connection single-consumer.
        """
        agent_id = 9992
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=agent_id)
        try:
            # First wait_one: let it subscribe and enter the consume phase.
            t1 = asyncio.create_task(listener.wait_one(timeout=5.0))
            # Give t1 time to subscribe and enter pubsub.get_message.
            await asyncio.sleep(0.15)

            # Second wait_one: before the fix, once past _ensure_subscribed it would
            # enter _consume_one -> pubsub.get_message() on the same connection while
            # t1 was still blocked there — two reads on one StreamReader, the crash.
            t2 = asyncio.create_task(listener.wait_one(timeout=2.0))

            # Both should complete without crashing.
            _done, _ = await asyncio.wait({t1, t2}, timeout=10.0)
            # At least t2 should finish (t1 may still be waiting).
            assert t2.done(), "Second wait_one should complete"
            # Neither should have raised an exception.
            for t in (t1, t2):
                if t.done():
                    exc = t.exception()
                    assert exc is None, f"wait_one raised: {type(exc).__name__}: {exc}"
            # Clean up
            if not t1.done():
                t1.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await t1
        finally:
            await listener.close()

    async def test_rapid_reentry_does_not_crash(self) -> None:
        """Calling wait_one in a tight sequential loop (the idle-wait recheck loop
        after each timeout) does not crash: every re-entry re-acquires `_wait_lock`,
        re-checks the subscription, and re-enters the consume phase cleanly.
        """
        agent_id = 9991
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=agent_id)
        try:
            # Short timeouts to force rapid re-entry.
            for _ in range(5):
                await listener.wait_one(timeout=0.1)
            # If we got here without crashing, the fix works.
        finally:
            await listener.close()


class TestWaitOneBudgetAndCancel:
    """`wait_one`'s wall-clock budget + task-cleanup contracts — the redis
    successors to the deleted `test_pg_listener_budget.py` (open wedge / consume
    wedge / caller-cancel cleanup). The listener's open + consume phases each
    run under their own bounded abandon backstop (`wait_one` / `_consume_one`,
    `_CONSUME_ABANDON_GRACE`); these pin that the budget holds and no orphan
    consume task is leaked."""

    async def test_wait_one_returns_within_budget_when_open_wedges(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        """When open/subscribe wedges (a connect into a black-holed network sits
        in kernel SYN retries far past any caller timeout), `wait_one` must still
        honour its budget: abandon the open attempt and return, with a warning."""
        listener = RedisInboundListener("redis://unused@127.0.0.1:1/0", agent_id=7001)
        opened = asyncio.Event()

        async def _wedged_open() -> object:
            opened.set()
            await asyncio.sleep(3600)  # connect black-holed; cancellable cleanup
            raise AssertionError("unreachable")

        monkeypatch.setattr(listener, "_ensure_subscribed", _wedged_open)

        t0 = asyncio.get_running_loop().time()
        await listener.wait_one(timeout=0.3)
        elapsed = asyncio.get_running_loop().time() - t0

        assert opened.is_set(), "the open attempt must actually have started"
        assert elapsed < 2.0, f"wait_one took {elapsed:.2f}s for a 0.3s budget — contract violated"
        assert any("did not complete within" in r["message"] for r in loguru_records)
        await asyncio.sleep(0)  # let the cancelled open task unwind before loop close

    async def test_wait_one_abandons_and_warns_when_consume_never_ticks(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        """A consume that never starts its own get_message timer (parked before
        it can tick) is abandoned within budget + `_CONSUME_ABANDON_GRACE`, with a
        warning — the genuinely-stuck case the grace-padded backstop targets."""
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=7002)

        async def _instant_open() -> object:
            return object()  # stand-in pubsub, only handed to the wedged consume

        started = asyncio.Event()

        async def _wedged_consume(pubsub: object, timeout: float) -> None:
            started.set()
            await asyncio.sleep(3600)  # never ticks a deadline; cancellable

        monkeypatch.setattr(listener, "_ensure_subscribed", _instant_open)
        monkeypatch.setattr(listener, "_consume_one", _wedged_consume)

        t0 = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(listener.wait_one(timeout=0.3), timeout=8.0)
        except TimeoutError:
            pytest.fail("wait_one hung past 8s on a 0.3s budget — abandon backstop lost")
        elapsed = asyncio.get_running_loop().time() - t0

        assert started.is_set(), "the consume attempt must actually have started"
        assert elapsed < 5.0, f"wait_one took {elapsed:.2f}s for a 0.3s budget + 2s grace"
        assert any(
            "consume never started its timer" in r["message"] and r["level"].no >= 30  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            for r in loguru_records
        )
        await asyncio.sleep(0)  # let the cancelled consume task unwind before loop close

    async def test_wait_one_consume_task_not_leaked_on_caller_cancel(self) -> None:
        """Cancelling `wait_one` mid-consume must not leave an orphaned consumer on
        the shared pubsub — a leaked `get_message` would race the next wait's
        consume and could swallow its wake. (PG version pinned this via the shared
        conn lock; here it's the shared pubsub.) Proof: a fresh wait on the same
        listener still wakes on a publish after the cancel."""
        agent_id = 7003
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id)
        try:
            await listener.ensure_listening()
            waiter = asyncio.create_task(listener.wait_one(timeout=30.0))
            await asyncio.sleep(0.3)  # settle into the consume
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            await asyncio.sleep(0.1)  # give any leaked consumer a beat to surface

            # A fresh wait on the same listener still wakes on publish — proof the
            # cancelled consume released the shared pubsub instead of leaking.
            wait_task = asyncio.create_task(listener.wait_one(timeout=10.0))
            await asyncio.sleep(0.1)
            t0 = time.monotonic()
            await _publish_inbound(agent_id)
            await asyncio.wait_for(wait_task, timeout=5.0)
            assert time.monotonic() - t0 < 3.0, (
                "publish did not wake after a prior cancel — consumer leaked"
            )
        finally:
            await listener.close()


class TestWakeHealth:
    """Wake-path health episodes across abandon, breadcrumb, and recovery paths."""

    async def _set_wake_key(self, agent_id: int, payload: str = "42") -> None:
        from shared.cluster import WAKE_KEY_TTL_S, wake_key
        from shared.redis_client import sync_redis

        r = sync_redis()
        try:
            r.set(wake_key(agent_id), payload, ex=WAKE_KEY_TTL_S)
        finally:
            r.close()

    async def test_clean_idle_timeout_stays_healthy(
        self,
        loguru_records: list[dict],
    ) -> None:
        """An ordinary idle timeout does not start a wake-degradation episode.

        Removing the clean-consume distinction would mark every quiet wait as
        degraded and emit needless wake-health events.
        """
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=6988)
        try:
            await listener.wait_one(timeout=0.3)
            assert listener.wake_state is WakeState.HEALTHY
            assert listener.wake_degrade_reason is None
            assert listener.wake_degraded_s is None
        finally:
            await listener.close()
        assert not any("wake path degraded" in record["message"] for record in loguru_records)

    async def test_getdel_timeout_degrades_drops_and_recovers(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        """A stuck GETDEL drops stale handles, re-delivers the wake key, and recovers.

        Without the close, the next wait reuses the black-holed command socket;
        without the clean-consume transition, recovery stays falsely degraded.
        """
        agent_id = 6987
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=agent_id)

        async def _hung_getdel(*args: object, **kwargs: object) -> object:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

        try:
            await listener.ensure_listening()
            await self._set_wake_key(agent_id)
            redis = listener._redis
            assert redis is not None
            monkeypatch.setattr(redis, "getdel", _hung_getdel)

            started = time.monotonic()
            await listener.wait_one(timeout=0.5)
            assert time.monotonic() - started < 2.0
            assert listener.wake_state is WakeState.DEGRADED
            assert listener.wake_degrade_reason == WakeFailure.GETDEL_TIMEOUT.value
            assert listener.wake_degraded_s is not None
            assert listener._redis is None
            assert listener._pubsub is None

            # A fresh client consumes the breadcrumb left by the abandoned
            # GETDEL, so the caller's SELECT recheck runs without the full wait.
            started = time.monotonic()
            await listener.wait_one(timeout=10.0)
            assert time.monotonic() - started < 3.0
            await listener.wait_one(timeout=0.5)
            assert listener.wake_state is WakeState.HEALTHY
            assert listener.wake_degrade_reason is None
            assert listener.wake_degraded_s is None
        finally:
            await listener.close()

        assert any(
            "wake path degraded (getdel_timeout)" in record["message"] and record["level"].no >= 30  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            for record in loguru_records
        )
        assert any("wake path recovered" in record["message"] for record in loguru_records)

    async def test_consume_abandon_degrades_and_drops_connection(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unreadable pubsub socket is closed so the next wait reconnects.

        Keeping the old socket makes each wait burn its full budget even when
        ping succeeds, leaving instant wake unavailable indefinitely.
        """
        agent_id = 6986
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=agent_id)

        async def _hung_consume(pubsub: object, timeout: float) -> None:
            await asyncio.sleep(3600)

        try:
            await listener.ensure_listening()
            with monkeypatch.context() as patch:
                patch.setattr(listener, "_consume_one", _hung_consume)
                started = time.monotonic()
                await listener.wait_one(timeout=0.3)
                assert time.monotonic() - started < 5.0

            assert listener.wake_state is WakeState.DEGRADED
            assert listener.wake_degrade_reason == WakeFailure.CONSUME_ABANDON.value
            assert listener._redis is None
            assert listener._pubsub is None

            await listener.ensure_listening()
            wait_task = asyncio.create_task(listener.wait_one(timeout=10.0))
            await _publish_inbound(agent_id)
            await asyncio.wait_for(wait_task, timeout=5.0)
            assert listener.wake_state is WakeState.HEALTHY
        finally:
            await listener.close()

    async def test_open_abandon_marks_degraded(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An open attempt that exceeds the wait budget marks wake degraded.

        Omitting this transition leaves the caller healthy-looking while the
        subscription cannot be established.
        """
        listener = RedisInboundListener("redis://unused@127.0.0.1:1/0", agent_id=6985)
        started = asyncio.Event()

        async def _wedged_open() -> object:
            started.set()
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

        monkeypatch.setattr(listener, "_ensure_subscribed", _wedged_open)
        try:
            started_at = time.monotonic()
            await listener.wait_one(timeout=0.3)
            assert time.monotonic() - started_at < 2.0
            assert started.is_set()
            assert listener.wake_state is WakeState.DEGRADED
            assert listener.wake_degrade_reason == WakeFailure.OPEN_ABANDON.value
        finally:
            await listener.close()
        await asyncio.sleep(0)


class TestInboundChannelPrefix:
    """`inbound_channel` maps cluster prefix → per-agent wake channel."""

    def test_prefix_derivation_main_and_dev(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # main cluster: bare `ava` prefix (the historical hardcoded shape)
        monkeypatch.setattr(settings.data_plane, "events_channel", "ava:events")
        assert redis_channel_prefix() == "ava"
        assert inbound_channel(7) == "ava:inbound:7"
        # dev cluster: `ava:<cluster>` prefix — the case the hardcode broke
        monkeypatch.setattr(settings.data_plane, "events_channel", "ava:mycluster:events")
        assert redis_channel_prefix() == "ava:mycluster"
        assert inbound_channel(7) == "ava:mycluster:inbound:7"


def _scoped_user_url(user: str) -> str:
    """A redis URL on the session's throwaway redis authenticating as ACL user
    `user` (password == username)."""
    parts = urlsplit(settings.data_plane.redis_url)
    netloc = f"{user}:{user}@{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _make_scoped_user(user: str, channel_grant: str) -> None:
    """Create/replace an ACL user scoped to `channel_grant` (e.g. `&ava:x:*`) —
    full key + command access, password == username. `resetchannels` clears the
    default channel grant first, so the user reaches ONLY the granted pattern,
    modelling `ensure_cluster_redis_acl`'s `resetchannels &<prefix>:*`."""
    from shared.redis_client import sync_redis

    r = sync_redis()
    try:
        r.execute_command(
            "ACL",
            "SETUSER",
            user,
            "on",
            f">{user}",
            "resetkeys",
            "~*",
            "resetchannels",
            channel_grant,
            "+@all",
        )
    finally:
        r.close()


class TestAclScopedChannel:
    """The per-cluster ACL interaction — the dev-cluster NOPERM path."""

    async def test_legacy_unprefixed_channel_denied_by_dev_acl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dev cluster's ACL user (`&ava:<cluster>:*`) REJECTS the old hardcoded
        `ava:inbound:*` yet PERMITS `inbound_channel` — the exact NOPERM boundary
        that crashed dev-cluster agents before the prefix fix.

        Redis surfaces the denial lazily: `subscribe` returns, and the
        `NoPermissionError` lands on the first `get_message` (the path the
        listener actually walks), so the assertion is made there."""
        monkeypatch.setattr(settings.data_plane, "events_channel", "ava:acltest:events")
        _make_scoped_user("acl_dev", "&ava:acltest:*")
        client = aredis.Redis.from_url(_scoped_user_url("acl_dev"), decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
        try:
            # Old hardcoded name is outside the &ava:acltest:* grant → NOPERM on read.
            denied = client.pubsub(ignore_subscribe_messages=True)  # pyright: ignore[reportUnknownMemberType]
            await denied.subscribe(f"ava:inbound:{5}")
            with pytest.raises(NoPermissionError):
                await denied.get_message(timeout=1.0)
            await denied.aclose()
            # The cluster-prefixed channel is inside the grant → no NOPERM.
            allowed = client.pubsub(ignore_subscribe_messages=True)  # pyright: ignore[reportUnknownMemberType]
            await allowed.subscribe(inbound_channel(5))
            assert await allowed.get_message(timeout=1.0) is None  # timeout, not error
            await allowed.aclose()
        finally:
            await client.aclose()

    async def test_listener_wakes_under_scoped_acl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A listener authenticating as the dev-cluster-scoped ACL user subscribes
        to `inbound_channel` (inside `&ava:acltest:*`) and wakes on a publish — the
        prefix fix makes wake actually work on a dev cluster."""
        monkeypatch.setattr(settings.data_plane, "events_channel", "ava:acltest:events")
        _make_scoped_user("acl_dev2", "&ava:acltest:*")
        agent_id = 8123
        listener = RedisInboundListener(_scoped_user_url("acl_dev2"), agent_id)
        try:
            wait_task = asyncio.create_task(listener.wait_one(timeout=10.0))
            await asyncio.sleep(0.1)
            # Default user publishes; the channel is the same prefixed name.
            await _publish_inbound(agent_id)
            await asyncio.wait_for(wait_task, timeout=5.0)
        finally:
            await listener.close()

    async def test_listener_degrades_when_subscribe_denied(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        """When subscribe is NOPERM (the ACL grants a DIFFERENT prefix), neither
        `ensure_listening` nor `wait_one` raises `NoPermissionError` — the listener
        logs once and `wait_one` sleeps out its budget then returns, so
        `wait_for_inbound`'s SELECT recheck still delivers wakes at the normal
        cadence instead of the agent crashing on idle-wait."""
        monkeypatch.setattr(settings.data_plane, "events_channel", "ava:acltest:events")
        # Scoped to a DIFFERENT prefix → inbound_channel (ava:acltest:*) is denied.
        _make_scoped_user("acl_wrong", "&ava:other:*")
        listener = RedisInboundListener(_scoped_user_url("acl_wrong"), agent_id=8124)
        try:
            # Must not raise — degrades to a no-op subscribe.
            await listener.ensure_listening()
            started = time.monotonic()
            await listener.wait_one(timeout=0.5)  # must not raise NoPermissionError
            elapsed = time.monotonic() - started
            # Degraded path sleeps out the budget then returns (no crash, no hot loop).
            assert 0.4 < elapsed < 3.0, f"degraded wait_one elapsed {elapsed:.2f}s"
            # Instant wake is genuinely off for the episode — the wake-health view
            # must say DEGRADED, not look healthy (fake-alive).
            assert listener.wake_state is WakeState.DEGRADED
            assert listener.wake_degrade_reason == WakeFailure.ACL_DENIED.value
        finally:
            await listener.close()
        assert any("rejected by redis" in r["message"] for r in loguru_records), (
            "expected an ACL-denial WARNING to be logged"
        )


class TestWakeKeyBreadcrumb:
    """The durable wake-key breadcrumb (`shared.cluster.wake_key`) — the
    publisher SETEXes it alongside every pub/sub wake; the listener GETDELs it
    after (re)subscribing so a wake lost to pub/sub's fire-and-forget semantics
    (publish landed while this connection was down / not yet subscribed) still
    makes the caller's SELECT recheck run immediately instead of after the full
    wait budget."""

    async def _set_wake_key(self, agent_id: int, payload: str = "42") -> None:
        from shared.cluster import WAKE_KEY_TTL_S, wake_key
        from shared.redis_client import sync_redis

        r = sync_redis()
        try:
            r.set(wake_key(agent_id), payload, ex=WAKE_KEY_TTL_S)
        finally:
            r.close()

    async def _get_wake_key(self, agent_id: int) -> str | None:
        from shared.cluster import wake_key
        from shared.redis_client import sync_redis

        r = sync_redis(decode_responses=True)
        try:
            val = r.get(wake_key(agent_id))
            return val.decode() if isinstance(val, bytes) else val
        finally:
            r.close()

    async def test_wait_one_returns_immediately_when_wake_key_set(self) -> None:
        """A wake key present before `wait_one` (the lost-publish window: the
        publisher SETEXed but the pub/sub message never arrived) makes
        `wait_one` return right after subscribing instead of sleeping out its
        budget — the caller's SELECT then finds the pending row."""
        agent_id = 6991
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=agent_id)
        try:
            await self._set_wake_key(agent_id)
            t0 = time.monotonic()
            await listener.wait_one(timeout=10.0)
            elapsed = time.monotonic() - t0
            assert elapsed < 3.0, (
                f"wait_one took {elapsed:.1f}s with a wake key set — breadcrumb not consumed"
            )
        finally:
            await listener.close()

    async def test_wake_key_consumed_on_wait_one(self) -> None:
        """`wait_one` GETDELs the key, so a consumed breadcrumb does not
        re-trigger on every subsequent wait."""
        agent_id = 6990
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=agent_id)
        try:
            await self._set_wake_key(agent_id)
            await listener.wait_one(timeout=5.0)
            assert await self._get_wake_key(agent_id) is None, (
                "wake key not consumed — would re-trigger a SELECT on every wait"
            )
        finally:
            await listener.close()

    async def test_wake_key_wakes_listener_after_reconnect_gap(self) -> None:
        """The prod-shaped regression: the listener's connection is dead when
        the publish lands (simulated: publish the wake key with no live
        listener), and the next `wait_one` — which reconnects — must return
        promptly via the breadcrumb."""
        agent_id = 6989
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=agent_id)
        try:
            # No subscription yet (the "disconnected" state).
            await self._set_wake_key(agent_id)
            t0 = time.monotonic()
            await listener.wait_one(timeout=10.0)
            elapsed = time.monotonic() - t0
            assert elapsed < 3.0, (
                f"reconnect + wake key took {elapsed:.1f}s — lost wake not recovered fast"
            )
        finally:
            await listener.close()

    async def test_publish_sets_wake_key_too(self) -> None:
        """The publisher writes the key alongside the pub/sub message (pinned
        at the `shared.db.publish_inbound_wake` boundary via
        `insert_inbound_message`), so a wake that the listener DOES receive
        leaves a breadcrumb for the next reconnect window too."""
        from shared.db import create_agent, insert_inbound_message

        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
            agent_id = create_agent(conn)
            insert_inbound_message(conn, agent_id, "wake", "user")
        assert await self._get_wake_key(agent_id) is not None, (
            "insert_inbound_message did not SETEX the wake key"
        )
        # cleanup
        from shared.cluster import wake_key
        from shared.redis_client import sync_redis

        r = sync_redis()
        try:
            r.delete(wake_key(agent_id))
        finally:
            r.close()


class TestDeadTransportSurvival:
    """The agent-2613 outage shape (2026-08-04): the pubsub connection's
    asyncio transport died mid-wait (connection_lost fired, redis-py not
    told). The health-check PING then wrote the dead transport and raised
    `TypeError: 'NoneType' object is not callable`, which escaped the
    listener's except chain and crashed the agent's claim node. Both the
    transport-aware connection and the TypeError catch must keep
    `wait_one` alive and recovering."""

    async def test_wait_one_survives_transport_death_and_recovers(self) -> None:
        """A transport killed under a parked listener must not raise out of
        `wait_one`; the next `wait_one` reconnects and wakes on publish."""
        agent_id = 6998
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=agent_id)
        try:
            await listener.ensure_listening()
            pubsub = listener._pubsub
            assert pubsub is not None
            conn = pubsub.connection
            assert conn is not None
            writer = conn._writer
            assert writer is not None
            transport = writer.transport
            transport.abort()
            await asyncio.sleep(0.1)
            conn.next_health_check = 0.0  # force the health-check PING write path

            # Must not raise — before the fix this was TypeError (the crash).
            await listener.wait_one(timeout=1.0)

            # Recovery: a publish wakes the re-subscribed listener.
            wait_task = asyncio.create_task(listener.wait_one(timeout=10.0))
            await asyncio.sleep(0.1)
            await _publish_inbound(agent_id)
            await asyncio.wait_for(wait_task, timeout=5.0)
        finally:
            await listener.close()

    async def test_wait_one_treats_typeerror_as_conn_lost(
        self,
        monkeypatch: pytest.MonkeyPatch,
        loguru_records: list[dict],
    ) -> None:
        """Defence in depth: even if redis-py surfaces the dead-transport
        TypeError again (the is_connected check -> write race window),
        `wait_one` treats it as a lost connection — close, backoff, retry —
        instead of letting it kill the agent process."""
        agent_id = 6997
        listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=agent_id)
        calls = {"n": 0}

        async def _flaky_consume(pubsub: object, timeout: float) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise TypeError("'NoneType' object is not callable")
            # Second attempt (after reconnect) consumes normally: return
            # immediately (as a clean consume would on timeout).

        monkeypatch.setattr(listener, "_consume_one", _flaky_consume)
        try:
            await listener.wait_one(timeout=2.0)  # must not raise
        finally:
            await listener.close()
        assert calls["n"] >= 2, "listener should have retried after the TypeError"
        assert any("conn lost / open failed" in r["message"] for r in loguru_records), (
            "expected a conn-lost WARNING for the TypeError"
        )
