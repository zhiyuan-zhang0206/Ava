"""Original source acceptance replays independently of mutable work and queue state."""

import asyncio
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.db import claim_inbound_batch
from base.agents.compaction.commands import accept, observe
from base.agents.compaction.models import CompactConflictError, CompactHeldError
from base.agents.compaction.tests.helpers import source


async def test_fresh_concurrent_acceptance_and_changed_body(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    _, target, *_ = await source(db_conn, aops_pool)
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        receipts = await asyncio.gather(
            *[
                asyncio.to_thread(
                    accept, pool, str(target.observation_id), target.source.agent_id, target
                )
                for _ in range(4)
            ]
        )
        assert all(receipt == receipts[0] for receipt in receipts)
        with pytest.raises(CompactConflictError, match="different source"):
            await asyncio.to_thread(
                accept,
                pool,
                str(target.observation_id),
                target.source.agent_id,
                target.model_copy(update={"model": "claude-sonnet-5"}),
            )
        db_conn.execute("DELETE FROM native_graph_work WHERE id=%s", (target.source.work_id,))
        db_conn.execute(
            "UPDATE agents_meta SET runtime_owner=%s,status='terminated' WHERE id=%s",
            (uuid4(), target.source.agent_id),
        )
        db_conn.commit()
        assert (
            await asyncio.to_thread(
                accept, pool, str(target.observation_id), target.source.agent_id, target
            )
            == receipts[0]
        )
    assert db_conn.execute(
        "SELECT count(*) FROM native_compact_commands WHERE agent_id=%s", (target.source.agent_id,)
    ).fetchone() == (1,)


async def test_pending_chat_is_not_a_quiescent_source(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    _, target, *_ = await source(db_conn, aops_pool)
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,source) VALUES(%s,'later','user')",
        (target.source.agent_id,),
    )
    db_conn.commit()
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        with pytest.raises(CompactConflictError, match="no eligible"):
            observe(pool, target.source.agent_id)
        with pytest.raises(CompactConflictError, match="no longer eligible"):
            accept(pool, str(target.observation_id), target.source.agent_id, target)
    assert db_conn.execute(
        "SELECT count(*) FROM native_compact_commands WHERE agent_id=%s", (target.source.agent_id,)
    ).fetchone() == (0,)


async def test_pending_compact_fences_generic_claim_without_consuming_chat(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    incarnation, target, *_ = await source(db_conn, aops_pool)
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        accept(pool, str(target.observation_id), target.source.agent_id, target)
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,source) VALUES(%s,'later','user')",
        (target.source.agent_id,),
    )
    db_conn.commit()
    with pytest.raises(CompactHeldError, match="before generic"):
        await claim_inbound_batch(
            aops_pool, target.source.agent_id, incarnation=incarnation, work=None
        )
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s", (target.source.agent_id,)
    ).fetchall() == [("pending",)]


async def test_acceptance_audit_failure_rolls_back_pointer_and_replay_audits_once(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.agents.compaction import commands

    _, target, *_ = await source(db_conn, aops_pool)
    original = commands.record_audit

    def fail_after_audit(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise RuntimeError("lost producer after actual audit insert")

    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        with monkeypatch.context() as patch:
            patch.setattr(commands, "record_audit", fail_after_audit)
            with pytest.raises(RuntimeError, match="actual audit"):
                accept(pool, str(target.observation_id), target.source.agent_id, target)
        assert db_conn.execute(
            "SELECT count(*) FROM native_compact_commands WHERE agent_id=%s",
            (target.source.agent_id,),
        ).fetchone() == (0,)
        assert db_conn.execute(
            "SELECT count(*) FROM audit_events WHERE agent_id=%s AND event_name='compact'",
            (target.source.agent_id,),
        ).fetchone() == (0,)
        first = accept(pool, str(target.observation_id), target.source.agent_id, target)
        assert accept(pool, str(target.observation_id), target.source.agent_id, target) == first
    assert db_conn.execute(
        "SELECT count(*) FROM audit_events WHERE agent_id=%s AND event_name='compact'",
        (target.source.agent_id,),
    ).fetchone() == (1,)
