"""Durable acceptance is not completion, and never retargets a successor."""

from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.lifecycle_intent import accept_lifecycle_intent, settle_superseded_intent
from shared.db import insert_inbound_message
from shared.db_transaction import async_write_transaction
from shared.turn_identity import bind_turn_identity
from tests.agent.test_inbound_ownership import _admit, _agent


def test_request_cannot_prepopulate_reserved_result(db_conn: psycopg.Connection) -> None:
    agent_id = _agent(db_conn)
    with pytest.raises(ValueError, match="reserved"):
        insert_inbound_message(
            db_conn,
            agent_id,
            "",
            "user",
            "restart",
            {"lifecycle_result": {"outcome": "superseded", "reason": "target_replaced"}},
        )
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchone() == (0,)


def _command(conn: psycopg.Connection, agent_id: int, kind: str) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES (%s,'',%s,'user') RETURNING id",
        (agent_id, kind),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


async def test_acceptance_survives_caller_loss_without_ack_or_retarget(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    first = _command(db_conn, agent_id, "restart")
    second = _command(db_conn, agent_id, "terminate")
    with bind_turn_identity(agent_id, incarnation=owner):
        async with async_write_transaction(aops_pool) as conn:
            intent = await accept_lifecycle_intent(conn, agent_id)
            assert intent is not None and intent.id == first
    accepted = db_conn.execute(
        "SELECT status,claimed_at,target_generation,target_owner,applied_at,observed_at "
        "FROM inbound_messages WHERE id=%s",
        (first,),
    ).fetchone()
    assert accepted is not None
    assert accepted[0] == "claimed" and accepted[1] is not None
    assert accepted[2:] == (owner.generation, owner.owner, None, None)
    with bind_turn_identity(agent_id, incarnation=owner):
        async with async_write_transaction(aops_pool) as conn:
            intent = await accept_lifecycle_intent(conn, agent_id)
            assert intent is not None and intent.id == first
    assert (
        db_conn.execute(
            "SELECT status,claimed_at,target_generation,target_owner,applied_at,observed_at "
            "FROM inbound_messages WHERE id=%s",
            (first,),
        ).fetchone()
        == accepted
    )
    assert db_conn.execute(
        "SELECT status,claimed_at FROM inbound_messages WHERE id=%s", (second,)
    ).fetchone() == ("pending", None)


async def test_acceptance_rollback_leaves_no_pointer_or_claim(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    inbound = _command(db_conn, agent_id, "terminate")
    with (
        bind_turn_identity(agent_id, incarnation=owner),
        pytest.raises(RuntimeError, match="crash"),
    ):
        async with async_write_transaction(aops_pool) as conn:
            await accept_lifecycle_intent(conn, agent_id)
            raise RuntimeError("crash before acceptance commit")
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)
    assert db_conn.execute(
        "SELECT status,claimed_at,target_generation FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("pending", None, None)


async def test_pending_pointer_blocks_retention_and_foreign_agent_reference(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    inbound = _command(db_conn, agent_id, "restart")
    with bind_turn_identity(agent_id, incarnation=owner):
        async with async_write_transaction(aops_pool) as conn:
            await accept_lifecycle_intent(conn, agent_id)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db_conn.transaction():
        db_conn.execute("DELETE FROM inbound_messages WHERE id=%s", (inbound,))
    other = _agent(db_conn)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db_conn.transaction():
        db_conn.execute(
            "UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s", (inbound, other)
        )


async def test_replacement_does_not_inherit_accepted_target(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    inbound = _command(db_conn, agent_id, "restart")
    with bind_turn_identity(agent_id, incarnation=owner):
        async with async_write_transaction(aops_pool) as conn:
            intent = await accept_lifecycle_intent(conn, agent_id)
            assert intent is not None
            assert not await settle_superseded_intent(conn, intent)
    replacement = uuid4()
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s WHERE id=%s", (replacement, agent_id)
    )
    db_conn.commit()
    assert db_conn.execute(
        "SELECT target_generation, target_owner FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == (owner.generation, owner.owner)
    async with async_write_transaction(aops_pool) as conn:
        assert await settle_superseded_intent(conn, intent)
        assert not await settle_superseded_intent(conn, intent)
    assert db_conn.execute(
        "SELECT status,applied_at,observed_at,payload->'lifecycle_result' "
        "FROM inbound_messages WHERE id=%s",
        (inbound,),
    ).fetchone() == ("done", None, None, {"outcome": "superseded", "reason": "target_replaced"})
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)
