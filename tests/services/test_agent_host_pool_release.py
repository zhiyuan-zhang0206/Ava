"""The host daemon's pre-stop pool release — `/release-db-pools` + lazy pools.

`cluster_stop_op` dials this route after the agent drain; it must close every
idle connection in BOTH host pools and leave both able to grow again on the
first borrow after resume (the pools' `min_size=0` is what keeps resume from
eagerly re-opening one).
"""

from __future__ import annotations

import json

from services.agent_host.daemon import _release_pools_route
from services.agent_host.pools import build_control_pool, build_shared_pool
from shared.config import settings


def test_host_pools_start_lazy() -> None:
    """`min_size=0`: an idle host holds no client connection to strand."""
    shared = build_shared_pool("postgresql://unused")
    control = build_control_pool("postgresql://unused")
    assert shared.min_size == 0
    assert control.min_size == 0


async def test_release_route_closes_both_pools_and_the_next_borrow_reconnects() -> None:
    workload = build_shared_pool(settings.data_plane.db_url)
    control = build_control_pool(settings.data_plane.db_url)
    try:
        await workload.open()
        await control.open()
        workload_conn = await workload.getconn(timeout=5.0)
        await workload_conn.execute("SELECT 1")
        await workload.putconn(workload_conn)
        control_conn = await control.getconn(timeout=5.0)
        await control_conn.execute("SELECT 1")
        await control.putconn(control_conn)

        status, body, content_type = await _release_pools_route(workload, control)(b"")

        assert status == 200 and content_type == "application/json"
        assert json.loads(body) == {"released": {"workload": 1, "control": 1}}
        assert workload_conn.closed and control_conn.closed
        assert workload.get_stats()["pool_available"] == 0
        assert control.get_stats()["pool_available"] == 0

        resumed = await workload.getconn(timeout=5.0)
        assert not resumed.closed
        await resumed.execute("SELECT 1")
        await workload.putconn(resumed)
    finally:
        await workload.close()
        await control.close()


async def test_release_route_on_idle_pools_reports_zero() -> None:
    workload = build_shared_pool(settings.data_plane.db_url)
    control = build_control_pool(settings.data_plane.db_url)
    try:
        await workload.open()
        await control.open()
        status, body, _ = await _release_pools_route(workload, control)(b"")
        assert status == 200
        assert json.loads(body) == {"released": {"workload": 0, "control": 0}}
    finally:
        await workload.close()
        await control.close()
