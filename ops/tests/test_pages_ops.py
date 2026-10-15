from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from base.db import create_agent
from ops.pages import PageRow, register_page_in_transaction

_HOST = "127.0.0.1"


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
    aid = create_agent(db_conn)
    r1 = _register(db_conn, aid, "p", 8001, _HOST, None, serve_dir="/data/a")
    r2 = _register(db_conn, aid, "p", 8002, "10.0.0.2", "v2", serve_dir="/data/b")
    assert r2.id == r1.id
    assert r2.port == 8002
    assert r2.title == "v2"
    assert r2.serve_dir == "/data/b"
    db_conn.rollback()
    rows = _page_rows(db_conn, aid)
    assert rows == [("p", 8002, "10.0.0.2", "v2", "/data/b", None)]


def test_register_page_raced_conflict_raises_domain_error(
    db_conn: psycopg.Connection,
) -> None:
    from ops.pages import PagePortConflictError

    owner = create_agent(db_conn)
    other = create_agent(db_conn)
    _register(db_conn, owner, "holder", 8776, _HOST, None)
    with pytest.raises(PagePortConflictError, match="'holder'"):
        _register(db_conn, other, "clash", 8776, _HOST, None)


@pytest.mark.parametrize("changed", [False, True])
def test_explicit_registration_resets_admission_only_for_a_changed_server(
    db_conn: psycopg.Connection, changed: bool
) -> None:
    aid = create_agent(db_conn)
    original = _register(db_conn, aid, "admission", 8900, _HOST, None, serve_dir="/data/a")
    db_conn.execute(
        "UPDATE agent_pages SET session_name = 'known-page-session', server_token = 'token' "
        "WHERE id = %s",
        (original.id,),
    )
    db_conn.commit()
    _register(
        db_conn,
        aid,
        "admission",
        8901 if changed else 8900,
        _HOST,
        "updated title",
        serve_dir="/data/a",
    )
    state = db_conn.execute(
        "SELECT session_name, server_token FROM agent_pages WHERE id = %s", (original.id,)
    ).fetchone()
    assert state == ((None, None) if changed else ("known-page-session", "token"))
