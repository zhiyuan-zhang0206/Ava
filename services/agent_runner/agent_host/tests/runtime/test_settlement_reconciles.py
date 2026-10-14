"""A settlement disposes the inbounds its turn claimed: when each reconcile pass dispatches."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, Protocol
from unittest.mock import AsyncMock

import pytest

from agent.ownership.hosted import TurnFatalStamp, TurnSettlement
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from services.agent_runner.agent_host import settlement
from services.agent_runner.agent_host.runtime import TurnOutcome
from services.agent_runner.agent_host.tests.runtime.host_support import (
    Build,
    Row,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    host_plugin as host_plugin,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    wired as wired,
)


class _Drive(Protocol):
    def __call__(
        self,
        agent: int,
        runtime: object,
        slices: object,
        /,
        *,
        incarnation: object,
        resources: object,
    ) -> Awaitable[TurnOutcome]: ...


class TestSettlementReconciles:
    """A settlement disposes the inbounds its turn claimed.

    The settled abort disposes them (task #3615); a finished non-crashed
    turn runs the same pass at its own settlement too (#3999). This locks
    WHEN each pass dispatches — after the settle, and never for a crash.
    Their own gates live in `test_agent_host_abort_reconcile.py` /
    `test_agent_host_turn_reconcile.py`; the row-visible split in
    `services/agent_runner/agent_host/recovery/tests/test_reconcile_after_abort.py`.
    """

    async def _run_ending(
        self,
        wired: Build,
        monkeypatch: pytest.MonkeyPatch,
        drive: _Drive,
        order: list[str],
    ) -> None:
        host, _, _ = wired(
            {1: Row()},
        )

        async def settle_and_stamp(
            _pool: object,
            _incarnation: object,
            *,
            bus: object,
            exited: bool,
            crashed: bool,
            resources: object,
        ) -> TurnSettlement:
            order.append("settle")
            return TurnSettlement(stamp=TurnFatalStamp(applied=crashed), settled=not exited)

        async def reconcile(_pool: object, _incarnation: object, *, resources: object) -> None:
            order.append("reconcile")

        async def reconcile_turn(_pool: object, _incarnation: object, *, resources: object) -> None:
            order.append("reconcile-turn")

        monkeypatch.setattr(settlement, "settle_and_stamp_turn", settle_and_stamp)
        monkeypatch.setattr(settlement, "reconcile_inbounds_after_abort", reconcile)
        monkeypatch.setattr(settlement, "reconcile_inbounds_after_turn", reconcile_turn)
        monkeypatch.setattr(host, "_runtime_for", AsyncMock(return_value=object()))
        monkeypatch.setattr(host, "_drive_turns", drive)
        await asyncio.wait_for(host.run_turn(1), 2)

    async def test_settled_abort_reconciles_after_the_settle(
        self, wired: Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def drive(
            _agent: int,
            _runtime: object,
            _slices: object,
            *,
            incarnation: object,
            resources: object,
            checkpoints: Any = None,
        ) -> TurnOutcome:
            return TurnOutcome(exited=False, crashed=True, aborted=True)

        order: list[str] = []
        await self._run_ending(wired, monkeypatch, drive, order)
        assert order == ["settle", "reconcile"]

    async def test_unclassified_crash_leaves_the_reconcile_to_the_next_admission(
        self, wired: Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the fatal-abort settlement proves the checkpoint settled; an
        unclassified crash drops the runtime instead, and the next admission
        (or boot) reconciles."""

        async def drive(
            _agent: int,
            _runtime: object,
            _slices: object,
            *,
            incarnation: object,
            resources: object,
            checkpoints: Any = None,
        ) -> TurnOutcome:
            raise ValueError("unclassified crash")

        order: list[str] = []
        with pytest.raises(ValueError):
            await self._run_ending(wired, monkeypatch, drive, order)
        assert order == ["settle"]

    async def test_finished_turn_reconciles_after_the_settle(
        self, wired: Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The finished turn's flushed checkpoint settles its claimed rows
        (#3999): the pass runs right after the settle, never for a crash.
        A cancelled turn carries the same outcome shape without the flush —
        its unconfirmable claims re-deliver at-least-once (see the pass's
        docstring)."""

        async def drive(
            _agent: int,
            _runtime: object,
            _slices: object,
            *,
            incarnation: object,
            resources: object,
            checkpoints: Any = None,
        ) -> TurnOutcome:
            return TurnOutcome(exited=False, crashed=False)

        order: list[str] = []
        await self._run_ending(wired, monkeypatch, drive, order)
        assert order == ["settle", "reconcile-turn"]
