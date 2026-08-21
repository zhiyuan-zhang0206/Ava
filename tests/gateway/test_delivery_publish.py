"""`deliver_chat_inbound`: a redis outage must neither roll back the user's write
nor escape as an exception, and the badge-refresh event must be published only
AFTER the delivery transaction commits (commit-before-publish).
"""

from __future__ import annotations

import asyncio

import psycopg
import pytest
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool
from redis.exceptions import ConnectionError as RedisConnectionError

from gateway.routers._delivery import deliver_chat_inbound
from shared import redis_client
from shared.agents import AgentStatus
from shared.config import settings
from shared.db import create_agent


def _sync_pool() -> ConnectionPool[psycopg.Connection[TupleRow]]:
    """A sync ConnectionPool typed to match `deliver_chat_inbound`'s param; opened
    by the caller's `with` block (mirrors conftest's `aops_pool`, open=False)."""
    return ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=False)


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


def _seed_idling_agent(conn: psycopg.Connection) -> int:
    tid = create_agent(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, status) VALUES (%s, 'idling') "
            "ON CONFLICT (id) DO UPDATE SET status = 'idling'",
            (tid,),
        )
    conn.commit()
    return tid


async def test_deliver_survives_publish_failure(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """redis down (both the sync badge publish and the async InboundArrived) must
    not stop the inbound INSERT from committing, and nothing may escape."""
    tid = _seed_idling_agent(db_conn)

    monkeypatch.setattr(
        redis_client,
        "sync_redis",
        lambda **_: _BoomSyncClient(RedisConnectionError("down")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    monkeypatch.setattr(
        redis_client, "get_async_redis", lambda: _BoomAsyncClient(RedisConnectionError("down"))
    )

    with _sync_pool() as pool:
        # Must NOT raise despite every publish on the path throwing.
        await deliver_chat_inbound(pool, tid, prepare=lambda _c: "hello there", refresh_badge=True)
        await asyncio.sleep(0.05)  # let the fire-and-forget InboundArrived task settle

    # The user's inbound is durably committed.
    with db_conn.cursor() as cur:
        cur.execute("SELECT content FROM inbound_messages WHERE agent_id = %s", (tid,))
        rows = cur.fetchall()
    assert rows == [("hello there",)]


async def test_deliver_degrades_when_badge_step_raises(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole post-commit badge step is off the delivery's critical path: even
    if it raises for a NON-publish reason (e.g. the fresh-connection snapshot READ
    fails), the delivery must not 500 — the inbound stays committed and the
    InboundArrived + resurrect tail still runs (proven by the returned status)."""
    tid = _seed_idling_agent(db_conn)

    def _boom_badge(_conn: psycopg.Connection, _agent_id: int) -> None:
        raise RuntimeError("snapshot read failed")  # simulate the read, not the publish

    monkeypatch.setattr("gateway.routers._delivery.publish_agent_updated_sync", _boom_badge)

    with _sync_pool() as pool:
        status = await deliver_chat_inbound(pool, tid, prepare=lambda _c: "hi", refresh_badge=True)
        await asyncio.sleep(0.05)

    # Delivery-time status returned (resurrect tail ran) — not a 500.
    assert status == AgentStatus.IDLING
    with db_conn.cursor() as cur:
        cur.execute("SELECT content FROM inbound_messages WHERE agent_id = %s", (tid,))
        assert cur.fetchall() == [("hi",)]


async def test_deliver_passes_inserted_chat_as_auto_resurrect_guard(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The durable chat id is the evidence the home runner re-checks before
    reviving; delivery must not fall back to an unguarded resurrect."""
    import gateway.routers._delivery as delivery

    tid = _seed_idling_agent(db_conn)
    calls: list[tuple[int, int | None, str | None]] = []

    async def _resurrect(
        agent_id: int,
        *,
        trigger_inbound_id: int | None = None,
        trigger_inbound_kind: str | None = None,
    ) -> AgentStatus:
        calls.append((agent_id, trigger_inbound_id, trigger_inbound_kind))
        return AgentStatus.IDLING

    monkeypatch.setattr(delivery._ops, "resurrect_if_terminated", _resurrect)

    with _sync_pool() as pool:
        status = await deliver_chat_inbound(pool, tid, prepare=lambda _c: "guard me")

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM inbound_messages WHERE agent_id = %s AND content = 'guard me'",
            (tid,),
        )
        row = cur.fetchone()
    assert row is not None
    assert status is AgentStatus.IDLING
    assert calls == [(tid, row[0], "chat")]


async def test_badge_publish_happens_after_commit(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refresh_badge publish must run only AFTER the delivery transaction
    commits: a separate connection must already see `prepare`'s write at the
    moment the publish fires. (Old code published inside the transaction, so a
    fresh connection could not yet see the uncommitted write — and a publish
    failure there rolled the write back.)"""
    tid = _seed_idling_agent(db_conn)

    observed: dict[str, bool] = {}

    def _spy_publish(_conn: psycopg.Connection, agent_id: int) -> None:
        # A DISTINCT connection: it sees the marker only if the delivery txn has
        # already committed by the time the badge publish is invoked.
        probe = psycopg.connect(settings.data_plane.db_url)
        try:
            with probe.cursor() as cur:
                cur.execute("SELECT label FROM agents WHERE id = %s", (agent_id,))
                row = cur.fetchone()
            observed["committed"] = row is not None and row[0] == "ordering-marker"
        finally:
            probe.close()

    monkeypatch.setattr("gateway.routers._delivery.publish_agent_updated_sync", _spy_publish)

    def _prepare(conn: psycopg.Connection) -> None:
        # A write inside the delivery txn, no inbound content (so the txn commits
        # only at block exit — the case where in-txn publish would roll it back).
        with conn.cursor() as cur:
            cur.execute("UPDATE agents SET label = 'ordering-marker' WHERE id = %s", (tid,))

    with _sync_pool() as pool:
        await deliver_chat_inbound(pool, tid, prepare=_prepare, refresh_badge=True)

    assert observed.get("committed") is True, (
        "badge publish saw uncommitted state — it ran before the delivery commit"
    )
    # And the write itself is durable.
    with db_conn.cursor() as cur:
        cur.execute("SELECT label FROM agents WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "ordering-marker"
