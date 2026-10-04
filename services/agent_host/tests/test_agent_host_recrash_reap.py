"""The recrash prompt reap at the settle boundary — `services/agent_host/settlement.py`.

A turn that dies again under its own corpse mark must be terminated at once,
with the corpse reaper's termination and events; a first death keeps its full
grace. Every gap — the gray switch off, a settle that never reached idling, a
row that moved on — skips the reap fail-closed and leaves the grace-window
reap as the backstop. The row-visible termination contract lives in
`tests/agent/test_corpse_reap.py`; this file locks the dispatch, the gates,
and the order (task #3616).
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from agent.ownership.corpse_reap import ReapedCorpse, reap_crash_corpses
from agent.ownership.hosted import (
    TurnFatalStamp,
    TurnSettlement,
    admit_hosted_runtime,
)
from base.config import settings
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.telemetry import Event
from services.agent_host import settlement as settlement_mod
from services.agent_host.runtime import TurnOutcome


def _settlement(*, crashed: bool, recrash: bool, settled: bool) -> TurnSettlement:
    return TurnSettlement(stamp=TurnFatalStamp(applied=crashed, recrash=recrash), settled=settled)


async def _close_captured(
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcome: TurnOutcome,
    settlement: TurnSettlement,
    order: list[str],
    reap_result: list[int] | None = None,
) -> None:
    """Run the real close_hosted_turn with each step swapped for a recorder."""

    async def settle_and_stamp(
        _pool: object, _incarnation: object, *, bus: object, exited: bool, crashed: bool
    ) -> TurnSettlement:
        del exited, crashed
        order.append("settle")
        return settlement

    async def reconcile(_pool: object, _checkpointer: object, _incarnation: object) -> None:
        order.append("reconcile")

    async def reap(_pool: object, _incarnation: object, *, bus: object) -> list[int]:
        order.append("reap")
        return reap_result if reap_result is not None else []

    monkeypatch.setattr(settlement_mod, "settle_and_stamp_turn", settle_and_stamp)
    monkeypatch.setattr(settlement_mod, "reconcile_inbounds_after_abort", reconcile)
    monkeypatch.setattr(settlement_mod, "reap_recrashed_corpse", reap)
    await settlement_mod.close_hosted_turn(
        cast(AsyncConnectionPool[Any], object()),
        cast(AsyncConnectionPool[Any], object()),
        Database.from_settings(),
        EventBus.from_settings(),
        cast(AsyncPostgresSaver, object()),
        RuntimeIncarnation(42, uuid4(), uuid4()),
        outcome,
    )


@pytest.fixture
def reap_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.daemon, "hosted_recrash_prompt_reap_enabled", True)


def _skips(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r["extra"].get("event") == "host_recrash_reap_skipped"]


async def test_aborted_recrash_reaps_after_settle_and_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    reap_enabled: None,
) -> None:
    """The reap runs last: the abort's reconcile needs the abort's own live
    incarnation, and the reap terminates it."""
    order: list[str] = []
    await _close_captured(
        monkeypatch,
        outcome=TurnOutcome(exited=False, crashed=True, aborted=True),
        settlement=_settlement(crashed=True, recrash=True, settled=True),
        order=order,
    )
    assert order == ["settle", "reconcile", "reap"]


async def test_unclassified_recrash_reaps_after_the_settle(
    monkeypatch: pytest.MonkeyPatch,
    reap_enabled: None,
) -> None:
    order: list[str] = []
    await _close_captured(
        monkeypatch,
        outcome=TurnOutcome(exited=False, crashed=True),
        settlement=_settlement(crashed=True, recrash=True, settled=True),
        order=order,
    )
    assert order == ["settle", "reap"]


async def test_first_crash_keeps_its_full_grace(
    monkeypatch: pytest.MonkeyPatch,
    reap_enabled: None,
) -> None:
    """The first death under a mark is never prompt-reaped: the grace window
    is the one self-heal chance the mechanism deliberately keeps."""
    order: list[str] = []
    await _close_captured(
        monkeypatch,
        outcome=TurnOutcome(exited=False, crashed=True, aborted=True),
        settlement=_settlement(crashed=True, recrash=False, settled=True),
        order=order,
    )
    assert order == ["settle", "reconcile"]


async def test_clean_turn_never_reaps(
    monkeypatch: pytest.MonkeyPatch,
    reap_enabled: None,
) -> None:
    order: list[str] = []
    await _close_captured(
        monkeypatch,
        outcome=TurnOutcome(exited=False, crashed=False),
        settlement=_settlement(crashed=False, recrash=False, settled=True),
        order=order,
    )
    assert order == ["settle"]


async def test_switch_off_skips_the_reap_logged(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(settings.daemon, "hosted_recrash_prompt_reap_enabled", False)
    order: list[str] = []
    await _close_captured(
        monkeypatch,
        outcome=TurnOutcome(exited=False, crashed=True),
        settlement=_settlement(crashed=True, recrash=True, settled=True),
        order=order,
    )
    assert order == ["settle"]
    assert [r["extra"]["reason"] for r in _skips(loguru_records)] == ["disabled"]


async def test_unsettled_turn_skips_the_reap_logged(
    monkeypatch: pytest.MonkeyPatch,
    reap_enabled: None,
    loguru_records: list[dict[str, Any]],
) -> None:
    """A settle that could not reach idling (unsettled resources, or a row
    already replaced) leaves the corpse to the grace-window reap."""
    order: list[str] = []
    await _close_captured(
        monkeypatch,
        outcome=TurnOutcome(exited=False, crashed=True),
        settlement=_settlement(crashed=True, recrash=True, settled=False),
        order=order,
    )
    assert order == ["settle"]
    assert [r["extra"]["reason"] for r in _skips(loguru_records)] == ["settle_incomplete"]


async def test_row_moved_on_skips_the_reap_logged(
    monkeypatch: pytest.MonkeyPatch,
    reap_enabled: None,
    loguru_records: list[dict[str, Any]],
) -> None:
    order: list[str] = []
    await _close_captured(
        monkeypatch,
        outcome=TurnOutcome(exited=False, crashed=True),
        settlement=_settlement(crashed=True, recrash=True, settled=True),
        order=order,
        reap_result=[],
    )
    assert order == ["settle", "reap"]
    assert [r["extra"]["reason"] for r in _skips(loguru_records)] == ["row_moved_on"]


# ── the settle boundary against real rows ────────────────────────────────────


def _agent(conn: psycopg.Connection[Any]) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', 'host-test') "
        "ON CONFLICT (id) DO UPDATE SET status = 'idling', machine = 'host-test'",
        (agent_id,),
    )
    conn.commit()
    return agent_id


class _ReapEffects:
    """What the corpse reap emitted outside the row: audit events, published
    agent updates, and the recovery attempts handed the reaped corpses."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.published: list[int] = []
        self.attempts: list[list[ReapedCorpse]] = []


def _capture_reap_effects(monkeypatch: pytest.MonkeyPatch) -> _ReapEffects:
    effects = _ReapEffects()

    async def _event(_conn: object, event: Event) -> Event:
        if event.attributes.get("reason") == "corpse_reaper":
            effects.events.append(event.attributes)
        return event

    async def _publish(_bus: object, agent_id: int) -> None:
        effects.published.append(agent_id)

    async def _recover(_db: object, _bus: object, reaped: list[ReapedCorpse]) -> None:
        effects.attempts.append(list(reaped))

    monkeypatch.setattr("agent.ownership.corpse_reap.record_audit_async", _event)
    monkeypatch.setattr("agent.ownership.corpse_reap.publish_agent_updated", _publish)
    monkeypatch.setattr(settlement_mod, "recover_reaped_corpses", _recover)
    return effects


async def _crash_a_fresh_admission(
    pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    *,
    agent_id: int,
    owner: UUID,
) -> None:
    """Admit the idling agent as a hosted turn and settle that turn as crashed."""
    incarnation = await admit_hosted_runtime(
        pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert incarnation is not None
    await settlement_mod.close_hosted_turn(
        pool,
        pool,
        Database.from_settings(),
        event_bus,
        cast(AsyncPostgresSaver, object()),
        incarnation,
        TurnOutcome(exited=False, crashed=True),
    )


def _assert_prompt_reap_recovery_wake(
    db_conn: psycopg.Connection[Any], effects: _ReapEffects, agent_id: int
) -> None:
    """The prompt reap's committed wake is consumed on the spot: one attempt
    carrying the wake id, and the chat row the return value named (#4039)."""
    assert [corpse.agent_id for corpse in effects.attempts[0]] == [agent_id]
    wake_id = effects.attempts[0][0].recovery_wake_id
    assert wake_id is not None
    assert db_conn.execute(
        "SELECT kind, source, payload FROM inbound_messages WHERE id = %s", (wake_id,)
    ).fetchone() == ("chat", "system", {"hosted_turn_recovery": True})


async def test_settle_boundary_prompt_reaps_the_second_crash_at_once(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    reap_enabled: None,
    loguru_records: list[dict[str, Any]],
    database: Database,
    event_bus: EventBus,
) -> None:
    """The drill: crash -> (grace kept) -> retry crash -> immediate reap.

    The first crash settles to a marked idling corpse that stays put inside
    its full grace window; the retry's crash re-settles it and the prompt reap
    terminates it on the spot, with the confirmed crash count on the events.
    """
    effects = _capture_reap_effects(monkeypatch)
    agent_id, owner = _agent(db_conn), uuid4()

    await _crash_a_fresh_admission(aops_pool, database, event_bus, agent_id=agent_id, owner=owner)

    row = db_conn.execute(
        "SELECT status, last_turn_fatal_at IS NOT NULL FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert row == ("idling", True)
    # The first death is not harvested: it keeps the whole grace window even
    # under an enabled switch.
    assert await reap_crash_corpses(aops_pool, "host-test", owner, bus=event_bus) == []
    assert effects.events == [] and effects.published == []

    # The retry: a wake admits the zombie again, and this turn dies too.
    await _crash_a_fresh_admission(aops_pool, database, event_bus, agent_id=agent_id, owner=owner)

    row = db_conn.execute(
        "SELECT status, termination_source, lease_expires_at, "
        "last_turn_fatal_at IS NOT NULL FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert row == ("terminated", "reaper", None, True)
    assert effects.events == [
        {
            "from": "idling",
            "to": "terminated",
            "reason": "corpse_reaper",
            "crash_count": 2,
        }
    ]
    assert effects.published == [agent_id]
    reaped_records = [
        r for r in loguru_records if r["extra"].get("event") == "corpse_reaper_terminated"
    ]
    assert [r["extra"]["crash_count"] for r in reaped_records] == [2]
    _assert_prompt_reap_recovery_wake(db_conn, effects, agent_id)
