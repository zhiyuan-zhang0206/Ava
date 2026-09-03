"""Database invariants for immutable agent spawner lineage."""

from __future__ import annotations

from pathlib import Path
from typing import LiteralString, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors, sql


_BORN_SPAWNER_MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations" / "20260903T175722_add-born-spawner.sql"
)


def _seed_agents_meta(
    db_conn: psycopg.Connection,
    rows: list[tuple[int, str, str, int | None]],
) -> None:
    """Insert explicit agent identities and their lifecycle metadata."""
    with db_conn.cursor() as cur:
        for agent_id, spawner, status, fork_source_agent_id in rows:
            cur.execute("INSERT INTO agents (id) VALUES (%s)", (agent_id,))
            cur.execute(
                "INSERT INTO agents_meta "
                "(id, spawner, born_spawner, status, fork_source_agent_id, fork_source_checkpoint_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    agent_id,
                    spawner,
                    spawner,
                    status,
                    fork_source_agent_id,
                    "checkpoint" if fork_source_agent_id is not None else None,
                ),
            )
    db_conn.commit()


def _spawner(db_conn: psycopg.Connection, agent_id: int) -> str:
    with db_conn.cursor() as cur:
        cur.execute("SELECT spawner FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _born_spawner(db_conn: psycopg.Connection, agent_id: int) -> str | None:
    with db_conn.cursor() as cur:
        cur.execute("SELECT born_spawner FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_terminating_parent_leaves_live_child_spawner_unchanged(
    db_conn: psycopg.Connection,
) -> None:
    """Terminating a parent does not reassign a live child's lineage."""
    _seed_agents_meta(
        db_conn,
        [
            (228, "user", "running", None),
            (2780, "agent:228", "running", None),
            (3241, "agent:2780", "running", None),
        ],
    )

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = 2780")

    assert _spawner(db_conn, 3241) == "agent:2780"


def test_terminating_root_leaves_child_spawner_unchanged(
    db_conn: psycopg.Connection,
) -> None:
    """Terminating a user-spawned parent does not promote its child to user."""
    _seed_agents_meta(
        db_conn,
        [
            (1, "user", "running", None),
            (2, "agent:1", "idling", None),
        ],
    )

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = 1")

    assert _spawner(db_conn, 2) == "agent:1"


def test_terminating_middle_of_chain_leaves_descendant_spawners_unchanged(
    db_conn: psycopg.Connection,
) -> None:
    """Every descendant retains its direct spawn record after a middle terminates."""
    _seed_agents_meta(
        db_conn,
        [
            (1, "user", "running", None),
            (2, "agent:1", "running", None),
            (3, "agent:2", "running", None),
            (4, "agent:3", "restarting", None),
        ],
    )

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = 2")

    assert [_spawner(db_conn, agent_id) for agent_id in (3, 4)] == ["agent:2", "agent:3"]


def test_resurrecting_terminated_agent_leaves_spawner_unchanged(
    db_conn: psycopg.Connection,
) -> None:
    """Returning to a live status does not reassign a terminated parent's child."""
    _seed_agents_meta(
        db_conn,
        [
            (1, "user", "terminated", None),
            (2, "agent:1", "terminated", None),
        ],
    )

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = 2")

    assert _spawner(db_conn, 2) == "agent:1"


def test_terminating_parent_preserves_spawner_and_fork_provenance(
    db_conn: psycopg.Connection,
) -> None:
    """A lifecycle event cannot rewrite any immutable spawn or fork record."""
    _seed_agents_meta(
        db_conn,
        [
            (1, "user", "running", None),
            (2, "agent:1", "running", 1),
        ],
    )

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = 1")
        cur.execute(
            "SELECT spawner, born_spawner, fork_source_agent_id, fork_source_checkpoint_id "
            "FROM agents_meta WHERE id = 2"
        )
        row = cur.fetchone()

    assert row == ("agent:1", "agent:1", 1, "checkpoint")


def test_terminating_agent_without_children_leaves_spawner_unchanged(
    db_conn: psycopg.Connection,
) -> None:
    """A terminal status transition without children only changes that status."""
    _seed_agents_meta(db_conn, [(1, "user", "running", None)])

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = 1")
        cur.execute("SELECT status, spawner FROM agents_meta WHERE id = 1")
        row = cur.fetchone()

    assert row == ("terminated", "user")


def test_born_spawner_is_append_only(db_conn: psycopg.Connection) -> None:
    _seed_agents_meta(db_conn, [(1, "user", "running", None)])

    with (
        pytest.raises(errors.RaiseException, match="born_spawner is append-only"),
        db_conn.transaction(),
    ):
        db_conn.execute("UPDATE agents_meta SET born_spawner = 'agent:1' WHERE id = 1")

    assert _born_spawner(db_conn, 1) == "user"


def test_born_spawner_migration_backfills_fork_then_post_ruling_spawner_then_timed_chat(
    db_conn: psycopg.Connection,
) -> None:
    """Backfill order: fork source, then the spawner of any agent born after the
    2026-08-28 ruling made it the true lineage, then the timed agent chat
    heuristic that only reconstructs lineage for older rows.
    """
    schema = "born_spawner_" + uuid4().hex
    migration = sql.SQL(cast(LiteralString, _BORN_SPAWNER_MIGRATION.read_text()))
    with db_conn.transaction(force_rollback=True):
        db_conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        db_conn.execute(
            sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema))
        )
        db_conn.execute(
            "CREATE TABLE agents_meta ("
            "id BIGINT PRIMARY KEY, spawner TEXT NOT NULL, fork_source_agent_id BIGINT, "
            "spawned_at TIMESTAMPTZ NOT NULL)"
        )
        db_conn.execute(
            "CREATE TABLE inbound_messages ("
            "id BIGINT PRIMARY KEY, agent_id BIGINT NOT NULL, kind TEXT NOT NULL, "
            "source TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)"
        )
        db_conn.execute(
            "INSERT INTO agents_meta (id, spawner, fork_source_agent_id, spawned_at) VALUES "
            "(1, 'agent:999', 71, '2026-09-03 00:00:00+00'), "
            "(2, 'agent:999', NULL, '2026-09-03 00:00:00+00'), "
            "(3, 'user', NULL, '2026-09-03 00:00:00+00'), "
            "(4, 'cron', NULL, '2026-09-03 00:00:00+00'), "
            "(5, 'agent:999', NULL, '2026-08-10 00:00:00+00')"
        )
        db_conn.execute(
            "INSERT INTO inbound_messages (id, agent_id, kind, source, created_at) VALUES "
            "(1, 2, 'chat', 'agent:22', '2026-09-03 00:09:00+00'), "
            "(2, 2, 'chat', 'agent:23', '2026-09-03 00:01:00+00'), "
            "(3, 3, 'chat', 'agent:33', '2026-09-03 00:10:01+00'), "
            "(4, 4, 'restart', 'agent:44', '2026-09-03 00:01:00+00'), "
            "(5, 5, 'chat', 'agent:55', '2026-08-10 00:09:00+00')"
        )
        db_conn.execute(migration)

        rows = db_conn.execute("SELECT id, born_spawner FROM agents_meta ORDER BY id").fetchall()
        assert rows == [
            (1, "agent:71"),
            (2, "agent:999"),
            (3, "user"),
            (4, "cron"),
            (5, "agent:55"),
        ]
