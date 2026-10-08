"""Synthetic cluster records shared by data-plane contract tests."""

from typing import cast

import pytest

from base import cluster
from base.config import settings
from base.host.env.dotenv_boot import resolve_ava_home


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
            "insights": 18027,
        },
        created_at="now",
    )


_FOREIGN_DB = "postgresql://ava:pw@10.9.8.7:5432/ava"
_FOREIGN_REDIS = "rediss://ava:pw@10.9.8.7:6380/0"


@pytest.fixture(autouse=True)
def remote_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the settings singleton at a foreign data plane for every test here
    (restored by monkeypatch after each test)."""
    monkeypatch.setattr(settings.data_plane, "db_url", _FOREIGN_DB)
    monkeypatch.setattr(settings.data_plane, "redis_url", _FOREIGN_REDIS)
