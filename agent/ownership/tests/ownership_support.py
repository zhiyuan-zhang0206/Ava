from uuid import uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import admit_hosted_runtime
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.native_process.runtime_incarnation import RuntimeIncarnation


def agent_row(conn: psycopg.Connection) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id,status,machine) VALUES (%s,'idling','claim-test') "
        "ON CONFLICT (id) DO UPDATE SET status='idling',machine='claim-test'",
        (agent_id,),
    )
    conn.commit()
    return agent_id


def insert(conn: psycopg.Connection, agent_id: int) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source) "
        "VALUES (%s, 'durable', 'chat', 'user') RETURNING id",
        (agent_id,),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


async def admit_owner(
    pool: AsyncConnectionPool, agent_id: int, database_gate: ProcessDbGate
) -> RuntimeIncarnation:
    owner = await admit_hosted_runtime(
        pool,
        agent_id,
        "claim-test",
        uuid4(),
        expected_from="idling",
        db=Database.from_settings(gate=database_gate),
    )
    assert owner is not None
    return owner
