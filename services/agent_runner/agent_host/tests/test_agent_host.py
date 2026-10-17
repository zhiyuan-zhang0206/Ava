from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from psycopg_pool import AsyncConnectionPool

import meiva
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from meiva.tests.fixtures.pin_agent import pin_agent, pin_no_identity
from services.agent_runner.agent_host.tests.host_policy import (
    assert_sdk_context,
    configured_machine,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    Build,
    PendingScanPool,
    Row,
    build_agent_host,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    host_plugin as host_plugin,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    wired as wired,
)


class TestPendingInboundBackstop:
    async def test_stale_running_rows_qualified_by_the_scan(
        self, *, model_catalog: ModelCatalog, database_gate: ProcessDbGate
    ) -> None:
        pool = PendingScanPool([(17,), (23,)])
        host = build_agent_host(
            catalog=model_catalog,
            pool=pool,
            checkpointer=object(),
            graph=object(),
            database_gate=database_gate,
            machine=configured_machine(name="this-box"),
        )

        candidates = await host.pending_inbound_wakes()

        assert candidates == [17, 23]
        assert pool.params == ("this-box", host._owner)
        assert "m.runtime_owner=%s" in pool.sql
        assert "m.status IN ('idling','running')" in pool.sql
        assert "m.machine=%s" in pool.sql
        assert "pending.status='pending'" in pool.sql

    async def test_scan_uses_the_reserved_control_pool(
        self, *, model_catalog: ModelCatalog, database_gate: ProcessDbGate
    ) -> None:

        class _ForbiddenTurnPool:
            def connection(self) -> object:
                raise AssertionError("pending scan borrowed from the turn pool")

        control_pool = PendingScanPool([(17,)])
        host = build_agent_host(
            catalog=model_catalog,
            pool=cast(AsyncConnectionPool[Any], _ForbiddenTurnPool()),
            control_pool=cast(AsyncConnectionPool[Any], control_pool),
            checkpointer=object(),
            graph=object(),
            database_gate=database_gate,
            machine=configured_machine(name="this-box"),
        )

        assert await host.pending_inbound_wakes() == [17]


class TestConcurrentAgentIsolation:
    @pytest.mark.parametrize("sdk_bound", [False, True])
    async def test_two_overlapping_turns_each_see_their_own_everything(
        self,
        wired: Build,
        sdk_bound: bool,
        machine_identity: MachineIdentity,
    ) -> None:
        if sdk_bound:
            pin_agent(999)
        else:
            pin_no_identity()
        rows = {
            11: Row(overlay={"llm_model": "model-for-11", "marker": "plug-for-11"}),
            22: Row(overlay={"llm_model": "model-for-22", "marker": "plug-for-22"}),
        }
        host, graph, _ = wired(
            rows,
        )
        graph.gate(11)
        graph.gate(22)

        child_context = getattr(meiva, "context", None)
        assert_sdk_context(child_context, bound=sdk_bound)
        t1 = asyncio.create_task(host.run_turn(11))
        t2 = asyncio.create_task(host.run_turn(22))
        await asyncio.wait_for(graph.arrival(11).wait(), 2)
        await asyncio.wait_for(graph.arrival(22).wait(), 2)

        assert_sdk_context(child_context, bound=sdk_bound)
        graph.gates[11].set()
        graph.gates[22].set()
        await asyncio.wait_for(asyncio.gather(t1, t2), 2)
        assert_sdk_context(child_context, bound=sdk_bound)

        seen = {o.agent_id: o for o in graph.observations}
        assert set(seen) == {11, 22}, "identity must not leak between concurrent turns"
        assert seen[11].machine is machine_identity
        assert seen[22].machine is machine_identity
        assert seen[11].model == "model-for-11"
        assert seen[22].model == "model-for-22"
        assert seen[11].plugin_marker == "plug-for-11"
        assert seen[22].plugin_marker == "plug-for-22"
        assert seen[11].llm is not seen[22].llm
        assert seen[11].llm.name == "model-for-11"
        assert seen[11].publisher.agent_id == 11
        assert seen[22].publisher.agent_id == 22

    async def test_nothing_leaks_after_a_turn_ends(
        self,
        wired: Build,
    ) -> None:
        host, _, _ = wired(
            {11: Row(overlay={"llm_model": "model-for-11"})},
        )
        child_context = getattr(meiva, "context", None)
        await asyncio.wait_for(host.run_turn(11), 2)
        assert getattr(meiva, "context", None) is child_context
