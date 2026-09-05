"""ensure_cluster_redis_acl against a throwaway redis.

The cluster's redis ACL user is the runtime redis identity (mirroring the
cluster's Postgres role); the user name is passed in as data (names-as-data),
never derived from a cluster name. These tests run against an ephemeral redis-server
(no prod), validating that the user is created enabled + channel-scoped, that it
authenticates with the cluster secret, and that the channel scope actually isolates
pub/sub.
"""

from __future__ import annotations

import pytest
import redis
from redis.exceptions import AuthenticationError, NoPermissionError

from shared.cluster import ensure_cluster_redis_acl
from shared.url_secret import url_with_userinfo
from tests._containers import redis_server

_SECRET = "redisacltestsecret"  # noqa: S105 — test fixture, not a real credential


def _acl_users(admin_url: str) -> list[str]:
    with redis.Redis.from_url(admin_url, decode_responses=True) as r:  # pyright: ignore[reportUnknownMemberType]
        return r.execute_command("ACL", "LIST")  # pyright: ignore[reportUnknownMemberType]


def test_ensure_creates_enabled_channel_scoped_user() -> None:
    with redis_server() as admin_url:
        ensure_cluster_redis_acl(
            "ava_feat_x",
            redis_admin_url=admin_url,
            runtime_password=_SECRET,
            channel_prefix="ava:feat-x",
        )
        users = _acl_users(admin_url)
        line = next((u for u in users if "ava_feat_x" in u), None)
        assert line is not None and " on " in f" {line} ", "ACL user must exist + be enabled"
        assert "ava:feat-x:*" in line, "channel scope must be the cluster prefix"

        # The user authenticates with the cluster secret and may publish to its own
        # channel, but NOT to another cluster's channel (the isolation that matters).
        user_url = url_with_userinfo(admin_url, "ava_feat_x", _SECRET)
        with redis.Redis.from_url(user_url, decode_responses=True) as r:  # pyright: ignore[reportUnknownMemberType]
            assert (
                r.publish("ava:feat-x:events", "ok") == 0  # pyright: ignore[reportUnknownMemberType]
            )  # delivered to 0 subscribers, allowed
            with pytest.raises(NoPermissionError):
                r.publish("ava:other:events", "denied")  # pyright: ignore[reportUnknownMemberType]


def test_ensure_grants_the_hosted_dispatcher_psubscribe_pattern() -> None:
    """The hosted agent-host dispatcher PSUBSCRIBEs `<prefix>:inbound:*`. Redis
    ACL checks the subscription PATTERN, not the channels it would match, and
    `&<prefix>:*` alone rejects it — the dispatcher reconnect-looped on
    NoPermissionError at soak startup (2026-08-30). The grant must exist and the
    pattern subscription must be allowed (DRYRUN, version-independent)."""
    with redis_server() as admin_url:
        ensure_cluster_redis_acl(
            "ava_feat_x",
            redis_admin_url=admin_url,
            runtime_password=_SECRET,
            channel_prefix="ava:feat-x",
        )
        with redis.Redis.from_url(admin_url, decode_responses=True) as admin:  # pyright: ignore[reportUnknownMemberType]
            assert (
                admin.execute_command(  # pyright: ignore[reportUnknownMemberType]
                    "ACL", "DRYRUN", "ava_feat_x", "PSUBSCRIBE", "ava:feat-x:inbound:*"
                )
                == "OK"
            )
            # The per-agent literal channel the process-mode listener uses keeps
            # working under the same grant.
            assert (
                admin.execute_command(  # pyright: ignore[reportUnknownMemberType]
                    "ACL", "DRYRUN", "ava_feat_x", "SUBSCRIBE", "ava:feat-x:inbound:42"
                )
                == "OK"
            )


def test_ensure_is_idempotent() -> None:
    with redis_server() as admin_url:
        for _ in range(2):
            ensure_cluster_redis_acl(
                "ava_feat_x",
                redis_admin_url=admin_url,
                runtime_password=_SECRET,
                channel_prefix="ava:feat-x",
            )
        assert sum("ava_feat_x" in u for u in _acl_users(admin_url)) == 1


def test_ensure_invalidates_the_previous_secret_on_rotation() -> None:
    """Redis ACL passwords are additive by default (`>password` ADDS to the
    valid set rather than replacing it) — re-affirming with a NEW secret must
    still drop the old one, or a "rotated" secret keeps working forever."""
    with redis_server() as admin_url:
        ensure_cluster_redis_acl(
            "ava_feat_x",
            redis_admin_url=admin_url,
            runtime_password=_SECRET,
            channel_prefix="ava:feat-x",
        )
        old_user_url = url_with_userinfo(admin_url, "ava_feat_x", _SECRET)
        with redis.Redis.from_url(old_user_url, decode_responses=True) as r:  # pyright: ignore[reportUnknownMemberType]
            assert r.ping()  # pyright: ignore[reportUnknownMemberType]

        new_secret = "rotated-" + _SECRET
        ensure_cluster_redis_acl(
            "ava_feat_x",
            redis_admin_url=admin_url,
            runtime_password=new_secret,
            channel_prefix="ava:feat-x",
        )

        with (
            pytest.raises(AuthenticationError),
            redis.Redis.from_url(old_user_url, decode_responses=True) as r,  # pyright: ignore[reportUnknownMemberType]
        ):
            r.ping()  # pyright: ignore[reportUnknownMemberType]
        new_user_url = url_with_userinfo(admin_url, "ava_feat_x", new_secret)
        with redis.Redis.from_url(new_user_url, decode_responses=True) as r:  # pyright: ignore[reportUnknownMemberType]
            assert r.ping()  # pyright: ignore[reportUnknownMemberType]
