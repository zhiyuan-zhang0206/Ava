"""The unique index on live page ports guards raced registrations."""

from __future__ import annotations

import psycopg
import pytest

from base.db import create_agent

_HOST = "127.0.0.1"  # loopback — the single-box posture the SDK registers (audit P1-4: only loopback / the agent's own machine are legal proxy targets)


def test_live_port_unique_index_guards_raced_registrations(
    db_conn: psycopg.Connection,
) -> None:
    """DB-level pin of migration page-live-port-unique: two rows cannot both be
    live on one (host, port), while an expired row is no obstacle."""
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
