"""The best-effort Redis wake `ava.self` sends after inserting its own inbound row.

The namespace module `ava/self/__init__.py` is the facade: it reads the bound process context
for the redis handle and the agent's identity, then calls this function with both, so a test
passes a fake handle and an id instead of replacing anything.
"""

from __future__ import annotations

from typing import Any

from redis.exceptions import ResponseError

from base.cluster import inbound_channel
from base.config import settings
from base.log import logger

__all__ = ["publish_inbound_wake"]


def publish_inbound_wake(redis: Any, agent_id: int) -> None:
    """Publish the wake for `agent_id`'s own inbound channel; never raises.

    `redis` is the SDK redis handle (anything with `publish(channel, message)`).

    Same contract as `base.agents.impersonation.wake.publish_inbound_wake`: the claim loop's
    SELECT recheck delivers the row regardless, but a `ResponseError` (a `NoPermissionError`
    is one) is logged as an ACL misconfiguration rather than swallowed - this agent's redis
    ACL user is not granted its own `<prefix>:inbound:*` channel. The channel comes from
    `inbound_channel`, so it matches `RedisInboundListener` and stays inside the ACL grant.
    """
    channel = inbound_channel(agent_id, settings.data_plane.events_channel)  # ambient: O
    try:
        redis.publish(channel, "0")
    except ResponseError as exc:
        logger.warning(
            "self inbound wake publish to {ch!r} rejected by redis ({exc!r}) - "
            "this agent's redis ACL user lacks its inbound channel; the "
            "self-restart/terminate/compact wake falls back to the claim SELECT "
            "recheck. Check ensure_cluster_redis_acl.",
            ch=channel,
            exc=exc,
        )
    except Exception:
        logger.opt(exception=True).warning(
            "self inbound wake publish to {ch!r} failed; the wake falls back to the claim "
            "SELECT recheck",
            ch=channel,
        )
