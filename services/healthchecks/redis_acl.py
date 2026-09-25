"""Bounded Redis runtime-credential protocol probe; no recovery authority."""

import redis

_TIMEOUT_S = 3.0


def _ping(cluster_url: str) -> None:
    """PING redis as the cluster identity; raises on auth failure or dead server."""
    # redis-py types from_url()/ping()'s **kwargs as Unknown; both calls are fully typed here.
    with redis.Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        cluster_url, socket_connect_timeout=_TIMEOUT_S, socket_timeout=_TIMEOUT_S
    ) as r:
        r.ping()  # pyright: ignore[reportUnknownMemberType]
