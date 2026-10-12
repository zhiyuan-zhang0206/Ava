"""A failed continuation is alerted and re-driven, never latched, and a failed wake
outside the cohort is no continuation failure."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from base.deploy.maintenance import admission, pause_owner
from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase
from services.agent_runner.agent_host.tests.test_agent_host import (
    _Build,
    _FakeConnCtx,
    _FakePool,
    _Row,
)
from services.agent_runner.agent_host.tests.test_agent_host import host_plugin as host_plugin
from services.agent_runner.agent_host.tests.test_agent_host import wired as wired
from services.agent_runner.agent_host.tests.test_maintenance_receipt_grading import (
    FC10_FOREIGN,
    Alerts,
    fc10_hold,
)
from services.agent_runner.agent_host.tests.test_maintenance_receipt_grading import alerts as alerts
from tests.components.agent.test_maintenance import WHEN
from tests.components.agent.test_maintenance import isolate as isolate


async def test_a_failed_continuation_is_alerted_and_the_held_control_stays_runnable(
    wired: _Build, monkeypatch: pytest.MonkeyPatch, alerts: Alerts
) -> None:
    """The turn raises while its restart command is owed: the host reports it and
    latches nothing in the journal. (This fake pool cannot settle the command, so
    the held control stays runnable; the settled case is tested against Postgres in
    `test_maintenance_receipt_grading`.)"""
    host, graph, _pool = wired({11: _Row(status="idling")})
    hold = MaintenanceHold(MaintenancePhase.DRAINING, {11: 100})

    async def broken(*_args: object, **_kwargs: object) -> None:
        before = pause_owner.begin_maintenance("failed", WHEN).snapshot
        assert before.maintenance is not None
        pause_owner.change_maintenance("failed", WHEN, before.maintenance, hold)
        raise RuntimeError("isolated final flush failure")

    monkeypatch.setattr(graph, "ainvoke", broken)
    with pytest.raises(RuntimeError, match="final flush"):
        await host.run_turn(11)
    (row,) = alerts.rows
    assert row["event"] == "agent_continuation_lost" and row["agent_id"] == 11
    assert row["failure_category"] == "RuntimeError" and row["holder"] == "failed"
    assert admission.require_operation("failed", WHEN).maintenance == hold
    assert admission.pending_command(11) == 100

    control = AsyncMock()
    monkeypatch.setattr(host, "_run_held_controls", control)
    await host.run_turn(11)
    control.assert_awaited_once()


class _BlockedRead:
    """The first row read is cancelled; subsequent cleanup can read no receipts."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self._first_read = True
        self._cleanup_pool = _FakePool({})

    def connection(self, timeout: float | None = None) -> "_BlockedRead | _FakeConnCtx":
        if self._first_read:
            self._first_read = False
            return self
        return self._cleanup_pool.connection(timeout=timeout)

    async def __aenter__(self) -> None:
        self.entered.set()
        await asyncio.Event().wait()

    async def __aexit__(self, *_exc: object) -> bool:
        return False


@pytest.mark.parametrize(
    ("phase", "drained", "agent", "reported"),
    [
        *(("stopping", (2, 6, 7), agent, False) for agent in FC10_FOREIGN),
        ("stopping", (2, 6, 7), 2, False),
        ("draining", (6, 7), 2, True),
    ],
    ids=["foreign-3", "foreign-8", "foreign-9", "drained-member", "draining-member"],
)
async def test_a_wake_cancelled_before_its_row_read_reports_only_owed_continuations(
    wired: _Build,
    monkeypatch: pytest.MonkeyPatch,
    alerts: Alerts,
    phase: str,
    drained: tuple[int, ...],
    agent: int,
    reported: bool,
) -> None:
    """FC-10 F20: a stop's SIGTERM cancels every task (`cancel_and_drain`).

    Caught before `read_stored_config` could say "not ours", a wake for
    another machine's agent, or one for a member already drained, owes no
    continuation and raises no alert. A member still draining does.
    """
    host, _graph, pool = wired({})
    blocked = _BlockedRead()
    monkeypatch.setattr(pool, "connection", blocked.connection)
    fc10_hold(phase, drained)
    turn = asyncio.create_task(host.run_turn(agent))
    await blocked.entered.wait()
    work = [
        task
        for task in asyncio.all_tasks()
        if getattr(task.get_coro(), "__qualname__", "") == "AgentHost._run_turn"
    ]
    assert len(work) == 1
    for task in (turn, *work):
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert [row["agent_id"] for row in alerts.rows] == ([agent] if reported else [])


@pytest.mark.parametrize("phase", ["stopping", "stopped"])
@pytest.mark.parametrize("agent", [2, 5], ids=["drained", "parked"])
async def test_a_settled_members_held_wake_claims_nothing(
    wired: _Build, monkeypatch: pytest.MonkeyPatch, phase: str, agent: int
) -> None:
    """Why its failure is no receipt: after the certified drain a drained or
    parked member's wake reads the row and returns; it never reaches the held
    control that claims, so its inbound messages stay pending."""
    host, _graph, pool = wired({agent: _Row(status="idling")})
    control = AsyncMock()
    monkeypatch.setattr(host, "_run_held_controls", control)
    fc10_hold(phase, parked=(5,))
    await host.run_turn(agent)
    control.assert_not_awaited()
    # Pre-turn qualification and the closed-pump compact source read.
    assert pool.reads == 2
