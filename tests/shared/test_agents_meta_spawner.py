"""Database invariants for the live agent-tree spawner chain."""

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


def test_terminating_agent_folds_live_child_to_living_parent(
    db_conn: psycopg.Connection,
) -> None:
    """A live grandchild remains under the nearest living tree ancestor."""
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

    assert _spawner(db_conn, 3241) == "agent:228"


def test_terminating_root_folds_live_child_to_root_spawner(
    db_conn: psycopg.Connection,
) -> None:
    """A user-spawned parent leaves live children as top-level roots."""
    _seed_agents_meta(
        db_conn,
        [
            (1, "user", "running", None),
            (2, "agent:1", "idling", None),
        ],
    )

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = 1")

    assert _spawner(db_conn, 2) == "user"


def test_terminating_agent_skips_terminated_ancestors(
    db_conn: psycopg.Connection,
) -> None:
    """A live child folds past all terminated ancestors to a living one."""
    _seed_agents_meta(
        db_conn,
        [
            (1, "user", "running", None),
            (2, "agent:1", "terminated", None),
            (3, "agent:2", "running", None),
            (4, "agent:3", "restarting", None),
        ],
    )

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = 3")

    assert _spawner(db_conn, 4) == "agent:1"


def test_terminating_agent_only_folds_live_children(
    db_conn: psycopg.Connection,
) -> None:
    """Every live status folds, while a terminated child's lineage remains intact."""
    _seed_agents_meta(
        db_conn,
        [
            (1, "user", "running", None),
            (2, "agent:1", "running", None),
            (3, "agent:1", "idling", None),
            (4, "agent:1", "restarting", None),
            (5, "agent:1", "terminated", None),
        ],
    )

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = 1")

    assert [_spawner(db_conn, agent_id) for agent_id in (2, 3, 4)] == ["user", "user", "user"]
    assert _spawner(db_conn, 5) == "agent:1"


def test_resurrecting_terminated_child_folds_to_living_ancestor(
    db_conn: psycopg.Connection,
) -> None:
    """A preserved terminated lineage is repaired before its child is live again."""
    _seed_agents_meta(
        db_conn,
        [
            (1, "user", "running", None),
            (2, "agent:1", "running", None),
            (3, "agent:2", "terminated", None),
        ],
    )

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = 2")
        cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = 3")

    assert _spawner(db_conn, 3) == "agent:1"


def test_terminating_agent_folds_tree_spawner_without_changing_fork_source(
    db_conn: psycopg.Connection,
) -> None:
    """Tree repair does not rewrite the fork's immutable bloodline."""
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

    assert row == ("user", 1, "checkpoint")


def test_terminating_agent_without_children_is_a_no_op(db_conn: psycopg.Connection) -> None:
    """An ordinary terminal status transition still succeeds with no tree children."""
    _seed_agents_meta(db_conn, [(1, "user", "running", None)])

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = 1")
        cur.execute("SELECT status FROM agents_meta WHERE id = 1")
        row = cur.fetchone()

    assert row == ("terminated",)


def test_terminating_agent_with_missing_parent_folds_live_child_to_user(
    db_conn: psycopg.Connection,
) -> None:
    """A broken ancestor link is made an intentional top-level root."""
    _seed_agents_meta(
        db_conn,
        [
            (1, "agent:999", "running", None),
            (2, "agent:1", "running", None),
        ],
    )

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = 1")

    assert _spawner(db_conn, 2) == "user"


def test_terminating_agent_stops_cyclic_ancestor_walk_at_hop_limit(
    db_conn: psycopg.Connection,
) -> None:
    """A corrupt cycle cannot loop forever and falls back to a root spawner."""
    _seed_agents_meta(
        db_conn,
        [
            (1, "agent:2", "running", None),
            (2, "agent:3", "terminated", None),
            (3, "agent:2", "terminated", None),
            (4, "agent:1", "running", None),
        ],
    )

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = 1")

    assert _spawner(db_conn, 4) == "user"
