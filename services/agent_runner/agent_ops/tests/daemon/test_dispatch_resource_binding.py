"""Request dispatch requires its invocation's resource bindings."""

from __future__ import annotations

import inspect
from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg_pool import ConnectionPool

from base.db import Database
from services.agent_runner.agent_ops import daemon


def test_dispatch_requires_daemon_pool() -> None:
    """An unbound daemon dispatch cannot silently borrow ambient startup state."""
    with pytest.raises(TypeError, match="pool"):
        inspect.signature(daemon._dispatch).bind("status_probe", {}, active_ops={}, workers=set())


def test_dispatch_idempotent_requires_daemon_pool() -> None:
    """Idempotency cannot run without its invocation's pool binding."""
    with pytest.raises(TypeError, match="pool"):
        inspect.signature(daemon._dispatch_idempotent).bind(
            "spawn-launch-v2", {"agent_id": 1}, "key-4", active_ops={}, workers=set()
        )


@pytest.mark.asyncio
async def test_dispatch_status_probe_passes_the_daemon_pool(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The steady-state probe reuses the daemon's already-open central DB pool."""
    from ops.cluster_status import ClusterStatus

    pool: ConnectionPool = ConnectionPool(open=False)
    seen: list[object] = []
    dispatch_pool: ConnectionPool = pool

    def _status(_db: Database, probe_pool: object) -> ClusterStatus:
        seen.append(probe_pool)
        return ClusterStatus(
            machine_name="win",
            serve_gateway=False,
            serve_agent_runner=True,
            paused=False,
        )

    monkeypatch.setattr(daemon.cluster, "cluster_status_op", _status)

    status, result = await daemon._dispatch(
        "status_probe", {}, active_ops={}, workers=set(), pool=dispatch_pool, executor=op_executor
    )

    assert status == "completed"
    assert result["machine_name"] == "win"
    assert seen == [pool]


def test_dispatch_requires_invocation_executor() -> None:
    with pytest.raises(TypeError, match="executor"):
        inspect.signature(daemon._dispatch).bind(
            "status_probe", {}, active_ops={}, workers=set(), pool=ConnectionPool(open=False)
        )
