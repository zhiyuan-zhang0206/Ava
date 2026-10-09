"""Force cleanup evidence and cancellation handoff retain their actual owners."""

import asyncio
import time
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph.exec._subprocess import _run_in_subprocess
from agent.ownership import hosted
from agent.tests.claim.test_inbound_ownership import _admit, _agent
from base.db import Database
from base.events.live.bus import EventBus
from services.agent_runner.agent_host.dispatcher import TurnScheduler
from services.agent_runner.agent_host.tests.test_hosted_force_quiescence import (
    _host_wakes_need_no_provider_credentials as _host_wakes_need_no_provider_credentials,
)
from tests.fixtures.pin_agent import exec_context as ctx_of


async def test_formatted_exec_cleanup_failure_retains_actual_resource_evidence(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
) -> None:
    from agent.graph.exec._result import _ExecCrashed
    from base.native_process.exec_domain import ExecProcessDomain
    from base.native_process.turn_identity import HostedTurnResources, bind_hosted_resources

    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    original_close = ExecProcessDomain.close_confirmed

    def failed_close(domain: ExecProcessDomain, deadline: float) -> None:
        original_close(domain, deadline)
        raise PermissionError("injected unverifiable domain closure")

    monkeypatch.setattr(ExecProcessDomain, "close_confirmed", failed_close)
    scope = HostedTurnResources()
    with bind_hosted_resources(scope):
        ctx = ctx_of(agent_id)
        outcome, _ = await _run_in_subprocess(
            database,
            "print('resource-proof')",
            ctx,
            asyncio.Event(),
            10,
            exec_dir=tmp_path,
            accumulation_max_chars=1_000_000,
        )
        assert isinstance(outcome, _ExecCrashed)
        assert "teardown failure" in outcome.output
        assert len(scope.unresolved) == 1
        path, domain = next(iter(scope.unresolved.items()))
        assert path.exists() and isinstance(domain, ExecProcessDomain)
        assert not scope.complete(path, object())
        assert scope.unresolved[path] is domain
        assert domain.proc.returncode is None  # unresolved closure must not reap
        # A formatted tool failure cannot become a positive lifecycle barrier.
        assert await hosted.apply_hosted_lifecycle(aops_pool, incarnation, bus=event_bus) is None
        assert not await hosted.settle_hosted_runtime(aops_pool, incarnation, bus=event_bus)
    assert len(scope.unresolved) == 1  # cache/context reset does not erase the evidence
    original_close(domain, time.monotonic() + 5)
    domain.proc.wait(timeout=5)


async def test_cancel_validation_spanning_task_handoff_never_cancels_new_turn() -> None:
    first_entered, first_release = asyncio.Event(), asyncio.Event()
    second_entered, second_release = asyncio.Event(), asyncio.Event()
    validating, validated = asyncio.Event(), asyncio.Event()
    calls = 0

    async def run_turn(agent_id: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_entered.set()
            await first_release.wait()
        else:
            second_entered.set()
            await second_release.wait()

    async def validate(agent_id: int, command_id: int) -> bool:
        validating.set()
        await validated.wait()
        return True

    scheduler = TurnScheduler(run_turn)
    scheduler.wake(1)
    await first_entered.wait()
    cancellation = asyncio.create_task(scheduler.cancel_exact_force(1, 7, validate))
    await validating.wait()
    scheduler.wake(1)
    first_release.set()
    await second_entered.wait()
    validated.set()
    try:
        assert not await cancellation
        assert 1 in scheduler.active_agents
        assert calls == 2
    finally:
        second_release.set()
        await scheduler.aclose()
