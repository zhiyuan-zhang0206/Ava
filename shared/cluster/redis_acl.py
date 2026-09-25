"""Apply explicit Redis ACL authority, optionally bound to owned native storage."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit


def ensure_cluster_redis_acl(
    user: str,
    *,
    redis_admin_url: str,
    runtime_password: str,
    channel_prefix: str,
    expected_data_dir: Path | None = None,
) -> None:
    """Create (or re-affirm) the cluster's redis ACL user `user` — the runtime
    redis identity, mirroring the per-cluster Postgres role. Idempotent; safe on
    every bring-up. `user` is names-as-data: read from the cluster's own
    redis_url (`identity_from_url`) for an existing cluster, `DATA_PLANE_IDENTITY`
    at birth. The user authenticates with its independent runtime password and is scoped
    to keys (`~*`) + pub/sub channels (`&<channel_prefix>:*` plus the hosted
    dispatcher's `&<channel_prefix>:inbound:*` subscription pattern); `-@dangerous` denies
    FLUSHALL / CONFIG / SHUTDOWN. The secret travels over the redis connection, never
    a process argv.

    `resetpass` precedes `>runtime_password`: Redis ACL passwords are additive by
    default (`>password` ADDS a valid password rather than replacing the set), so
    without it a runtime-password rotation would leave the previous password still
    authenticating this user indefinitely — confirmed empirically while building
    `scripts/rotate_cluster_secret.py`. `resetpass` clears the password list first,
    so re-affirming with an unchanged secret still ends at exactly one valid
    password (this call is idempotent either way), and re-affirming with a
    rotated one actually invalidates the old one.

    Empty secret (single-box no-auth): the user is created with `nopass` instead
    of a password. The runtime URLs still carry the identity as username
    (names-as-data holds with or without auth), and a URL with a username makes
    redis-py send AUTH — a missing user would WRONGPASS forever and the wake bus
    would never deliver. `nopass` lets that AUTH succeed while the posture stays
    unauthenticated (requirepass is off and the `default` user is nopass too).

    redis_admin_url connects as the Redis `default` user with the independent
    gateway-only Redis admin password."""
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    from shared.cluster import ownership

    # redis-py types from_url's **kwargs as Unknown; the call itself is fully typed.
    client = redis.Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        redis_admin_url,
        decode_responses=True,
        single_connection_client=True,
        retry=Retry(NoBackoff(), 0),
        socket_timeout=3,
        socket_connect_timeout=3,
    )
    try:
        if expected_data_dir is not None:
            custody = ownership.RedisConnectionCustody()
            if client.connection is None:
                raise RuntimeError("Redis ownership requires a dedicated connection")
            client.connection.register_connect_callback(custody.refuse_reconnect)  # pyright: ignore[reportUnknownMemberType] — redis callback stubs
            identity = ownership.redis_server(
                client.info("server"),  # pyright: ignore[reportUnknownMemberType] — redis command stubs
                client.config_get("dir"),  # pyright: ignore[reportUnknownMemberType] — redis command stubs
                expected_data_dir,
            )
            ownership.require_listener(identity, urlsplit(redis_admin_url).port or 6379)
        # redis-py types execute_command()'s signature as partially Unknown; the call is fully typed.
        client.execute_command(  # pyright: ignore[reportUnknownMemberType]
            "ACL",
            "SETUSER",
            user,
            "on",
            "resetpass",
            f">{runtime_password}" if runtime_password else "nopass",
            "resetkeys",
            "~*",
            "resetchannels",
            f"&{channel_prefix}:*",
            # The hosted dispatcher PSUBSCRIBEs `<prefix>:inbound:*`. Redis
            # checks the subscription PATTERN, not the channels it would match,
            # and `&<prefix>:*` does not cover it (empirically, Redis 8) — the
            # agent-host reconnect-looped on NoPermissionError without this
            # grant (2026-08-30 soak startup).
            f"&{channel_prefix}:inbound:*",
            "+@all",
            "-@dangerous",
        )
    finally:
        client.close()
