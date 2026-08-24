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
repair took. On a connection failure it calls the same idempotent Redis bring-up
that `ava start` uses, then verifies that the cluster identity can PING again.

A legacy cluster whose `.env` redis_url carries no username (`redis://:<secret>@host/0`,
born before the names-as-data ACL model) dials as the redis `default` admin user —
the identity this check re-affirms is absent BY CONFIG, not by a server restart, so
re-affirming is impossible and raising every watchdog round is pure noise (~4.5k
ERROR tracebacks for one such .env). `main()` warns and skips instead; `ava start`
converge backfills the username into the .env URL (see
cli/commands/_converge.py:_ensure_redis_url_identity_step).

No-secret clusters still run the liveness and respawn path: a dead local Redis
stops their message bus just as completely. They deliberately skip the ACL
re-affirm branch because they have no admin credential to re-assert.

Gateway-capability only: redis is the cluster's own instance on loopback — an
agent-runner-only host has no local redis.
"""

import logging
from urllib.parse import urlsplit

import redis
from redis.exceptions import AuthenticationError, ConnectionError, NoPermissionError, TimeoutError

from shared.cluster import (
    ensure_cluster_redis_acl,
    get_record,
    record_redis_port,
    redis_admin_url,
    redis_channel_prefix,
    redis_identity,
)
from shared.config import settings
from shared.log import init_gateway_process
from shared.paths import ava_home

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
    user: str,
    *,
    redis_port: int,
    cluster_url: str,
    admin_url: str,
    cluster_secret: str,
    channel_prefix: str,
    reaffirm_acl: bool,
) -> None:
    """PING as `user`; repair missing Redis or, with a secret, its ACL user.

    `user` is names-as-data — the caller reads it from the cluster's own
    redis_url (`redis_identity`), never derives it from a name.

    `redis_port` is a registry fact, because Redis always listens locally even
    when the URL names this host's reachable address. `reaffirm_acl` is false
    for no-secret clusters: they still respawn a dead Redis but do not attempt
    an ACL repair without an admin credential.

    Raises when the repair path itself fails (the Redis bring-up returns nonzero,
    admin auth is rejected, or the repaired server still cannot authenticate) —
    the watchdog logs it as a failing healthcheck rather than this module deciding
    what to swallow.
    """
    try:
        _ping(cluster_url)
        _log.debug("[redis-acl healthcheck] %s authenticates, no-op", user)
        return
    except (AuthenticationError, NoPermissionError) as exc:
        if not reaffirm_acl:
            raise
        _log.info("[redis-acl healthcheck] %s rejected (%s) — re-affirming ACL user", user, exc)
    except (ConnectionError, TimeoutError) as exc:
        _log.warning(
            "[redis-acl healthcheck] redis unreachable (%s) — restarting its local instance",
            exc,
        )
        from cli.commands._cluster_instance import _start_redis

        rc = _start_redis(redis_port, cluster_secret, user)
        if rc != 0:
            raise RuntimeError(f"redis restart did not start :{redis_port} (rc={rc})") from exc
        _ping(cluster_url)
        _log.info("[redis-acl healthcheck] redis restarted and %s authenticates", user)
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
    rec = get_record(ava_home())
    if rec is None:
        # Redis's listener port is a registry fact, so without a record there is
        # no safe target to restart. Guessing from an URL could target a remote
        # host and turn this gateway-only repair into the wrong operation.
        _log.warning(
            "[redis-acl healthcheck] no registry record for %s — cannot resolve Redis "
            "port; skipping",
            ava_home(),
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
        redis_port=record_redis_port(rec),
        reaffirm_acl=bool(settings.data_plane.cluster_secret),
    )


if __name__ == "__main__":
    main()
