"""ensure_cluster_redis_acl against a throwaway redis.

The cluster's redis ACL user is the runtime redis identity (mirroring the
cluster's Postgres role); the user name is passed in as data (names-as-data),
never derived from a cluster name. These tests run against an ephemeral redis-server
(no prod), validating that the user is created enabled + channel-scoped, that it
authenticates with the cluster secret, and that the channel scope actually isolates
pub/sub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import redis
from redis.exceptions import AuthenticationError, NoPermissionError

from shared.cluster import ensure_cluster_redis_acl, ownership
from shared.url_secret import url_with_userinfo
from tests._containers import redis_server

_SECRET = "redisacltestsecret"  # noqa: S105 — test fixture, not a real credential


def _acl_users(admin_url: str) -> list[str]:
    with redis.Redis.from_url(admin_url, decode_responses=True) as r:  # pyright: ignore[reportUnknownMemberType]
        return r.execute_command("ACL", "LIST")  # pyright: ignore[reportUnknownMemberType,reportReturnType]


def test_ensure_creates_enabled_channel_scoped_user() -> None:
    with redis_server() as admin_url:
        with redis.Redis.from_url(admin_url, decode_responses=True) as admin:  # pyright: ignore[reportUnknownMemberType] — test double or third-party stubs
            directory = Path(str(admin.config_get("dir")["dir"]))  # pyright: ignore[reportUnknownMemberType] — test double or third-party stubs
        ensure_cluster_redis_acl(
            "ava_feat_x",
            redis_admin_url=admin_url,
            runtime_password=_SECRET,
            channel_prefix="ava:feat-x",
            expected_data_dir=directory,
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


def test_owned_acl_refuses_reconnect_before_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    with redis_server() as url:
        with redis.Redis.from_url(url, decode_responses=True) as admin:  # pyright: ignore[reportUnknownMemberType] — test double or third-party stubs
            directory = Path(str(admin.config_get("dir")["dir"]))  # pyright: ignore[reportUnknownMemberType] — test double or third-party stubs
        before = _acl_users(url)
        original = redis.Redis.from_url  # pyright: ignore[reportUnknownMemberType] — test double or third-party stubs
        clients: list[redis.Redis] = []

        def capture_client(url: str, **kwargs: Any) -> redis.Redis:
            client = original(url, **kwargs)
            clients.append(client)
            return client

        observe = ownership.require_listener

        def disconnect_after_observation(owner: ownership.OwnedProcess, port: int) -> None:
            observe(owner, port)
            connection = clients[-1].connection
            assert connection is not None
            connection.disconnect()  # pyright: ignore[reportUnknownMemberType] — test double or third-party stubs

        with monkeypatch.context() as patch:
            patch.setattr(redis.Redis, "from_url", capture_client)
            patch.setattr(ownership, "require_listener", disconnect_after_observation)
            with pytest.raises(RuntimeError, match="connection changed"):
                ensure_cluster_redis_acl(
                    "ava_unverified",
                    redis_admin_url=url,
                    runtime_password=_SECRET,
                    channel_prefix="ava:test",
                    expected_data_dir=directory,
                )
        assert _acl_users(url) == before


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


def test_ensure_refuses_empty_runtime_password() -> None:
    """Redis always authenticates: an empty runtime password is refused before
    any ACL effect, never turned into a password-less (`nopass`) user."""
    with redis_server() as admin_url:
        before = _acl_users(admin_url)
        with pytest.raises(ValueError, match="runtime password"):
            ensure_cluster_redis_acl(
                "ava_feat_x",
                redis_admin_url=admin_url,
                runtime_password="",
                channel_prefix="ava:feat-x",
            )
        assert _acl_users(admin_url) == before


def test_runtime_user_is_confined_to_cluster_data_and_channel_prefix() -> None:
    """The runtime identity reads and writes this cluster's own instance keyspace
    (every cluster owns its Redis, and `ava.REDIS` plus the gateway caches use
    unprefixed keys) and publishes only under its channel prefix; it cannot
    reach another prefix, administer the instance, or become the admin user."""
    with redis_server() as admin_url:
        ensure_cluster_redis_acl(
            "ava_feat_x",
            redis_admin_url=admin_url,
            runtime_password=_SECRET,
            channel_prefix="ava:feat-x",
        )
        user_url = url_with_userinfo(admin_url, "ava_feat_x", _SECRET)
        with redis.Redis.from_url(user_url, decode_responses=True) as r:  # pyright: ignore[reportUnknownMemberType]
            assert r.set("ava:feat-x:wake:1", "1", ex=60)  # pyright: ignore[reportUnknownMemberType]
            assert r.set("fleet_graph:cache", "v", ex=60)  # pyright: ignore[reportUnknownMemberType]
            assert r.get("fleet_graph:cache") == "v"  # pyright: ignore[reportUnknownMemberType]
            assert r.publish("ava:feat-x:events", "ok") == 0  # pyright: ignore[reportUnknownMemberType]
            with pytest.raises(NoPermissionError):
                r.publish("ava:other:events", "denied")  # pyright: ignore[reportUnknownMemberType]
            with pytest.raises(NoPermissionError):
                r.config_get("requirepass")  # pyright: ignore[reportUnknownMemberType]
        with redis.Redis.from_url(admin_url, decode_responses=True) as admin:  # pyright: ignore[reportUnknownMemberType]
            for command in (
                ("FLUSHALL",),
                ("SHUTDOWN",),
                ("ACL", "SETUSER", "ava_feat_x", "~*", "&*"),
                ("PSUBSCRIBE", "ava:other:inbound:*"),
            ):
                verdict = admin.execute_command("ACL", "DRYRUN", "ava_feat_x", *command)  # pyright: ignore[reportUnknownMemberType]
                assert verdict != "OK", command
