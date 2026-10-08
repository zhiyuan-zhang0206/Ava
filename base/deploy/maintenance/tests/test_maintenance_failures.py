"""A failed continuation cannot become drained after journal I/O recovers, and a
failed wake outside the cohort never becomes a receipt."""

import asyncio
from typing import Any
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
    FC10_AT,
    FC10_FOREIGN,
    FC10_HOLDER,
    fc10_hold,
)
from tests.agent.test_maintenance import WHEN
from tests.agent.test_maintenance import isolate as isolate


@pytest.mark.parametrize("broken_io", ["read", "write"])
async def test_failure_is_latched_before_any_journal_io(
    wired: _Build,
    monkeypatch: pytest.MonkeyPatch,
    broken_io: str,
) -> None:
    host, graph, _pool = wired({11: _Row(status="idling")})
    real_snapshot = admission.snapshot
    fail_next_read = False

    def read() -> pause_owner.PauseOwnerSnapshot | None:
        nonlocal fail_next_read
        if fail_next_read:
            fail_next_read = False
            raise RuntimeError("isolated journal read failure")
        return real_snapshot()

    def write(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("isolated journal write failure")

    async def broken(*_args: Any, **_kwargs: Any) -> None:
        nonlocal fail_next_read
        before = pause_owner.begin_maintenance("failed", WHEN).snapshot
        assert before.maintenance is not None
        pause_owner.change_maintenance(
            "failed",
            WHEN,
            before.maintenance,
            MaintenanceHold(MaintenancePhase.DRAINING, {11: 100}),
        )
        fail_next_read = broken_io == "read"
        raise RuntimeError("isolated final flush failure")

    monkeypatch.setattr(admission, "snapshot", read)
    if broken_io == "write":
        monkeypatch.setattr(admission, "record_failure", write)
    monkeypatch.setattr(graph, "ainvoke", broken)
    with pytest.raises((RuntimeError, OSError), match="journal"):
        await host.run_turn(11)
    assert admission.pending_command(11) == 100
    control = AsyncMock()
    monkeypatch.setattr(host, "_run_held_controls", control)
    await host.run_turn(11)
    control.assert_not_awaited()


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
    ("phase", "drained", "agent", "latched"),
    [
        *(("stopping", (2, 6, 7), agent, {}) for agent in FC10_FOREIGN),
        ("stopping", (2, 6, 7), 2, {}),
        ("draining", (6, 7), 2, {2: "CancelledError"}),
    ],
    ids=["foreign-3", "foreign-8", "foreign-9", "drained-member", "draining-member"],
)
async def test_a_wake_cancelled_before_its_row_read_latches_only_live_continuations(
    wired: _Build,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    drained: tuple[int, ...],
    agent: int,
    latched: dict[int, str],
) -> None:
    """FC-10 F20: a stop's SIGTERM cancels every task (`cancel_and_drain`).

    Caught before `_read_stored_config` could say "not ours", a wake for
    another machine's agent latched a blocking receipt on the stopping unit's
    hold, and so could one for a member already drained. A member still
    draining keeps its receipt.
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
    current = admission.require_operation(FC10_HOLDER, FC10_AT)
    assert current.maintenance is not None
    assert current.maintenance.failures == latched


@pytest.mark.parametrize("phase", ["stopping", "stopped", "starting", "ready"])
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
