"""`DataPlaneConfig`: what the data-plane commands read, mapped once and never leaked."""

from __future__ import annotations

import dataclasses

import pytest

from base.config.tests.configuration import snapshot_process_config
from cli.commands.data_plane.config import DataPlaneConfig, data_plane_config_from_boot

_LOOPBACK_DB = "postgresql://meiva@127.0.0.1:5432/meiva"
_LOOPBACK_REDIS = "redis://meiva@127.0.0.1:6380/0"
_FOREIGN = "10.9.8.7"


def test_the_mapping_reads_every_field_from_its_own_setting(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    boot = snapshot_process_config()
    redis_bin = str(tmp_path_factory.mktemp("redis-bin"))
    expected = {
        "db_url": "postgresql://owner:db-secret@db.example:5432/meiva",
        "redis_url": "redis://meiva:redis-secret@cache.example:6380/0",
        "redis_admin_password": "mapped-admin-password",
        "cluster_secret": "mapped-cluster-secret-0123456789abcdef",
        "pgbouncer_enabled": False,
        "pooler_max_client_conn": 1234,
        "redis_bin_dir": redis_bin,
        "trusted_cidrs": "10.1.0.0/16",
        "events_channel": "meiva:mapped:events",
        "memory_search_backend": "numpy",
        "embedding_backend": "gemini",
    }
    for name, value in expected.items():
        boot.set_field(name, value)

    config = data_plane_config_from_boot(boot)

    assert {
        field.name: getattr(config, field.name) for field in dataclasses.fields(config)
    } == expected


def test_is_remote_is_derived_from_the_urls_and_follows_a_replaced_url(
    data_plane_config: DataPlaneConfig,
) -> None:
    local = dataclasses.replace(data_plane_config, db_url=_LOOPBACK_DB, redis_url=_LOOPBACK_REDIS)
    assert local.is_remote is False
    assert dataclasses.replace(local, db_url=f"postgresql://meiva@{_FOREIGN}:5432/meiva").is_remote
    assert dataclasses.replace(local, redis_url=f"redis://meiva@{_FOREIGN}:6380/0").is_remote
    assert "is_remote" not in {field.name for field in dataclasses.fields(local)}


def test_no_secret_or_login_reaches_the_repr(data_plane_config: DataPlaneConfig) -> None:
    config = dataclasses.replace(
        data_plane_config,
        db_url="postgresql://owner:db-secret@db.example:5432/meiva",
        redis_url="redis://meiva:redis-secret@cache.example:6380/0",
        redis_admin_password="admin-secret-value",  # noqa: S106
        cluster_secret="cluster-secret-value",  # noqa: S106
    )
    text = repr(config)
    for hidden in (
        "db-secret",
        "redis-secret",
        "admin-secret-value",
        "cluster-secret-value",
        "db.example",
    ):
        assert hidden not in text
    assert "pgbouncer_enabled" in text and "trusted_cidrs" in text  # the repr still names the rest


def test_the_config_is_frozen(data_plane_config: DataPlaneConfig) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        data_plane_config.pgbouncer_enabled = not data_plane_config.pgbouncer_enabled  # pyright: ignore[reportAttributeAccessIssue]
