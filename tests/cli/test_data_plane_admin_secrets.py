"""Post-migration data-plane credential split tests."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest

from cli.commands import _data_plane_admin_secrets as split
from shared import cluster
from shared.config import settings

_LEGACY = "legacy-bearer"
_NEW_DB = "new-db"
_NEW_REDIS_ADMIN = "new-redis-admin"
_NEW_REDIS_RUNTIME = "new-redis-runtime"


def _patch_gateway(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setattr(split, "ava_home", lambda: home)

    def _record(_home: Path) -> cluster.ClusterRecord:
        return cluster.ClusterRecord(
            ports=cast(
                "cluster.ClusterPorts", {"postgres": 15433, "redis": 16380, "pgbouncer": 16433}
            ),
            gateway_home=str(home),
            created_at="now",
        )

    monkeypatch.setattr(
        split,
        "get_record",
        _record,
    )
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _LEGACY)
    monkeypatch.setattr(settings.data_plane, "db_admin_password", "")
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", "")
    monkeypatch.setattr(
        settings.data_plane,
        "db_url",
        f"postgresql://ava_main:{_LEGACY}@127.0.0.1:16433/ava_main",
    )
    monkeypatch.setattr(
        settings.data_plane,
        "redis_url",
        f"redis://ava_main:{_LEGACY}@127.0.0.1:16380/0",
    )
    monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", False)


def test_no_auth_cluster_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")

    assert split.ensure_data_plane_admin_secrets() is False


def test_existing_split_values_are_a_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_gateway(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        "AVA_DB_ADMIN_PASSWORD=db-admin\n"
        "AVA_REDIS_ADMIN_PASSWORD=redis-admin\n"
        "AVA_REDIS_PASSWORD=redis-runtime\n"
    )

    assert split.ensure_data_plane_admin_secrets() is False


def test_missing_values_are_minted_activated_and_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_gateway(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        f"AVA_DB_URL=postgresql://ava_main:{_LEGACY}@127.0.0.1:16433/ava_main\n"
        f"AVA_REDIS_URL=redis://ava_main:{_LEGACY}@127.0.0.1:16380/0\n"
    )
    minted = iter((_NEW_DB, _NEW_REDIS_ADMIN, _NEW_REDIS_RUNTIME))
    role_calls: list[dict[str, object]] = []
    acl_calls: list[dict[str, object]] = []
    writes: list[dict[str, str]] = []

    class _Redis:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["password"] == _LEGACY

        def __enter__(self) -> _Redis:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def ping(self) -> bool:
            return True

        def execute_command(self, *args: str) -> None:
            assert args == ("CONFIG", "SET", "requirepass", _NEW_REDIS_ADMIN)

    def _token_urlsafe(_bytes: int) -> str:
        return next(minted)

    def _pg_admin_url(_port: int) -> str:
        return "postgresql://admin"

    def _ensure_cluster_role(*_args: object, **kwargs: object) -> None:
        role_calls.append(kwargs)

    def _ensure_cluster_redis_acl(*_args: object, **kwargs: object) -> None:
        acl_calls.append(kwargs)

    def _upsert_env(_path: Path, values: dict[str, str]) -> None:
        writes.append(values)

    monkeypatch.setattr(split.secrets, "token_urlsafe", _token_urlsafe)
    monkeypatch.setattr(split, "pg_admin_url", _pg_admin_url)
    monkeypatch.setattr(split, "ensure_cluster_role", _ensure_cluster_role)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(split, "ensure_cluster_redis_acl", _ensure_cluster_redis_acl)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("redis.Redis", _Redis)
    monkeypatch.setattr(split, "upsert_env", _upsert_env)

    assert split.ensure_data_plane_admin_secrets() is True

    assert role_calls == [{"base_admin_url": "postgresql://admin", "db_admin_password": _NEW_DB}]
    assert acl_calls == [
        {
            "redis_admin_url": f"redis://default:{_NEW_REDIS_ADMIN}@127.0.0.1:16380",
            "runtime_password": _NEW_REDIS_RUNTIME,
            "channel_prefix": settings.data_plane.events_channel.removesuffix(":events"),
        }
    ]
    write = writes[0]
    assert write["AVA_DB_ADMIN_PASSWORD"] == _NEW_DB
    assert write["AVA_REDIS_ADMIN_PASSWORD"] == _NEW_REDIS_ADMIN
    assert write["AVA_REDIS_PASSWORD"] == _NEW_REDIS_RUNTIME
    assert urlsplit(write["AVA_DB_URL"]).password == _NEW_DB
    assert urlsplit(write["AVA_REDIS_URL"]).password == _NEW_REDIS_RUNTIME
    assert settings.data_plane.db_admin_password == _NEW_DB
    assert settings.data_plane.redis_admin_password == _NEW_REDIS_ADMIN
    assert urlsplit(settings.data_plane.db_url).password == _NEW_DB
    assert urlsplit(settings.data_plane.redis_url).password == _NEW_REDIS_RUNTIME
