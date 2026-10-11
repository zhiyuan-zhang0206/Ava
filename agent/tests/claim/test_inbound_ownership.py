"""A stale consumer cannot steal durable work from the admitted owner."""

import asyncio
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.db import (
    claim_inbound_batch,
    finalize_claimed_inbounds,
    reconcile_claimed_inbounds,
)
from agent.graph.claim._batch import _defer_chats_to_pending
from agent.ownership.hosted import admit_hosted_runtime
from agent.ownership.inbound import RuntimeOwnershipLostError, lock_inbound_owner
from base.config import settings
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.db.transaction import async_write_transaction
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


def _insert(conn: psycopg.Connection, agent_id: int) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source) "
        "VALUES (%s, 'durable', 'chat', 'user') RETURNING id",
        (agent_id,),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


async def _admit(
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


@pytest.mark.parametrize("kind", ["restart", "terminate"])
async def test_unowned_lifecycle_refuses_before_acknowledging_any_batch(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, kind: str
) -> None:
    agent_id = agent_row(db_conn)
    chat = _insert(db_conn, agent_id)
    command = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES(%s,'',%s,'user') RETURNING id",
        (agent_id, kind),
    ).fetchone()
    assert command is not None
    db_conn.commit()
    with pytest.raises(RuntimeError, match="lifecycle claim requires an admitted"):
        await claim_inbound_batch(aops_pool, agent_id, incarnation=None, work=None)
    assert db_conn.execute(
        "SELECT id,status,claimed_at FROM inbound_messages WHERE agent_id=%s ORDER BY id",
        (agent_id,),
    ).fetchall() == [(chat, "pending", None), (command[0], "pending", None)]


async def test_unowned_ordinary_batch_remains_compatible(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = agent_row(db_conn)
    _insert(db_conn, agent_id)
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES(%s,'summary','compact_summary','user')",
        (agent_id,),
    )
    db_conn.commit()
    assert [
        item.kind
        for item in await claim_inbound_batch(aops_pool, agent_id, incarnation=None, work=None)
    ] == [
        "chat",
        "compact_summary",
    ]
    assert db_conn.execute(
        "SELECT kind,status FROM inbound_messages WHERE agent_id=%s ORDER BY id", (agent_id,)
    ).fetchall() == [("chat", "claimed"), ("compact_summary", "done")]


async def test_successful_claim_clears_wake_suppression(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = agent_row(db_conn)
    inbound = _insert(db_conn, agent_id)
    db_conn.execute(
        "UPDATE agents_meta SET wake_suppressed_until=now()+interval '1 hour', "
        "wake_suppress_reason='resurrect_failed' WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()

    assert [
        row.id
        for row in await claim_inbound_batch(aops_pool, agent_id, incarnation=None, work=None)
    ] == [inbound]
    assert db_conn.execute(
        "SELECT wake_suppressed_until,wake_suppress_reason FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (None, None)


@pytest.mark.parametrize("missing", [False, True])
async def test_stale_or_missing_owner_cannot_claim(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    missing: bool,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await _admit(aops_pool, agent_id, database_gate=database_gate)
    inbound = _insert(db_conn, agent_id)
    stale = None if missing else RuntimeIncarnation(agent_id, owner.generation, uuid4())
    with pytest.raises(RuntimeOwnershipLostError):
        await claim_inbound_batch(aops_pool, agent_id, incarnation=stale, work=None)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("pending",)


async def test_missing_runtime_metadata_cannot_claim(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = create_agent(db_conn)
    inbound = _insert(db_conn, agent_id)
    with pytest.raises(RuntimeOwnershipLostError):
        await claim_inbound_batch(aops_pool, agent_id, incarnation=None, work=None)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("pending",)


async def test_two_owners_compete_only_current_owner_claims(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database_gate: ProcessDbGate
) -> None:
    agent_id = agent_row(db_conn)
    owner = await _admit(aops_pool, agent_id, database_gate=database_gate)
    inbound = _insert(db_conn, agent_id)

    async def claim(token: RuntimeIncarnation) -> list[int]:
        try:
            return [
                row.id
                for row in await claim_inbound_batch(
                    aops_pool, agent_id, incarnation=token, work=None
                )
            ]
        except RuntimeOwnershipLostError:
            return []

    stale = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    assert await asyncio.gather(claim(stale), claim(owner)) == [[], [inbound]]


async def test_expired_owner_zero_claim_then_takeover(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    old = await _admit(aops_pool, agent_id, database_gate=database_gate)
    inbound = _insert(db_conn, agent_id)
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()
    with pytest.raises(RuntimeOwnershipLostError):
        await claim_inbound_batch(aops_pool, agent_id, incarnation=old, work=None)
    new = await admit_hosted_runtime(
        aops_pool, agent_id, "claim-test", uuid4(), expected_from="running", db=database
    )
    assert new is not None
    assert [
        r.id for r in await claim_inbound_batch(aops_pool, agent_id, incarnation=new, work=None)
    ] == [inbound]


async def test_owner_locked_queue_mutation_rolls_back(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database_gate: ProcessDbGate
) -> None:
    agent_id = agent_row(db_conn)
    owner = await _admit(aops_pool, agent_id, database_gate=database_gate)
    inbound = _insert(db_conn, agent_id)
    with pytest.raises(RuntimeError, match="injected rollback"):
        async with async_write_transaction(aops_pool) as conn:
            await lock_inbound_owner(conn, agent_id, incarnation=owner)
            await conn.execute(
                "UPDATE inbound_messages SET status='claimed' WHERE id=%s", (inbound,)
            )
            raise RuntimeError("injected rollback")
    assert [
        r.id for r in await claim_inbound_batch(aops_pool, agent_id, incarnation=owner, work=None)
    ] == [inbound]


async def test_lock_wait_past_unchanged_lease_expiry_refuses_claim(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database_gate: ProcessDbGate
) -> None:
    agent_id = agent_row(db_conn)
    owner = await _admit(aops_pool, agent_id, database_gate=database_gate)
    inbound = _insert(db_conn, agent_id)
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at=clock_timestamp()+interval '1 second' WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()
    async with async_write_transaction(aops_pool) as conn:
        await conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
        waiter = asyncio.create_task(
            claim_inbound_batch(aops_pool, agent_id, incarnation=owner, work=None)
        )
        # Hold the unchanged tuple past expiry; statement-time predicates
        # can be true before blocking and must not authorize after waking.
        await asyncio.sleep(1.1)
        assert not waiter.done()
    with pytest.raises(RuntimeOwnershipLostError):
        await asyncio.wait_for(waiter, 3)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("pending",)


async def test_claim_lock_timeout_releases_connection_for_retry(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
) -> None:
    """A blocked owner row aborts promptly and returns its pool connection."""
    monkeypatch.setattr(
        settings.agent, "db_pool_acquire_timeout_seconds", 0.05
    )  # lock timeout 50ms
    agent_id = agent_row(db_conn)
    owner = await _admit(aops_pool, agent_id, database_gate=database_gate)
    inbound = _insert(db_conn, agent_id)
    async with async_write_transaction(aops_pool) as blocker:
        await blocker.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
        with pytest.raises(psycopg.errors.LockNotAvailable):
            await claim_inbound_batch(aops_pool, agent_id, incarnation=owner, work=None)

        # The blocker holds one of the two pool slots. This borrow can only
        # succeed if the failed claim returned the other connection.
        async with aops_pool.connection(timeout=0.5) as conn:
            assert await (await conn.execute("SELECT 1")).fetchone() == (1,)

    assert [
        row.id
        for row in await claim_inbound_batch(aops_pool, agent_id, incarnation=owner, work=None)
    ] == [inbound]


async def test_missing_redis_publish_recovers_from_durable_pg(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database_gate: ProcessDbGate
) -> None:
    agent_id = agent_row(db_conn)
    owner = await _admit(aops_pool, agent_id, database_gate=database_gate)
    # Direct SQL deliberately omits all Redis notifications.
    inbound = _insert(db_conn, agent_id)
    assert [
        r.id for r in await claim_inbound_batch(aops_pool, agent_id, incarnation=owner, work=None)
    ] == [inbound]


@pytest.mark.parametrize("action", ["reconcile_empty", "reconcile_committed", "finalize", "defer"])
async def test_old_owner_cannot_reconcile_finalize_or_defer_successor_work(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    action: str,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await _admit(aops_pool, agent_id, database_gate=database_gate)
    inbound = _insert(db_conn, agent_id)
    await claim_inbound_batch(aops_pool, agent_id, incarnation=owner, work=None)
    stale = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    with pytest.raises(RuntimeOwnershipLostError):
        if action == "reconcile_empty":
            await reconcile_claimed_inbounds(aops_pool, agent_id, set(), incarnation=stale)
        elif action == "reconcile_committed":
            await reconcile_claimed_inbounds(aops_pool, agent_id, {inbound}, incarnation=stale)
        elif action == "finalize":
            await finalize_claimed_inbounds(aops_pool, agent_id, incarnation=stale)
        else:
            await _defer_chats_to_pending(aops_pool, agent_id, [inbound], incarnation=stale)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("claimed",)


async def test_chat_finalization_does_not_ack_lifecycle_intent(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database_gate: ProcessDbGate
) -> None:
    agent_id = agent_row(db_conn)
    owner = await _admit(aops_pool, agent_id, database_gate=database_gate)
    chat = _insert(db_conn, agent_id)
    lifecycle = _insert(db_conn, agent_id)
    db_conn.execute(
        "UPDATE inbound_messages SET kind='restart', status='claimed' WHERE id=%s", (lifecycle,)
    )
    db_conn.commit()
    await claim_inbound_batch(aops_pool, agent_id, incarnation=owner, work=None)
    assert await finalize_claimed_inbounds(aops_pool, agent_id, incarnation=owner) == 1
    assert await reconcile_claimed_inbounds(
        aops_pool, agent_id, {lifecycle}, incarnation=owner
    ) == (0, 0, 0)
    assert db_conn.execute(
        "SELECT id,status FROM inbound_messages WHERE id=ANY(%s) ORDER BY id", ([chat, lifecycle],)
    ).fetchall() == [(chat, "done"), (lifecycle, "claimed")]
