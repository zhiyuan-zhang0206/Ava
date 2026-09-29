"""base/agents/tasks/rules.py — the registry's transition rules, locked against a
real database.

Both write surfaces call these (the fleet plugin's create/update and the gateway
PATCH), so this is where the rules themselves are pinned; the callers' own tests
cover their messages and locking.
"""

from __future__ import annotations

from itertools import count

import psycopg

from base.agents.tasks import rules

_TITLE = count(1)


def _make_agent(db: psycopg.Connection) -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        aid = int(cur.fetchone()[0])  # type: ignore[index]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
            (aid,),
        )
    db.commit()
    return aid


def _make_task(
    db: psycopg.Connection, *, owner: int, parent_id: int | None = None, title: str | None = None
) -> int:
    # Distinct titles by default: a partial unique index forbids two in_progress
    # rows sharing a title.
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, owner, created_by, parent_id) "
            "VALUES (%s, 'd', 'in_progress', %s, 'user', %s) RETURNING id",
            (title or f"rules-{next(_TITLE)}", owner, parent_id),
        )
        tid = int(cur.fetchone()[0])  # type: ignore[index]
    db.commit()
    return tid


def test_first_open_child_none_when_no_open_children(db_conn: psycopg.Connection) -> None:
    """No direct child still in_progress: None."""
    owner = _make_agent(db_conn)
    parent = _make_task(db_conn, owner=owner)
    closed_child = _make_task(db_conn, owner=owner, parent_id=parent)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agent_tasks SET status = 'done' WHERE id = %s", (closed_child,))
    db_conn.commit()
    with db_conn.cursor() as cur:
        assert rules.first_open_child(cur, parent) is None


def test_first_open_child_returns_lowest_id_and_count(db_conn: psycopg.Connection) -> None:
    """Two in_progress children: the lowest id, plus the total open-child count."""
    owner = _make_agent(db_conn)
    parent = _make_task(db_conn, owner=owner)
    first_child = _make_task(db_conn, owner=owner, parent_id=parent)
    _make_task(db_conn, owner=owner, parent_id=parent)
    with db_conn.cursor() as cur:
        assert rules.first_open_child(cur, parent) == (first_child, 2)


def test_open_title_holder_honours_exclude_id(db_conn: psycopg.Connection) -> None:
    """Finds the in_progress holder of a title, but not when it is the excluded id."""
    owner = _make_agent(db_conn)
    title = f"rules-{next(_TITLE)}"
    tid = _make_task(db_conn, owner=owner, title=title)
    with db_conn.cursor() as cur:
        assert rules.open_title_holder(cur, title) == (tid, "in_progress")
        assert rules.open_title_holder(cur, title, exclude_id=tid) is None


def test_is_closed_covers_every_status() -> None:
    """done and cancelled are closed; in_progress and "no status change" are not."""
    assert rules.is_closed("done")
    assert rules.is_closed("cancelled")
    assert not rules.is_closed("in_progress")
    assert not rules.is_closed(None)
