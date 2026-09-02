"""Database invariants for immutable agent spawner lineage."""

from __future__ import annotations

import psycopg


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
                "(id, spawner, status, fork_source_agent_id, fork_source_checkpoint_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    agent_id,
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
            "SELECT spawner, fork_source_agent_id, fork_source_checkpoint_id "
            "FROM agents_meta WHERE id = 2"
        )
        row = cur.fetchone()

    assert row == ("agent:1", 1, "checkpoint")


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
