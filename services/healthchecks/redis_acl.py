"""Per-cluster redis ACL healthcheck — called every 60s by the gateway watchdog.

The cluster's redis runtime identity (the ACL user carried by this cluster's own
redis_url — names-as-data — re-affirmed via ensure_cluster_redis_acl)
lives only in redis server memory: the per-cluster redis carries just the admin
`requirepass` (the cluster secret) — no persisted `user` lines, no aclfile — so a
redis-server restart silently drops the ACL user. From then on every component's
redis reconnect fails with AuthenticationError: agents crash on their next redis
call and the gateway's SSE stream dies on attach, while the gateway's HTTP health
stays green (2026-07-14 prod outage). Between `ava start` runs this check is the
only re-affirm point.

The check PINGs redis as the cluster identity. On an auth failure with the
server otherwise reachable, it re-runs the idempotent provisioning primitive as
the `default` admin user (requirepass == the cluster secret) and verifies the
repair took. Connection failures are left alone — a dead server is repaired by the
next `ava start`, which re-affirms the ACL as part of bring-up.

A legacy cluster whose `.env` redis_url carries no username (`redis://:<secret>@host/0`,
born before the names-as-data ACL model) dials as the redis `default` admin user —
the identity this check re-affirms is absent BY CONFIG, not by a server restart, so
re-affirming is impossible and raising every watchdog round is pure noise (~4.5k
ERROR tracebacks for one such .env). `main()` warns and skips instead; `ava start`
converge backfills the username into the .env URL (see
cli/commands/_converge.py:_ensure_redis_url_identity_step).

Gateway-capability only: redis is the cluster's own instance on loopback — an
agent-runner-only host has no local redis.
"""

import logging
from urllib.parse import urlsplit

import redis
from redis.exceptions import AuthenticationError, NoPermissionError

from shared.cluster import (
    ensure_cluster_redis_acl,
    redis_admin_url,
    redis_channel_prefix,
    redis_identity,
)
from shared.config import settings
from shared.log import init_gateway_process

_log = logging.getLogger("services.healthchecks.redis_acl")

_TIMEOUT_S = 3.0


def _ping(cluster_url: str) -> None:
    """PING redis as the cluster identity; raises on auth failure or dead server."""
    # redis-py types from_url()/ping()'s **kwargs as Unknown; both calls are fully typed here.
    with redis.Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        cluster_url, socket_connect_timeout=_TIMEOUT_S, socket_timeout=_TIMEOUT_S
    ) as r:
        r.ping()  # pyright: ignore[reportUnknownMemberType]


def check(
    user: str, *, cluster_url: str, admin_url: str, cluster_secret: str, channel_prefix: str
) -> None:
    """PING as `user`; on auth failure re-affirm the ACL user and verify.

    `user` is names-as-data — the caller reads it from the cluster's own
    redis_url (`redis_identity`), never derives it from a name.

    Raises when the repair path itself fails (admin auth rejected, or the
    re-affirmed user still cannot authenticate) — the watchdog logs it as a
    failing healthcheck rather than this module deciding what to swallow."""
    try:
        _ping(cluster_url)
        _log.debug("[redis-acl healthcheck] %s authenticates, no-op", user)
        return
    except (AuthenticationError, NoPermissionError) as exc:
        _log.info("[redis-acl healthcheck] %s rejected (%s) — re-affirming ACL user", user, exc)
    except redis.RedisError as exc:
        _log.warning(
            "[redis-acl healthcheck] redis unreachable (%s) — leaving the process to the "
            "service manager; next round repairs the ACL",
            exc,
        )
        return

    ensure_cluster_redis_acl(
        user,
        redis_admin_url=admin_url,
        cluster_secret=cluster_secret,
        channel_prefix=channel_prefix,
    )
    _ping(cluster_url)
    _log.info("[redis-acl healthcheck] ACL user %s re-affirmed", user)


def _hostport(url: str) -> str:
    """`host:port` of a URL with any userinfo stripped — log lines never carry
    the credential a data-plane URL embeds."""
    return urlsplit(url).netloc.rpartition("@")[2]


def main() -> None:
    init_gateway_process(name="redis_acl-healthcheck")
    if not settings.data_plane.cluster_secret:
        # A no-secret cluster provisions no ACL user and no requirepass — the
        # runtime dials as the redis `default` user with no credential, and a
        # server restart drops nothing. Nothing to re-affirm; skip.
        _log.warning(
            "[redis-acl healthcheck] cluster has no secret — redis runs without "
            "requirepass/ACL user, nothing to re-affirm; skipping"
        )
        return
    cluster_url = settings.data_plane.redis_url
    if not urlsplit(cluster_url).username:
        # No ACL identity to re-affirm: a username-less URL dials as the redis
        # `default` user, whose requirepass persists in redis.conf, so a server
        # restart drops nothing. Skip (one warning per round, matching the
        # unreachable-server branch) instead of raising a traceback every round.
        _log.warning(
            "[redis-acl healthcheck] redis_url carries no ACL username (%s) — runtime "
            "dials as the redis `default` user, nothing to re-affirm; skipping. Run "
            "`ava converge` to backfill the username into the .env URL.",
            _hostport(cluster_url),
        )
        return
    check(
        redis_identity(),
        cluster_url=cluster_url,
        admin_url=redis_admin_url(),
        cluster_secret=settings.data_plane.cluster_secret,
        channel_prefix=redis_channel_prefix(),
    )


if __name__ == "__main__":
    main()
