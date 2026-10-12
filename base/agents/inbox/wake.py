"""The inbound wake: the Redis fast path that rouses an idle agent."""

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from base.cluster import WAKE_KEY_TTL_S, inbound_channel, wake_key
from base.db import Database
from base.events.live.bus import EventBus
from base.log import logger


def publish_inbound_wake(db: Database, bus: EventBus, agent_id: int, payload: str) -> bool:
    """Best-effort Redis publish to wake an idle agent — the fast path paired
    with the claim loop's SELECT recheck. Also SETEXes the agent's wake key
    (`base.cluster.wake_key`) as a durable breadcrumb so a wake lost to a
    disconnected listener is recovered on the listener's next (re)subscribe
    instead of waiting out the full recheck budget.

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
    # Imported here because `base.agents.impersonation` itself imports this
    # module at load time, so a module-level import would be a load-time cycle.
    from base.agents.impersonation import relay_liveness_alert

    relay_liveness_alert(db, agent_id)
    channel = inbound_channel(agent_id)
    try:
        r = bus.sync_redis()
        try:
            # redis-py types publish()'s **kwargs as Unknown, so the bound
            # method reads as partially-unknown; the call itself is fully typed.
            r.publish(channel, payload)  # pyright: ignore[reportUnknownMemberType]
            # Durable breadcrumb for the lost-wake window: pub/sub is
            # fire-and-forget, so a publish that lands while the agent's
            # listener is disconnected is otherwise unrecoverable until the
            # claim loop's 30s SELECT recheck. The listener GETDELs this key
            # on (re)subscribe and SELECTs immediately when it is present.
            r.set(wake_key(agent_id), payload, ex=WAKE_KEY_TTL_S)
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
