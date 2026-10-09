"""Hosted turn admission preserves independent workload and control pools."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import TurnFatalStamp, TurnSettlement
from base.config import settings
from base.db import Database
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host import settlement
from services.agent_runner.agent_host.tests.test_agent_host import (
    _Build,
    _FakePool,
    _host,
    _Row,
)
from services.agent_runner.agent_host.tests.test_agent_host import (
    host_plugin as host_plugin,
)
from services.agent_runner.agent_host.tests.test_agent_host import (
    wired as wired,
)


class TestPoolIsolation:
    @pytest.mark.parametrize("turn_limit", [0, 2, 1000])
    def test_pool_capacity_is_independent_of_agent_admission(
        self, monkeypatch: pytest.MonkeyPatch, turn_limit: int, database: Database
    ) -> None:
        """Admitting more agents must not expand either database client pool."""
        from services.agent_runner.agent_host.pools import build_control_pool, build_shared_pool

        monkeypatch.setattr(settings.daemon, "host_max_concurrent_turns", turn_limit)
        monkeypatch.setattr(settings.daemon, "host_db_pool_max_size", 12)
        monkeypatch.setattr(settings.daemon, "host_control_pool_max_size", 3)
        workload_pool = build_shared_pool(database)
        control_pool = build_control_pool(database)

        assert workload_pool is not control_pool
        assert workload_pool.max_size == 12
        assert control_pool.max_size == 3

    async def test_turn_work_and_lifecycle_control_use_separate_pools(
        self,
        wired: _Build,
        monkeypatch: pytest.MonkeyPatch,
        *,
        database: Database,
        model_catalog: ModelCatalog,
    ) -> None:
        """A busy turn may use the work pool without consuming control capacity."""
        import services.agent_runner.agent_host.host as host_mod
        from base.native_process.runtime_incarnation import RuntimeIncarnation

        rows = {11: _Row(status="idling")}
        original, graph, turn_pool = wired(rows)
        control_pool = _FakePool(rows)
        calls: list[tuple[str, object]] = []

        async def admit(
            pool: object,
            agent_id: int,
            _machine: str,
            owner: UUID,
            *,
            expected_from: str,
            db: object,
        ) -> RuntimeIncarnation:
            assert expected_from == "idling"
            calls.append(("admit", pool))
            return RuntimeIncarnation(agent_id, uuid4(), owner)

        async def settle_and_stamp(
            pool: object,
            incarnation: RuntimeIncarnation,
            *,
            bus: object,
            exited: bool,
            crashed: bool,
            resources: object,
        ) -> TurnSettlement:
            if not exited:
                calls.append(("settle", pool))
            return TurnSettlement(
                stamp=TurnFatalStamp(applied=crashed, recrash=False), settled=not exited
            )

        async def force(pool: object, *_args: object, **_kwargs: object) -> bool:
            calls.append(("force", pool))
            return False

        monkeypatch.setattr(host_mod, "admit_hosted_runtime", admit)
        # This force fixture has no strong native-work command.
        monkeypatch.setattr(host_mod, "recover_native_cancel", AsyncMock(return_value=True))
        monkeypatch.setattr(
            "services.agent_runner.agent_host.invocation.native_work.prepare_native_invocation",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(settlement, "settle_and_stamp_turn", settle_and_stamp)
        monkeypatch.setattr("base.agents.incarnation.hosted_force.original_host_force", force)
        host = _host(
            catalog=model_catalog,
            pool=cast(AsyncConnectionPool[Any], turn_pool),
            control_pool=cast(AsyncConnectionPool[Any], control_pool),
            checkpointer=original._checkpointer,
            graph=graph,
            plugin_configs=original._plugin_configs,
        )

        await host.run_turn(11)

        assert turn_pool.reads == 0
        # Pre-turn config plus the quiescent compact source qualification.
        assert control_pool.reads == 2
        assert graph.observations[-1].ops_pool is turn_pool
        assert calls == [("admit", control_pool), ("settle", control_pool), ("force", control_pool)]
