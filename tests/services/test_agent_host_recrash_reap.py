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
from uuid import uuid4

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from agent.corpse_reap import reap_crash_corpses
from agent.hosted_ownership import (
    TurnFatalStamp,
    TurnSettlement,
    admit_hosted_runtime,
)
from services.agent_host import settlement as settlement_mod
from services.agent_host.runtime import TurnOutcome
from shared.config import settings
from shared.db import create_agent
from shared.runtime_incarnation import RuntimeIncarnation


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
        _pool: object, _incarnation: object, *, exited: bool, crashed: bool
    ) -> TurnSettlement:
        del exited, crashed
        order.append("settle")
        return settlement

    async def reconcile(_pool: object, _checkpointer: object, _incarnation: object) -> None:
        order.append("reconcile")

    async def reap(_pool: object, _incarnation: object) -> list[int]:
        order.append("reap")
        return reap_result if reap_result is not None else []

    monkeypatch.setattr(settlement_mod, "settle_and_stamp_turn", settle_and_stamp)
    monkeypatch.setattr(settlement_mod, "reconcile_inbounds_after_abort", reconcile)
    monkeypatch.setattr(settlement_mod, "reap_recrashed_corpse", reap)
    await settlement_mod.close_hosted_turn(
        cast(AsyncConnectionPool[Any], object()),
        cast(AsyncConnectionPool[Any], object()),
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


async def test_settle_boundary_prompt_reaps_the_second_crash_at_once(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    reap_enabled: None,
    loguru_records: list[dict[str, Any]],
) -> None:
    """The drill: crash -> (grace kept) -> retry crash -> immediate reap.

    The first crash settles to a marked idling corpse that stays put inside
    its full grace window; the retry's crash re-settles it and the prompt reap
    terminates it on the spot, with the confirmed crash count on the events.
    """
    events: list[dict[str, object]] = []

    async def _event(
        event_type: str, agent_id: int, *, payload: dict[str, object] | None = None, **_kw: object
    ) -> None:
        del event_type, agent_id
        if payload is not None and payload.get("reason") == "corpse_reaper":
            events.append(payload)

    monkeypatch.setattr("agent.corpse_reap.insert_event_log_async", _event)
    published: list[int] = []

    async def _publish(_pool: object, agent_id: int) -> None:
        published.append(agent_id)

    monkeypatch.setattr("agent.corpse_reap.publish_agent_updated", _publish)

    agent_id, owner = _agent(db_conn), uuid4()
    first = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling"
    )
    assert first is not None

    await settlement_mod.close_hosted_turn(
        aops_pool,
        aops_pool,
        cast(AsyncPostgresSaver, object()),
        first,
        TurnOutcome(exited=False, crashed=True),
    )

    row = db_conn.execute(
        "SELECT status, last_turn_fatal_at IS NOT NULL FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert row == ("idling", True)
    # The first death is not harvested: it keeps the whole grace window even
    # under an enabled switch.
    assert await reap_crash_corpses(aops_pool, "host-test", owner) == []
    assert events == [] and published == []

    # The retry: a wake admits the zombie again, and this turn dies too.
    retry = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling"
    )
    assert retry is not None
    await settlement_mod.close_hosted_turn(
        aops_pool,
        aops_pool,
        cast(AsyncPostgresSaver, object()),
        retry,
        TurnOutcome(exited=False, crashed=True),
    )

    row = db_conn.execute(
        "SELECT status, termination_source, lease_expires_at, "
        "last_turn_fatal_at IS NOT NULL FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert row == ("terminated", "reaper", None, True)
    assert events == [
        {
            "from": "idling",
            "to": "terminated",
            "reason": "corpse_reaper",
            "crash_count": 2,
        }
    ]
    assert published == [agent_id]
    reaped_records = [
        r for r in loguru_records if r["extra"].get("event") == "corpse_reaper_terminated"
    ]
    assert [r["extra"]["crash_count"] for r in reaped_records] == [2]
