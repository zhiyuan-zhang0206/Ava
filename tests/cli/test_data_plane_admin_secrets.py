"""Post-migration data-plane credential split tests."""

from __future__ import annotations

import json
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
    monkeypatch.setattr(settings, "data_plane", settings.data_plane)

    legacy_db_url = f"postgresql://ava_main:{_LEGACY}@127.0.0.1:16433/ava_main"
    legacy_redis_url = f"redis://ava_main:{_LEGACY}@127.0.0.1:16380/0"
    # The production transition deliberately mutates os.environ so clients
    # created later in the surviving process adopt it. Arm monkeypatch with the
    # pre-transition values so teardown restores the test process for the next
    # module's DB-clean fixture.
    for key, value in {
        "AVA_DB_URL": legacy_db_url,
        "AVA_REDIS_URL": legacy_redis_url,
        "AVA_DB_ADMIN_PASSWORD": "",
        "AVA_REDIS_ADMIN_PASSWORD": "",
        "AVA_REDIS_PASSWORD": "",
    }.items():
        monkeypatch.setenv(key, value)

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
        legacy_db_url,
    )
    monkeypatch.setattr(
        settings.data_plane,
        "redis_url",
        legacy_redis_url,
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


def test_legacy_split_defers_without_a_handoff_capable_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_gateway(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        f"AVA_DB_URL=postgresql://ava_main:{_LEGACY}@127.0.0.1:16433/ava_main\n"
        f"AVA_REDIS_URL=redis://ava_main:{_LEGACY}@127.0.0.1:16380/0\n"
    )
    monkeypatch.setattr(
        split,
        "ensure_cluster_role",
        lambda *_args, **_kwargs: pytest.fail("deferred split must not mutate postgres"),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert split.ensure_data_plane_admin_secrets(allow_legacy_upgrade=False) is False
    assert not split._transition_path().exists()


def test_transition_payload_rejects_unknown_protocol_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_gateway(monkeypatch, tmp_path)
    path = split._transition_path()
    path.parent.mkdir(parents=True)
    path.write_text('{"db_admin_password":"only-one-field"}')

    with pytest.raises(ValueError, match="malformed data-plane credential transition"):
        split.pending_data_plane_bootstrap_credentials()


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

    redis_commands: list[tuple[str, ...]] = []

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
            redis_commands.append(args)

    def _token_urlsafe(_bytes: int) -> str:
        return next(minted)

    def _pg_admin_url(_port: int) -> str:
        return "postgresql://admin"

    def _ensure_cluster_role(*_args: object, **kwargs: object) -> None:
        role_calls.append(kwargs)

    def _ensure_cluster_redis_acl(*_args: object, **kwargs: object) -> None:
        acl_calls.append(kwargs)

    def _upsert_env(_path: Path, values: dict[str, str], *, audit_site: str | None = None) -> None:
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
    assert redis_commands == [
        ("CONFIG", "SET", "requirepass", _NEW_REDIS_ADMIN),
        ("CONFIG", "REWRITE"),
    ]
    assert not split._transition_path().exists()


def test_credential_split_refuses_a_foreign_redis_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The credential split is local-instance provisioning: a URL naming a
    foreign host makes the whole data plane remote-managed (Task #1752), so
    the split must no-op — never dial or provision a foreign service — even
    when only ONE of the two URLs is foreign."""
    _patch_gateway(monkeypatch, tmp_path)
    foreign_redis_url = f"redis://ava_main:{_LEGACY}@10.0.0.7:16380/0"
    monkeypatch.setattr(settings.data_plane, "redis_url", foreign_redis_url)
    (tmp_path / ".env").write_text(
        f"AVA_DB_URL=postgresql://ava_main:{_LEGACY}@127.0.0.1:16433/ava_main\n"
        f"AVA_REDIS_URL={foreign_redis_url}\n"
    )

    def _must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("no local provisioning may run against a remote data plane")

    monkeypatch.setattr(split.secrets, "token_urlsafe", _must_not_run)
    monkeypatch.setattr(split, "ensure_cluster_role", _must_not_run)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(split, "ensure_cluster_redis_acl", _must_not_run)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(split, "upsert_env", _must_not_run)

    assert split.ensure_data_plane_admin_secrets() is False
    assert not split._transition_path().exists()


def test_interrupted_split_journals_one_secret_set_and_replays_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_gateway(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        f"AVA_DB_URL=postgresql://ava_main:{_LEGACY}@127.0.0.1:16433/ava_main\n"
        f"AVA_REDIS_URL=redis://ava_main:{_LEGACY}@127.0.0.1:16380/0\n"
    )
    minted = iter((_NEW_DB, _NEW_REDIS_ADMIN, _NEW_REDIS_RUNTIME))
    monkeypatch.setattr(split.secrets, "token_urlsafe", lambda _bytes: next(minted))  # pyright: ignore[reportUnknownArgumentType]

    def _interrupt(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(split, "ensure_cluster_role", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        split.ensure_data_plane_admin_secrets()

    journal = json.loads(split._transition_path().read_text())
    assert journal["db_admin_password"] == _NEW_DB
    assert journal["redis_admin_password"] == _NEW_REDIS_ADMIN
    assert journal["redis_password"] == _NEW_REDIS_RUNTIME

    role_passwords: list[str] = []

    def _record_role(*_args: object, **kwargs: object) -> None:
        role_passwords.append(str(kwargs["db_admin_password"]))

    def _redis_admin(_host: str, _port: int, _candidates: tuple[str, ...]) -> str:
        return _NEW_REDIS_ADMIN

    class _Redis:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["password"] == _NEW_REDIS_ADMIN

        def __enter__(self) -> _Redis:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute_command(self, *_args: str) -> None:
            return None

    monkeypatch.setattr(split, "ensure_cluster_role", _record_role)
    monkeypatch.setattr(split, "_working_redis_admin_password", _redis_admin)
    monkeypatch.setattr(split, "ensure_cluster_redis_acl", lambda *_args, **_kwargs: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("redis.Redis", _Redis)

    assert split.resume_pending_data_plane_admin_secrets() is True
    assert role_passwords == [_NEW_DB]
    assert not split._transition_path().exists()
    values = (tmp_path / ".env").read_text()
    assert f"AVA_DB_ADMIN_PASSWORD={_NEW_DB}" in values
    assert f"AVA_REDIS_ADMIN_PASSWORD={_NEW_REDIS_ADMIN}" in values
    assert f"AVA_REDIS_PASSWORD={_NEW_REDIS_RUNTIME}" in values


def test_gateway_data_plane_retries_with_journal_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cli.commands import _cluster_instance
    from cli.commands import start as start_mod

    record = cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", {"postgres": 15433, "redis": 16380, "pgbouncer": 16433}),
        gateway_home=str(tmp_path),
        created_at="now",
    )
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr("shared.cluster.get_record", lambda _home: record)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.cluster.redis_password_from_env", lambda: _LEGACY)
    monkeypatch.setattr(
        split,
        "pending_data_plane_bootstrap_credentials",
        lambda: (_NEW_DB, _NEW_REDIS_ADMIN, _NEW_REDIS_RUNTIME),
    )
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _LEGACY)
    monkeypatch.setattr(settings.data_plane, "db_admin_password", _LEGACY)
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", _LEGACY)
    monkeypatch.setattr(
        settings.data_plane, "db_url", f"postgresql://ava_main:{_LEGACY}@127.0.0.1:16433/ava_main"
    )
    monkeypatch.setattr(
        settings.data_plane, "redis_url", f"redis://ava:{_LEGACY}@127.0.0.1:16380/0"
    )

    attempts: list[tuple[str, str, str]] = []

    def _ensure(**kwargs: object) -> int:
        assert kwargs["identity"] == "ava_main"
        assert kwargs["redis_user"] == "ava"
        credentials = (
            str(kwargs["db_admin_password"]),
            str(kwargs["redis_admin_password"]),
            str(kwargs["redis_password"]),
        )
        attempts.append(credentials)
        return 0 if credentials == (_NEW_DB, _NEW_REDIS_ADMIN, _NEW_REDIS_RUNTIME) else 1

    monkeypatch.setattr(_cluster_instance, "ensure_cluster_instance", _ensure)

    assert start_mod._ensure_gateway_data_plane() == 0
    assert attempts == [
        (_LEGACY, _LEGACY, _LEGACY),
        (_NEW_DB, _NEW_REDIS_ADMIN, _NEW_REDIS_RUNTIME),
    ]
