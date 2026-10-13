"""Wire envelopes and degraded status from the real daemon dispatch boundary."""

import asyncio
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from ops.cluster import operations as cluster_ops
from services.agent_runner.agent_ops import daemon


@pytest.mark.asyncio
async def test_ops_route_wraps_dispatch_in_envelope(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """A valid body returns 200 with {status, result} from _dispatch."""
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    dispatch_sem = asyncio.Semaphore(4)

    async def _fake_dispatch(
        kind: str,
        payload: dict[str, Any],
        *,
        active_ops: daemon.ActiveOps,
        workers: daemon.maintenance_activity.WorkerFutures,
        pool: ConnectionPool,
        executor: ThreadPoolExecutor,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
        database: Callable[[], Database],
        image: LoadedCommit,
        local_machine: MachineIdentity,
    ) -> tuple[str, dict[str, object]]:
        assert local_machine is machine_identity
        assert kind == "status_probe"
        return "completed", {"paused": False}

    monkeypatch.setattr(daemon, "_dispatch", _fake_dispatch)
    status, body, ctype = await daemon.ops_route(
        json.dumps({"kind": "status_probe"}).encode(),
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
    )
    assert status == 200
    assert ctype == "application/json"
    assert json.loads(body) == {"status": "completed", "result": {"paused": False}}


@pytest.mark.asyncio
async def test_ops_route_status_probe_serializes_datetime_fields(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """status_probe through the real _dispatch must survive ops_route's json.dumps
    even when ClusterStatus carries datetime values nested inside it.

    Regression: a python-mode model_dump left datetime objects in the result and
    every status_probe crashed with 'Object of type datetime is not JSON
    serializable', so the gateway's status page lost all agent-runner info.
    The live datetime rides in `agent_groups` (typed dict[str, object]) so the
    test keeps guarding the wire serialization even though the current
    `_group_agent_sessions` pre-serializes its shells to JSON-mode dicts.
    """
    from datetime import UTC

    from ops.cluster_status import ClusterStatus

    dispatch_sem = asyncio.Semaphore(1)
    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    created = datetime(2026, 6, 11, 8, 30, 0, tzinfo=UTC)

    def _status(
        _db: Database, probe_pool: object, *, image: LoadedCommit, local_machine: MachineIdentity
    ) -> ClusterStatus:
        assert local_machine is machine_identity
        assert probe_pool is dispatch_pool
        return ClusterStatus(
            machine_name="runner-1",
            serve_gateway=False,
            serve_agent_runner=True,
            paused=False,
            agent_count=1,
            session_count=1,
            agent_groups=[
                {
                    "agent_id": 7,
                    "label": "",
                    "shells": [
                        {"name": "ava-agent-7", "created_at": created, "uptime_seconds": 120}
                    ],
                }
            ],
        )

    monkeypatch.setattr(cluster_ops, "cluster_status_op", _status)
    status, body, ctype = await daemon.ops_route(
        json.dumps({"kind": "status_probe"}).encode(),
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
    )
    assert status == 200
    assert ctype == "application/json"
    parsed = json.loads(body)
    assert parsed["status"] == "completed"
    shell = parsed["result"]["agent_groups"][0]["shells"][0]
    assert shell["name"] == "ava-agent-7"
    assert shell["created_at"] == "2026-06-11T08:30:00Z"


@pytest.mark.asyncio
async def test_ops_route_completes_with_a_db_down_degraded_status(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """DB-down, including an unreachable paused row, remains HTTP 200 completed."""
    from ops.cluster_status import ClusterStatus

    pool: ConnectionPool = ConnectionPool(open=False)
    dispatch_sem = asyncio.Semaphore(1)
    dispatch_pool: ConnectionPool = pool

    def _degraded_status(
        _db: Database, probe_pool: object, *, image: LoadedCommit, local_machine: MachineIdentity
    ) -> ClusterStatus:
        assert local_machine is machine_identity
        assert probe_pool is pool
        assert image is ops_image
        return ClusterStatus(
            machine_name="runner-a",
            serve_gateway=False,
            serve_agent_runner=True,
            paused=False,
            resource=None,
        )

    monkeypatch.setattr(cluster_ops, "cluster_status_op", _degraded_status)

    status, body, ctype = await daemon.ops_route(
        json.dumps({"kind": "status_probe"}).encode(),
        active_ops={},
        dispatch_sem=dispatch_sem,
        workers=set(),
        requests=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
    )

    assert status == 200
    assert ctype == "application/json"
    parsed = json.loads(body)
    assert parsed["status"] == "completed"
    assert parsed["result"]["paused"] is False
    assert "current_orchestration" not in parsed["result"]
    assert "last_updater_outcome" not in parsed["result"]
    assert parsed["result"]["resource"] is None
