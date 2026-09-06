"""A stale consumer cannot steal durable work from the admitted owner."""

import asyncio
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.db import (
    claim_inbound_batch,
    finalize_claimed_inbounds,
    mark_agent_status,
    reconcile_claimed_inbounds,
    wait_for_inbound,
)
from agent.graph._claim_batch import _defer_chats_to_pending
from agent.hosted_ownership import admit_hosted_runtime
from agent.inbound_ownership import RuntimeOwnershipLostError, lock_inbound_owner
from shared.db import create_agent
from shared.db_transaction import async_write_transaction
from shared.redis_listener import RedisInboundListener
from shared.runtime_incarnation import RuntimeIncarnation
from shared.turn_identity import bind_turn_identity


def _agent(conn: psycopg.Connection) -> int:
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


async def _admit(pool: AsyncConnectionPool, agent_id: int) -> RuntimeIncarnation:
    owner = await admit_hosted_runtime(
        pool, agent_id, "claim-test", uuid4(), expected_from="idling"
    )
    assert owner is not None
    return owner


@pytest.mark.parametrize("kind", ["restart", "terminate"])
async def test_unowned_lifecycle_refuses_before_acknowledging_any_batch(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, kind: str
) -> None:
    agent_id = _agent(db_conn)
    chat = _insert(db_conn, agent_id)
    command = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES(%s,'',%s,'user') RETURNING id",
        (agent_id, kind),
    ).fetchone()
    assert command is not None
    db_conn.commit()
    with pytest.raises(RuntimeError, match="lifecycle claim requires an admitted"):
        await claim_inbound_batch(aops_pool, agent_id)
    assert db_conn.execute(
        "SELECT id,status,claimed_at FROM inbound_messages WHERE agent_id=%s ORDER BY id",
        (agent_id,),
    ).fetchall() == [(chat, "pending", None), (command[0], "pending", None)]


async def test_unowned_ordinary_batch_remains_compatible(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    _insert(db_conn, agent_id)
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES(%s,'summary','compact_summary','user')",
        (agent_id,),
    )
    db_conn.commit()
    assert [item.kind for item in await claim_inbound_batch(aops_pool, agent_id)] == [
        "chat",
        "compact_summary",
    ]
    assert db_conn.execute(
        "SELECT kind,status FROM inbound_messages WHERE agent_id=%s ORDER BY id", (agent_id,)
    ).fetchall() == [("chat", "claimed"), ("compact_summary", "done")]


async def test_successful_claim_clears_wake_suppression(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    inbound = _insert(db_conn, agent_id)
    db_conn.execute(
        "UPDATE agents_meta SET wake_suppressed_until=now()+interval '1 hour', "
        "wake_suppress_reason='resurrect_failed' WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()

    assert [row.id for row in await claim_inbound_batch(aops_pool, agent_id)] == [inbound]
    assert db_conn.execute(
        "SELECT wake_suppressed_until,wake_suppress_reason FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (None, None)


@pytest.mark.parametrize("missing", [False, True])
async def test_stale_or_missing_owner_cannot_claim(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    missing: bool,
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    inbound = _insert(db_conn, agent_id)
    stale = None if missing else RuntimeIncarnation(agent_id, owner.generation, uuid4())
    with bind_turn_identity(agent_id, incarnation=stale), pytest.raises(RuntimeOwnershipLostError):
        await claim_inbound_batch(aops_pool, agent_id)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("pending",)


async def test_missing_runtime_metadata_cannot_claim(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = create_agent(db_conn)
    inbound = _insert(db_conn, agent_id)
    with bind_turn_identity(agent_id, incarnation=None), pytest.raises(RuntimeOwnershipLostError):
        await claim_inbound_batch(aops_pool, agent_id)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("pending",)


async def test_two_owners_compete_only_current_owner_claims(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    inbound = _insert(db_conn, agent_id)

    async def claim(token: RuntimeIncarnation) -> list[int]:
        with bind_turn_identity(agent_id, incarnation=token):
            try:
                return [row.id for row in await claim_inbound_batch(aops_pool, agent_id)]
            except RuntimeOwnershipLostError:
                return []

    stale = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    assert await asyncio.gather(claim(stale), claim(owner)) == [[], [inbound]]


async def test_expired_owner_zero_claim_then_takeover(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id = _agent(db_conn)
    old = await _admit(aops_pool, agent_id)
    inbound = _insert(db_conn, agent_id)
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()
    with bind_turn_identity(agent_id, incarnation=old), pytest.raises(RuntimeOwnershipLostError):
        await claim_inbound_batch(aops_pool, agent_id)
    new = await admit_hosted_runtime(
        aops_pool, agent_id, "claim-test", uuid4(), expected_from="running"
    )
    assert new is not None
    with bind_turn_identity(agent_id, incarnation=new):
        assert [r.id for r in await claim_inbound_batch(aops_pool, agent_id)] == [inbound]
    with bind_turn_identity(agent_id, incarnation=old), pytest.raises(RuntimeOwnershipLostError):
        await mark_agent_status(aops_pool, agent_id, "idling", expected_from="running")


async def test_owner_locked_queue_mutation_rolls_back(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    inbound = _insert(db_conn, agent_id)
    with bind_turn_identity(agent_id, incarnation=owner):
        with pytest.raises(RuntimeError, match="injected rollback"):
            async with async_write_transaction(aops_pool) as conn:
                await lock_inbound_owner(conn, agent_id)
                await conn.execute(
                    "UPDATE inbound_messages SET status='claimed' WHERE id=%s", (inbound,)
                )
                raise RuntimeError("injected rollback")
        assert [r.id for r in await claim_inbound_batch(aops_pool, agent_id)] == [inbound]


async def test_lock_wait_past_unchanged_lease_expiry_refuses_claim(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    inbound = _insert(db_conn, agent_id)
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at=clock_timestamp()+interval '1 second' WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()
    with bind_turn_identity(agent_id, incarnation=owner):
        async with async_write_transaction(aops_pool) as conn:
            await conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
            waiter = asyncio.create_task(claim_inbound_batch(aops_pool, agent_id))
            # Hold the unchanged tuple past expiry; statement-time predicates
            # can be true before blocking and must not authorize after waking.
            await asyncio.sleep(1.1)
            assert not waiter.done()
        with pytest.raises(RuntimeOwnershipLostError):
            await asyncio.wait_for(waiter, 3)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("pending",)


async def test_missing_redis_publish_recovers_from_durable_pg(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    # Direct SQL deliberately omits all Redis notifications.
    inbound = _insert(db_conn, agent_id)
    with bind_turn_identity(agent_id, incarnation=owner):
        await asyncio.wait_for(
            wait_for_inbound(aops_pool, aredis_inbound_listener, agent_id=agent_id), 3
        )
        assert [r.id for r in await claim_inbound_batch(aops_pool, agent_id)] == [inbound]


@pytest.mark.parametrize("action", ["reconcile_empty", "reconcile_committed", "finalize", "defer"])
async def test_old_owner_cannot_reconcile_finalize_or_defer_successor_work(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    action: str,
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    inbound = _insert(db_conn, agent_id)
    with bind_turn_identity(agent_id, incarnation=owner):
        await claim_inbound_batch(aops_pool, agent_id)
    stale = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    with bind_turn_identity(agent_id, incarnation=stale), pytest.raises(RuntimeOwnershipLostError):
        if action == "reconcile_empty":
            await reconcile_claimed_inbounds(aops_pool, agent_id, set())
        elif action == "reconcile_committed":
            await reconcile_claimed_inbounds(aops_pool, agent_id, {inbound})
        elif action == "finalize":
            await finalize_claimed_inbounds(aops_pool, agent_id)
        else:
            await _defer_chats_to_pending(aops_pool, agent_id, [inbound])
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("claimed",)


async def test_chat_finalization_does_not_ack_lifecycle_intent(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    chat = _insert(db_conn, agent_id)
    lifecycle = _insert(db_conn, agent_id)
    db_conn.execute(
        "UPDATE inbound_messages SET kind='restart', status='claimed' WHERE id=%s", (lifecycle,)
    )
    db_conn.commit()
    with bind_turn_identity(agent_id, incarnation=owner):
        await claim_inbound_batch(aops_pool, agent_id)
        assert await finalize_claimed_inbounds(aops_pool, agent_id) == 1
        assert await reconcile_claimed_inbounds(aops_pool, agent_id, {lifecycle}) == (0, 0, 0)
    assert db_conn.execute(
        "SELECT id,status FROM inbound_messages WHERE id=ANY(%s) ORDER BY id", ([chat, lifecycle],)
    ).fetchall() == [(chat, "done"), (lifecycle, "claimed")]
