"""redis-acl healthcheck: a redis-server restart drops the in-memory per-cluster
ACL user (redis.conf carries only the admin requirepass); the gateway watchdog's
redis-acl check re-affirms the user within one round instead of waiting for the
next `ava start` (2026-07-14 prod outage).

Runs against a throwaway redis-server (tests._containers.redis_server) — no prod.
"""

from __future__ import annotations

import logging

import pytest
import redis
from redis.exceptions import AuthenticationError

from services.healthchecks import redis_acl
from services.healthchecks.redis_acl import check
from shared.cluster import ensure_cluster_redis_acl
from shared.config import settings
from shared.url_secret import url_with_userinfo
from tests._containers import redis_server

_SECRET = "redisaclhealthsecret"  # noqa: S105 — test fixture, not a real credential
_USER = "ava_feat_x"  # the ACL identifier, passed as data (names-as-data)
_PREFIX = "ava:feat-x"


def _provision(admin_url: str) -> str:
    """Create the cluster ACL user; return the cluster-identity URL."""
    ensure_cluster_redis_acl(
        _USER, redis_admin_url=admin_url, cluster_secret=_SECRET, channel_prefix=_PREFIX
    )
    return url_with_userinfo(admin_url, _USER, _SECRET)


def _check(cluster_url: str, admin_url: str) -> None:
    check(
        _USER,
        cluster_url=cluster_url,
        admin_url=admin_url,
        cluster_secret=_SECRET,
        channel_prefix=_PREFIX,
    )


def test_repairs_dropped_acl_user() -> None:
    """The outage shape: user provisioned, redis 'restarts' (DELUSER stands in for
    the memory wipe), the check restores authentication."""
    with redis_server() as admin_url:
        cluster_url = _provision(admin_url)
        with redis.Redis.from_url(admin_url) as r:  # pyright: ignore[reportUnknownMemberType]
            r.execute_command("ACL", "DELUSER", "ava_feat_x")  # pyright: ignore[reportUnknownMemberType]
        with redis.Redis.from_url(cluster_url) as r, pytest.raises(AuthenticationError):  # pyright: ignore[reportUnknownMemberType]
            r.ping()  # pyright: ignore[reportUnknownMemberType]

        _check(cluster_url, admin_url)

        with redis.Redis.from_url(cluster_url) as r:  # pyright: ignore[reportUnknownMemberType]
            assert r.ping()  # pyright: ignore[reportUnknownMemberType]


def test_noop_when_healthy() -> None:
    with redis_server() as admin_url:
        cluster_url = _provision(admin_url)
        _check(cluster_url, admin_url)
        with redis.Redis.from_url(cluster_url) as r:  # pyright: ignore[reportUnknownMemberType]
            assert r.ping()  # pyright: ignore[reportUnknownMemberType]


def test_unreachable_server_is_left_to_the_service_manager() -> None:
    """A dead redis is not this check's job (launchd/systemd revives the process);
    the check must return quietly, not raise or hang."""
    dead = "redis://127.0.0.1:1/0"
    _check(dead, dead)


def test_repairs_via_password_authed_admin() -> None:
    """The prod shape: redis carries a requirepass, so the repair path must
    authenticate as `default` with the box admin secret (redis_admin_url's form) —
    the passwordless-fixture tests above would not catch a broken admin-auth path."""
    with redis_server() as admin_url:
        with redis.Redis.from_url(admin_url) as r:  # pyright: ignore[reportUnknownMemberType]
            r.execute_command("CONFIG", "SET", "requirepass", "boxadminsecret")  # pyright: ignore[reportUnknownMemberType]
        authed_admin = url_with_userinfo(admin_url, "default", "boxadminsecret")
        ensure_cluster_redis_acl(
            _USER, redis_admin_url=authed_admin, cluster_secret=_SECRET, channel_prefix=_PREFIX
        )
        cluster_url = url_with_userinfo(admin_url, "ava_feat_x", _SECRET)
        with redis.Redis.from_url(authed_admin) as r:  # pyright: ignore[reportUnknownMemberType]
            r.execute_command("ACL", "DELUSER", "ava_feat_x")  # pyright: ignore[reportUnknownMemberType]

        check(
            _USER,
            cluster_url=cluster_url,
            admin_url=authed_admin,
            cluster_secret=_SECRET,
            channel_prefix=_PREFIX,
        )

        with redis.Redis.from_url(cluster_url) as r:  # pyright: ignore[reportUnknownMemberType]
            assert r.ping()  # pyright: ignore[reportUnknownMemberType]


def test_failing_repair_raises() -> None:
    """When the admin identity itself is rejected the check must fail loud — the
    watchdog logs it as a failing healthcheck; nothing is swallowed."""
    with redis_server() as admin_url:
        cluster_url = _provision(admin_url)
        with redis.Redis.from_url(admin_url) as r:  # pyright: ignore[reportUnknownMemberType]
            r.execute_command("ACL", "DELUSER", "ava_feat_x")  # pyright: ignore[reportUnknownMemberType]
        # The fixture's `default` user is nopass (any AUTH succeeds), so a wrong
        # password would not be rejected — an unknown username is.
        bad_admin = url_with_userinfo(admin_url, "no_such_admin", "irrelevant")
        with pytest.raises(AuthenticationError):
            _check(cluster_url, bad_admin)


def test_main_skips_when_url_carries_no_username(monkeypatch: pytest.MonkeyPatch, caplog) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """The legacy .env shape (`redis://:<secret>@host/0`): the runtime dials as the
    redis `default` user (requirepass persists across restarts), so no ACL identity
    can be dropped and the check must SKIP with a warning — not raise a traceback
    every watchdog round (the 4.5k-ERROR flood this regression test pins)."""
    from services.healthchecks import redis_acl

    monkeypatch.setattr(redis_acl, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(redis_acl.settings.data_plane, "redis_url", "redis://:sek@127.0.0.1:6380/0")
    # the legacy .env shape implies a cluster with a secret — the no-secret
    # skip must not pre-empt the legacy-URL skip
    monkeypatch.setattr(redis_acl.settings.data_plane, "cluster_secret", "sek")
    calls = []
    monkeypatch.setattr(redis_acl, "check", lambda *a, **k: calls.append((a, k)))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    with caplog.at_level(logging.WARNING, logger="services.healthchecks.redis_acl"):  # pyright: ignore[reportUnknownMemberType]
        redis_acl.main()
    assert calls == []
    assert "no ACL username" in caplog.text  # pyright: ignore[reportUnknownMemberType]
    assert "127.0.0.1:6380" in caplog.text  # pyright: ignore[reportUnknownMemberType]
    assert (
        "sek" not in caplog.text  # pyright: ignore[reportUnknownMemberType]
    )  # the credential never reaches a log line  # pyright: ignore[reportUnknownMemberType]


def test_main_runs_check_with_the_url_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.healthchecks import redis_acl

    monkeypatch.setattr(redis_acl.settings.data_plane, "cluster_secret", "sek")
    """A URL carrying a username goes to check() with the identity read from the
    URL as data (names-as-data), not a re-derived one."""
    from services.healthchecks import redis_acl

    monkeypatch.setattr(redis_acl, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(
        redis_acl.settings.data_plane, "redis_url", "redis://ava_main:sek@127.0.0.1:6380/0"
    )
    calls = []
    monkeypatch.setattr(redis_acl, "check", lambda *a, **k: calls.append((a, k)))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    redis_acl.main()
    assert len(calls) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert calls[0][0][0] == "ava_main"
    assert calls[0][1]["cluster_url"] == "redis://ava_main:sek@127.0.0.1:6380/0"


def test_main_skips_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """A no-secret cluster provisions no ACL user and no requirepass — nothing
    to re-affirm; the check skips instead of trying to provision against a
    passwordless admin user."""
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    called: list[str] = []

    def _noop_check(*_a, **_k):  # type: ignore[no-untyped-def]
        called.append("check")

    monkeypatch.setattr(redis_acl, "check", _noop_check)  # pyright: ignore[reportUnknownArgumentType]
    redis_acl.main()
    assert called == []
