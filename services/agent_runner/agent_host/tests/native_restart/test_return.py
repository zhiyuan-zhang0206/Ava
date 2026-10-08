"""A returned original invocation settles cancel before its accepted restart."""

import asyncio
from typing import Any

import psycopg
import pytest
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.state import AgentState
from agent.tests.claim.test_inbound_ownership import _insert
from base.agents.incarnation.native_restart_models import NativeRestartRequest
from base.agents.messages.native_cancel import accept_native_cancel, observe_native_work
from base.agents.messages.native_restart import accept_native_restart, native_restart_progress
from base.native_process.turn_identity import bind_turn_identity
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work
from services.agent_runner.agent_host.tests.native_cancel.test_continuation import _install_faults
from services.agent_runner.agent_host.tests.native_cancel.test_return_boundaries import (
    _blocked_host,
)


@pytest.mark.parametrize("first", ["cancel", "restart"])
@pytest.mark.parametrize("fault_site", ["after_ack", "before_flush"])
async def test_cancel_and_restart_both_orders_settle_without_second_invocation(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    fault_site: str,
) -> None:
    incarnation, initial = await managed_work(db_conn, aops_pool)
    _insert(db_conn, initial.agent_id)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def model(_state: AgentState) -> Command[Any]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return Command(update={"halted": True, "turn_idle": True}, goto="__end__")

    graph, _saver, host, ctx = await _blocked_host(aops_pool, model)
    faults = _install_faults(monkeypatch, initial.agent_id, fault_site)
    with bind_turn_identity(initial.agent_id, incarnation=incarnation):
        running = asyncio.create_task(host._invoke_until_done(initial.agent_id, ctx))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
                target = await asyncio.to_thread(observe_native_work, pool, initial.agent_id)
                assert target is not None

                async def cancel() -> Any:
                    return await asyncio.to_thread(
                        accept_native_cancel, pool, "original-cancel", initial.agent_id, target
                    )

                async def restart() -> Any:
                    return await asyncio.to_thread(
                        accept_native_restart,
                        pool,
                        "original-restart",
                        initial.agent_id,
                        NativeRestartRequest(target=target),
                        lambda _request: None,
                    )

                if first == "cancel":
                    cancelled = await cancel()
                    accepted = await restart()
                else:
                    accepted = await restart()
                    cancelled = await cancel()
                queued = _insert(db_conn, initial.agent_id)
                release.set()
                outcome = await asyncio.wait_for(running, 10)
                progress = await asyncio.to_thread(
                    native_restart_progress, pool, initial.agent_id, accepted.command_id
                )
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    assert faults.injected and calls == 1
    assert not outcome.exited and not outcome.native_held
    assert progress is not None and progress.outcome == "applied"
    assert db_conn.execute(
        "SELECT outcome FROM native_cancel_commands WHERE id=%s", (cancelled.command_id,)
    ).fetchone() == ("applied",)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (queued,)
    ).fetchone() == ("pending",)
    snapshot = await graph.aget_state({"configurable": {"thread_id": str(initial.agent_id)}})
    assert snapshot.values["halted"] is True


@pytest.mark.parametrize("site", ["before_apply", "after_apply", "after_observe_cleanup"])
async def test_original_completion_survives_apply_or_observation_response_loss(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
) -> None:
    from agent.ownership.hosted import apply_hosted_lifecycle
    from agent.tests.claim.test_inbound_ownership import _admit
    from services.agent_runner.agent_host import host as host_owner

    incarnation, initial = await managed_work(db_conn, aops_pool)
    _insert(db_conn, initial.agent_id)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    injected = False

    async def model(_state: AgentState) -> Command[Any]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return Command(update={"halted": True, "turn_idle": True}, goto="__end__")

    _graph, _saver, host, ctx = await _blocked_host(aops_pool, model)

    async def interrupted_apply(pool: AsyncConnectionPool, token: Any, **kwargs: Any) -> Any:
        nonlocal injected
        if not injected:
            injected = True
            if site != "before_apply":
                await apply_hosted_lifecycle(pool, token, **kwargs)
            if site == "after_observe_cleanup":
                successor = await _admit(aops_pool, initial.agent_id)
                assert successor != incarnation
                db_conn.execute(
                    "DELETE FROM inbound_messages WHERE id=%s", (kwargs["expected_command_id"],)
                )
                db_conn.commit()
            raise psycopg.OperationalError("test lost original lifecycle response")
        return await apply_hosted_lifecycle(pool, token, **kwargs)

    monkeypatch.setattr(host_owner, "apply_hosted_lifecycle", interrupted_apply)
    with bind_turn_identity(initial.agent_id, incarnation=incarnation):
        running = asyncio.create_task(host._invoke_until_done(initial.agent_id, ctx))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
                target = await asyncio.to_thread(observe_native_work, pool, initial.agent_id)
                assert target is not None
                accepted = await asyncio.to_thread(
                    accept_native_restart,
                    pool,
                    "original-completion",
                    initial.agent_id,
                    NativeRestartRequest(target=target),
                    lambda _request: None,
                )
                queued = _insert(db_conn, initial.agent_id)
                release.set()
                outcome = await asyncio.wait_for(running, 10)
                progress = await asyncio.to_thread(
                    native_restart_progress, pool, initial.agent_id, accepted.command_id
                )
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    assert injected and calls == 1
    assert not outcome.exited and not outcome.crashed
    assert progress is not None and progress.outcome == (
        "observed" if site == "after_observe_cleanup" else "applied"
    )
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (queued,)
    ).fetchone() == ("pending",)
