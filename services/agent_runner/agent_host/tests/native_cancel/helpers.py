"""Real managed-owner and original-work fixtures for the native cancel chain."""

from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.tests.claim.test_inbound_ownership import _admit, agent_row
from base.agents.incarnation.native_work import activate_work
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.agents.incarnation.resources import ResourceBirth
from base.db.code_version_gate import ProcessDbGate
from base.db.transaction import async_write_transaction
from base.native_process.runtime_incarnation import RuntimeIncarnation
from services.agent_runner.agent_host.invocation.native_work import (
    NativeWorkContinuation,
    prepare_native_invocation,
)


async def managed_work(
    conn: psycopg.Connection,
    pool: AsyncConnectionPool,
    *,
    active: bool = True,
    database_gate: ProcessDbGate,
) -> tuple[RuntimeIncarnation, NativeWorkTarget]:
    agent = agent_row(conn)
    conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
        (Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")), agent),
    )
    conn.commit()
    incarnation = await _admit(pool, agent, database_gate=database_gate)
    work = NativeWorkContinuation(uuid4())
    await prepare_native_invocation(pool, work, incarnation)
    assert work.target is not None
    if active:
        async with async_write_transaction(pool) as connection:
            await activate_work(connection, work.target)
    return incarnation, work.target
