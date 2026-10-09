"""The Redis transport: client classes with the cluster's resilience settings, ACL-transition
auth retry, and the best-effort publish discipline.

Nothing here reads the settings: a caller names the URL (`open_async_redis`, `open_sync_redis`)
or goes through `EventBus` (`base.events.live.bus`), which holds the cluster URL and events
channel and hands out one shared async client per event loop. `aredis.Redis` binds its
connections to the running loop at first use, so the shared client is per loop; callers must
not `aclose()` it.
"""

from __future__ import annotations

import asyncio
import random
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

import redis as _redis_sync
import redis.asyncio as aredis
from redis.exceptions import AuthenticationError, NoPermissionError, ResponseError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from base.events.live.redis_resilience import (
    _HEALTH_CHECK_INTERVAL_S,
    _SOCKET_CONNECT_TIMEOUT_S,
    keepalive_options,
)
from base.host.net.predicates import is_ipv4_literal
from base.log import logger

# weak-network resilience (F2):
# The central Postgres/Redis sit behind a TLS-MITM corp link; a half-dead
# connection must surface in tens of seconds, not the OS default TCP timeout
# (minutes) that once hung exec output streaming on a Redis hiccup. Mirrors the
# self-heal the agent db_pool already gets from psycopg check_connection on PG.
# Values live in base/events/live/redis_resilience.py — the single source both this
# module and redis_listener import (audit 2026-08-08 P2).

# socket_timeout must be None EXPLICITLY: since redis-py 5 the constructor
# default is 5s (DEFAULT_SOCKET_TIMEOUT), and a blanket 5s read timeout
# periodically cuts pubsub.listen()'s long blocking read (the hosted
# agent-host dispatcher reconnect-looped every 5s of idle — 2026-08-30 soak
# startup). Keepalive + health_check detect dead links without that side effect.
RESILIENCE_KWARGS: dict[str, Any] = {
    "socket_timeout": None,
    "socket_keepalive": True,
    "socket_keepalive_options": keepalive_options(),
    "socket_connect_timeout": _SOCKET_CONNECT_TIMEOUT_S,
    "health_check_interval": _HEALTH_CHECK_INTERVAL_S,
}


class _PinnedIPv4Connection(_redis_sync.Connection):
    """`redis.Connection` whose `_connect` bypasses `getaddrinfo` for an
    IPv4-literal host, connecting directly over `AF_INET`.

    `redis.asyncio.Connection._connect` goes through `asyncio.open_connection`
    (stdlib asyncio's own `BaseEventLoop._ensure_resolved` already checks
    `ipaddress.ip_address(host)` and skips `getaddrinfo` for a literal — 0
    `getaddrinfo` calls, confirmed empirically), so it needs no fix. This
    sync `Connection._connect` calls `socket.getaddrinfo` unconditionally —
    no such check — so on a DNS64/NAT64 network it can receive a synthesized
    AAAA for a literal host (`base.host.net.predicates.is_ipv4_literal`), the same
    failure mode `base/host/net/http_dial.py` fixes for httpx's sync transport.
    """

    def _connect(self) -> socket.socket:
        if not is_ipv4_literal(self.host):
            return super()._connect()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self.socket_keepalive:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                # redis.Connection types this `Mapping[int, int|bytes] | object |
                # None` (its own __init__ docstring) -- the bare `object` member is
                # never the runtime value (it's coalesced to `{}` at assignment;
                # see redis/connection.py), so narrow it here for pyright.
                keepalive_options = cast("dict[int, int]", self.socket_keepalive_options)
                for k, v in keepalive_options.items():
                    sock.setsockopt(socket.IPPROTO_TCP, k, v)
            sock.settimeout(self.socket_connect_timeout)
            sock.connect((self.host, self.port))
            sock.settimeout(self.socket_timeout)
        except OSError:
            sock.close()
            raise
        return sock


class _TransportAwareAsyncConnection(aredis.Connection):
    """`aredis.Connection` whose `is_connected` also sees a dead asyncio transport.

    redis-py's own `is_connected` only checks that `_reader` / `_writer` are
    non-None. When the peer / network kills the socket (outage, private-network blip),
    asyncio fires `connection_lost` on the transport — which nulls
    `_SelectorSocketTransport._write_ready` — but the redis Connection object
    is never told: `_writer` still references the dead transport, so
    `is_connected` stays True and `send_packed_command` skips reconnect, then
    `writelines` -> `_write_ready()` raises
    `TypeError: 'NoneType' object is not callable`. That is not an OSError /
    TimeoutError, so redis-py's except chain does not catch it; on the pubsub
    health-check path (parse_response -> check_health -> PING) it escaped all
    the way up and killed an agent process (an agent on a runner,
    2026-08-04, claim node crashed after a 45-minute network outage).

    Treating a transport that `is_closing()` (connection_lost already fired,
    or the transport was explicitly closed) as disconnected makes
    `send_packed_command` take redis-py's own reconnect path
    (`connect_check_health`), so the TypeError never happens. The half-open
    window before asyncio notices the death is still covered by redis-py's
    OSError / TimeoutError handling (keepalive bounds it in tens of seconds).
    """

    @property
    def is_connected(self) -> bool:
        if not super().is_connected:
            return False
        # `_writer` is an asyncio.StreamWriter; its `.transport` is None only
        # once the writer itself was closed, and `transport.is_closing()` is
        # True exactly when connection_lost fired (or close() was called) —
        # either way the socket can no longer carry writes.
        transport = getattr(self._writer, "transport", None)
        return not (transport is not None and transport.is_closing())


# Redis accepts TCP connections before the per-cluster ACL user is re-affirmed
# after a fresh start. Retrying auth failures through the normal redis-py retry
# path does not work: that retry intentionally only covers transport failures.
# Keep retries bounded so a bad ACL cannot pin a gateway request or an agent
# publisher forever; full jitter avoids a fleet of restarted processes striking
# Redis again on the same exponential schedule.
_AUTH_RETRY_INITIAL_DELAY_S = 0.5
_AUTH_RETRY_DELAY_CAP_S = 10.0
_AUTH_RETRY_WINDOW_S = 60.0
_AUTH_RETRY_MAX_ATTEMPTS = 10
_AUTH_RETRY_ERRORS = (AuthenticationError, NoPermissionError)

# A normal command must not inherit the pub/sub listener's unbounded read. The
# shared client deliberately pins ``socket_timeout=None`` because listeners use
# the same connection class and legitimately block while idle; command callers
# that are only a latency optimization need their own operation-level bound.
_BEST_EFFORT_PUBLISH_ATTEMPT_TIMEOUT_S = 2.0

# The two indirections make the retry contract deterministic in unit tests
# without making real callers choose a scheduler or clock implementation.
_sleep_sync = time.sleep
_sleep_async = asyncio.sleep


def _auth_retry_jitter(delay_cap: float) -> float:
    """Return equal jitter within one exponential-delay cap."""
    return random.uniform(delay_cap / 2, delay_cap)  # noqa: S311 — scheduling jitter, not a secret


def _next_auth_retry_delay(*, failure_count: int, waited_s: float, started_at: float) -> float:
    """Choose the next jittered delay without exceeding the retry window."""
    remaining_s = min(
        _AUTH_RETRY_WINDOW_S - waited_s,
        started_at + _AUTH_RETRY_WINDOW_S - time.monotonic(),
    )
    if remaining_s <= 0:
        return 0.0
    exponential_cap = min(
        _AUTH_RETRY_INITIAL_DELAY_S * (2 ** (failure_count - 1)),
        _AUTH_RETRY_DELAY_CAP_S,
        remaining_s,
    )
    return max(0.0, min(_auth_retry_jitter(exponential_cap), exponential_cap, remaining_s))


async def retry_auth_failures_async[T](
    operation: Callable[[], Awaitable[T]], *, attempt_timeout_s: float | None = None
) -> T:
    """Run an async Redis operation with bounded retry for ACL transition errors.

    Only AuthenticationError and NoPermissionError retry. Connection failures
    intentionally retain their existing caller-specific best-effort behavior.
    A caller that already has a per-operation timeout can keep it on each
    attempt while allowing the whole auth-transition sequence to use its
    bounded retry window. Callers composing commands own this one retry loop
    and explicitly disable command-level retries on those commands.
    """
    failure_count = 0
    waited_s = 0.0
    started_at = time.monotonic()
    while True:
        try:
            if attempt_timeout_s is None:
                return await operation()
            return await asyncio.wait_for(operation(), timeout=attempt_timeout_s)
        except _AUTH_RETRY_ERRORS:
            failure_count += 1
            if failure_count >= _AUTH_RETRY_MAX_ATTEMPTS:
                raise
            delay_s = _next_auth_retry_delay(
                failure_count=failure_count, waited_s=waited_s, started_at=started_at
            )
            if delay_s <= 0:
                raise
            await _sleep_async(delay_s)
            waited_s += delay_s


def retry_auth_failures_sync[T](operation: Callable[[], T]) -> T:
    """Synchronous counterpart of retry_auth_failures_async."""
    failure_count = 0
    waited_s = 0.0
    started_at = time.monotonic()
    while True:
        try:
            return operation()
        except _AUTH_RETRY_ERRORS:
            failure_count += 1
            if failure_count >= _AUTH_RETRY_MAX_ATTEMPTS:
                raise
            delay_s = _next_auth_retry_delay(
                failure_count=failure_count, waited_s=waited_s, started_at=started_at
            )
            if delay_s <= 0:
                raise
            _sleep_sync(delay_s)
            waited_s += delay_s


class _AuthRetryAsyncRedis(aredis.Redis):
    """Redis client whose ordinary async commands survive ACL re-affirmation."""

    async def execute_command(self, *args: Any, auth_retry: bool = True, **options: Any) -> Any:
        async def _execute() -> Any:
            return await cast(
                Awaitable[Any],
                aredis.Redis.execute_command(  # pyright: ignore[reportUnknownMemberType]
                    self, *args, **options
                ),
            )

        return await retry_auth_failures_async(_execute) if auth_retry else await _execute()


class _AuthRetrySyncRedis(_redis_sync.Redis):
    """Redis client whose ordinary synchronous commands survive ACL re-affirmation."""

    def execute_command(self, *args: Any, auth_retry: bool = True, **options: Any) -> Any:
        def _execute() -> Any:
            return cast(
                Any,
                _redis_sync.Redis.execute_command(  # pyright: ignore[reportUnknownMemberType]
                    self, *args, **options
                ),
            )

        return retry_auth_failures_sync(_execute) if auth_retry else _execute()


def open_async_redis(redis_url: str, *, decode_responses: bool = True) -> _AuthRetryAsyncRedis:
    """Open an async Redis client for a caller-owned connection lifecycle.

    Pub/sub subscribers use this rather than the per-loop shared publisher
    client because they own a socket per subscription, while ordinary commands
    still receive the same ACL-transition retry policy.
    """
    return cast(
        _AuthRetryAsyncRedis,
        _AuthRetryAsyncRedis.from_url(  # pyright: ignore[reportUnknownMemberType] — redis-py types from_url's **kwargs as Unknown; the call is fully typed.
            redis_url,
            decode_responses=decode_responses,
            connection_class=_TransportAwareAsyncConnection,
            **RESILIENCE_KWARGS,
        ),
    )


# Rate-limit the NOPERM WARNING per (channel, error-type): a persistent ACL
# outage funnels every event through here, so warning on each one would flood the
# log. First occurrence — and then at most once per `_WARN_THROTTLE_S` — logs
# WARNING; suppressed repeats drop to DEBUG so the signal stays visible without
# spamming. Keyed per channel so a genuinely new mis-scoped channel still surfaces.
_WARN_THROTTLE_S = 60.0


def _log_publish_failure(
    exc: BaseException, *, channel: str, context: str, warn_last: dict[tuple[str, str], float]
) -> None:
    """Log a known Redis/network publish failure after bounded recovery.

    ACL rejection warns with per-bus throttling; transport failures log at DEBUG.
    Unknown exceptions propagate from the publisher to its calling owner.
    """
    tag = f" [{context}]" if context else ""
    if isinstance(exc, ResponseError):
        key = (channel, type(exc).__name__)
        now = time.monotonic()
        last = warn_last.get(key)
        if last is None or now - last >= _WARN_THROTTLE_S:
            warn_last[key] = now
            logger.warning(
                "publish to {ch!r} rejected by redis ({exc!r}){tag} — the cluster "
                "redis ACL user lacks this channel; the live event is dropped "
                "(best-effort). Check ensure_cluster_redis_acl.",
                ch=channel,
                exc=exc,
                tag=tag,
            )
        else:
            logger.debug(
                "publish to {ch!r} still rejected by redis ({exc!r}){tag} — "
                "rate-limited (already warned within {w:.0f}s).",
                ch=channel,
                exc=exc,
                tag=tag,
                w=_WARN_THROTTLE_S,
            )
    elif isinstance(exc, (RedisConnectionError, RedisTimeoutError, OSError, TimeoutError)):
        logger.debug(
            "publish to {ch!r} skipped ({exc!r}){tag} — best-effort; pub/sub is a "
            "latency optimization and the durable DB write is unaffected.",
            ch=channel,
            exc=exc,
            tag=tag,
        )


async def publish_via(
    client: Callable[[], _AuthRetryAsyncRedis],
    channel: str,
    payload: str,
    *,
    warn_last: dict[tuple[str, str], float],
    context: str = "",
) -> int | None:
    """Publish on the caller-owned client, recovering only known Redis/network errors.

    Client construction is inside the same bounded failure policy as publish.
    Programming errors in either step propagate to the caller.
    """
    try:

        async def _publish() -> int:
            # redis-py types publish()'s **kwargs as Unknown; the call itself is fully typed.
            return await client().publish(  # pyright: ignore[reportUnknownMemberType]
                channel, payload, auth_retry=False
            )

        return await retry_auth_failures_async(
            _publish, attempt_timeout_s=_BEST_EFFORT_PUBLISH_ATTEMPT_TIMEOUT_S
        )
    except (ResponseError, RedisConnectionError, RedisTimeoutError, OSError, TimeoutError) as exc:
        _log_publish_failure(exc, channel=channel, context=context, warn_last=warn_last)
        return None


def publish_sync_via(
    open_client: Callable[[], _AuthRetrySyncRedis],
    channel: str,
    payload: str,
    *,
    warn_last: dict[tuple[str, str], float],
    context: str = "",
) -> int | None:
    """`publish_best_effort_sync` on a one-off client from `open_client()`; shared by the handle
    (`EventBus.publish_best_effort_sync`) and the module-level shim."""
    try:
        client = open_client()
        try:

            def _publish() -> int:
                # redis-py types publish()'s **kwargs as Unknown; the call itself is fully typed.
                return client.publish(channel, payload, auth_retry=False)  # pyright: ignore[reportUnknownMemberType]

            return retry_auth_failures_sync(_publish)
        finally:
            client.close()
    except (ResponseError, RedisConnectionError, RedisTimeoutError, OSError, TimeoutError) as exc:
        _log_publish_failure(exc, channel=channel, context=context, warn_last=warn_last)
        return None


def open_sync_redis(redis_url: str, *, decode_responses: bool = False) -> _AuthRetrySyncRedis:
    """A new synchronous client on `redis_url` with the cluster's resilience settings."""
    return cast(
        _AuthRetrySyncRedis,
        _AuthRetrySyncRedis.from_url(  # pyright: ignore[reportUnknownMemberType] — redis-py types from_url's **kwargs as Unknown; the call is fully typed.
            redis_url,
            decode_responses=decode_responses,
            connection_class=_PinnedIPv4Connection,
            **RESILIENCE_KWARGS,
        ),
    )
