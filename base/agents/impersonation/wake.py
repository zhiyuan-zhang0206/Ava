"""The inbound wake: the Redis fast path that rouses an idle agent.

It lives with the impersonation relay because every wake first runs the relay liveness alert;
impersonation publishes through it and so does the inbox.
"""

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from base.agents.impersonation.status import (
    RELAY_FAILURE_STAMP_SECONDS,
    RELAY_HEARTBEAT_STALE_SECONDS,
)
from base.cluster import inbound_channel
from base.db import Database
from base.events.live.announce import publish_agent_updated_sync, publish_impersonation_changed_sync
from base.events.live.bus import EventBus
from base.log import logger


def publish_inbound_wake(db: Database, bus: EventBus, agent_id: int, payload: str) -> bool:
    """Best-effort Redis publish to wake an idle agent — the fast path paired
    with the claim loop's SELECT recheck. Pub/sub is fire-and-forget: a wake
    published while the agent's listener is disconnected is not replayed, and
    the agent picks the inbound up on its next SELECT recheck.

    Also runs the best-effort impersonation relay liveness alert before
    publishing. Known Redis/network failures are recovered by `wait_for_inbound`'s
    SELECT within `timeout_s`, so the caller's INSERT+commit is never held hostage to
    Redis. Returns True when the wake reached Redis, False when the publish
    was rejected or skipped — callers that meter delivery (the delivery
    watchdog's dispatch counter) must read the return value, never assume
    success. Ignoring the return value stays backward compatible.
    But the failure is NOT swallowed blindly — a `NoPermissionError`
    (a `ResponseError`) means the publisher's redis ACL user is not granted this
    cluster's `<prefix>:inbound:*` channel (a channel-prefix or ACL misconfig),
    which would silently disable instant wake fleet-wide, so it is logged at
    WARNING. Transient failures (redis down) log at DEBUG; any other exception
    propagates to its caller. Channel is derived via
    `inbound_channel` so publish and `RedisInboundListener` subscribe stay in
    sync and stay inside the ACL grant."""
    # A wake for an impersonated agent whose relay heartbeat is stale must not
    # be silent: the inbound can sit unread in the inbox forever. Best-effort.
    relay_liveness_alert(db, agent_id)
    channel = inbound_channel(agent_id)
    try:
        r = bus.sync_redis()
        try:
            # redis-py types publish()'s **kwargs as Unknown, so the bound
            # method reads as partially-unknown; the call itself is fully typed.
            r.publish(channel, payload)  # pyright: ignore[reportUnknownMemberType]
            return True
        finally:
            r.close()
    except ResponseError as exc:
        logger.warning(
            "inbound wake publish to {ch!r} rejected by redis ({exc!r}) — the "
            "cluster redis ACL user lacks this channel; instant wake off, agents "
            "fall back to their SELECT recheck. Check ensure_cluster_redis_acl.",
            ch=channel,
            exc=exc,
        )
        return False
    except (RedisConnectionError, RedisTimeoutError, OSError) as exc:
        logger.debug(
            "inbound wake publish to {ch!r} skipped ({exc!r}) — best-effort; the "
            "agent's SELECT recheck delivers within timeout_s.",
            ch=channel,
            exc=exc,
        )
        return False


def relay_liveness_alert(db: Database, agent_id: int) -> None:
    """Loud, best-effort signal when a wake lands for an agent whose active
    lease has a stale relay heartbeat. Never raises: the wake itself must not
    be held hostage to this diagnostic. Logged at most once per stamp interval.
    """
    try:
        # Staleness is judged by the database clock, in the same statement that reads the lease.
        with db.connect() as conn:
            row = conn.execute(
                "SELECT relay_provider,relay_heartbeat_at,"
                "relay_heartbeat_at IS NULL OR "
                "relay_heartbeat_at < clock_timestamp() - make_interval(secs => %s),"
                "relay_last_failure_at IS NOT NULL AND "
                "relay_last_failure_at >= clock_timestamp() - make_interval(secs => %s) "
                "FROM agent_impersonations WHERE agent_id=%s AND status='active' "
                "ORDER BY created_at DESC LIMIT 1",
                (RELAY_HEARTBEAT_STALE_SECONDS, RELAY_FAILURE_STAMP_SECONDS, agent_id),
            ).fetchone()
        if row is None:
            return
        provider, heartbeat, stale, recently_stamped = row
        if stale and not recently_stamped:
            logger.error(
                "inbound wake for an impersonated agent whose relay heartbeat is stale; "
                "messages may sit unread in the inbox",
                agent_id=agent_id,
                relay_provider=provider,
                relay_heartbeat_at=str(heartbeat),
            )
    except Exception:
        logger.exception("impersonation relay liveness check failed", agent_id=agent_id)


def wake_agent(db: Database, bus: EventBus, agent_id: int, *, roster_changed: bool = False) -> None:
    publish_inbound_wake(db, bus, agent_id, "impersonation")
    publish_impersonation_changed_sync(bus, agent_id)
    if roster_changed:
        publish_agent_updated_sync(bus, agent_id)
