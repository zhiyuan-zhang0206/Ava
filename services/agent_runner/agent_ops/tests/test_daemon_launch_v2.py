"""Versioned launch operations reach the runner's repeatable wake handler."""

from __future__ import annotations

import pytest
from psycopg_pool import ConnectionPool

from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent
from services.agent_runner.agent_ops import daemon


@pytest.mark.asyncio
async def test_versioned_launch_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    pool: ConnectionPool = ConnectionPool(open=False)
    dispatch_pool: ConnectionPool = pool
    seen: list[tuple[int, object]] = []

    async def _launch(
        _db: object, _bus: object, body: LaunchAgentRequest, received_pool: object
    ) -> SpawnedAgent:
        seen.append((body.agent_id, received_pool))
        return SpawnedAgent(id=body.agent_id)

    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", _launch)
    status, result = await daemon._dispatch(
        "spawn-launch-v2", {"agent_id": 777}, active_ops={}, workers=set(), pool=dispatch_pool
    )
    assert (status, result) == ("completed", {"id": 777})
    assert seen == [(777, pool)]
