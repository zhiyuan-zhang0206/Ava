"""Versioned launch operations reach the runner's repeatable wake handler."""

from __future__ import annotations

import pytest

from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent
from services.agent_ops import daemon


@pytest.mark.asyncio
async def test_versioned_launch_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = object()
    monkeypatch.setattr(daemon, "_db_pool", pool)
    seen: list[tuple[int, object]] = []

    async def _launch(body: LaunchAgentRequest, received_pool: object) -> SpawnedAgent:
        seen.append((body.agent_id, received_pool))
        return SpawnedAgent(id=body.agent_id)

    monkeypatch.setattr(daemon.ops_lifecycle, "launch_agent_op", _launch)
    status, result = await daemon._dispatch("spawn-launch-v2", {"agent_id": 777})
    assert (status, result) == ("completed", {"id": 777})
    assert seen == [(777, pool)]
