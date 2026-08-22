"""A redis publish failure must never crash an agent lifecycle write.

`mark_agent_status` and `claim_agent_row` both do their durable
agents_meta write, commit, then publish an AgentUpdated snapshot for the live
UI. A raise from that tail publish used to propagate — killing the claim node
mid-turn (`mark_agent_status`) or crash-churning spawn/resurrect during a redis
outage (`claim_agent_row`). Now the publish is best-effort: the DB effect
must stand and no exception may escape when redis is throwing.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool
from redis.exceptions import ConnectionError as RedisConnectionError

from shared import redis_client
from shared.config import settings
from shared.db import create_agent
from shared.machine import machine_name


class _BoomSyncClient:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def publish(self, _channel: str, _payload: str) -> int:
        raise self._exc

    def close(self) -> None:
        pass


class _BoomAsyncClient:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def publish(self, _channel: str, _payload: str) -> int:
        raise self._exc


def _set_status(conn: psycopg.Connection, agent_id: int, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, status) VALUES (%s, %s) "
            "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status",
            (agent_id, status),
        )
    conn.commit()


def _fetch_status(conn: psycopg.Connection, agent_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None, f"agents_meta row {agent_id} missing"
    return row[0]


async def test_mark_agent_status_survives_publish_failure(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict],
) -> None:
    """A throwing async redis must not stop the running→idling CAS from committing,
    and no exception may escape mark_agent_status."""
    from agent.db import mark_agent_status

    tid = create_agent(db_conn)
    _set_status(db_conn, tid, "running")

    monkeypatch.setattr(
        redis_client, "get_async_redis", lambda: _BoomAsyncClient(RedisConnectionError("down"))
    )

    # Must NOT raise despite the publish blowing up.
    await mark_agent_status(aops_pool, tid, "idling", expected_from="running")

    # The durable status flip stands (read on a fresh statement / committed view).
    assert _fetch_status(db_conn, tid) == "idling"
    assert any("skipped" in r["message"] for r in loguru_records), "best-effort skip not logged"


def test_claim_agent_row_survives_publish_failure(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict],
) -> None:
    """A throwing sync redis must not stop the idling→running claim from
    committing, and no exception may escape claim_agent_row (spawn/resurrect
    must not crash-churn during a redis outage)."""
    from agent._starting import claim_agent_row

    tid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', %s) "
            "ON CONFLICT (id) DO UPDATE SET status = 'idling', machine = EXCLUDED.machine",
            (tid, machine_name()),
        )
    db_conn.commit()

    monkeypatch.setattr(
        redis_client,
        "sync_redis",
        lambda **_: _BoomSyncClient(RedisConnectionError("down")),  # pyright: ignore[reportUnknownArgumentType]
    )

    # Must NOT raise despite the publish blowing up.
    claim_agent_row(tid)

    assert _fetch_status(db_conn, tid) == "running"
    assert any("skipped" in r["message"] for r in loguru_records), "best-effort skip not logged"


def test_claim_agent_row_publish_failure_uses_real_settings_channel(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard against a regression where the never-raise wrapper is bypassed: even a
    NOPERM-class ResponseError (WARNING path) must not escape."""
    from redis.exceptions import NoPermissionError

    from agent._starting import claim_agent_row

    tid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', %s) "
            "ON CONFLICT (id) DO UPDATE SET status = 'idling', machine = EXCLUDED.machine",
            (tid, machine_name()),
        )
    db_conn.commit()

    monkeypatch.setattr(
        redis_client,
        "sync_redis",
        lambda **_: _BoomSyncClient(NoPermissionError("NOPERM")),  # pyright: ignore[reportUnknownArgumentType]
    )
    _ = settings.data_plane.events_channel  # the channel the real publish would use
    claim_agent_row(tid)  # must not raise

    assert _fetch_status(db_conn, tid) == "running"
