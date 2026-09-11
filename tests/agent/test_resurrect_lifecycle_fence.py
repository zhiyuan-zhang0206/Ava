"""A resurrection supersedes earlier unapplied lifecycle commands (issue #2158).

Pre-fix, a pending terminate created during a rollout drain was adopted by the
incarnation a later resurrect had just admitted: the resurrect reported
"spawned", the older command applied seconds later, and the agent was killed
again (five agents in the 2026-09-10 rollout). The resurrection now records the
id of the kind='resurrect' inbound it enqueues as an epoch fence and settles
every earlier unapplied lifecycle command as superseded - visibly, never
silently - and acceptance settles any row that committed too late for the
resurrect transaction to see.
"""

import asyncio
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.db import claim_inbound_batch
from agent.hosted_ownership import admit_hosted_runtime, apply_hosted_lifecycle
from agent.lifecycle_intent import accept_lifecycle_intent
from ops.agent_wake import resurrect_agent
from ops.ops_exit import _force_terminate_transaction
from ops.resurrection_retry import ResurrectExitDeferredError
from shared.config import settings
from shared.db import PG_KEEPALIVE_KWARGS, create_agent
from shared.db_transaction import async_write_transaction
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


def _command(conn: psycopg.Connection, agent_id: int, kind: str) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES (%s,'',%s,'user') RETURNING id",
        (agent_id, kind),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _die(conn: psycopg.Connection, agent_id: int) -> None:
    """The row dies by a route other than the queued command (force / rollout)."""
    conn.execute(
        "UPDATE agents_meta SET status='terminated', termination_source='user' WHERE id=%s",
        (agent_id,),
    )
    conn.commit()


async def _admit(pool: AsyncConnectionPool, agent_id: int) -> RuntimeIncarnation:
    owner = await admit_hosted_runtime(
        pool, agent_id, "claim-test", uuid4(), expected_from="idling"
    )
    assert owner is not None
    return owner


def _epoch(conn: psycopg.Connection, agent_id: int) -> int:
    row = conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s AND kind='resurrect' "
        "ORDER BY id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    assert row is not None
    return row[0]


def _command_row(conn: psycopg.Connection, command: int) -> tuple[object, ...]:
    row = conn.execute(
        "SELECT status,applied_at,payload->'lifecycle_result' FROM inbound_messages WHERE id=%s",
        (command,),
    ).fetchone()
    assert row is not None
    return row


async def test_resurrect_supersedes_the_pending_terminate_it_would_replay(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """The incident: a delayed earlier terminate must not kill the new incarnation."""
    agent_id = _agent(db_conn)
    stale = _command(db_conn, agent_id, "terminate")
    _die(db_conn, agent_id)

    resurrect_agent(agent_id, resurrected_by="user")

    epoch = _epoch(db_conn, agent_id)
    assert _command_row(db_conn, stale) == (
        "done",
        None,
        {"outcome": "superseded", "reason": "resurrect", "resurrect_inbound_id": epoch},
    )
    assert db_conn.execute(
        "SELECT last_resurrect_inbound_id,lifecycle_command_id FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (epoch, None)

    owner = await _admit(aops_pool, agent_id)
    with bind_turn_identity(agent_id, incarnation=owner):
        batch = await claim_inbound_batch(aops_pool, agent_id)
    assert [item.kind for item in batch] == ["resurrect"]
    assert await apply_hosted_lifecycle(aops_pool, owner) is None
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("running",)


async def test_acceptance_settles_a_command_that_committed_after_the_epoch(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """Commit-order race: inserted before the resurrect, committed after it.

    The resurrect transaction cannot see an uncommitted row, so the epoch fence
    must also stop acceptance from adopting it later.
    """
    agent_id = _agent(db_conn)
    stale = _command(db_conn, agent_id, "terminate")
    epoch = db_conn.execute(
        "INSERT INTO inbound_messages (agent_id,content,kind,source) "
        "VALUES (%s,'','resurrect','user') RETURNING id",
        (agent_id,),
    ).fetchone()
    assert epoch is not None
    db_conn.execute(
        "UPDATE agents_meta SET last_resurrect_inbound_id=%s WHERE id=%s", (epoch[0], agent_id)
    )
    db_conn.commit()

    owner = await _admit(aops_pool, agent_id)
    with bind_turn_identity(agent_id, incarnation=owner):
        async with async_write_transaction(aops_pool) as conn:
            assert await accept_lifecycle_intent(conn, agent_id) is None
    assert _command_row(db_conn, stale) == (
        "done",
        None,
        {"outcome": "superseded", "reason": "resurrect", "resurrect_inbound_id": epoch[0]},
    )
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)


async def test_a_command_created_after_the_resurrect_still_applies(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """The fence supersedes the past, not the future: a later kill still lands."""
    agent_id = _agent(db_conn)
    _command(db_conn, agent_id, "terminate")
    _die(db_conn, agent_id)
    resurrect_agent(agent_id, resurrected_by="user")
    owner = await _admit(aops_pool, agent_id)
    newer = _command(db_conn, agent_id, "terminate")

    with bind_turn_identity(agent_id, incarnation=owner):
        batch = await claim_inbound_batch(aops_pool, agent_id)
    assert [item.kind for item in batch] == ["terminate"]
    assert await apply_hosted_lifecycle(aops_pool, owner) == "terminate"
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("terminated",)
    assert db_conn.execute(
        "SELECT status,applied_at IS NOT NULL FROM inbound_messages WHERE id=%s", (newer,)
    ).fetchone() == ("done", True)


async def test_an_accepted_but_unapplied_command_is_superseded_not_deferred(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """A claimed command that never applied has no external effect to preserve."""
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    command = _command(db_conn, agent_id, "terminate")
    with bind_turn_identity(agent_id, incarnation=owner):
        async with async_write_transaction(aops_pool) as conn:
            intent = await accept_lifecycle_intent(conn, agent_id)
    assert intent is not None and intent.id == command
    _die(db_conn, agent_id)

    resurrect_agent(agent_id, resurrected_by="user")

    epoch = _epoch(db_conn, agent_id)
    assert _command_row(db_conn, command) == (
        "done",
        None,
        {"outcome": "superseded", "reason": "resurrect", "resurrect_inbound_id": epoch},
    )
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)


async def test_force_then_immediate_resurrect_never_replays_the_force(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """The incident wiring: force acceptance is durable before its host applies it.

    The host is down, so the force command stays pending after the row is marked
    terminated; the operator resurrects immediately. The new incarnation must not
    adopt the delayed force.
    """
    agent_id = _agent(db_conn)
    with ConnectionPool[psycopg.Connection](
        settings.data_plane.db_url, min_size=1, max_size=1, kwargs=PG_KEEPALIVE_KWARGS
    ) as pool:
        _, _, _, force = await asyncio.to_thread(
            _force_terminate_transaction, agent_id, pool, source="user"
        )
    assert db_conn.execute(
        "SELECT status,applied_at FROM inbound_messages WHERE id=%s", (force,)
    ).fetchone() == ("pending", None)

    resurrect_agent(agent_id, resurrected_by="user")

    epoch = _epoch(db_conn, agent_id)
    assert _command_row(db_conn, force) == (
        "done",
        None,
        {"outcome": "superseded", "reason": "resurrect", "resurrect_inbound_id": epoch},
    )
    owner = await _admit(aops_pool, agent_id)
    with bind_turn_identity(agent_id, incarnation=owner):
        batch = await claim_inbound_batch(aops_pool, agent_id)
    assert "terminate" not in [item.kind for item in batch]
    assert await apply_hosted_lifecycle(aops_pool, owner) is None
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("running",)


async def test_refused_resurrect_leaves_no_fence_or_marker_for_the_retry(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """An applied-but-unobserved terminate still refuses honestly.

    Nothing half-written may remain: no fence, no marker, and the applied command
    untouched, so the operator's retry (after observation) proceeds cleanly and
    the already-applied command is never rewritten.
    """
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    command = _command(db_conn, agent_id, "terminate")
    with bind_turn_identity(agent_id, incarnation=owner):
        async with async_write_transaction(aops_pool) as conn:
            assert await accept_lifecycle_intent(conn, agent_id) is not None
    db_conn.execute(
        "UPDATE inbound_messages SET applied_at=clock_timestamp() WHERE id=%s", (command,)
    )
    _die(db_conn, agent_id)

    with pytest.raises(ResurrectExitDeferredError):
        resurrect_agent(agent_id, resurrected_by="user")

    assert db_conn.execute(
        "SELECT last_resurrect_inbound_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='resurrect'",
        (agent_id,),
    ).fetchone() == (0,)
    assert db_conn.execute(
        "SELECT status,applied_at IS NOT NULL,payload->'lifecycle_result' "
        "FROM inbound_messages WHERE id=%s",
        (command,),
    ).fetchone() == ("claimed", True, None)

    db_conn.execute(
        "UPDATE inbound_messages SET status='done',observed_at=clock_timestamp() WHERE id=%s",
        (command,),
    )
    db_conn.execute("UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s", (agent_id,))
    db_conn.commit()
    resurrect_agent(agent_id, resurrected_by="user")
    assert (
        db_conn.execute(
            "SELECT last_resurrect_inbound_id FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone()
        is not None
    )


async def test_reacceptance_is_idempotent_and_preserves_command_history(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    stale = db_conn.execute(
        "INSERT INTO inbound_messages (agent_id,content,kind,source,payload) "
        "VALUES (%s,'','terminate','user','{\"note\": \"drain\"}') RETURNING id",
        (agent_id,),
    ).fetchone()
    assert stale is not None
    db_conn.commit()
    _die(db_conn, agent_id)
    resurrect_agent(agent_id, resurrected_by="user")
    epoch = _epoch(db_conn, agent_id)
    settled = _command_row(db_conn, stale[0])

    owner = await _admit(aops_pool, agent_id)
    for _ in range(2):
        with bind_turn_identity(agent_id, incarnation=owner):
            async with async_write_transaction(aops_pool) as conn:
                assert await accept_lifecycle_intent(conn, agent_id) is None
    assert _command_row(db_conn, stale[0]) == settled
    payload = db_conn.execute(
        "SELECT payload FROM inbound_messages WHERE id=%s", (stale[0],)
    ).fetchone()
    assert payload is not None
    assert payload[0]["note"] == "drain"
    assert payload[0]["lifecycle_result"] == {
        "outcome": "superseded",
        "reason": "resurrect",
        "resurrect_inbound_id": epoch,
    }
