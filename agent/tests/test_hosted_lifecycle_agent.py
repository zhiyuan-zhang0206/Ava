"""The agent side of hosted lifecycle application: a stale unapplied pointer closes without retargeting, a restart leaves shell sessions for the later terminate, and an apply without a bound killer refuses a kill request."""

from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.hosted import admit_hosted_runtime, apply_hosted_lifecycle
from base.config import settings
from base.native_process.turn_identity import bind_turn_identity
from tests.agent.test_inbound_ownership import _admit, _agent
from tests.agent.test_lifecycle_intent import _command


async def test_stale_unapplied_pointer_closes_without_retargeting(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    old = await _admit(aops_pool, agent_id)
    first = _command(db_conn, agent_id, "restart")
    with bind_turn_identity(agent_id, incarnation=old):
        await claim_inbound_batch(aops_pool, agent_id)
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at=now()-interval '1 second' WHERE id=%s", (agent_id,)
    )
    db_conn.commit()
    new = await admit_hosted_runtime(
        aops_pool, agent_id, "claim-test", uuid4(), expected_from="running"
    )
    assert new is not None
    second = _command(db_conn, agent_id, "restart")
    with bind_turn_identity(agent_id, incarnation=new):
        assert [row.id for row in await claim_inbound_batch(aops_pool, agent_id)] == [second]
    assert db_conn.execute(
        "SELECT status,applied_at,target_generation FROM inbound_messages WHERE id=%s", (first,)
    ).fetchone() == ("done", None, old.generation)


def _terminate_command(
    conn: psycopg.Connection, agent_id: int, *, source: str = "user", kill: bool = False
) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
        "VALUES (%s,'','terminate',%s,%s) RETURNING id",
        (agent_id, source, Jsonb({"kill_all_shell_sessions": True}) if kill else None),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _record_kills(
    monkeypatch: pytest.MonkeyPatch, *, fail: bool = False
) -> list[tuple[int, str | None]]:
    """Patch the host's kill primitive; record (agent id, status at kill time)."""
    calls: list[tuple[int, str | None]] = []

    def _kill(agent_id: int) -> list[int]:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
            row = conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent_id,)).fetchone()
        calls.append((agent_id, None if row is None else row[0]))
        if fail:
            raise RuntimeError("failed to kill shell session(s) [4]")
        return [4]

    monkeypatch.setattr("ops.cluster_status.kill_agent_shells", _kill)
    return calls


async def test_hosted_restart_leaves_shell_sessions_for_the_later_terminate(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    _command(db_conn, agent_id, "restart")
    _terminate_command(db_conn, agent_id, kill=True)
    kills = _record_kills(monkeypatch)
    with bind_turn_identity(agent_id, incarnation=owner):
        await claim_inbound_batch(aops_pool, agent_id)
        assert (
            await apply_hosted_lifecycle(
                aops_pool, owner, kill_shell_sessions=lambda _aid: kills.append((_aid, None))
            )
            == "restart"
        )
    assert kills == []


async def test_hosted_apply_without_a_bound_killer_refuses_a_kill_request(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """Fail fast: a caller that binds no killer cannot silently drop the
    request — the apply raises and rolls back, the termination stays pending."""
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id)
    command = _terminate_command(db_conn, agent_id, kill=True)
    with bind_turn_identity(agent_id, incarnation=owner):
        await claim_inbound_batch(aops_pool, agent_id)
        with pytest.raises(RuntimeError, match="no killer is bound"):
            await apply_hosted_lifecycle(aops_pool, owner)
    assert db_conn.execute(
        "SELECT i.applied_at, m.status FROM inbound_messages i JOIN agents_meta m "
        "ON m.id=i.agent_id WHERE i.id=%s",
        (command,),
    ).fetchone() == (None, "running")
