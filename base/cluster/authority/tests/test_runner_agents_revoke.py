"""A cluster born when the runner matrix granted UPDATE on agents converges to
none: the allocator migration, then the start-path grant refresh. The runner
matrix itself lives in tests/base/test_runner_role.py."""

from __future__ import annotations

from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest

from base.cluster.authority import ensure_groups
from tests.path_scoped.db_authority_tests import AuthorityCluster

_MIGRATION = (
    Path(__file__).resolve().parents[4]
    / "migrations"
    / "20261003T120000_impersonation-allocator-definer.sql"
)


def _open_lease(conn: psycopg.Connection, agent_id: int) -> tuple[object, ...] | None:
    return conn.execute(
        "INSERT INTO agent_impersonations (id, agent_id, source, machine, status,"
        " ttl_seconds, expires_at) VALUES (gen_random_uuid(), %s, 'codex', 'test-machine',"
        " 'requested', 3600, now() + interval '1 hour') RETURNING session_id",
        (agent_id,),
    ).fetchone()


def test_refresh_revokes_the_runner_agents_update_an_earlier_release_granted(
    authority_postgres: AuthorityCluster,
) -> None:
    cluster = authority_postgres
    with cluster.admin() as conn:
        # The pre-migration shape: INVOKER allocator, table-wide runner UPDATE.
        conn.execute("ALTER FUNCTION allocate_impersonation_session() SECURITY INVOKER")
        conn.execute("ALTER FUNCTION allocate_impersonation_session() RESET search_path")
        conn.execute("GRANT UPDATE ON agents TO ava_runner")
        agent_ids: list[int] = []
        for _ in range(2):
            row = conn.execute("INSERT INTO agents (label) VALUES ('seed') RETURNING id").fetchone()
            assert row is not None
            agent_ids.append(row[0])
            conn.execute(
                "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'idling')",
                (row[0],),
            )
    before, after = agent_ids

    with cluster.connect_class("runner", autocommit=True) as conn:
        conn.execute("UPDATE agents SET label = 'old-grant' WHERE id = %s", (before,))
        assert _open_lease(conn, before) == (0,)

    with cluster.admin() as conn:
        # Migrations run before the refresh on every start.
        conn.execute(cast(LiteralString, _MIGRATION.read_text()))
        for _ in range(2):
            ensure_groups(
                conn, owner=cluster.owner, database=cluster.database, groups=cluster.groups
            )

    with cluster.connect_class("runner", autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE agents SET label = 'revoked' WHERE id = %s", (before,))
        assert _open_lease(conn, after) == (0,)
        assert conn.execute(
            "SELECT impersonation_index FROM agents WHERE id = %s", (after,)
        ).fetchone() == (1,)
