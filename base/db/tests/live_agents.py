from __future__ import annotations

import psycopg

from base import db


def seed_agent(db_conn: psycopg.Connection, status: str, *, live_lease: bool = True) -> int:
    from datetime import UTC, datetime, timedelta

    agent_id = db.create_agent(db_conn)
    lease = datetime.now(UTC) + timedelta(seconds=600) if live_lease else None
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, lease_expires_at) "
            "VALUES (%s, 'test', %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, "
            "    lease_expires_at = EXCLUDED.lease_expires_at",
            (agent_id, status, lease),
        )
    db_conn.commit()
    return agent_id
