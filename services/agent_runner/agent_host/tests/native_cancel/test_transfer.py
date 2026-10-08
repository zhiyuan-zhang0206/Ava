"""Actual dead-host and observed-force admissions certify only predecessor stop."""

import asyncio
import os
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.ownership import hosted as hosted_owner
from agent.ownership.hosted import admit_hosted_runtime
from agent.tests.claim.test_inbound_ownership import _agent, _insert
from base.agents.context import AvaContext
from base.agents.incarnation.hosted_force import original_host_force
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.agents.incarnation.resources import ResourceBirth
from base.agents.messages.native_cancel import accept_native_cancel
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.native_process.turn_identity import bind_turn_identity
from ops.agents.wake import resurrect_agent
from ops.lifecycle.termination import _force_terminate_transaction
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.native_work import recover_native_cancel
from services.agent_runner.agent_host.tests.history.test_hosted_compact_failure import (
    _prepare_graph,
)
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work

_CHILD = """
import asyncio, json, os, sys
from uuid import uuid4
from psycopg_pool import AsyncConnectionPool
from agent.ownership.hosted import admit_hosted_runtime
from base.config import settings
from base.db import Database
from base.db.transaction import async_write_transaction
from base.agents.incarnation.native_work import activate_work
from services.agent_runner.agent_host.native_work import NativeWorkContinuation, prepare_native_invocation
async def main():
    settings.data_plane.db_url = os.environ["AVA_TEST_NATIVE_DB"]
    async with AsyncConnectionPool(settings.data_plane.db_url, open=False) as pool:
        agent = int(os.environ["AVA_TEST_NATIVE_AGENT"])
        inc = await admit_hosted_runtime(pool,agent,"claim-test",uuid4(),db=Database.from_settings(),expected_from="running" if os.environ.get("AVA_TEST_NATIVE_SUCCESSOR") else "idling")
        assert inc is not None
        if os.environ.get("AVA_TEST_NATIVE_SUCCESSOR"):
            sys.stdout.write(json.dumps({"generation":str(inc.generation),"owner":str(inc.owner)})+"\\n")
            sys.stdout.flush()
        else:
            work = NativeWorkContinuation(uuid4())
            await prepare_native_invocation(pool,work,inc)
            assert work.target is not None
            async with async_write_transaction(pool) as conn:
                await activate_work(conn,work.target)
            sys.stdout.write(work.target.model_dump_json()+"\\n")
            sys.stdout.flush()
        await asyncio.to_thread(sys.stdin.readline)
asyncio.run(main())
"""


@pytest.mark.parametrize("hops", [1, 2])
async def test_real_child_exit_certificate_and_commit_rollback(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    hops: int,
) -> None:
    pool: ConnectionPool
    agent = _agent(db_conn)
    db_conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
        (Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")), agent),
    )
    db_conn.commit()
    child = subprocess.Popen(  # noqa: S603 -- fixed local interpreter and test-only source
        [sys.executable, "-c", _CHILD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "AVA_TEST_NATIVE_DB": db_conn.info.dsn,
            "AVA_TEST_NATIVE_AGENT": str(agent),
        },
    )
    try:
        assert child.stdout is not None
        line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 15)
        assert line, _child_failure(child)
        target = NativeWorkTarget.model_validate_json(line)
        with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
            await asyncio.to_thread(accept_native_cancel, pool, "dead-child", agent, target)
        assert child.stdin is not None
        child.stdin.write("exit\n")
        child.stdin.flush()
        await asyncio.wait_for(asyncio.to_thread(child.wait), 15)
        assert child.returncode == 0
        if hops == 2:
            await _exit_successor_child(db_conn, agent)
        prior_owner = db_conn.execute(
            "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s", (agent,)
        ).fetchone()
        prior_chain = db_conn.execute(
            "SELECT transfer_chain FROM native_graph_work WHERE id=%s", (target.work_id,)
        ).fetchone()
        actual_certify = hosted_owner.certify_transfer

        async def rollback_certificate(*args: Any, **kwargs: Any) -> None:
            await actual_certify(*args, **kwargs)
            raise RuntimeError("fault after certificate append")

        with monkeypatch.context() as patch:
            patch.setattr(hosted_owner, "certify_transfer", rollback_certificate)
            with pytest.raises(RuntimeError, match="fault after certificate append"):
                await admit_hosted_runtime(
                    aops_pool,
                    agent,
                    "claim-test",
                    uuid4(),
                    db=Database.from_settings(),
                    expected_from="running",
                )
        assert (
            db_conn.execute(
                "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s", (agent,)
            ).fetchone()
            == prior_owner
        )
        assert (
            db_conn.execute(
                "SELECT transfer_chain FROM native_graph_work WHERE id=%s", (target.work_id,)
            ).fetchone()
            == prior_chain
        )
        incarnation = await admit_hosted_runtime(
            aops_pool,
            agent,
            "claim-test",
            uuid4(),
            db=Database.from_settings(),
            expected_from="running",
        )
        assert incarnation is not None
        proof = db_conn.execute(
            "SELECT transfer_chain FROM native_graph_work WHERE id=%s", (target.work_id,)
        ).fetchone()
        _assert_exit_chain(proof, target, hops)
        replies: list[str] = []
        graph, saver, config, _history = await _prepare_graph(aops_pool, agent, 1, replies)
        assert await recover_native_cancel(aops_pool, saver, graph, incarnation)
        assert db_conn.execute(
            "SELECT outcome,checkpoint_id FROM native_cancel_commands WHERE work_id=%s",
            (target.work_id,),
        ).fetchone() == ("recovered_stopped", None)
        assert (
            db_conn.execute(
                "SELECT transfer_chain FROM native_graph_work WHERE id=%s", (target.work_id,)
            ).fetchone()
            == proof
        )
        await _assert_successor_turns(
            db_conn,
            aops_pool,
            target,
            incarnation,
            graph,
            saver,
            config,
            replies,
            expected_first=[],
        )
    finally:
        await _reap_child(child)


async def test_actual_force_observation_then_admission_recovered_stop(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    target, successor, force = await _force_successor(db_conn, aops_pool)
    proof = db_conn.execute(
        "SELECT transfer_chain FROM native_graph_work WHERE id=%s", (target.work_id,)
    ).fetchone()
    assert proof is not None and proof[0][0]["proof"]["reason"] == "lifecycle_receipt"
    assert proof[0][0]["proof"]["lifecycle_command_id"] == force
    replies: list[str] = []
    graph, saver, config, _history = await _prepare_graph(aops_pool, target.agent_id, 1, replies)
    assert await recover_native_cancel(aops_pool, saver, graph, successor)
    assert db_conn.execute(
        "SELECT outcome FROM native_cancel_commands WHERE work_id=%s", (target.work_id,)
    ).fetchone() == ("recovered_stopped",)

    # Explicit resurrection is a new accepted input, so its existing resume
    # behavior remains valid after pausing the original cancelled conversation.
    await _assert_successor_turns(
        db_conn,
        aops_pool,
        target,
        successor,
        graph,
        saver,
        config,
        replies,
        expected_first=["continued"],
    )


async def _assert_successor_turns(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    target: NativeWorkTarget,
    successor: Any,
    graph: Any,
    saver: Any,
    config: Any,
    replies: list[str],
    *,
    expected_first: list[str],
) -> None:
    receipt = db_conn.execute(
        "SELECT checkpoint_id,recovery_checkpoint_id FROM native_cancel_commands WHERE work_id=%s",
        (target.work_id,),
    ).fetchone()
    assert receipt is not None and receipt[0] is None and receipt[1] is not None
    snapshot = await graph.aget_state(config)
    assert snapshot.values["native_cancel"] is None
    assert snapshot.values["halted"] is True
    host = AgentHost(
        pool=aops_pool,
        checkpointer=saver,
        graph=graph,
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    ctx = AvaContext(
        ops_pool=aops_pool,
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
    )
    with bind_turn_identity(target.agent_id, incarnation=successor):
        outcome = await host._invoke_until_done(target.agent_id, ctx)
        assert not outcome.native_held and not outcome.crashed
        assert replies == expected_first
        new_work = db_conn.execute(
            "SELECT native_work_id FROM agents_meta WHERE id=%s", (target.agent_id,)
        ).fetchone()
        assert new_work is not None and new_work[0] != target.work_id
        _insert(db_conn, target.agent_id)
        outcome = await host._invoke_until_done(target.agent_id, ctx)
        assert not outcome.native_held and not outcome.crashed
        assert replies == [*expected_first, "continued"]
        # Terminal replay does not project the old work over a newer checkpoint.
        latest = await graph.aget_state(config)
        assert await recover_native_cancel(aops_pool, saver, graph, successor)
        assert (await graph.aget_state(config)).config == latest.config
        assert (
            db_conn.execute(
                "SELECT checkpoint_id,recovery_checkpoint_id FROM native_cancel_commands WHERE work_id=%s",
                (target.work_id,),
            ).fetchone()
            == receipt
        )


async def _force_successor(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> tuple[NativeWorkTarget, Any, int]:
    pool: ConnectionPool
    original, target = await managed_work(db_conn, aops_pool)
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        await asyncio.to_thread(accept_native_cancel, pool, "before-force", target.agent_id, target)
        *_prefix, force = await asyncio.to_thread(
            _force_terminate_transaction, target.agent_id, pool, source="user"
        )
    assert await original_host_force(
        aops_pool, target.agent_id, original.owner, "claim-test", command_id=force, quiescent=True
    )
    await asyncio.to_thread(
        resurrect_agent,
        Database.from_settings(),
        EventBus.from_settings(),
        target.agent_id,
        resurrected_by="user",
    )
    successor = await admit_hosted_runtime(
        aops_pool,
        target.agent_id,
        "claim-test",
        uuid4(),
        db=Database.from_settings(),
        expected_from="idling",
    )
    assert successor is not None
    return target, successor, force


async def _exit_successor_child(db_conn: psycopg.Connection, agent: int) -> None:
    next_child = subprocess.Popen(  # noqa: S603 -- fixed offline test-only child
        [sys.executable, "-c", _CHILD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "AVA_TEST_NATIVE_DB": db_conn.info.dsn,
            "AVA_TEST_NATIVE_AGENT": str(agent),
            "AVA_TEST_NATIVE_SUCCESSOR": "1",
        },
    )
    try:
        assert next_child.stdout is not None and next_child.stdin is not None
        assert await asyncio.wait_for(asyncio.to_thread(next_child.stdout.readline), 15)
        next_child.stdin.write("exit\n")
        next_child.stdin.flush()
        await asyncio.wait_for(asyncio.to_thread(next_child.wait), 15)
        assert next_child.returncode == 0
    finally:
        await _reap_child(next_child)


def _child_failure(child: subprocess.Popen[str]) -> str:
    return child.stderr.read() if child.stderr is not None else "child did not return work"


async def _reap_child(child: subprocess.Popen[str]) -> None:
    if child.poll() is None:
        child.kill()
    await asyncio.to_thread(child.communicate, timeout=10)


def _assert_exit_chain(proof: Any, target: NativeWorkTarget, hops: int) -> None:
    assert proof is not None
    assert len(proof[0]) == hops
    assert proof[0][0]["proof"]["reason"] == "exact_host_exit"
    assert proof[0][0]["proof"]["source_owner"] == str(target.owner)
