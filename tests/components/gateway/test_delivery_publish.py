"""`deliver_chat_inbound`: a redis outage must neither roll back the user's write
nor escape as an exception, and the badge-refresh event must be published only
AFTER the delivery transaction commits (commit-before-publish).
"""

from __future__ import annotations

import asyncio
from typing import Literal

import psycopg
import pytest
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool
from redis.exceptions import ConnectionError as RedisConnectionError

from base.agents import AgentStatus
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.fakes import patch_async_redis, patch_sync_redis
from ops.agents.delivery import deliver_chat_inbound
from tests.fixtures.unit.identity import machine_identity as machine_identity


def _sync_pool() -> ConnectionPool[psycopg.Connection[TupleRow]]:
    """A sync ConnectionPool typed to match `deliver_chat_inbound`'s param; opened
    by the caller's `with` block (mirrors conftest's `aops_pool`, open=False)."""
    return ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=False)


class _BoomSyncClient:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def publish(self, _channel: str, _payload: str, *, auth_retry: bool = True) -> int:
        raise self._exc

    def close(self) -> None:
        pass


class _BoomAsyncClient:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def publish(self, _channel: str, _payload: str, *, auth_retry: bool = True) -> int:
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
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """redis down (both the sync badge publish and the async InboundArrived) must
    not stop the inbound INSERT from committing, and nothing may escape."""
    tid = _seed_idling_agent(db_conn)

    patch_sync_redis(monkeypatch, lambda: _BoomSyncClient(RedisConnectionError("down")))
    patch_async_redis(monkeypatch, lambda: _BoomAsyncClient(RedisConnectionError("down")))

    with _sync_pool() as pool:
        # Must NOT raise despite every publish on the path throwing.
        await deliver_chat_inbound(
            pool,
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            tid,
            prepare=lambda _c: "hello there",
            refresh_badge=True,
            local_machine=machine_identity.name(),
        )

    # The user's inbound is durably committed.
    with db_conn.cursor() as cur:
        cur.execute("SELECT content FROM inbound_messages WHERE agent_id = %s", (tid,))
        rows = cur.fetchall()
    assert rows == [("hello there",)]


async def test_unknown_badge_error_exposes_committed_delivery(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> None:
    tid = _seed_idling_agent(db_conn)

    def _boom_badge(_bus: object, _agent_id: int) -> None:
        raise RuntimeError("lifecycle hint failed")

    monkeypatch.setattr("ops.agents.delivery.publish_agent_updated_sync", _boom_badge)
    with _sync_pool() as pool, pytest.raises(RuntimeError, match="lifecycle hint failed"):
        await deliver_chat_inbound(
            pool,
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            tid,
            prepare=lambda _c: "hi",
            refresh_badge=True,
            client_message_id="badge-committed",
            local_machine=machine_identity.name(),
        )
    with db_conn.cursor() as cur:
        cur.execute("SELECT content FROM inbound_messages WHERE agent_id = %s", (tid,))
        assert cur.fetchall() == [("hi",)]


async def test_unknown_live_publish_error_is_awaited_after_commit(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> None:
    tid = _seed_idling_agent(db_conn)
    patch_async_redis(monkeypatch, lambda: _BoomAsyncClient(AttributeError("live bug")))
    with _sync_pool() as pool, pytest.raises(RuntimeError, match="live bug"):
        await deliver_chat_inbound(
            pool,
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            tid,
            prepare=lambda _c: "live committed",
            client_message_id="live-committed",
            local_machine=machine_identity.name(),
        )
    with db_conn.cursor() as cur:
        cur.execute("SELECT content FROM inbound_messages WHERE agent_id = %s", (tid,))
        assert cur.fetchall() == [("live committed",)]


async def test_deliver_passes_inserted_chat_as_auto_resurrect_guard(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """The durable chat id is the evidence the home runner re-checks before
    reviving; delivery must not fall back to an unguarded resurrect."""
    from ops.agents import delivery

    tid = _seed_idling_agent(db_conn)
    calls: list[tuple[int, int | None, str | None]] = []

    async def _resurrect(
        _db: object,
        _bus: EventBus,
        agent_id: int,
        *,
        trigger_inbound_id: int | None = None,
        trigger_inbound_kind: str | None = None,
        local_machine: str,
    ) -> AgentStatus:
        assert local_machine == machine_identity.name()
        calls.append((agent_id, trigger_inbound_id, trigger_inbound_kind))
        return AgentStatus.IDLING

    monkeypatch.setattr(delivery._ops, "resurrect_if_terminated", _resurrect)

    with _sync_pool() as pool:
        delivery = await deliver_chat_inbound(
            pool,
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            tid,
            prepare=lambda _c: "guard me",
            local_machine=machine_identity.name(),
        )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM inbound_messages WHERE agent_id = %s AND content = 'guard me'",
            (tid,),
        )
        row = cur.fetchone()
    assert row is not None
    assert delivery.status is AgentStatus.IDLING
    assert calls == [(tid, row[0], "chat")]


async def test_retried_client_message_resurrects_terminated_agent_once(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """A same-key retry reuses one chat and cannot create two resurrection effects."""
    from ops.agents import delivery

    tid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, status) VALUES (%s, 'terminated') "
            "ON CONFLICT (id) DO UPDATE SET status = 'terminated'",
            (tid,),
        )
    db_conn.commit()

    async def _resurrect_once(
        _db: object,
        _bus: EventBus,
        agent_id: int,
        *,
        trigger_inbound_id: int,
        trigger_inbound_kind: Literal["chat", "compact_request"],
        local_machine: str,
    ) -> AgentStatus:
        assert local_machine == machine_identity.name()
        assert trigger_inbound_kind == "chat"
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s FOR UPDATE", (agent_id,))
            row = cur.fetchone()
            assert row is not None
            if row[0] == "terminated":
                cur.execute(
                    "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                    "VALUES (%s, '', 'resurrect', 'system')",
                    (agent_id,),
                )
                cur.execute(
                    "UPDATE agents_meta SET status = 'idling' WHERE id = %s",
                    (agent_id,),
                )
        db_conn.commit()
        return AgentStatus.IDLING

    monkeypatch.setattr(delivery._ops, "resurrect_if_terminated", _resurrect_once)

    with _sync_pool() as pool:
        first = await deliver_chat_inbound(
            pool,
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            tid,
            prepare=lambda _c: "wake once",
            client_message_id="client-wake-once",
            local_machine=machine_identity.name(),
        )
        second = await deliver_chat_inbound(
            pool,
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            tid,
            prepare=lambda _c: "wake once",
            client_message_id="client-wake-once",
            local_machine=machine_identity.name(),
        )

    assert first.inbound_id == second.inbound_id
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind, count(*) FROM inbound_messages WHERE agent_id = %s "
            "GROUP BY kind ORDER BY kind",
            (tid,),
        )
        assert cur.fetchall() == [("chat", 1), ("resurrect", 1)]


async def test_fresh_peer_message_wakes_owner_despite_previous_suppression(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """A new explicit peer message retries after a previous wake failure."""
    from ops.cluster import rpc as cluster_rpc

    tid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta "
            "(id, status, machine, wake_suppressed_until, wake_suppress_reason) "
            "VALUES (%s, 'terminated', 'peer-home', now() + interval '1 hour', "
            "'resurrect_failed')",
            (tid,),
        )
    db_conn.commit()
    forwards: list[str] = []

    async def _record_forward(*_args: object, **_kwargs: object) -> dict[str, str]:
        forwards.append("forwarded")
        return {"status": "already_alive"}

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _record_forward)

    with _sync_pool() as pool:
        delivery = await deliver_chat_inbound(
            pool,
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            tid,
            prepare=lambda _conn: "peer work",
            source="agent:42",
            local_machine=machine_identity.name(),
        )

    assert delivery.status is AgentStatus.TERMINATED
    assert delivery.inbound_id is not None
    assert forwards == ["forwarded"]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status,kind,source,content FROM inbound_messages WHERE id=%s",
            (delivery.inbound_id,),
        )
        assert cur.fetchone() == ("pending", "chat", "agent:42", "peer work")
        cur.execute(
            "SELECT target_agent_id, attributes->>'inbound_id' FROM audit_events "
            "WHERE agent_id=%s AND event_name='send_message'",
            (tid,),
        )
        assert cur.fetchall() == [(42, str(delivery.inbound_id))]


async def test_concurrent_same_key_terminated_delivery_has_one_resurrect_effect(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """Two requests that both observe terminated create at most one wake effect.

    The exact-trigger/fence SQL itself has real-DB coverage in
    test_agents_internals; this test concentrates on the delivery fan-in.
    """
    from ops.agents import delivery

    tid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (tid,))
    db_conn.commit()
    both_observed_terminated = asyncio.Event()
    effect_lock = asyncio.Lock()
    arrivals = 0
    observed_status = AgentStatus.TERMINATED

    async def _real_guard(
        _db: object,
        _bus: EventBus,
        agent_id: int,
        *,
        trigger_inbound_id: int,
        trigger_inbound_kind: Literal["chat", "compact_request"],
        local_machine: str,
    ) -> AgentStatus:
        assert local_machine == machine_identity.name()
        nonlocal arrivals, observed_status
        assert observed_status is AgentStatus.TERMINATED
        arrivals += 1
        if arrivals == 2:
            both_observed_terminated.set()
        await both_observed_terminated.wait()
        async with effect_lock:
            if observed_status is AgentStatus.TERMINATED:
                assert trigger_inbound_kind == "chat"
                with db_conn.cursor() as cur:
                    cur.execute(
                        "SELECT status, kind FROM inbound_messages WHERE id = %s",
                        (trigger_inbound_id,),
                    )
                    assert cur.fetchone() == ("pending", "chat")
                    cur.execute(
                        "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                        "VALUES (%s, '', 'resurrect', 'system')",
                        (agent_id,),
                    )
                    cur.execute(
                        "UPDATE agents_meta SET status = 'idling' WHERE id = %s",
                        (agent_id,),
                    )
                db_conn.commit()
                observed_status = AgentStatus.IDLING
        return observed_status

    monkeypatch.setattr(delivery._ops, "resurrect_if_terminated", _real_guard)
    with _sync_pool() as pool:
        first, second = await asyncio.gather(
            deliver_chat_inbound(
                pool,
                Database.from_settings(gate=database_gate),
                EventBus.from_settings(),
                tid,
                prepare=lambda _c: "concurrent wake",
                client_message_id="client-concurrent-wake",
                local_machine=machine_identity.name(),
            ),
            deliver_chat_inbound(
                pool,
                Database.from_settings(gate=database_gate),
                EventBus.from_settings(),
                tid,
                prepare=lambda _c: "concurrent wake",
                client_message_id="client-concurrent-wake",
                local_machine=machine_identity.name(),
            ),
        )

    assert first.inbound_id == second.inbound_id
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind, count(*) FROM inbound_messages WHERE agent_id = %s "
            "GROUP BY kind ORDER BY kind",
            (tid,),
        )
        assert cur.fetchall() == [("chat", 1), ("resurrect", 1)]


async def test_badge_publish_happens_after_commit(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """The refresh_badge publish must run only AFTER the delivery transaction
    commits: a separate connection must already see `prepare`'s write at the
    moment the publish fires. (Old code published inside the transaction, so a
    fresh connection could not yet see the uncommitted write — and a publish
    failure there rolled the write back.)"""
    tid = _seed_idling_agent(db_conn)

    observed: dict[str, bool] = {}

    def _spy_publish(_bus: object, agent_id: int) -> None:
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

    monkeypatch.setattr("ops.agents.delivery.publish_agent_updated_sync", _spy_publish)

    def _prepare(conn: psycopg.Connection) -> None:
        # A write inside the delivery txn, no inbound content (so the txn commits
        # only at block exit — the case where in-txn publish would roll it back).
        with conn.cursor() as cur:
            cur.execute("UPDATE agents SET label = 'ordering-marker' WHERE id = %s", (tid,))

    with _sync_pool() as pool:
        await deliver_chat_inbound(
            pool,
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            tid,
            prepare=_prepare,
            refresh_badge=True,
            local_machine=machine_identity.name(),
        )

    assert observed.get("committed") is True, (
        "badge publish saw uncommitted state — it ran before the delivery commit"
    )
    # And the write itself is durable.
    with db_conn.cursor() as cur:
        cur.execute("SELECT label FROM agents WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "ordering-marker"


async def test_cancelled_caller_stops_live_publish_and_reconciles_same_receipt(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from ops.agents import delivery

    tid = _seed_idling_agent(db_conn)
    entered = asyncio.Event()
    exited = asyncio.Event()

    async def hold_publish(*_args: object, **_kwargs: object) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    with _sync_pool() as pool:
        with monkeypatch.context() as patch:
            patch.setattr(delivery._ops, "publish_inbound_arrived", hold_publish)
            caller = asyncio.create_task(
                deliver_chat_inbound(
                    pool,
                    Database.from_settings(gate=database_gate),
                    EventBus.from_settings(),
                    tid,
                    prepare=lambda _c: "cancel after commit",
                    client_message_id="cancel-committed",
                    local_machine=machine_identity.name(),
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM inbound_messages WHERE agent_id=%s AND content=%s",
                    (tid, "cancel after commit"),
                )
                row = cur.fetchone()
                assert row is not None
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert exited.is_set()
        recovered = await delivery.reconcile_chat_delivery(
            pool,
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            tid,
            local_machine=machine_identity.name(),
            client_message_id="cancel-committed",
            content="cancel after commit",
            source="user",
            payload=None,
        )
    assert recovered is not None and recovered.inbound_id == row[0]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (tid,))
        assert cur.fetchone() == (1,)
