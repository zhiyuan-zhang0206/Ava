from __future__ import annotations

import psycopg
import pytest

from base.db import create_agent

_HOST = "127.0.0.1"


def test_live_port_unique_index_guards_raced_registrations(
    db_conn: psycopg.Connection,
) -> None:
    first = create_agent(db_conn)
    second = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host) VALUES (%s, 'x', 8775, %s)",
            (first, _HOST),
        )
    db_conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation), db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host) VALUES (%s, 'y', 8775, %s)",
            (second, _HOST),
        )
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET expired_at = now() WHERE agent_id = %s AND name = 'x'",
            (first,),
        )
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host) VALUES (%s, 'z', 8775, %s)",
            (second, _HOST),
        )
    db_conn.commit()
