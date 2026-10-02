"""`ops.pages`: an expired row is revived, a re-register upserts the serve dir, and a raced conflict is a domain error."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from base.db import create_agent

_HOST = "127.0.0.1"  # loopback — the single-box posture the SDK registers (audit P1-4: only loopback / the agent's own machine are legal proxy targets)


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
    from ops.pages import register_page

    aid = create_agent(db_conn)
    original = register_page(db_conn, aid, "revive", 8001, _HOST, None, ttl_seconds=30)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET expired_at = now() WHERE id = %s",
            (original.id,),
        )
    db_conn.commit()

    before = datetime.now(UTC)
    revived = register_page(db_conn, aid, "revive", 8002, _HOST, "Again", ttl_seconds=240)

    assert revived.id == original.id
    assert revived.port == 8002
    expires_at, expired_at = _page_deadline(db_conn, aid, "revive")
    assert expires_at is not None
    assert (
        before + timedelta(seconds=239) <= expires_at <= datetime.now(UTC) + timedelta(seconds=241)
    )
    assert expired_at is None


def test_register_page_upsert_updates_serve_dir(db_conn: psycopg.Connection) -> None:
    """ops.register_page \u540c\u540d upsert\uff08UPDATE \u5206\u652f\uff09\uff1aport/title/serve_dir \u4e00\u8d77\u66f4\u65b0\uff0c\u884c id \u4e0d\u53d8\u3002"""
    from ops.pages import register_page

    aid = create_agent(db_conn)
    r1 = register_page(db_conn, aid, "p", 8001, _HOST, None, serve_dir="/data/a")
    r2 = register_page(db_conn, aid, "p", 8002, "10.0.0.2", "v2", serve_dir="/data/b")
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
    """ops.register_page (the path behind the router's pre-check) refuses a
    port a live row holds and names the occupant — the index backstop."""
    from ops.pages import PagePortConflictError, register_page

    owner = create_agent(db_conn)
    other = create_agent(db_conn)
    register_page(db_conn, owner, "holder", 8776, _HOST, None)
    with pytest.raises(PagePortConflictError, match="'holder'"):
        register_page(db_conn, other, "clash", 8776, _HOST, None)
