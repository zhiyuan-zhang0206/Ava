"""Gateway-owned completion digest delivery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import psycopg
import pytest

from gateway import completion_notice_flusher
from shared.completion_notices import CompletionNotice, record_hourly_notice
from shared.db import create_agent


def _agent(db_conn: psycopg.Connection) -> int:
    agent_id = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'idling')",
            (agent_id,),
        )
    db_conn.commit()
    return agent_id


def test_flush_once_delivers_one_digest_and_marks_the_authoritative_events(
    db_conn: psycopg.Connection,
) -> None:
    import shared.db

    agent_id = _agent(db_conn)
    record_hourly_notice(
        db_conn,
        agent_id,
        CompletionNotice(
            source="shell:70",
            content="Background command 'ok' exited with code 0. Full output at ok.log.",
            outcome="exit",
            exit_code=0,
        ),
    )
    record_hourly_notice(
        db_conn,
        agent_id,
        CompletionNotice(
            source="shell:71",
            content="Background command 'failed' exited with code 1. Full output at bad.log.",
            outcome="exit",
            exit_code=1,
        ),
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE completion_notice_events SET created_at = %s",
            (datetime(2026, 9, 22, 10, tzinfo=UTC),),
        )
    db_conn.commit()

    pool = shared.db.pool(max_size=2)
    try:
        assert (
            asyncio.run(
                completion_notice_flusher.flush_once(
                    pool, now=datetime(2026, 9, 22, 12, tzinfo=UTC)
                )
            )
            == 1
        )
    finally:
        pool.close()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(digest_inbound_id) FROM completion_notice_events "
            "WHERE agent_id = %s",
            (agent_id,),
        )
        assert cur.fetchone() == (2, 2)
        cur.execute(
            "SELECT content, source FROM inbound_messages WHERE agent_id = %s",
            (agent_id,),
        )
        digest = cur.fetchone()
    assert digest is not None
    assert digest[1] == "system:completion-digest"
    assert "2 completion notices" in digest[0]
    assert "Recent failures" in digest[0]

    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE completion_notice_events SET created_at = %s WHERE agent_id = %s",
            (datetime(2026, 9, 14, 10, tzinfo=UTC), agent_id),
        )
    db_conn.commit()
    pool = shared.db.pool(max_size=2)
    try:
        assert (
            asyncio.run(
                completion_notice_flusher.flush_once(
                    pool, now=datetime(2026, 9, 22, 12, tzinfo=UTC)
                )
            )
            == 0
        )
    finally:
        pool.close()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM completion_notice_events WHERE agent_id = %s", (agent_id,)
        )
        assert cur.fetchone() == (0,)


def test_flush_once_continues_after_one_digest_delivery_failure(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unavailable digest does not prevent another agent's completed hour."""
    import shared.db

    blocked_agent = _agent(db_conn)
    delivered_agent = _agent(db_conn)
    for agent_id in (blocked_agent, delivered_agent):
        record_hourly_notice(
            db_conn,
            agent_id,
            CompletionNotice(
                source=f"shell:{agent_id}",
                content="Background command 'ok' exited with code 0. Full output at ok.log.",
                outcome="exit",
                exit_code=0,
            ),
        )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE completion_notice_events SET created_at = %s",
            (datetime(2026, 9, 22, 10, tzinfo=UTC),),
        )
    db_conn.commit()

    real_deliver = completion_notice_flusher.deliver_chat_inbound

    async def fail_one(pool: object, agent_id: int, **kwargs: object) -> object:
        if agent_id == blocked_agent:
            raise RuntimeError("poison digest")
        return await real_deliver(pool, agent_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(completion_notice_flusher, "deliver_chat_inbound", fail_one)
    pool = shared.db.pool(max_size=2)
    try:
        delivered = asyncio.run(
            completion_notice_flusher.flush_once(pool, now=datetime(2026, 9, 22, 12, tzinfo=UTC))
        )
    finally:
        pool.close()

    assert delivered == 1
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT agent_id, digest_inbound_id IS NOT NULL FROM completion_notice_events "
            "ORDER BY agent_id"
        )
        assert cur.fetchall() == [(blocked_agent, False), (delivered_agent, True)]
