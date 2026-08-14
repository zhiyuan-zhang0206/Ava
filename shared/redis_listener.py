"""Long-lived Redis pub/sub listener with auto-reconnect + re-subscribe.

Wraps a Redis pub/sub subscription for inbound wake-up, exposing
`wait_one` / `ensure_listening` / `close` — the interface `wait_for_inbound`
(and historically the interrupt watcher; that watcher now polls the DB and no
longer shares this listener — see `agent/graph/_interrupt.py`) relies on.

Why Redis pub/sub, not PG LISTEN/NOTIFY:
- PgBouncer transaction pooling breaks LISTEN (session-scoped).
- Redis pub/sub is already in the stack for SSE events and agent lifecycle.
- Moving inbound wake to Redis lets PG connections drop from 3→2 per agent
  and enables PgBouncer integration.

Channel: `<prefix>:inbound:{agent_id}` (`shared.cluster.inbound_channel`) — one
per agent, cluster-scoped so each agent only receives its own wake-ups AND the
channel falls inside the cluster redis ACL user's `&<prefix>:*` grant.

ACL degradation: if `subscribe` is rejected by the redis ACL
(`NoPermissionError`, a `ResponseError` — NOT a `ConnectionError`), the listener
does not crash the idle-wait. It logs once and degrades to the SELECT recheck in
`wait_for_inbound` (wake still works, just at the recheck cadence, not instant),
recovering automatically if the ACL is later fixed.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import redis.asyncio as aredis
from redis.asyncio.client import PubSub as _RedisPubSub

from shared.cluster import inbound_channel, wake_key
from shared.log import logger
from shared.redis_client import _TransportAwareAsyncConnection
from shared.redis_resilience import (
    _HEALTH_CHECK_INTERVAL_S,
    _SOCKET_CONNECT_TIMEOUT_S,
    keepalive_options,
)

# Backstop margin for the consume phase in `wait_one`. The inner
# `get_message(timeout=...)` deadline only starts ticking once the consume task
# is scheduled — a few event-loop ticks after the outer `asyncio.wait` timer.
# With equal budgets the outer timer would fire first on every ordinary
# zero-message idle expiry, misreporting it as a stuck consume. Padding the
# outer timer guarantees a running inner timer always expires first (silent
# return); the outer firing now means the consume never started its timer — the
# genuinely-stuck case worth abandoning.
_CONSUME_ABANDON_GRACE = 2.0

# Redis resilience values — single source shared/redis_resilience.py, imported
# by both redis_client and this module (audit 2026-08-08 P2: these were
# replicated by hand under a "kept in sync" comment — the drift surface R2
# exists to kill).
_PROBE_TIMEOUT_S = 5.0
# Liveness probe timeout — bounds only pubsub.ping(), never a blocking
# get_message(). Previously the probe called self._redis.ping(), which
# borrows a connection from the client pool — a different socket from the
# pubsub one, so it proved nothing about self._pubsub's health.

_KEEPALIVE_OPTIONS: dict[int, int] = keepalive_options()


class RedisInboundListener:
    """Per-agent Redis pub/sub listener for inbound message wake-ups.

    Subscribes to the cluster-scoped `<prefix>:inbound:{agent_id}`
    (`shared.cluster.inbound_channel`) and exposes `wait_one(timeout)` /
    `ensure_listening()` / `close()` — the interface `wait_for_inbound` and
    `subscribe_interrupt` rely on.

    Auto-reconnects on connection loss: if the Redis connection dies
    (network blip, Redis restart), the next `wait_one` or `ensure_listening`
    transparently reconnects and re-subscribes.

    ACL-denial tolerant: a `ResponseError` on subscribe (an ACL
    `NoPermissionError`) does not propagate — it is logged once and the listener
    degrades to the caller's SELECT recheck, recovering if the ACL is later fixed.
    """

    def __init__(self, redis_url: str, agent_id: int) -> None:
        self._redis_url = redis_url
        self._agent_id = agent_id
        self._channel = inbound_channel(agent_id)
        self._redis: aredis.Redis | None = None
        self._pubsub: _RedisPubSub | None = None
        self._lock = asyncio.Lock()
        # Serialize wait_one() so the claim-node idle-wait and the interrupt
        # watcher (which share this listener's single pubsub connection) never
        # run two _consume_one -> pubsub.get_message() reads on the same asyncio
        # StreamReader at once — that trips "readuntil() called while another
        # coroutine is already waiting for incoming data".
        self._wait_lock = asyncio.Lock()
        # Latch so an ACL subscribe rejection is logged once per episode (this
        # method is retried every wait_one) and its recovery is logged too.
        self._acl_denied_logged = False

    async def _open_and_subscribe(self) -> _RedisPubSub:
        """Open a fresh Redis connection, subscribe to the agent's channel.

        Caller must hold `self._lock`.  The new connection + pubsub are
        only attached to `self._redis` / `self._pubsub` after subscribe
        succeeds; on failure the local objects are closed before raising.
        """
        redis = aredis.Redis.from_url(  # pyright: ignore[reportUnknownMemberType] — redis-py types from_url's **kwargs as Unknown; the call is fully typed.
            self._redis_url,
            decode_responses=True,
            socket_connect_timeout=_SOCKET_CONNECT_TIMEOUT_S,
            health_check_interval=_HEALTH_CHECK_INTERVAL_S,
            socket_keepalive=True,
            socket_keepalive_options=_KEEPALIVE_OPTIONS,
            # Dead-transport detection: see `_TransportAwareAsyncConnection`.
            # Without it, a pubsub connection whose asyncio transport already
            # fired connection_lost (network outage) still reports
            # `is_connected=True`; the next `parse_response` health-check PING
            # then writes the dead transport and raises
            # `TypeError: 'NoneType' object is not callable`, which escaped
            # redis-py's except chain and killed an agent's claim node
            # (agent 2613, 2026-08-04).
            connection_class=_TransportAwareAsyncConnection,
        )
        try:
            # redis-py types pubsub()'s **kwargs as Unknown; the call itself is fully typed.
            pubsub = redis.pubsub(ignore_subscribe_messages=True)  # pyright: ignore[reportUnknownMemberType]
            await pubsub.subscribe(self._channel)
        except BaseException:
            try:
                await redis.aclose()
            except Exception as exc:
                logger.debug(
                    "RedisInboundListener[agent={a}]: ignoring close error "
                    "during open rollback: {exc!r}",
                    a=self._agent_id,
                    exc=exc,
                )
            raise
        self._redis = redis
        self._pubsub = pubsub
        return pubsub

    async def _ensure_subscribed(self) -> _RedisPubSub:
        """Return a subscribed pubsub handle, reconnecting if needed.

        Idempotent — if the current connection is alive, returns it as-is.
        """
        async with self._lock:
            if self._pubsub is not None and self._redis is not None:
                try:
                    # Probe the PUBSUB connection, not the client pool.
                    # self._redis.ping() borrows a different, transparently-
                    # reconnected socket from the pool and proves nothing
                    # about self._pubsub's health. pubsub.ping() round-trips
                    # through the actual pubsub socket.
                    await asyncio.wait_for(
                        self._pubsub.ping(),  # pyright: ignore[reportUnknownMemberType] — redis-py types ping's message param as Unknown; the call is fully typed.
                        timeout=_PROBE_TIMEOUT_S,
                    )
                    return self._pubsub
                except Exception:
                    # Connection dead — close and reopen below.
                    await self._close_inner()
            return await self._open_and_subscribe()

    async def ensure_listening(self) -> None:
        """Eagerly open the subscription (idempotent).

        Lets the caller close the "SELECT before subscribe took effect" gap
        by subscribing before an initial SELECT, so a publish landing in
        between is captured rather than missed until the next timeout.

        A synchronous `ResponseError` from subscribe is swallowed after logging,
        not propagated, so it cannot crash a caller (the claim node's idle
        wait). Note redis
        surfaces an ACL `NoPermissionError` LAZILY — `subscribe` returns without
        error and the denial lands on the first `get_message`, so the actual ACL
        degradation happens in `wait_one`; this handler only covers a subscribe
        that fails synchronously. It does NOT clear the denied latch: only a
        clean consume in `wait_one` proves the channel is readable.
        """
        try:
            await self._ensure_subscribed()
        except aredis.ResponseError as exc:
            self._note_subscribe_denied(exc)

    def _note_subscribe_denied(self, exc: aredis.ResponseError) -> None:
        """Log a redis subscribe rejection once per episode (until it recovers).

        Almost always `NoPermissionError`: the cluster's redis ACL user
        (`ava_<cluster>`, scoped to `&<prefix>:*`) is not granted this agent's
        inbound channel — a misconfigured / drifted ACL, or (the bug this
        replaced) a channel name missing the cluster prefix. Wake degrades to the
        SELECT recheck rather than crashing, but instant pub/sub wake stays off
        until the ACL is fixed, so it is logged loudly — once, to avoid a
        per-`wait_one` (every `timeout_s`) spam."""
        if self._acl_denied_logged:
            return
        self._acl_denied_logged = True
        logger.warning(
            "RedisInboundListener[agent={a}]: subscribe to {ch!r} rejected by redis "
            "({exc!r}) — the cluster redis ACL user is not granted this channel. "
            "Falling back to the SELECT recheck for wake (instant pub/sub wake off "
            "until the ACL grants &{ch}); re-run `ava start` / check "
            "ensure_cluster_redis_acl.",
            a=self._agent_id,
            ch=self._channel,
            exc=exc,
        )

    def _clear_subscribe_denied(self) -> None:
        """Note recovery: a subscribe that succeeds after a prior denial."""
        if not self._acl_denied_logged:
            return
        self._acl_denied_logged = False
        logger.info(
            "RedisInboundListener[agent={a}]: subscribe to {ch!r} now permitted — "
            "instant pub/sub wake restored.",
            a=self._agent_id,
            ch=self._channel,
        )

    async def _consume_wake_key(self) -> bool:
        """Whether a durable wake breadcrumb exists for this agent; consume it.

        The inbound publisher SETEXes `wake_key(agent_id)` alongside every
        pub/sub publish (see `shared.db.publish_inbound_wake`). GETDEL it here
        so a wake lost to pub/sub's fire-and-forget semantics (the publish
        landed while this connection was down or not yet subscribed) still
        makes the caller's SELECT recheck run immediately instead of after the
        full wait budget. Best-effort: any failure returns False and the caller
        falls back to its normal wait.
        """
        redis = self._redis
        if redis is None:
            return False
        try:
            # redis-py types getdel()'s return as Unknown; bool() narrows it.
            return bool(  # pyright: ignore[reportUnknownMemberType]
                await redis.getdel(wake_key(self._agent_id))
            )
        except Exception:
            return False

    async def _consume_one(self, pubsub: _RedisPubSub, timeout: float) -> None:
        """Consume one message from pubsub, or return when `timeout` expires.

        `pubsub.get_message(timeout=timeout)` returns None on timeout.
        We loop to skip non-data messages (subscribe/unsubscribe
        confirmations) even though `ignore_subscribe_messages=True`
        filters most — a stray reconnect subscribe message could still
        arrive.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            msg = cast(
                "dict[str, Any] | None",
                await pubsub.get_message(timeout=remaining, ignore_subscribe_messages=True),
            )
            if msg is not None and msg.get("type") == "message":
                return  # Got a real inbound wake-up
            # msg is None (timeout) or a subscribe/unsubscribe confirmation — loop

    async def wait_one(self, timeout: float) -> None:
        """Block until a publish arrives on this agent's channel, or `timeout`
        seconds elapse.

        Returns on either condition — the caller does a SELECT recheck to
        handle both "message arrived" and "message lost / not sent" cases
        uniformly.  Bounded by `timeout` total wall-clock; internal retries
        across reconnect attempts share that budget with backoff so a
        persistently-down Redis does not spin in a tight loop.

        Serialised by `_wait_lock`: the claim-node idle-wait and the interrupt
        watcher share one listener (hence one pubsub connection), so two
        `wait_one` calls could otherwise each drive `_consume_one` ->
        `pubsub.get_message()` on the same asyncio StreamReader concurrently,
        tripping its "readuntil() called while another coroutine is already
        waiting for incoming data" guard. The lock keeps the pubsub connection
        single-consumer.
        """
        async with self._wait_lock:
            await self._wait_one_impl(timeout)

    async def _wait_one_impl(self, timeout: float) -> None:
        """Serialised body of `wait_one` — see it for the return contract.

        Bounded by `timeout` total wall-clock; internal retries across
        reconnect attempts share that budget with backoff so a
        persistently-down Redis does not spin in a tight loop.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        backoff = 0.5
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            try:
                # Open/reconnect must honour the caller's budget.
                open_task = asyncio.ensure_future(self._ensure_subscribed())
                done, _ = await asyncio.wait({open_task}, timeout=remaining)
                if not done:
                    open_task.cancel()
                    logger.warning(
                        "RedisInboundListener[agent={a}]: open/subscribe did not "
                        "complete within {r:.1f}s budget — abandoning attempt",
                        a=self._agent_id,
                        r=remaining,
                    )
                    return
                pubsub = open_task.result()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return
                # A wake may have been published while this connection was
                # down (or before it subscribed): pub/sub is fire-and-forget,
                # so the publisher also SETEXes a durable wake key. Consume it
                # so the caller's SELECT recheck runs NOW instead of after a
                # full wait budget — this turns a lost wake into (near-)instant
                # delivery instead of the 30s fallback.
                if await self._consume_wake_key():
                    return
                # Consume with bounded budget (see `_CONSUME_ABANDON_GRACE`).
                consume_task = asyncio.ensure_future(self._consume_one(pubsub, remaining))
                try:
                    done, _ = await asyncio.wait(
                        {consume_task}, timeout=remaining + _CONSUME_ABANDON_GRACE
                    )
                finally:
                    if not consume_task.done():
                        consume_task.cancel()
                if not done:
                    logger.warning(
                        "RedisInboundListener[agent={a}]: consume never started "
                        "its timer within {r:.1f}s budget + {g:.1f}s grace — "
                        "abandoning attempt",
                        a=self._agent_id,
                        r=remaining,
                        g=_CONSUME_ABANDON_GRACE,
                    )
                    return
                consume_task.result()
                # Reaching a clean consume (message or timeout, no error) proves
                # the channel is actually readable — the only reliable recovery
                # signal, since `subscribe` returns without surfacing a redis-side
                # NoPermissionError (it lands on the first get_message below).
                self._clear_subscribe_denied()
                return
            except aredis.ResponseError as exc:
                # A command-level rejection — almost always NoPermissionError:
                # the cluster redis ACL user is not granted this channel. Unlike
                # a dropped connection this will NOT fix itself by retrying
                # within this call, so we must not spin: log once, drop the
                # unusable connection, and sleep out the remaining budget so
                # wait_for_inbound's defensive SELECT recheck runs at its normal
                # cadence. Wake still works (via SELECT), just not instantly —
                # far better than crashing the idle-wait, which a bare
                # propagation of this (a ResponseError, NOT an OSError /
                # ConnectionError, so uncaught by the branch below) would do.
                self._note_subscribe_denied(exc)
                async with self._lock:
                    await self._close_inner()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                return
            except (OSError, aredis.ConnectionError, aredis.TimeoutError, TypeError) as exc:
                # TypeError: a defensive catch for redis-py's
                # `TypeError: 'NoneType' object is not callable` when a
                # connection_lost transport is written through (agent 2613
                # crash, 2026-08-04). `_TransportAwareAsyncConnection` makes
                # that path reconnect instead, but the race window between
                # the is_connected check and the write still exists — an
                # agent process must never die to a redis library quirk, so
                # treat it like any other lost connection (close + backoff).
                logger.warning(
                    "RedisInboundListener[agent={a}]: conn lost / open failed, will retry: {exc!r}",
                    a=self._agent_id,
                    exc=exc,
                )
                async with self._lock:
                    await self._close_inner()
                if remaining <= backoff:
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max(0.5, remaining / 2))

    async def _close_inner(self) -> None:
        """Close the underlying Redis connection + pubsub.  Caller must hold
        `self._lock`."""
        pubsub = self._pubsub
        redis = self._redis
        self._pubsub = None
        self._redis = None
        if pubsub is not None:
            try:
                await pubsub.aclose()
            except Exception as exc:
                logger.debug(
                    "RedisInboundListener[agent={a}]: ignoring error closing pubsub: {exc!r}",
                    a=self._agent_id,
                    exc=exc,
                )
        if redis is not None:
            try:
                await redis.aclose()
            except Exception as exc:
                logger.debug(
                    "RedisInboundListener[agent={a}]: ignoring error closing redis: {exc!r}",
                    a=self._agent_id,
                    exc=exc,
                )

    async def close(self) -> None:
        """Close the underlying connection for clean shutdown."""
        async with self._lock:
            await self._close_inner()
