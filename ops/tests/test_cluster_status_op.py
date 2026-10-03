"""`ops.cluster.cluster_status_op` returns the status snapshot of its pool."""

from __future__ import annotations

import pytest

from base.deploy.maintenance.tests.test_admission import isolate as isolate
from ops import cluster


def test_cluster_status_op_returns_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from ops.cluster_status import ClusterStatus

    snap = ClusterStatus(
        machine_name="wsl", serve_gateway=False, serve_agent_runner=True, paused=False
    )
    expected_pool = object()
    seen: list[object] = []

    def _snapshot(pool: object | None = None) -> ClusterStatus:
        assert pool is expected_pool
        seen.append(pool)
        return snap

    monkeypatch.setattr(cluster, "status_snapshot", _snapshot)

    assert cluster.cluster_status_op(expected_pool) is snap
    assert seen == [expected_pool]
