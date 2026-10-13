"""A failed corpse stamp keeps its cause — `agent/ownership/hosted.py:settle_and_stamp_turn`.

The grace-window reaper itself (`agent/ownership/corpse_reap.py`) is locked in
`agent/ownership/tests/test_hosted_ownership.py`.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership import hosted
from agent.ownership.hosted import (
    admit_hosted_runtime,
    settle_and_stamp_turn,
)
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus


def _agent(conn: psycopg.Connection[Any], machine: str = "host-test") -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', %s) "
        "ON CONFLICT (id) DO UPDATE SET status = 'idling', machine = %s",
        (agent_id, machine, machine),
    )
    conn.commit()
    return agent_id


async def test_corpse_stamp_failure_keeps_its_traceback(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    loguru_records: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
) -> None:
    """A failed corpse stamp is best-effort — but its log keeps the cause (task #4979)."""

    async def _broken(_pool: object, _incarnation: object) -> None:
        raise RuntimeError("stamp store down")

    monkeypatch.setattr(hosted, "stamp_turn_fatal", _broken)

    agent_id, owner = _agent(db_conn), uuid4()
    incarnation = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        "host-test",
        owner,
        expected_from="idling",
        db=Database.from_settings(gate=database_gate),
    )
    assert incarnation is not None

    settlement = await settle_and_stamp_turn(
        aops_pool,
        incarnation,
        bus=EventBus.from_settings(),
        exited=False,
        crashed=True,
        resources=None,
    )

    assert settlement.stamp is None
    record = next(r for r in loguru_records if r["extra"].get("event") == "corpse_stamp_failed")
    assert record["exception"] is not None
    assert record["exception"].type is RuntimeError
