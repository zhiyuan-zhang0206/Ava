"""Launch operations reach the runner's repeatable wake handler."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg_pool import ConnectionPool

from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent
from services.agent_runner.agent_ops import daemon


@pytest.mark.asyncio
async def test_launch_dispatch(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    pool: ConnectionPool = ConnectionPool(open=False)
    dispatch_pool: ConnectionPool = pool
    seen: list[tuple[int, object]] = []

    async def _launch(
        _db: object,
        _bus: object,
        body: LaunchAgentRequest,
        received_pool: object,
        *,
        catalog: ModelCatalog,
    ) -> SpawnedAgent:
        seen.append((body.agent_id, received_pool))
        return SpawnedAgent(id=body.agent_id)

    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", _launch)
    status, result = await daemon._dispatch(
        "spawn-launch",
        {"launch_attempt_id": "00000000-0000-0000-0000-000000000001", "agent_id": 777},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert (status, result) == ("completed", {"id": 777})
    assert seen == [(777, pool)]
