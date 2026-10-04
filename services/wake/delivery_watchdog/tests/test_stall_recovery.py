"""The stalled crash-marked harvest loop (task #3618): one request per owner,
a persisted 60s cooldown, the knob, and the `delivery_recovery_decision` event."""

from __future__ import annotations

import asyncio

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import ops.lifecycle as ol
from base.config import settings
from base.daemon.loop_health import LoopProgress
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from services.wake.delivery_watchdog import attempts, rounds, stall_recovery

_THRESHOLD_S = 30.0


@pytest.fixture
def pool():
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=4, open=True)
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def progress() -> LoopProgress:
    return LoopProgress("harvest", rounds.loop_liveness_timeout_s())


def _crash_marked_agent_with_stalled_chats(
    db: psycopg.Connection, *, chats: int = 1
) -> tuple[int, int]:
    """An idling corpse (`last_turn_fatal_at` set) holding `chats` stalled chats;
    returns the agent id and its OLDEST stalled inbound id."""
    from tests.fixtures.units import spawn_agent

    aid = spawn_agent(spawner="user")
    db.execute(
        "UPDATE agents_meta SET status = 'idling', last_turn_fatal_at = now() WHERE id = %s",
        (aid,),
    )
    inbound_ids: list[int] = []
    for index in range(chats):
        iid = insert_inbound_message(
            db,
            aid,
            "stale",
            source="user",
            bus=EventBus.from_settings(),
            database=Database.from_settings(),
        )
        db.execute(
            "UPDATE inbound_messages SET created_at = now() - make_interval(secs => %s) "
            "WHERE id = %s",
            (_THRESHOLD_S + 100.0 - index, iid),
        )
        inbound_ids.append(iid)
    db.commit()
    return aid, inbound_ids[0]


def _ignore_emit(*_args: object, **_kwargs: object) -> None:
    return None


def _expire_cooldown(db: psycopg.Connection, aid: int) -> None:
    db.execute(
        "UPDATE delivery_watchdog_attempts SET last_attempt_at = now() - interval '1 hour' "
        "WHERE kind = 'harvest' AND agent_id = %s",
        (aid,),
    )
    db.commit()


def _stub_requester(
    monkeypatch: pytest.MonkeyPatch, decision: tuple[str, str | None]
) -> list[tuple[int, int]]:
    calls: list[tuple[int, int]] = []

    async def requester(
        _db: object, _bus: EventBus, agent_id: int, *, stalled_inbound_id: int
    ) -> tuple[str, str | None]:
        calls.append((agent_id, stalled_inbound_id))
        return decision

    monkeypatch.setattr(ol, "recover_crash_marked_if_stalled", requester)
    return calls


async def test_request_runs_once_and_emits_the_decision(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    progress: LoopProgress,
    monkeypatch: pytest.MonkeyPatch,
    event_bus: EventBus,
) -> None:
    zombie, inbound_id = _crash_marked_agent_with_stalled_chats(db_conn)
    calls = _stub_requester(monkeypatch, ("harvested", None))
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record_emit(*args: object, **kwargs: object) -> None:
        emitted.append((args, kwargs))

    monkeypatch.setattr(stall_recovery.telemetry, "emit", record_emit)

    await stall_recovery.stall_recovery_round(
        pool, Database.from_settings(), event_bus, progress, _THRESHOLD_S
    )

    assert calls == [(zombie, inbound_id)]
    assert emitted == [
        (
            ("telemetry", "delivery_recovery_decision"),
            {
                "agent_id": zombie,
                "source": "system",
                "attributes": {"inbound_id": inbound_id, "decision": "harvested", "reason": None},
            },
        )
    ]


async def test_one_request_per_owner_for_the_oldest_stalled_chat(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    progress: LoopProgress,
    monkeypatch: pytest.MonkeyPatch,
    event_bus: EventBus,
) -> None:
    zombie, oldest = _crash_marked_agent_with_stalled_chats(db_conn, chats=3)
    calls = _stub_requester(monkeypatch, ("refused", "not_settled:running"))
    monkeypatch.setattr(stall_recovery.telemetry, "emit", _ignore_emit)

    await stall_recovery.stall_recovery_round(
        pool, Database.from_settings(), event_bus, progress, _THRESHOLD_S
    )

    assert calls == [(zombie, oldest)]


async def test_cooldown_lives_in_the_database_and_suppresses_repeat_requests(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    progress: LoopProgress,
    monkeypatch: pytest.MonkeyPatch,
    event_bus: EventBus,
) -> None:
    zombie, _ = _crash_marked_agent_with_stalled_chats(db_conn)
    calls = _stub_requester(monkeypatch, ("refused", "not_settled:running"))
    monkeypatch.setattr(stall_recovery.telemetry, "emit", _ignore_emit)

    await stall_recovery.stall_recovery_round(
        pool, Database.from_settings(), event_bus, progress, _THRESHOLD_S
    )
    await stall_recovery.stall_recovery_round(
        pool, Database.from_settings(), event_bus, progress, _THRESHOLD_S
    )
    assert len(calls) == 1

    _expire_cooldown(db_conn, zombie)
    await stall_recovery.stall_recovery_round(
        pool, Database.from_settings(), event_bus, progress, _THRESHOLD_S
    )
    assert len(calls) == 2


async def test_disabled_knob_skips_the_round(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    progress: LoopProgress,
    monkeypatch: pytest.MonkeyPatch,
    event_bus: EventBus,
) -> None:
    _crash_marked_agent_with_stalled_chats(db_conn)
    calls = _stub_requester(monkeypatch, ("harvested", None))
    monkeypatch.setattr(settings.daemon, "delivery_stalled_recovery_enabled", False)

    await stall_recovery.stall_recovery_round(
        pool, Database.from_settings(), event_bus, progress, _THRESHOLD_S
    )

    assert calls == []


async def test_hung_request_is_cut_at_the_deadline_and_reported_as_an_error(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    progress: LoopProgress,
    monkeypatch: pytest.MonkeyPatch,
    event_bus: EventBus,
) -> None:
    zombie, inbound_id = _crash_marked_agent_with_stalled_chats(db_conn)
    emitted: list[dict[str, object]] = []

    async def hang(
        _db: object, _bus: EventBus, agent_id: int, *, stalled_inbound_id: int
    ) -> tuple[str, str | None]:
        await asyncio.Event().wait()
        return "harvested", None

    monkeypatch.setattr(ol, "recover_crash_marked_if_stalled", hang)

    def record_emit(*_args: object, **kwargs: object) -> None:
        emitted.append(kwargs)

    monkeypatch.setattr(stall_recovery.telemetry, "emit", record_emit)
    monkeypatch.setattr(rounds, "rpc_deadline_s", lambda: 0.05)

    await stall_recovery.stall_recovery_round(
        pool, Database.from_settings(), event_bus, progress, _THRESHOLD_S
    )

    assert emitted[0]["attributes"] == {
        "inbound_id": inbound_id,
        "decision": "error",
        "reason": "harvest request failed",
    }
    claimed, _ = attempts.claim_attempts(pool, attempts.HARVEST, [zombie], 60.0)
    assert claimed == []
