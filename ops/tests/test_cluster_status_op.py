"""`ops.cluster.operations.cluster_status_op` returns the status snapshot of its pool."""

from __future__ import annotations

from pathlib import Path

import pytest

from base.db import Database
from base.deploy.maintenance.tests.test_admission import isolate as isolate
from base.native_process.loaded_commit import LoadedCommit
from ops.cluster import operations as cluster


def test_cluster_status_op_returns_snapshot(
    monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    from ops.cluster_status import ClusterStatus

    snap = ClusterStatus(
        machine_name="wsl", serve_gateway=False, serve_agent_runner=True, paused=False
    )
    expected_pool = object()
    expected_image = LoadedCommit(source_root=Path(__file__).parent, sha=None)
    seen: list[object] = []

    def _snapshot(
        _db: Database, pool: object | None = None, *, image: LoadedCommit
    ) -> ClusterStatus:
        assert pool is expected_pool
        assert image is expected_image
        seen.append(pool)
        return snap

    monkeypatch.setattr(cluster, "status_snapshot", _snapshot)

    assert cluster.cluster_status_op(database, expected_pool, image=expected_image) is snap
    assert seen == [expected_pool]
