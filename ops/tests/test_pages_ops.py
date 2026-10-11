"""`ops.pages`: an expired row is revived, a re-register upserts the serve dir, and a raced conflict is a domain error."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from base.db import create_agent
from ops.pages import PageRow, register_page_in_transaction

_HOST = "127.0.0.1"  # loopback — the single-box posture the SDK registers (audit P1-4: only loopback / the agent's own machine are legal proxy targets)


def _register(
    conn: psycopg.Connection,
    agent_id: int,
    name: str,
    port: int,
    host: str,
    title: str | None,
    **kwargs: Any,
) -> PageRow:
    row = register_page_in_transaction(conn, agent_id, name, port, host, title, **kwargs)
    conn.commit()
    return row


def _page_rows(conn: psycopg.Connection, agent_id: int) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, port, host, title, serve_dir, closed_at FROM agent_pages "
            "WHERE agent_id = %s ORDER BY id ASC",
            (agent_id,),
        )
        return cur.fetchall()


def _page_deadline(
    conn: psycopg.Connection, agent_id: int, name: str
) -> tuple[datetime | None, datetime | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT expires_at, expired_at FROM agent_pages WHERE agent_id = %s AND name = %s",
            (agent_id, name),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0], row[1]


def test_register_page_revives_expired_row_and_resets_deadline(
    db_conn: psycopg.Connection,
) -> None:

    aid = create_agent(db_conn)
    original = _register(db_conn, aid, "revive", 8001, _HOST, None, ttl_seconds=30)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET expired_at = now() WHERE id = %s",
            (original.id,),
        )
    db_conn.commit()

    before = datetime.now(UTC)
    revived = _register(db_conn, aid, "revive", 8002, _HOST, "Again", ttl_seconds=240)

    assert revived.id == original.id
    assert revived.port == 8002
    expires_at, expired_at = _page_deadline(db_conn, aid, "revive")
    assert expires_at is not None
    assert (
        before + timedelta(seconds=239) <= expires_at <= datetime.now(UTC) + timedelta(seconds=241)
    )
    assert expired_at is None


def test_register_page_upsert_updates_serve_dir(db_conn: psycopg.Connection) -> None:
    """Re-registering a name is an UPDATE: port, title and serve_dir change together and the row id is stable."""
    aid = create_agent(db_conn)
    r1 = _register(db_conn, aid, "p", 8001, _HOST, None, serve_dir="/data/a")
    r2 = _register(db_conn, aid, "p", 8002, "10.0.0.2", "v2", serve_dir="/data/b")
    assert r2.id == r1.id  # UPDATE, not INSERT
    assert r2.port == 8002
    assert r2.title == "v2"
    assert r2.serve_dir == "/data/b"
    db_conn.rollback()
    rows = _page_rows(db_conn, aid)
    assert rows == [("p", 8002, "10.0.0.2", "v2", "/data/b", None)]


def test_register_page_raced_conflict_raises_domain_error(
    db_conn: psycopg.Connection,
) -> None:
    """ops.pages.register_page_in_transaction (the path behind the router's pre-check) refuses a
    port a live row holds and names the occupant — the index backstop."""
    from ops.pages import PagePortConflictError

    owner = create_agent(db_conn)
    other = create_agent(db_conn)
    _register(db_conn, owner, "holder", 8776, _HOST, None)
    with pytest.raises(PagePortConflictError, match="'holder'"):
        _register(db_conn, other, "clash", 8776, _HOST, None)
