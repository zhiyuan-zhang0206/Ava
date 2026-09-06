"""redis-acl healthcheck: a redis-server restart drops the in-memory per-cluster
ACL user (redis.conf carries only the admin requirepass); the gateway watchdog's
redis-acl check re-affirms the user within one round instead of waiting for the
next `ava start` (2026-07-14 prod outage).

Runs against a throwaway redis-server (tests._containers.redis_server) — no prod.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import redis
from redis.exceptions import AuthenticationError, ConnectionError

from services.healthchecks import redis_acl
from services.healthchecks.redis_acl import check
from shared.cluster import ensure_cluster_redis_acl
from shared.config import settings
from shared.url_secret import url_with_userinfo
from tests._containers import redis_server

_SECRET = "redisaclhealthsecret"  # noqa: S105 — test fixture, not a real credential
_USER = "ava_feat_x"  # the ACL identifier, passed as data (names-as-data)
_PREFIX = "ava:feat-x"
_REDIS_PORT = 16380


def _registry_record(_home: Path) -> object:
    """Placeholder only for `main()` tests; its port accessor is patched too."""
    return object()


def _registry_redis_port(_rec: object) -> int:
    return _REDIS_PORT


def _provision(admin_url: str) -> str:
    """Create the cluster ACL user; return the cluster-identity URL."""
    ensure_cluster_redis_acl(
        _USER, redis_admin_url=admin_url, runtime_password=_SECRET, channel_prefix=_PREFIX
    )
    return url_with_userinfo(admin_url, _USER, _SECRET)


def _check(cluster_url: str, admin_url: str) -> None:
    check(
        _USER,
        redis_port=_REDIS_PORT,
        cluster_url=cluster_url,
        admin_url=admin_url,
        cluster_secret=_SECRET,
        channel_prefix=_PREFIX,
        reaffirm_acl=True,
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


def test_unreachable_server_is_restarted_and_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead Redis is restarted through the same idempotent bring-up as `ava start`,
    then PINGed again before the check claims recovery."""
    pings: list[Exception | None] = [ConnectionError("connection refused"), None]
    starts: list[tuple[int, str, str, str, str]] = []

    def _mock_ping(_url: str) -> None:
        outcome = pings.pop(0)
        if outcome is not None:
            raise outcome

    def _start(port: int, redis_admin: str, runtime: str, bearer: str, identity: str) -> int:
        starts.append((port, redis_admin, runtime, bearer, identity))
        return 0

    monkeypatch.setattr(redis_acl, "_ping", _mock_ping)
    monkeypatch.setattr(
        "cli.commands._cluster_instance._start_redis",
        _start,
        raising=True,
    )

    _check("redis://ava_feat_x@127.0.0.1:16380/0", "redis://127.0.0.1:16380/0")

    assert starts == [(_REDIS_PORT, _SECRET, _SECRET, _SECRET, _USER)]
    assert pings == []


def test_failed_redis_restart_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A nonzero bring-up is not a repair: raising lets the watchdog report the
    continuing outage every round."""

    def _dead_ping(_url: str) -> None:
        raise ConnectionError("connection refused")

    def _failed_start(_port: int, _admin: str, _runtime: str, _bearer: str, _identity: str) -> int:
        return 1

    monkeypatch.setattr(redis_acl, "_ping", _dead_ping)
    monkeypatch.setattr("cli.commands._cluster_instance._start_redis", _failed_start, raising=True)

    with pytest.raises(RuntimeError, match="rc=1"):
        _check("redis://ava_feat_x@127.0.0.1:16380/0", "redis://127.0.0.1:16380/0")


def test_repairs_via_password_authed_admin() -> None:
    """The prod shape: redis carries a requirepass, so the repair path must
    authenticate as `default` with the box admin secret (redis_admin_url's form) —
    the passwordless-fixture tests above would not catch a broken admin-auth path."""
    with redis_server() as admin_url:
        with redis.Redis.from_url(admin_url) as r:  # pyright: ignore[reportUnknownMemberType]
            r.execute_command("CONFIG", "SET", "requirepass", "boxadminsecret")  # pyright: ignore[reportUnknownMemberType]
        authed_admin = url_with_userinfo(admin_url, "default", "boxadminsecret")
        ensure_cluster_redis_acl(
            _USER, redis_admin_url=authed_admin, runtime_password=_SECRET, channel_prefix=_PREFIX
        )
        cluster_url = url_with_userinfo(admin_url, "ava_feat_x", _SECRET)
        with redis.Redis.from_url(authed_admin) as r:  # pyright: ignore[reportUnknownMemberType]
            r.execute_command("ACL", "DELUSER", "ava_feat_x")  # pyright: ignore[reportUnknownMemberType]

        check(
            _USER,
            redis_port=_REDIS_PORT,
            cluster_url=cluster_url,
            admin_url=authed_admin,
            cluster_secret=_SECRET,
            redis_admin_password="boxadminsecret",  # noqa: S106 — test fixture
            runtime_password=_SECRET,
            channel_prefix=_PREFIX,
            reaffirm_acl=True,
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


def test_main_skips_when_url_carries_no_username(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """The legacy .env shape (`redis://:<secret>@host/0`): the runtime dials as the
    redis `default` user (requirepass persists across restarts), so no ACL identity
    can be dropped and the check must SKIP with a warning — not raise a traceback
    every watchdog round (the 4.5k-ERROR flood this regression test pins)."""
    from services.healthchecks import redis_acl

    monkeypatch.setattr(redis_acl, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(redis_acl.settings.data_plane, "redis_url", "redis://:sek@127.0.0.1:6380/0")
    # the legacy .env shape implies a cluster with a secret — the no-secret
    # skip must not pre-empt the legacy-URL skip
    monkeypatch.setattr(redis_acl.settings.data_plane, "cluster_secret", "sek")
    monkeypatch.setattr(redis_acl, "get_record", _registry_record)
    monkeypatch.setattr(redis_acl, "record_redis_port", _registry_redis_port)
    calls = []
    monkeypatch.setattr(redis_acl, "check", lambda *a, **k: calls.append((a, k)))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    with caplog.at_level(logging.WARNING, logger="services.healthchecks.redis_acl"):  # pyright: ignore[reportUnknownMemberType]
        redis_acl.main()
    assert calls == []
    assert "no ACL username" in caplog.text  # pyright: ignore[reportUnknownMemberType]
    assert "127.0.0.1:6380" in caplog.text  # pyright: ignore[reportUnknownMemberType]
    assert (
        "sek" not in caplog.text  # pyright: ignore[reportUnknownMemberType]
    )  # the credential never reaches a log line


def test_main_runs_check_with_the_url_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL carrying a username goes to check() with the identity read from the
    URL as data (names-as-data), not a re-derived one."""
    monkeypatch.setattr(redis_acl, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(redis_acl, "get_record", _registry_record)
    monkeypatch.setattr(redis_acl, "record_redis_port", _registry_redis_port)
    monkeypatch.setattr(redis_acl.settings.data_plane, "cluster_secret", "sek")
    monkeypatch.setattr(
        redis_acl.settings.data_plane, "redis_url", "redis://ava_main:sek@127.0.0.1:6380/0"
    )
    calls = []
    monkeypatch.setattr(redis_acl, "check", lambda *a, **k: calls.append((a, k)))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    redis_acl.main()
    assert len(calls) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert calls[0][0][0] == "ava_main"
    assert calls[0][1]["cluster_url"] == "redis://ava_main:sek@127.0.0.1:6380/0"
    assert calls[0][1]["redis_port"] == _REDIS_PORT
    assert calls[0][1]["reaffirm_acl"] is True


def test_main_without_secret_runs_the_liveness_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-secret clusters have no ACL re-affirmation, but their local Redis is
    still the message bus and must be restarted when it dies."""
    monkeypatch.setattr(redis_acl, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(redis_acl, "get_record", _registry_record)
    monkeypatch.setattr(redis_acl, "record_redis_port", _registry_redis_port)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    monkeypatch.setattr(settings.data_plane, "redis_url", "redis://ava_no_auth@127.0.0.1:6380/0")
    calls = []

    monkeypatch.setattr(redis_acl, "check", lambda *a, **k: calls.append((a, k)))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    redis_acl.main()

    assert len(calls) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert calls[0][0][0] == "ava_no_auth"
    assert calls[0][1]["redis_port"] == _REDIS_PORT
    assert calls[0][1]["reaffirm_acl"] is False


def test_no_secret_unreachable_server_is_restarted(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-secret mode skips the ACL branch but still uses `_start_redis` to restore
    the message bus after its local listener disappears."""
    pings: list[Exception | None] = [ConnectionError("connection refused"), None]
    starts: list[tuple[int, str, str, str, str]] = []

    def _mock_ping(_url: str) -> None:
        outcome = pings.pop(0)
        if outcome is not None:
            raise outcome

    def _start(port: int, redis_admin: str, runtime: str, bearer: str, identity: str) -> int:
        starts.append((port, redis_admin, runtime, bearer, identity))
        return 0

    def _unexpected_acl(
        _user: str, *, redis_admin_url: str, runtime_password: str, channel_prefix: str
    ) -> None:
        pytest.fail("no-secret liveness repair must skip ACL re-affirmation")

    monkeypatch.setattr(redis_acl, "_ping", _mock_ping)
    monkeypatch.setattr(
        "cli.commands._cluster_instance._start_redis",
        _start,
        raising=True,
    )
    monkeypatch.setattr(redis_acl, "ensure_cluster_redis_acl", _unexpected_acl)

    check(
        _USER,
        redis_port=_REDIS_PORT,
        cluster_url="redis://ava_feat_x@127.0.0.1:16380/0",
        admin_url="redis://127.0.0.1:16380/0",
        cluster_secret="",
        channel_prefix=_PREFIX,
        reaffirm_acl=False,
    )

    assert starts == [(_REDIS_PORT, "", "", "", _USER)]
    assert pings == []


def test_no_secret_auth_failure_does_not_reaffirm_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-secret mode probes and respawns for liveness, but does not replay the
    credentialed ACL repair path when a reachable server rejects the identity."""

    def _rejected_ping(_url: str) -> None:
        raise AuthenticationError("bad auth")

    def _unexpected_acl(
        _user: str, *, redis_admin_url: str, runtime_password: str, channel_prefix: str
    ) -> None:
        pytest.fail("no-secret mode must skip ACL re-affirmation")

    monkeypatch.setattr(redis_acl, "_ping", _rejected_ping)
    monkeypatch.setattr(redis_acl, "ensure_cluster_redis_acl", _unexpected_acl)

    with pytest.raises(AuthenticationError):
        check(
            _USER,
            redis_port=_REDIS_PORT,
            cluster_url="redis://ava_feat_x@127.0.0.1:16380/0",
            admin_url="redis://127.0.0.1:16380/0",
            cluster_secret="",
            channel_prefix=_PREFIX,
            reaffirm_acl=False,
        )
