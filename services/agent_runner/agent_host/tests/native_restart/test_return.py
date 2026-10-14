"""A returned original invocation settles cancel before its accepted restart."""

import asyncio
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.state import AgentState
from agent.tests.claim.test_inbound_ownership import insert
from base.agents.inbox.native_cancel import accept_native_cancel, observe_native_work
from base.agents.inbox.native_restart import accept_native_restart, native_restart_progress
from base.agents.incarnation.native_restart_models import NativeRestartRequest
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host import invocation
from services.agent_runner.agent_host.tests.managed_work import managed_work
from services.agent_runner.agent_host.tests.native_cancel.test_continuation import install_faults
from services.agent_runner.agent_host.tests.native_cancel.test_return_boundaries import (
    blocked_host,
)
from services.agent_runner.agent_host.tests.runtime.hosted_resources import hosted_scope


@pytest.mark.parametrize("first", ["cancel", "restart"])
@pytest.mark.parametrize("fault_site", ["none", "after_ack", "before_flush"])
async def test_cancel_and_restart_both_orders_settle_without_second_invocation(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    fault_site: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(name="claim-test")
    async with hosted_scope() as resources:
        incarnation, initial = await managed_work(db_conn, aops_pool, database_gate=database_gate)
        insert(db_conn, initial.agent_id)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def model(_state: AgentState) -> Command[Any]:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return Command(update={"halted": True, "turn_idle": True}, goto="__end__")

        graph, _saver, host, ctx = await blocked_host(
            aops_pool,
            model,
            model_catalog=model_catalog,
            database_gate=database_gate,
            machine=machine_identity,
        )
        ctx = replace(ctx, original_incarnation=incarnation, hosted_resources=resources)
        faults = install_faults(monkeypatch, initial.agent_id, fault_site)
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
                        catalog=model_catalog,
                        llm_override=config_authority.all_domains.lm.llm_override,
                        default_model=config_authority.all_domains.lm.llm_model,
                    )

                if first == "cancel":
                    cancelled = await cancel()
                    accepted = await restart()
                else:
                    accepted = await restart()
                    cancelled = await cancel()
                queued = insert(db_conn, initial.agent_id)
                release.set()
                if fault_site != "none":
                    with pytest.raises(psycopg.OperationalError):
                        await asyncio.wait_for(running, 10)
                    assert calls == 1
                    assert db_conn.execute(
                        "SELECT status FROM inbound_messages WHERE id=%s", (queued,)
                    ).fetchone() == ("pending",)
                    return
                outcome = await asyncio.wait_for(running, 10)
                progress = await asyncio.to_thread(
                    native_restart_progress, pool, initial.agent_id, accepted.command_id
                )
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        assert not faults.injected and calls == 1
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
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(name="claim-test")
    async with hosted_scope() as resources:
        from agent.ownership.hosted import apply_hosted_lifecycle
        from agent.tests.claim.test_inbound_ownership import admit_owner

        incarnation, initial = await managed_work(db_conn, aops_pool, database_gate=database_gate)
        insert(db_conn, initial.agent_id)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        injected = False

        async def model(_state: AgentState) -> Command[Any]:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return Command(update={"halted": True, "turn_idle": True}, goto="__end__")

        _graph, _saver, host, ctx = await blocked_host(
            aops_pool,
            model,
            model_catalog=model_catalog,
            database_gate=database_gate,
            machine=machine_identity,
        )
        ctx = replace(ctx, original_incarnation=incarnation, hosted_resources=resources)

        async def interrupted_apply(pool: AsyncConnectionPool, token: Any, **kwargs: Any) -> Any:
            nonlocal injected
            if not injected:
                injected = True
                if site != "before_apply":
                    await apply_hosted_lifecycle(pool, token, **kwargs)
                if site == "after_observe_cleanup":
                    successor = await admit_owner(
                        aops_pool, initial.agent_id, database_gate=database_gate
                    )
                    assert successor != incarnation
                    db_conn.execute(
                        "DELETE FROM inbound_messages WHERE id=%s", (kwargs["expected_command_id"],)
                    )
                    db_conn.commit()
                raise psycopg.OperationalError("test lost original lifecycle response")
            return await apply_hosted_lifecycle(pool, token, **kwargs)

        monkeypatch.setattr(invocation, "apply_hosted_lifecycle", interrupted_apply)
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
                    catalog=model_catalog,
                    llm_override=config_authority.all_domains.lm.llm_override,
                    default_model=config_authority.all_domains.lm.llm_model,
                )
                queued = insert(db_conn, initial.agent_id)
                release.set()
                with pytest.raises(psycopg.OperationalError):
                    await asyncio.wait_for(running, 10)
                progress = await asyncio.to_thread(
                    native_restart_progress, pool, initial.agent_id, accepted.command_id
                )
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        assert injected and calls == 1
        assert progress is not None and progress.outcome == (
            "observed"
            if site == "after_observe_cleanup"
            else "accepted"
            if site == "before_apply"
            else "applied"
        )
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (queued,)
        ).fetchone() == ("pending",)
