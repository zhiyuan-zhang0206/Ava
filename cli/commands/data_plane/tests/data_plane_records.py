"""Synthetic cluster records and the data-plane config every data-plane test starts from."""

import dataclasses
from typing import cast

import pytest

from base import cluster
from base.config.tests.configuration import snapshot_process_config
from base.paths.home import resolve_ava_home
from cli.commands.data_plane.config import DataPlaneConfig, data_plane_config_from_boot


def cluster_record(ports: dict[str, int], *, created_at: str) -> cluster.ClusterRecord:
    return cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", ports),
        gateway_home=str(resolve_ava_home()),
        created_at=created_at,
    )


def remote_cluster_record() -> cluster.ClusterRecord:
    return cluster_record(
        {
            "gateway": 18000,
            "frontend": 18001,
            "heartbeat": 18002,
            "labeler": 18004,
            "task_maintenance": 18005,
            "memory_indexer": 18006,
            "ops": 18007,
            "browser": 18009,
            "permissions_helper": 18010,
            "postgres": 18011,
            "redis": 18012,
            "events_maintenance": 18014,
            "delivery_watchdog": 18016,
            "im_bridge": 18017,
            "agent_host": 18019,
            "pg_backup": 18021,
            "ttl_reaper": 18025,
            "schedule_manager": 18026,
        },
        created_at="now",
    )


FOREIGN_DB = "postgresql://ava:pw@10.9.8.7:5432/ava"
FOREIGN_REDIS = "rediss://ava:pw@10.9.8.7:6380/0"


@pytest.fixture
def data_plane_config() -> DataPlaneConfig:
    """The unit environment's values, built by the mapping function the commands use.

    Tests change a field with `dataclasses.replace(data_plane_config, ...)`; nothing patches `settings`."""
    return data_plane_config_from_boot(snapshot_process_config())


@pytest.fixture
def remote_data_plane_config(data_plane_config: DataPlaneConfig) -> DataPlaneConfig:
    """A data plane hosted off-box: both URLs name a foreign host."""
    return dataclasses.replace(data_plane_config, db_url=FOREIGN_DB, redis_url=FOREIGN_REDIS)


def with_redis_admin(config: DataPlaneConfig, password: str) -> DataPlaneConfig:
    """`config` with the Redis admin login `password` (positional, so a literal is no hard-coded secret)."""
    return dataclasses.replace(config, redis_admin_password=password)
