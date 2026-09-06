"""shared.redis_client: per-loop singleton + uses settings.data_plane.redis_url."""

from __future__ import annotations

import asyncio
import socket
import threading
from typing import Any

import pytest
import redis.asyncio as aredis
from redis.asyncio.connection import AbstractConnection
from redis.exceptions import AuthenticationError, NoPermissionError
from redis.exceptions import ConnectionError as RedisConnectionError

from shared import redis_client as mod
from shared.config import settings
from shared.redis_client import _TransportAwareAsyncConnection


@pytest.fixture(autouse=True)
def _clear_loop_clients() -> None:
    """Each test starts with an empty per-loop registry; otherwise a cached
    client from a previous test would survive into the next loop."""
    mod._clients.clear()


async def test_same_loop_returns_same_instance() -> None:
    a = mod.get_async_redis()
    b = mod.get_async_redis()
    assert a is b, "two calls in the same event loop must return the same client"


async def test_open_async_redis_pins_socket_timeout_none() -> None:
    """redis-py >= 5 defaults socket_timeout to 5s; a pub/sub listener's long
    blocking read must not be cut by it (the hosted dispatcher reconnect-looped
    every 5s of idle until pinned to None — 2026-08-30 soak startup)."""
    client = mod.open_async_redis(settings.data_plane.redis_url)
    kwargs = client.connection_pool.connection_kwargs  # pyright: ignore[reportUnknownMemberType]
    # `in + is None`, not `.get()`: a MISSING key would silently pass the same
    # assertion while the redis-py 5s default quietly applies.
    assert "socket_timeout" in kwargs
    assert kwargs["socket_timeout"] is None
    await client.aclose()


async def test_uses_settings_redis_url() -> None:
    client = mod.get_async_redis()
    pool = client.connection_pool
    parsed_host = pool.connection_kwargs.get("host")  # pyright: ignore[reportUnknownMemberType]
    parsed_port = pool.connection_kwargs.get("port")  # pyright: ignore[reportUnknownMemberType]
    assert parsed_host is not None
    assert parsed_port is not None
    assert str(parsed_port) in settings.data_plane.redis_url  # pyright: ignore[reportUnknownArgumentType]


def test_get_async_redis_requires_running_loop() -> None:
    """Calling outside an async context fails fast (asyncio.get_running_loop raises)."""
    with pytest.raises(RuntimeError):
        mod.get_async_redis()


class _EventuallyAsyncPublisher:
    """Publish stand-in that holds auth failures until a configured attempt."""

    def __init__(self, error: type[Exception], failures: int) -> None:
        self._error = error
        self._failures = failures
        self.attempts = 0

    async def publish(self, _channel: str, _payload: str) -> int:
        self.attempts += 1
        if self.attempts <= self._failures:
            raise self._error("ACL is being re-affirmed")
        return 3


class _HalfOpenHealthCheckPublisher:
    """PUBLISH parked in redis-py's pre-command health-check response read."""

    def __init__(self) -> None:
        self.read_started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def publish(self, _channel: str, _payload: str) -> int:
        self.read_started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return 3


class _EventuallySyncPublisher:
    """Synchronous equivalent used to lock the one-off client contract."""

    def __init__(self, failures: int) -> None:
        self._failures = failures
        self.attempts = 0

    def publish(self, _channel: str, _payload: str) -> int:
        self.attempts += 1
        if self.attempts <= self._failures:
            raise AuthenticationError("ACL is being re-affirmed")
        return 3

    def close(self) -> None:
        pass


def _max_jitter(delay_cap: float) -> float:
    return delay_cap


@pytest.mark.parametrize("error", (AuthenticationError, NoPermissionError))
async def test_publish_retries_auth_failures_with_bounded_exponential_delays(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    publisher = _EventuallyAsyncPublisher(error, failures=2)
    delays: list[float] = []

    async def _record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(mod, "get_async_redis", lambda: publisher)
    monkeypatch.setattr(mod, "_sleep_async", _record_sleep, raising=False)
    monkeypatch.setattr(mod, "_auth_retry_jitter", _max_jitter, raising=False)

    assert await mod.publish_best_effort("ava:events", "payload") == 3
    assert publisher.attempts == 3
    assert delays == [0.5, 1.0]


async def test_publish_auth_retry_stops_within_its_total_wait_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = _EventuallyAsyncPublisher(AuthenticationError, failures=100)
    delays: list[float] = []

    async def _record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(mod, "get_async_redis", lambda: publisher)
    monkeypatch.setattr(mod, "_sleep_async", _record_sleep, raising=False)
    monkeypatch.setattr(mod, "_auth_retry_jitter", _max_jitter, raising=False)

    assert await mod.publish_best_effort("ava:events", "payload") is None
    assert publisher.attempts > 1
    assert sum(delays) <= 60.0
    assert max(delays) <= 10.0


async def test_publish_does_not_retry_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = _EventuallyAsyncPublisher(RedisConnectionError, failures=100)
    delays: list[float] = []

    async def _record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(mod, "get_async_redis", lambda: publisher)
    monkeypatch.setattr(mod, "_sleep_async", _record_sleep, raising=False)

    assert await mod.publish_best_effort("ava:events", "payload") is None
    assert publisher.attempts == 1
    assert delays == []


async def test_best_effort_publish_bounds_a_half_open_health_check_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reused connection can accept the PUBLISH write and then park forever
    reading its health-check PONG. The best-effort command owns a short attempt
    bound even though the shared socket timeout stays None for pub/sub.

    ``asyncio.wait`` observes the regression without cancelling it; if the
    production bound is removed, the release lets the test finish and fail
    instead of hanging alongside the bug.
    """
    publisher = _HalfOpenHealthCheckPublisher()
    monkeypatch.setattr(mod, "get_async_redis", lambda: publisher)
    monkeypatch.setattr(mod, "_BEST_EFFORT_PUBLISH_ATTEMPT_TIMEOUT_S", 0.01, raising=False)

    task = asyncio.create_task(mod.publish_best_effort("ava:events", "payload"))
    await asyncio.wait_for(publisher.read_started.wait(), timeout=1.0)
    done, _ = await asyncio.wait({task}, timeout=0.5)
    finished_within_bound = bool(done)
    if not finished_within_bound:
        publisher.release.set()
        await task

    assert finished_within_bound, "a half-open PUBLISH escaped its operation-level bound"
    assert task.result() is None
    assert publisher.cancelled


def test_sync_publish_retries_auth_failures_with_bounded_exponential_delays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = _EventuallySyncPublisher(failures=2)
    delays: list[float] = []

    def _record_sleep(delay: float) -> None:
        delays.append(delay)

    def _fake_sync_redis(*, decode_responses: bool = False) -> _EventuallySyncPublisher:
        return publisher

    monkeypatch.setattr(mod, "sync_redis", _fake_sync_redis)
    monkeypatch.setattr(mod, "_sleep_sync", _record_sleep, raising=False)
    monkeypatch.setattr(mod, "_auth_retry_jitter", _max_jitter, raising=False)

    assert mod.publish_best_effort_sync("ava:events", "payload") == 3
    assert publisher.attempts == 3
    assert delays == [0.5, 1.0]


async def test_different_loops_get_different_clients() -> None:
    """Inside this loop we hold one client; a separately-run nested loop binds
    its own client and does not collide with ours."""
    here = mod.get_async_redis()

    captured: dict[str, object] = {}

    def _inside_other_loop() -> None:
        async def _go() -> None:
            captured["other"] = mod.get_async_redis()

        asyncio.run(_go())

    import threading

    t = threading.Thread(target=_inside_other_loop)
    t.start()
    t.join()
    assert captured["other"] is not here, (
        "a separate event loop must receive its own client (per-loop semantics)"
    )


# ── weak-network resilience: central redis/pubsub over a flaky corp link must
# detect a dead connection fast and self-heal idle pubsub conns, instead of
# hanging publish to the OS default TCP timeout (minutes). See F2. ──


def _assert_resilient_kwargs(kwargs: dict) -> None:
    # TCP keepalive: probe a half-dead link in tens of seconds, not minutes.
    assert kwargs.get("socket_keepalive") is True  # pyright: ignore[reportUnknownMemberType]
    # health_check_interval: PING an idle conn before reuse so a stale pubsub /
    # publish conn reconnects transparently rather than dying silently.
    assert kwargs.get("health_check_interval", 0) > 0  # pyright: ignore[reportUnknownMemberType]
    # connect timeout bounds the TLS-MITM handshake.
    assert kwargs.get("socket_connect_timeout", 0) > 0  # pyright: ignore[reportUnknownMemberType]
    # socket_timeout is pinned to None explicitly — a blanket read timeout
    # would periodically cut pubsub.listen()'s long blocking read (e.g. the
    # SSE event stream). `in + is None`, not `.get()`: a missing key would
    # silently pass while the redis-py 5s default quietly applies.
    assert "socket_timeout" in kwargs
    assert kwargs["socket_timeout"] is None


async def test_async_client_weak_network_resilient() -> None:
    client = mod.get_async_redis()
    _assert_resilient_kwargs(client.connection_pool.connection_kwargs)  # pyright: ignore[reportUnknownMemberType]


def test_sync_client_weak_network_resilient() -> None:
    client = mod.sync_redis()
    try:
        _assert_resilient_kwargs(client.connection_pool.connection_kwargs)
    finally:
        client.close()


# ── IPv4-literal dial pinning (_PinnedIPv4Connection): a DNS64/NAT64 network
# can synthesize an AAAA answer even for a getaddrinfo() call on a plain IPv4
# literal (shared.netutil.is_ipv4_literal); redis-py's sync
# Connection._connect calls getaddrinfo unconditionally (unlike
# redis.asyncio, which goes through asyncio's own literal-aware
# _ensure_resolved), so the sync client needs its own bypass. ──


def test_sync_redis_uses_pinned_connection_class() -> None:
    client = mod.sync_redis()
    try:
        assert client.connection_pool.connection_class is mod._PinnedIPv4Connection
    finally:
        client.close()


class TestPinnedIPv4ConnectionConnect:
    def test_ipv4_literal_never_calls_getaddrinfo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        accepted: list[socket.socket] = []

        def serve() -> None:
            conn, _ = server.accept()
            accepted.append(conn)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        def _boom(*_a: object, **_kw: object) -> None:
            raise AssertionError("socket.getaddrinfo must not be called for an IPv4 literal")

        monkeypatch.setattr(socket, "getaddrinfo", _boom)

        conn = mod._PinnedIPv4Connection(
            host="127.0.0.1", port=port, socket_connect_timeout=3, socket_timeout=3
        )
        sock = conn._connect()
        try:
            thread.join(timeout=3)
            assert sock.getpeername()[0] == "127.0.0.1"
        finally:
            sock.close()
            server.close()
            for c in accepted:
                c.close()

    def test_hostname_falls_through_to_default_connect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A non-literal host must still take the stock (getaddrinfo-based) path —
        # this only ever changes behavior for an IPv4 literal.
        called: dict[str, object] = {}

        def fake_super_connect(self: object) -> str:
            called["host"] = self.host  # type: ignore[attr-defined]
            return "stub-socket"

        monkeypatch.setattr("redis.Connection._connect", fake_super_connect, raising=True)
        conn = mod._PinnedIPv4Connection(host="redis.example.com", port=6379)
        assert conn._connect() == "stub-socket"
        assert called["host"] == "redis.example.com"


# ── dead-transport detection (_TransportAwareAsyncConnection): after a network
# outage, asyncio fires `connection_lost` on a redis connection's transport
# (nulling `_SelectorSocketTransport._write_ready`), but redis-py's
# `Connection.is_connected` still returns True (it only checks _reader/_writer
# are non-None). The next write then raises
# `TypeError: 'NoneType' object is not callable` — not an OSError/TimeoutError,
# so redis-py's except chain lets it escape; on the pubsub health-check path it
# killed an agent's claim node (agent 2613 on laptop-host, 2026-08-04).
# `is_connected` also treating a closing transport as disconnected makes
# redis-py take its own reconnect path instead of writing a dead transport. ──


async def _abort_transport(conn: AbstractConnection) -> None:
    """Simulate a network outage: asyncio's transport fires connection_lost
    (the writer's `_write_ready` is nulled), while redis-py's Connection is
    not told — the exact state behind the agent-2613 crash."""
    writer = conn._writer
    assert writer is not None
    transport = writer.transport
    assert transport is not None
    transport.abort()
    # Let the connection_lost callback run.
    await asyncio.sleep(0.1)


def _make_client(**kwargs: Any) -> aredis.Redis:
    return aredis.Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.data_plane.redis_url,
        decode_responses=True,
        health_check_interval=30,
        **kwargs,
    )


class TestTransportAwareAsyncConnection:
    """`is_connected` sees an asyncio transport that already died."""

    async def test_is_connected_false_after_transport_abort(self) -> None:
        client = _make_client(connection_class=_TransportAwareAsyncConnection)
        await client.ping()  # pyright: ignore[reportUnknownMemberType]
        pool = client.connection_pool
        conn = pool._available_connections[0]
        assert isinstance(conn, _TransportAwareAsyncConnection)
        assert conn.is_connected

        await _abort_transport(conn)
        # redis-py's own is_connected stays True (the bug), ours sees the death.
        assert not conn.is_connected
        assert aredis.Connection.is_connected.fget(conn)  # type: ignore[attr-defined]
        await client.aclose()

    async def test_vanilla_connection_still_reports_connected(self) -> None:
        """Control: without the subclass, the same outage leaves
        `is_connected=True` — the exact state that produced the TypeError."""
        client = _make_client()
        await client.ping()  # pyright: ignore[reportUnknownMemberType]
        conn = client.connection_pool._available_connections[0]
        await _abort_transport(conn)
        assert conn.is_connected
        await client.aclose()


class TestDeadTransportRecovery:
    """End-to-end: a transport that died mid-flight no longer raises the
    TypeError and recovers (pool publish and pubsub read paths)."""

    async def test_pool_publish_recovers_after_transport_death(self) -> None:
        client = _make_client(connection_class=_TransportAwareAsyncConnection)
        await client.ping()  # pyright: ignore[reportUnknownMemberType]
        conn = client.connection_pool._available_connections[0]
        await _abort_transport(conn)

        # Before the fix this raised TypeError('NoneType' object is not
        # callable) from `_send_packed_command`; now it reconnects.
        receivers = await client.publish("ava:test:dead-transport", "hello")  # pyright: ignore[reportUnknownMemberType]
        assert receivers == 0
        # The pool connection was rebuilt, not poisoned.
        assert conn.is_connected
        assert await client.ping() is True  # pyright: ignore[reportUnknownMemberType]
        await client.aclose()

    async def test_pubsub_get_message_recovers_after_transport_death(self) -> None:
        """The exact agent-2613 shape: a parked pubsub whose transport died
        mid-read. `parse_response` -> check_health -> PING used to write the
        dead transport and raise TypeError; now it reconnects and re-subscribes,
        and a post-recovery publish still wakes the consumer."""
        client = _make_client(connection_class=_TransportAwareAsyncConnection)
        pubsub = client.pubsub(ignore_subscribe_messages=True)  # pyright: ignore[reportUnknownMemberType]
        await pubsub.subscribe("ava:test:dead-transport-pubsub")
        conn = pubsub.connection
        assert isinstance(conn, _TransportAwareAsyncConnection)

        await _abort_transport(conn)
        # Force the health-check PING to fire on the next parse_response (the
        # write path that hit the dead transport).
        conn.next_health_check = 0.0

        # Must not raise TypeError — before the fix this was the crash.
        msg = await asyncio.wait_for(pubsub.get_message(timeout=1.0), timeout=10)  # pyright: ignore[reportUnknownArgumentType]
        assert msg is None  # timeout, cleanly — reconnect happened internally

        # Recovery: a publish now reaches the (re-subscribed) pubsub. Read in
        # a loop like the listener's `_consume_one` — the first read after
        # reconnect may consume the health-check PONG (returned as None).
        publisher = _make_client()
        await publisher.publish("ava:test:dead-transport-pubsub", "wake!")  # pyright: ignore[reportUnknownMemberType]
        msg = None
        for _ in range(5):
            msg = await asyncio.wait_for(pubsub.get_message(timeout=1.0), timeout=10)  # pyright: ignore[reportUnknownArgumentType]
            if msg is not None and msg.get("data") == "wake!":  # pyright: ignore[reportUnknownMemberType]
                break
        assert msg is not None and msg.get("data") == "wake!", (  # pyright: ignore[reportUnknownMemberType]
            "post-recovery publish not received — resubscribe failed?"
        )
        await publisher.aclose()
        await client.aclose()

    async def test_vanilla_pubsub_raises_on_dead_transport_write(self) -> None:
        """Control: the same scenario on the stock connection class raises out
        of the dead-transport write (macOS: TypeError 'NoneType' object is not
        callable, the exact incident exception; Linux: AttributeError
        '_add_writer' — the write path falls through differently, but both are
        the un-fixed "write a connection_lost transport" crash)."""
        client = _make_client()
        pubsub = client.pubsub(ignore_subscribe_messages=True)  # pyright: ignore[reportUnknownMemberType]
        await pubsub.subscribe("ava:test:dead-transport-pubsub")
        conn = pubsub.connection
        assert conn is not None
        await _abort_transport(conn)
        conn.next_health_check = 0.0
        with pytest.raises((TypeError, AttributeError)):
            await asyncio.wait_for(pubsub.get_message(timeout=1.0), timeout=10)  # pyright: ignore[reportUnknownArgumentType]
        await client.aclose()
