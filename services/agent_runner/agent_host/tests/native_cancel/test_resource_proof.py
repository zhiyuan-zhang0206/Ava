import asyncio
from uuid import uuid4

import psutil
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.agents.incarnation.hosted_force import original_host_force
from base.agents.incarnation.resource_admission import admit_resources_async
from base.agents.incarnation.resources import ResourceEvidenceError, ResourceProcess
from base.db.code_version_gate import ProcessDbGate
from base.db.transaction import async_write_transaction
from base.native_process.runtime_incarnation import RuntimeIncarnation
from ops.lifecycle.termination import force_terminate_transaction
from services.agent_runner.agent_host.tests.managed_work import managed_work


@pytest.mark.parametrize(
    "fault", ["wrong_freeze", "unobserved", "other_owner", "restart", "unknown"]
)
async def test_resource_transfer_requires_release_even_with_a_settled_stop_receipt(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    fault: str,
    *,
    database_gate: ProcessDbGate,
) -> None:
    pool: ConnectionPool
    original, target = await managed_work(db_conn, aops_pool, database_gate=database_gate)
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        _status, _pid, _pages, force, _shell_cutoff = await asyncio.to_thread(
            force_terminate_transaction, target.agent_id, pool, source="user"
        )
    assert await original_host_force(
        aops_pool, target.agent_id, original.owner, "claim-test", command_id=force, quiescent=True
    )
    if fault == "wrong_freeze":
        db_conn.execute(
            "UPDATE agents_meta SET incarnation_resources=jsonb_set(incarnation_resources,'{frozen_by}',to_jsonb(%s::bigint)) WHERE id=%s",
            (force + 1, target.agent_id),
        )
    elif fault == "unobserved":
        db_conn.execute("UPDATE inbound_messages SET observed_at=NULL WHERE id=%s", (force,))
    elif fault == "other_owner":
        db_conn.execute("UPDATE inbound_messages SET target_owner=%s WHERE id=%s", (uuid4(), force))
    elif fault == "restart":
        db_conn.execute(
            "UPDATE inbound_messages SET kind='restart',status='claimed',observed_at=NULL WHERE id=%s",
            (force,),
        )
        db_conn.execute(
            "UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s", (force, target.agent_id)
        )
    else:
        db_conn.execute("DELETE FROM inbound_messages WHERE id=%s", (force,))
    db_conn.commit()
    successor = RuntimeIncarnation(target.agent_id, uuid4(), uuid4())
    with pytest.raises(ResourceEvidenceError, match="released ownership"):
        async with async_write_transaction(aops_pool) as conn:
            await admit_resources_async(conn, successor, ResourceProcess.capture(psutil.Process()))
