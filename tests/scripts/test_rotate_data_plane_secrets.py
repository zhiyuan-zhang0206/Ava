"""Contracts for independent data-plane credential rotation."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest

from scripts import rotate_data_plane_secrets as rotate
from shared import cluster
from shared.config import settings

_OLD_DB = "old-db"
_NEW_DB = "def"
_OLD_REDIS_ADMIN = "old-redis-admin"
_NEW_REDIS_ADMIN = "new-redis-admin"
_OLD_RUNNER_DB = "old-runner-db"
_NEW_RUNNER_DB = "ghi"
_OLD_REDIS_RUNTIME = "old-redis-runtime"
_NEW_REDIS_RUNTIME = "new-redis-runtime"


def _state(scope: str = "both") -> rotate.RotationState:
    return rotate.RotationState(
        scope=scope,
        identity="ava_main",
        old_db_admin_password=_OLD_DB,
        new_db_admin_password=_NEW_DB,
        old_redis_admin_password=_OLD_REDIS_ADMIN,
        new_redis_admin_password=_NEW_REDIS_ADMIN,
        old_runner_db_password=_OLD_RUNNER_DB,
        new_runner_db_password=_NEW_RUNNER_DB,
        old_redis_password=_OLD_REDIS_RUNTIME,
        new_redis_password=_NEW_REDIS_RUNTIME,
        pg_port=15433,
        redis_port=16380,
        pgbouncer_enabled=False,
        pgbouncer_port=16433,
    )


def _patch_gateway_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setattr(rotate, "ava_home", lambda: home)

    def _record(_home: Path) -> cluster.ClusterRecord:
        return cluster.ClusterRecord(
            ports=cast(
                "cluster.ClusterPorts", {"postgres": 15433, "redis": 16380, "pgbouncer": 16433}
            ),
            gateway_home=str(home),
            created_at="now",
        )

    monkeypatch.setattr(
        rotate,
        "get_record",
        _record,
    )
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "control-bearer")
    monkeypatch.setattr(
        settings.data_plane, "db_url", "postgresql://ava_main:old-db@127.0.0.1:16433/ava_main"
    )
    monkeypatch.setattr(
        settings.data_plane,
        "redis_url",
        "redis://ava_main:old-redis-runtime@127.0.0.1:16380/0",
    )
    monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", False)


def test_build_state_keeps_the_bearer_out_of_new_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_gateway_home(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        "AVA_DB_ADMIN_PASSWORD=old-db\n"
        "AVA_REDIS_ADMIN_PASSWORD=old-redis-admin\n"
        "AVA_RUNNER_DB_PASSWORD=old-runner-db\n"
        "AVA_REDIS_PASSWORD=old-redis-runtime\n"
    )

    state = rotate.build_state()

    assert state.scope == "both"
    assert state.new_db_admin_password not in {"control-bearer", state.old_db_admin_password}
    assert state.new_redis_admin_password not in {"control-bearer", state.old_redis_admin_password}
    assert state.new_runner_db_password not in {"control-bearer", state.old_runner_db_password}
    assert state.new_redis_password not in {"control-bearer", state.old_redis_password}


def test_runner_scope_requires_the_existing_runner_db_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_gateway_home(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        "AVA_DB_ADMIN_PASSWORD=old-db\nAVA_REDIS_ADMIN_PASSWORD=old-redis-admin\n"
    )

    with pytest.raises(RuntimeError, match="ensure-db-role"):
        rotate.build_state("runner")


def test_admin_scope_changes_owner_and_redis_default_only(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state("admin")
    role_calls: list[dict[str, object]] = []
    redis_commands: list[tuple[str, ...]] = []

    class _Redis:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Redis:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute_command(self, *args: str) -> None:
            redis_commands.append(args)

    def _pg_admin_url(_port: int) -> str:
        return "postgresql://admin"

    def _ensure_cluster_role(*_args: object, **kwargs: object) -> None:
        role_calls.append(kwargs)

    def _working_redis_admin_password(_state: rotate.RotationState) -> str:
        return "old-redis-admin"

    monkeypatch.setattr(rotate, "pg_admin_url", _pg_admin_url)
    monkeypatch.setattr(rotate, "ensure_cluster_role", _ensure_cluster_role)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(rotate, "_working_redis_admin_password", _working_redis_admin_password)
    monkeypatch.setattr(rotate.redis, "Redis", _Redis)

    rotate.apply_admin(state)

    assert role_calls == [{"base_admin_url": "postgresql://admin", "db_admin_password": "def"}]
    assert redis_commands == [("CONFIG", "SET", "requirepass", "new-redis-admin")]
    assert state.runner_db_password == _OLD_RUNNER_DB
    assert state.redis_password == _OLD_REDIS_RUNTIME


def test_runner_scope_rotates_both_runtime_credentials_and_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state("runner")
    runner_calls: list[dict[str, object]] = []
    acl_calls: list[dict[str, object]] = []

    def _pg_admin_url(_port: int) -> str:
        return "postgresql://admin"

    def _ensure_runner_role(*_args: object, **kwargs: object) -> None:
        runner_calls.append(kwargs)

    def _working_redis_admin_password(_state: rotate.RotationState) -> str:
        return "old-redis-admin"

    def _ensure_cluster_redis_acl(*_args: object, **kwargs: object) -> None:
        acl_calls.append(kwargs)

    monkeypatch.setattr(rotate, "pg_admin_url", _pg_admin_url)
    monkeypatch.setattr(rotate, "ensure_runner_role", _ensure_runner_role)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(rotate, "_working_redis_admin_password", _working_redis_admin_password)
    monkeypatch.setattr(rotate, "ensure_cluster_redis_acl", _ensure_cluster_redis_acl)  # pyright: ignore[reportUnknownArgumentType]

    rotate.apply_runner(state)

    assert runner_calls == [{"base_admin_url": "postgresql://admin", "runner_password": "ghi"}]
    assert acl_calls == [
        {
            "redis_admin_url": "redis://default:old-redis-admin@127.0.0.1:16380",
            "runtime_password": "new-redis-runtime",
            "channel_prefix": settings.data_plane.events_channel.removesuffix(":events"),
        }
    ]


def test_write_env_syncs_urls_with_the_active_scoped_passwords(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_gateway_home(monkeypatch, tmp_path)
    state = _state("runner")
    writes: list[dict[str, str]] = []

    def _upsert_env(_path: Path, values: dict[str, str]) -> None:
        writes.append(values)

    monkeypatch.setattr(rotate, "upsert_env", _upsert_env)

    rotate.write_env(state)

    write = writes[0]
    assert write["AVA_DB_ADMIN_PASSWORD"] == _OLD_DB
    assert write["AVA_REDIS_ADMIN_PASSWORD"] == _OLD_REDIS_ADMIN
    assert write["AVA_RUNNER_DB_PASSWORD"] == _NEW_RUNNER_DB
    assert write["AVA_REDIS_PASSWORD"] == _NEW_REDIS_RUNTIME
    assert urlsplit(write["AVA_DB_URL"]).password == _OLD_DB
    assert urlsplit(write["AVA_REDIS_URL"]).password == _NEW_REDIS_RUNTIME


def test_dry_run_never_calls_a_mutating_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    mutations: list[str] = []

    def _build_state(_scope: str) -> rotate.RotationState:
        return state

    def _print_plan(*_args: object, **_kwargs: object) -> None:
        return None

    def _preflight(_state: rotate.RotationState) -> bool:
        return True

    def _apply_admin(_state: rotate.RotationState) -> None:
        mutations.append("admin")

    monkeypatch.setattr(rotate, "build_state", _build_state)
    monkeypatch.setattr(rotate, "print_plan", _print_plan)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(rotate, "preflight", _preflight)
    monkeypatch.setattr(rotate, "apply_admin", _apply_admin)

    assert rotate.main([]) == 0
    assert mutations == []


def test_probes_and_dials_use_the_state_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rotation probes and admin dials must go to the URL-derived hosts on
    RotationState — asserted against a foreign host, so a re-hardcoded loopback
    literal fails this test (the loopback default alone would be vacuous)."""
    state = _state()
    state.pg_host = "10.0.0.7"
    state.redis_host = "10.0.0.7"

    pg_urls: list[str] = []
    redis_hosts: list[str] = []
    acl_urls: list[str] = []

    class _PgConn:
        def __enter__(self) -> _PgConn:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _sql: str) -> None:
            return None

    def _connect(url: str, **_kwargs: object) -> _PgConn:
        pg_urls.append(url)
        return _PgConn()

    class _Redis:
        def __init__(self, **kwargs: object) -> None:
            redis_hosts.append(str(kwargs["host"]))

        def __enter__(self) -> _Redis:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def ping(self) -> bool:
            return True

        def execute_command(self, *_args: str) -> None:
            return None

    def _ensure_cluster_role(*_args: object, **_kwargs: object) -> None:
        return None

    def _ensure_runner_role(*_args: object, **_kwargs: object) -> None:
        return None

    def _working_redis_admin_password(_state: rotate.RotationState) -> str:
        return "old-redis-admin"

    def _ensure_cluster_redis_acl(*_args: object, **kwargs: object) -> None:
        acl_urls.append(str(kwargs["redis_admin_url"]))

    monkeypatch.setattr(rotate.psycopg, "connect", _connect)
    monkeypatch.setattr(rotate.redis, "Redis", _Redis)
    monkeypatch.setattr(rotate, "ensure_cluster_role", _ensure_cluster_role)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(rotate, "ensure_runner_role", _ensure_runner_role)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(rotate, "_working_redis_admin_password", _working_redis_admin_password)
    monkeypatch.setattr(rotate, "ensure_cluster_redis_acl", _ensure_cluster_redis_acl)  # pyright: ignore[reportUnknownArgumentType]

    assert (
        rotate._pg_probe(state.pg_host, "ava_main", "ava_main", state.pg_port, "probe-pw") is True
    )
    assert (
        rotate._redis_probe(state.redis_host, state.redis_port, "probe-pw", username="default")
        is True
    )
    assert pg_urls == ["postgresql://ava_main:probe-pw@10.0.0.7:15433/ava_main"]
    assert redis_hosts == ["10.0.0.7"]

    rotate.apply_admin(state)
    rotate.apply_runner(state)
    # The CONFIG SET dial and the ACL-provisioning admin URL both hit the host.
    assert redis_hosts == ["10.0.0.7", "10.0.0.7"]
    assert acl_urls == ["redis://default:old-redis-admin@10.0.0.7:16380"]


def test_build_state_derives_hosts_from_the_cluster_urls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """build_state snapshots the dial hosts from this cluster's own URLs — the
    replaceable-source contract (external data plane, Task #1752)."""
    _patch_gateway_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        settings.data_plane, "db_url", "postgresql://ava_main:old-db@10.0.0.7:16433/ava_main"
    )
    monkeypatch.setattr(
        settings.data_plane, "redis_url", "redis://ava_main:old-redis-runtime@10.0.0.7:16380/0"
    )
    (tmp_path / ".env").write_text(
        "AVA_DB_ADMIN_PASSWORD=old-db\n"
        "AVA_REDIS_ADMIN_PASSWORD=old-redis-admin\n"
        "AVA_RUNNER_DB_PASSWORD=old-runner-db\n"
        "AVA_REDIS_PASSWORD=old-redis-runtime\n"
    )

    state = rotate.build_state()

    assert state.pg_host == "10.0.0.7"
    assert state.redis_host == "10.0.0.7"


def test_old_journal_without_host_fields_resumes_loopback(tmp_path: Path) -> None:
    """A rotation journal written before the host fields existed (no pg_host /
    redis_host) resumes with the loopback default — an in-flight rotation must
    survive the upgrade."""
    import json
    from dataclasses import asdict

    state = _state()
    data = asdict(state)
    del data["pg_host"]
    del data["redis_host"]
    path = tmp_path / "data-plane-old.json"
    path.write_text(json.dumps(data))

    loaded = rotate.RotationState.load(path)

    assert loaded.pg_host == "127.0.0.1"
    assert loaded.redis_host == "127.0.0.1"


def test_recovery_state_is_owner_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(rotate, "ava_home", lambda: tmp_path)
    state = _state()
    path = state.save()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert rotate.RotationState.load(path) == state
