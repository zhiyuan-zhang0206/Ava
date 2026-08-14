"""`redis_admin_url` derives from this cluster's own redis instance.

Every cluster owns its Postgres+Redis, so the admin URL is always the `default`
user (requirepass == the cluster secret) on the cluster's own redis port, read
from `settings.data_plane.redis_url` — there is no shared-instance / box-admin-secret path.
"""

import pytest

from shared import cluster


def test_redis_admin_url_uses_cluster_secret_and_own_port(monkeypatch: pytest.MonkeyPatch):
    """main's own instance: admin URL carries the cluster secret and the record's
    redis port (5433/6380 for main), never a box admin secret / 6379."""
    monkeypatch.setattr(
        cluster.settings.data_plane, "redis_url", "redis://ava_main:sek@127.0.0.1:6380/0"
    )
    monkeypatch.setattr(cluster.settings.data_plane, "cluster_secret", "sek")
    assert cluster.redis_admin_url() == "redis://default:sek@127.0.0.1:6380"


def test_redis_admin_url_reads_the_configured_port(monkeypatch: pytest.MonkeyPatch):
    """A dev cluster's own redis lives on its allocated block port — the admin URL
    tracks whatever port this cluster's redis_url carries."""
    monkeypatch.setattr(
        cluster.settings.data_plane, "redis_url", "redis://ava_dev:s2@127.0.0.1:18012/0"
    )
    monkeypatch.setattr(cluster.settings.data_plane, "cluster_secret", "s2")
    assert cluster.redis_admin_url() == "redis://default:s2@127.0.0.1:18012"
