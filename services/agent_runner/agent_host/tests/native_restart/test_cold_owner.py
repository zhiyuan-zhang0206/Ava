"""An actual dead original host cannot retarget its accepted restart successor."""

import asyncio
import os
import subprocess
import sys
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.ownership.hosted import admit_hosted_runtime
from agent.ownership.lifecycle_intent import accept_lifecycle_intent, settle_superseded_intent
from agent.tests.claim.test_inbound_ownership import _agent
from base.agents.incarnation.native_restart_models import NativeRestartRequest
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.agents.incarnation.resources import ResourceBirth
from base.agents.messages.native_restart import (
    accept_native_restart,
    lookup_native_restart,
    native_restart_progress,
)
from base.db import Database
from base.db.transaction import async_write_transaction
from services.agent_runner.agent_host.tests.native_cancel.test_transfer import (
    _CHILD,
    _child_failure,
)


async def test_dead_original_host_is_superseded_without_new_restart_target(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent = _agent(db_conn)
    db_conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
        (Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")), agent),
    )
    db_conn.commit()
    child = subprocess.Popen(  # noqa: S603 -- fixed interpreter and existing isolated proof fixture
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
        request = NativeRestartRequest(target=target)
        with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
            accepted = await asyncio.to_thread(
                accept_native_restart, pool, "dead-original", agent, request, lambda _request: None
            )
            assert child.stdin is not None
            child.stdin.write("exit\n")
            child.stdin.flush()
            await asyncio.wait_for(asyncio.to_thread(child.wait), 15)
            assert child.returncode == 0
            successor = await admit_hosted_runtime(
                aops_pool,
                agent,
                "claim-test",
                uuid4(),
                db=Database.from_settings(),
                expected_from="running",
            )
            assert successor is not None and successor.generation != target.generation
            async with async_write_transaction(aops_pool) as conn:
                original = await accept_lifecycle_intent(conn, agent, incarnation=successor)
                assert original is not None and original.id == accepted.command_id
                assert original.generation == target.generation
                assert await settle_superseded_intent(conn, original)
            progress = await asyncio.to_thread(
                native_restart_progress, pool, agent, accepted.command_id
            )
            assert progress is not None and progress.outcome == "superseded"
            assert progress.reason == "target_replaced" and progress.applied_at is None
            assert (
                await asyncio.to_thread(
                    lookup_native_restart, pool, "dead-original", agent, request
                )
                == accepted
            )
            assert db_conn.execute(
                "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='restart'",
                (agent,),
            ).fetchone() == (1,)
    finally:
        await _close_child(child)


async def _close_child(child: subprocess.Popen[str]) -> None:
    if child.poll() is None:
        child.kill()
        await asyncio.to_thread(child.wait)
    for stream in (child.stdin, child.stdout, child.stderr):
        if stream is not None:
            stream.close()
