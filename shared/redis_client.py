"""Shared async Redis client (one per event loop).

Every publish call site (gateway endpoints, agent loop, label updates) used
to `aredis.Redis.from_url(settings.data_plane.redis_url, decode_responses=True)` per
call and `aclose()` immediately. The reconnect + handshake overhead added
up under publish-heavy load (frontend live events, inbound fan-out, label
updates). This module hands out one shared client per event loop so all
callers in the same loop reuse the same connection pool.

Why per-loop rather than process-singleton: `aredis.Redis` binds its
internal connections to the running event loop at first use; reusing one
across loops raises "Event loop is closed" on the second use. In
production every long-running daemon has exactly one event loop, so this
behaves as a process-singleton there. In pytest each async test has its
own loop, so each test transparently gets its own client (and the previous
loop's client is garbage-collected with it).

Callers must **not** `aclose()` the returned client — closing the shared
client would break every other caller in the same loop. The OS reclaims
the underlying socket when the loop / process exits.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any, cast

import redis as _redis_sync
import redis.asyncio as aredis

from shared.config import settings
from shared.log import logger
from shared.netutil import is_ipv4_literal
from shared.redis_resilience import (
    _HEALTH_CHECK_INTERVAL_S,
    _SOCKET_CONNECT_TIMEOUT_S,
    keepalive_options,
)

# weak-network resilience (F2):
# The central Postgres/Redis sit behind a TLS-MITM corp link; a half-dead
# connection must surface in tens of seconds, not the OS default TCP timeout
# (minutes) that once hung exec output streaming on a Redis hiccup. Mirrors the
# self-heal the agent db_pool already gets from psycopg check_connection on PG.
# Values live in shared/redis_resilience.py — the single source both this
# module and redis_listener import (audit 2026-08-08 P2).

# socket_timeout is deliberately NOT set: a blanket read timeout would
# periodically cut pubsub.listen()'s long blocking read (e.g. the SSE event
# stream). Keepalive + health_check detect dead links without that side effect.
_RESILIENCE_KWARGS: dict[str, Any] = {
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
    AAAA for a literal host (`shared.netutil.is_ipv4_literal`), the same
    failure mode `shared/http_dial.py` fixes for httpx's sync transport.
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


_clients: dict[asyncio.AbstractEventLoop, aredis.Redis] = {}


def get_async_redis() -> aredis.Redis:
    """Return the shared async Redis client for the current event loop."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        client = aredis.Redis.from_url(  # pyright: ignore[reportUnknownMemberType] — redis-py types from_url's **kwargs as Unknown; the call is fully typed.
            settings.data_plane.redis_url,
            decode_responses=True,
            # Dead-transport detection: see `_TransportAwareAsyncConnection`.
            connection_class=_TransportAwareAsyncConnection,
            **_RESILIENCE_KWARGS,
        )
        _clients[loop] = client
    return client


# Rate-limit the NOPERM WARNING per (channel, error-type): a persistent ACL
# outage funnels every event through here, so warning on each one would flood the
# log. First occurrence — and then at most once per `_WARN_THROTTLE_S` — logs
# WARNING; suppressed repeats drop to DEBUG so the signal stays visible without
# spamming. Keyed per channel so a genuinely new mis-scoped channel still surfaces.
_WARN_THROTTLE_S = 60.0
_warn_last: dict[tuple[str, str], float] = {}


def _log_publish_failure(exc: BaseException, *, channel: str, context: str) -> None:
    """Classify + log a best-effort publish failure — never re-raises.

    The single discipline for every fire-and-forget event / live-UI publish,
    mirroring the `shared/db.py:publish_inbound_wake` template: pub/sub is only a
    latency optimization, so a publish must never propagate into (and roll back /
    crash) the caller's durable DB-write or agent-lifecycle path.

    A `ResponseError` (a redis NOPERM — the cluster redis ACL user is not granted
    this channel, an ACL / channel-prefix misconfig) would silently disable live
    updates fleet-wide, so it is logged at WARNING (rate-limited per channel, see
    `_warn_last`). A transient failure (redis down, connection dropped) is
    best-effort and logged at DEBUG — the durable DB write already happened and the
    frontend recovers on its next full fetch."""
    from redis.exceptions import ResponseError

    tag = f" [{context}]" if context else ""
    if isinstance(exc, ResponseError):
        key = (channel, type(exc).__name__)
        now = time.monotonic()
        last = _warn_last.get(key)
        if last is None or now - last >= _WARN_THROTTLE_S:
            _warn_last[key] = now
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
    else:
        logger.debug(
            "publish to {ch!r} skipped ({exc!r}){tag} — best-effort; pub/sub is a "
            "latency optimization and the durable DB write is unaffected.",
            ch=channel,
            exc=exc,
            tag=tag,
        )


async def publish_best_effort(channel: str, payload: str, *, context: str = "") -> int | None:
    """Publish `payload` on the shared async client, best-effort — NEVER raises.

    The async entry point for every fire-and-forget live-UI / lifecycle event. A
    publish failure must never propagate into the caller's DB-write or
    agent-lifecycle path (a raise at the tail of `mark_agent_status` used to kill
    the claim node during a redis outage). Returns the receiver count on success
    (0 = nobody subscribed), or None when the publish failed. Failure
    classification is `_log_publish_failure`."""
    try:
        # redis-py types publish()'s **kwargs as Unknown; the call itself is fully typed.
        return await get_async_redis().publish(channel, payload)  # pyright: ignore[reportUnknownMemberType]
    except Exception as exc:
        _log_publish_failure(exc, channel=channel, context=context)
        return None


def publish_best_effort_sync(
    channel: str, payload: str, *, decode_responses: bool = True, context: str = ""
) -> int | None:
    """Sync counterpart of `publish_best_effort` — opens a one-off client, publishes,
    closes it, and NEVER raises. Returns the receiver count on success (0 = nobody
    subscribed), or None when the publish failed."""
    try:
        client = sync_redis(decode_responses=decode_responses)
        try:
            # redis-py types publish()'s **kwargs as Unknown; the call itself is fully typed.
            return client.publish(channel, payload)  # pyright: ignore[reportUnknownMemberType]
        finally:
            client.close()
    except Exception as exc:
        _log_publish_failure(exc, channel=channel, context=context)
        return None


def sync_redis(*, decode_responses: bool = False) -> _redis_sync.Redis:
    """Open a new synchronous Redis client on the cluster Redis.

    The single entry point for the non-async call sites (auth, agent cache,
    one-off publishes) so they stop reading `settings.data_plane.redis_url` by hand. Unlike
    `get_async_redis`, this is not pooled/shared — the caller owns the returned
    client and should `close()` it (or use it as a context manager). `decode_responses`
    is passed through (some callers want str, some want bytes).

    `connection_class=_PinnedIPv4Connection` pins an IPv4-literal redis_url
    host to a direct AF_INET dial (see that class's docstring); a no-op for
    a hostname host.
    """
    return _redis_sync.Redis.from_url(  # pyright: ignore[reportUnknownMemberType] — redis-py types from_url's **kwargs as Unknown; the call is fully typed.
        settings.data_plane.redis_url,
        decode_responses=decode_responses,
        connection_class=_PinnedIPv4Connection,
        **_RESILIENCE_KWARGS,
    )
