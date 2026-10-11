"""The runner holds no UPDATE on agents; impersonation session allocation goes through
a SECURITY DEFINER function. The runner matrix itself lives in
tests/components/base/test_runner_role.py."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql

from base.cluster.authority import ensure_groups
from tests.path_scoped.db_authority_tests import AuthorityCluster


def _open_lease(conn: psycopg.Connection, agent_id: int) -> tuple[object, ...] | None:
    return conn.execute(
        "INSERT INTO agent_impersonations (id, agent_id, source, machine, status,"
        " ttl_seconds, expires_at) VALUES (gen_random_uuid(), %s, 'codex', 'test-machine',"
        " 'requested', 3600, now() + interval '1 hour') RETURNING session_id",
        (agent_id,),
    ).fetchone()


def test_runner_allocates_impersonation_sessions_without_agents_update(
    authority_postgres: AuthorityCluster,
) -> None:
    cluster = authority_postgres
    with cluster.admin() as conn:
        row = conn.execute("INSERT INTO agents (label) VALUES ('seed') RETURNING id").fetchone()
        assert row is not None
        agent_id = row[0]
        conn.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'idling')",
            (agent_id,),
        )

    with cluster.connect_class("runner", autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE agents SET label = 'denied' WHERE id = %s", (agent_id,))
        assert _open_lease(conn, agent_id) == (0,)
        assert conn.execute(
            "SELECT impersonation_index FROM agents WHERE id = %s", (agent_id,)
        ).fetchone() == (1,)


def test_runner_transport_maintenance_grants_do_not_allow_destination_mutation(
    authority_postgres: AuthorityCluster,
) -> None:
    cluster = authority_postgres
    with cluster.admin() as conn:
        row = conn.execute(
            "INSERT INTO agents(label) VALUES('transport grants') RETURNING id"
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO agents_meta(id,spawner,status) VALUES(%s,'user','idling')", (row[0],)
        )
        assert _open_lease(conn, row[0]) == (0,)
        ensure_groups(conn, owner=cluster.owner, database=cluster.database, groups=cluster.groups)
    with cluster.connect_class("runner", autocommit=True) as conn:
        conn.execute(
            "UPDATE agent_impersonations SET relay_generation=1,relay_identity='{}',"
            "relay_degraded_reason='unknown',terminal_notice_attempts=1 WHERE agent_id=%s",
            (row[0],),
        )
        for column in (
            "terminal_notice_snapshot",
            "terminal_notice_pending_at",
            "relay_thread_id",
            "machine",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    sql.SQL(
                        "UPDATE agent_impersonations SET {column}=NULL WHERE agent_id=%s"
                    ).format(column=sql.Identifier(column)),
                    (row[0],),
                )
