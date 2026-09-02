"""Incarnation fences exercise actual DB transitions, not just SQL strings."""

from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent._starting import claim_agent_row
from agent.db import renew_agent_lease
from ops.ops_exit import _mark_exited_blocking
from shared.config import settings
from shared.db import PG_KEEPALIVE_KWARGS, create_agent
from shared.machine import machine_name
from shared.runtime_incarnation import (
    RuntimeIncarnation,
    bind_process_incarnation,
    current_incarnation,
)


@pytest.fixture
def sync_pool() -> Iterator[ConnectionPool]:
    with ConnectionPool(
        settings.data_plane.db_url,
        min_size=1,
        max_size=2,
        kwargs=PG_KEEPALIVE_KWARGS,
    ) as pool:
        yield pool


def _row(conn: psycopg.Connection) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, machine, status) VALUES (%s, %s, 'idling') "
        "ON CONFLICT (id) DO UPDATE SET machine = EXCLUDED.machine",
        (agent_id, machine_name()),
    )
    conn.commit()
    return agent_id


def _replace(conn: psycopg.Connection, agent_id: int) -> RuntimeIncarnation:
    incarnation = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    conn.execute(
        "UPDATE agents_meta SET status = 'running', pid = 4242, runtime_kind = 'process', "
        "runtime_generation = %s, runtime_owner = %s, lease_expires_at = NULL WHERE id = %s",
        (incarnation.generation, incarnation.owner, agent_id),
    )
    conn.commit()
    return incarnation


def test_actual_admission_stamps_unknown_protocol(db_conn: psycopg.Connection) -> None:
    agent_id = _row(db_conn)
    claim_agent_row(agent_id)
    incarnation = current_incarnation(agent_id)
    assert incarnation is not None
    assert db_conn.execute(
        "SELECT runtime_generation, runtime_owner, runtime_kind, runtime_protocol_version "
        "FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone() == (incarnation.generation, incarnation.owner, "process", 0)


@pytest.mark.parametrize("scenario", ["missing", "stale_generation", "stale_owner", "current"])
def test_exit_matches_original_incarnation(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
    scenario: str,
) -> None:
    agent_id = _row(db_conn)
    current = _replace(db_conn, agent_id)
    generation = None if scenario == "missing" else current.generation
    owner = None if scenario == "missing" else current.owner
    if scenario == "stale_generation":
        generation = uuid4()
    if scenario == "stale_owner":
        owner = uuid4()
    changed, _, _ = _mark_exited_blocking(agent_id, sync_pool, generation, owner)
    assert changed == int(scenario == "current")


def test_legacy_exit_cannot_finalize_owned_row(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
) -> None:
    agent_id = _row(db_conn)
    _replace(db_conn, agent_id)
    assert _mark_exited_blocking(agent_id, sync_pool)[0] == 0


def test_legacy_hosted_exit_remains_supported(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
) -> None:
    agent_id = _row(db_conn)
    assert _mark_exited_blocking(agent_id, sync_pool)[0] == 1


@pytest.mark.parametrize("scenario", ["missing", "stale_generation", "stale_owner", "current"])
async def test_lease_matches_original_incarnation(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    scenario: str,
) -> None:
    agent_id = _row(db_conn)
    current = _replace(db_conn, agent_id)
    if scenario != "missing":
        bind_process_incarnation(
            RuntimeIncarnation(
                agent_id,
                uuid4() if scenario == "stale_generation" else current.generation,
                uuid4() if scenario == "stale_owner" else current.owner,
            )
        )
    await renew_agent_lease(aops_pool, agent_id)
    renewed = db_conn.execute(
        "SELECT lease_expires_at IS NOT NULL FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert renewed == (scenario == "current",)


def test_exit_after_handoff_does_not_terminate_unclaimed_successor(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
) -> None:
    agent_id = _row(db_conn)
    old = _replace(db_conn, agent_id)
    db_conn.execute(
        "UPDATE agents_meta SET pid = NULL, status = 'idling' WHERE id = %s", (agent_id,)
    )
    db_conn.commit()
    assert _mark_exited_blocking(agent_id, sync_pool, old.generation, old.owner)[0] == 0
