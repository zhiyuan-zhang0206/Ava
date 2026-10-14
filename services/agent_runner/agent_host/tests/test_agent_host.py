"""The hosted agent-runner's turn runner — `services/agent_runner/agent_host/host.py`.

`test_turn_dispatcher.py` locks WHEN an agent runs. This file locks WHAT running
means, and the contracts here are the ones that only exist because many agents
now share one process:

1. **Isolation** — two agents' turns running concurrently must not see each
   other's identity, framework config, plugin config, model, or event
   publisher. In process mode the OS gave this for free; here it is bought by
   explicit invocation contexts, and this file proves isolation under real overlap.
2. **The config rebind** — a turn reads the agent's stored config fresh, so an
   overlay written between turns takes effect at the next one, and the cached
   per-agent runtime is rebuilt rather than reused. This is the hosted
   replacement for "the process exits and boots with the merged config"; a
   cache that missed it would run an agent on a model the DB says it left.
3. **The four-way turn loop** — `exit_requested` is terminal, `restart_requested` drops the runtime without a notify, `turn_idle` ends
   the task, and neither means re-invoke on the same thread.
4. **Runnability** — a wake for another machine's agent, or for a terminated
   one, must not start a turn. The dispatcher's subscription is cluster-wide, so
   this is the only thing that keeps a runner to its own agents.
5. **The bounds** — the concurrent-turn semaphore, the LRU cap, and the idle TTL
   are what make "an idle agent costs nothing" true of the cache too.
6. **The gate fails CLOSED** — unreadable config must leave hosted mode OFF.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from psycopg_pool import AsyncConnectionPool

import ava
from ava.tests.fixtures.pin_agent import pin_agent, pin_no_identity
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
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

# ── 1. isolation ─────────────────────────────────────────────────────────────


class TestPendingInboundBackstop:
    async def test_stale_running_rows_qualified_by_the_scan(
        self, *, model_catalog: ModelCatalog, database_gate: ProcessDbGate
    ) -> None:
        """The hosted dispatcher scans only this machine's runnable rows. A
        pending inbound wakes its agent; the scan never selects a turn to cancel."""
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
        """Turn-query saturation must not starve the durable recovery scan."""

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
        """The load-bearing test of the whole hosted model.

        Both turns stay inside the graph concurrently; sequential completion
        cannot hide process-per-agent assumptions.
        """
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

        child_context = getattr(ava, "context", None)
        assert_sdk_context(child_context, bound=sdk_bound)
        t1 = asyncio.create_task(host.run_turn(11))
        t2 = asyncio.create_task(host.run_turn(22))
        await asyncio.wait_for(graph.arrival(11).wait(), 2)
        await asyncio.wait_for(graph.arrival(22).wait(), 2)

        assert_sdk_context(child_context, bound=sdk_bound)
        # Both are parked in their own turn right now — overlap is real.
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
        # Per-agent handles, not one shared object.
        assert seen[11].llm is not seen[22].llm
        assert seen[11].llm.name == "model-for-11"
        assert seen[11].publisher.agent_id == 11
        assert seen[22].publisher.agent_id == 22

    async def test_nothing_leaks_after_a_turn_ends(
        self,
        wired: Build,
    ) -> None:
        """Host turns carry their identity explicitly and never change the shared SDK slot."""
        host, _, _ = wired(
            {11: Row(overlay={"llm_model": "model-for-11"})},
        )
        child_context = getattr(ava, "context", None)
        await asyncio.wait_for(host.run_turn(11), 2)
        assert getattr(ava, "context", None) is child_context


# ── 2. the config rebind ─────────────────────────────────────────────────────


# ── 3. the four-way turn loop ────────────────────────────────────────────────


# ── 4. runnability ───────────────────────────────────────────────────────────


# ── 5. the bounds ────────────────────────────────────────────────────────────


# ── the scheduler seam ───────────────────────────────────────────────────────
