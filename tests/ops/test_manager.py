"""Controller-manager orchestration: fixed order, short-circuit on the first
blocker, per-dimension last-result recording, and off-thread reconcile so blocking
I/O never stalls the event loop."""

from __future__ import annotations

import logging
import threading

import pytest

from ops import manager
from ops.controllers.base import BlockScope, ReconcileResult
from ops.manager import ControllerManager, build_controllers


class _FakeController:
    def __init__(self, name: str, blocks: BlockScope, record: list[str]) -> None:
        self.name = name
        self._blocks = blocks
        self._record = record

    def reconcile(self, role: str) -> ReconcileResult:
        self._record.append(self.name)
        return ReconcileResult(dimension=self.name, blocks=self._blocks)


async def test_runs_in_order_until_first_blocker() -> None:
    record: list[str] = []
    mgr = ControllerManager(
        [
            _FakeController("a", BlockScope.NONE, record),
            _FakeController("b", BlockScope.ALL, record),
            _FakeController("c", BlockScope.NONE, record),
        ]
    )
    assert await mgr.reconcile("gateway") is BlockScope.ALL
    assert record == ["a", "b"]  # "c" never runs — short-circuited on the blocker


async def test_db_scoped_blocker_also_short_circuits_and_reports_its_scope() -> None:
    """A DB-scoped block still stops the controller chain (the later controllers all
    read the DB), and the manager passes its narrower scope up rather than widening it
    to "everything is blocked"."""
    record: list[str] = []
    mgr = ControllerManager(
        [
            _FakeController("a", BlockScope.DB_DEPENDENT, record),
            _FakeController("b", BlockScope.NONE, record),
        ]
    )
    assert await mgr.reconcile("gateway") is BlockScope.DB_DEPENDENT
    assert record == ["a"]


async def test_no_blocker_runs_all_and_returns_none_scope() -> None:
    record: list[str] = []
    mgr = ControllerManager(
        [
            _FakeController("a", BlockScope.NONE, record),
            _FakeController("b", BlockScope.NONE, record),
        ]
    )
    assert await mgr.reconcile("gateway") is BlockScope.NONE
    assert record == ["a", "b"]


async def test_records_last_result_per_dimension() -> None:
    mgr = ControllerManager(
        [
            _FakeController("a", BlockScope.NONE, []),
            _FakeController("b", BlockScope.ALL, []),
        ]
    )
    await mgr.reconcile("gateway")
    last = mgr.last_results()
    assert last["a"].blocks is BlockScope.NONE
    assert last["b"].blocks is BlockScope.ALL


async def test_reconcile_offloads_to_worker_thread() -> None:
    """Each controller reconcile runs off the event-loop thread (via asyncio.to_thread),
    so a blocking DB/HTTP call inside a controller cannot freeze the loop."""
    seen: dict[str, str] = {}

    class _ThreadProbe:
        name = "probe"

        def reconcile(self, role: str) -> ReconcileResult:
            seen["thread"] = threading.current_thread().name
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)

    await ControllerManager([_ThreadProbe()]).reconcile("gateway")
    assert seen["thread"] != threading.main_thread().name


def test_default_controllers_in_reconcile_order() -> None:
    """`updater` is first, and that position is the whole point of it: a hung
    `ava-updater` leaves this host paused, so a reaper behind `pause` would be
    short-circuited away in exactly the case it exists for.

    Its position got MORE load-bearing with #1074, without the order changing. `pause`
    and `pin` used to gate on the update lease alone, which a watchdog-spawned updater
    never takes, so neither could be stuck behind a corpse holding the session name.
    Both now read `current_orchestration` — the fix for a pin heal that force-checked
    out underneath a live updater — so all three of `pause`, `pin` and `code` are
    downstream of this reaper, and it has to run before any of them.

    `rollout` sits beside it and ahead of `pause` for a sharper version of the same
    reason. A cluster rollout pauses this host in Phase A, *before* it reaches anything
    that can hang, so a hung rollout is only ever met on a paused host — and on
    2026-08-02 the sole line prod's watchdog emitted through 67 minutes of a hung
    rollout was `round blocked by pause (scope=all)`. Neither of the two ahead of
    `pause` blocks, so nothing behind them loses a round to their position."""
    assert [c.name for c in build_controllers()] == [
        "updater",
        "rollout",
        "pause",
        "schema",
        "pin",
        "code",
    ]


# ─── a blocked round is never silent ──────────────────────────────────────────
#
# A skipped round and an all-green round used to look identical in the log, so a
# Windows runner's 3h07m reconcile gap (2026-07-28 22:04 -> 2026-07-29 01:11, blocked
# every minute by "Schema ahead of code") could only be found by counting
# `_run_check` lines per hour. These pin the line that makes it readable directly.


async def test_blocked_round_logs_dimension_scope_and_streak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mgr = ControllerManager([_FakeController("schema", BlockScope.DB_DEPENDENT, [])])
    with caplog.at_level(logging.WARNING, logger="ops.manager"):
        await mgr.reconcile("agent-runner")
        await mgr.reconcile("agent-runner")

    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2, "every blocked round logs, not just the first"
    assert "blocked by schema" in lines[0] and "db-dependent" in lines[0]
    assert "roster NOT fully reconciled" in lines[0]
    assert "1 consecutive round(s)" in lines[0]
    assert "2 consecutive round(s)" in lines[1]
    assert all(r.levelno == logging.WARNING for r in caplog.records)


async def test_blocked_streak_escalates_to_error_at_the_alarm_bound(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Past the bound the same situation is no longer a routine rollout window, so it
    stops whispering the same WARNING forever."""
    mgr = ControllerManager([_FakeController("pause", BlockScope.ALL, [])])
    with caplog.at_level(logging.WARNING, logger="ops.manager"):
        for _ in range(manager._BLOCKED_ROUND_ALARM_ROUNDS):
            await mgr.reconcile("gateway")

    levels = [r.levelno for r in caplog.records]
    assert levels[: manager._BLOCKED_ROUND_ALARM_ROUNDS - 1] == [logging.WARNING] * (
        manager._BLOCKED_ROUND_ALARM_ROUNDS - 1
    )
    assert levels[-1] == logging.ERROR


async def test_clearing_a_streak_is_logged_and_resets(caplog: pytest.LogCaptureFixture) -> None:
    """The gap needs an end timestamp, not just a start — and the counter must reset so
    a later short block does not inherit an old streak's ERROR level."""
    blocker = _FakeController("schema", BlockScope.DB_DEPENDENT, [])
    passer = _FakeController("schema", BlockScope.NONE, [])
    mgr = ControllerManager([blocker])
    await mgr.reconcile("gateway")
    await mgr.reconcile("gateway")

    mgr._controllers = (passer,)
    with caplog.at_level(logging.INFO, logger="ops.manager"):
        await mgr.reconcile("gateway")
    assert "no longer blocked after 2 consecutive blocked round(s)" in caplog.text

    mgr._controllers = (blocker,)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ops.manager"):
        await mgr.reconcile("gateway")
    assert "1 consecutive round(s)" in caplog.text
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


async def test_unblocked_rounds_do_not_log(caplog: pytest.LogCaptureFixture) -> None:
    """A healthy host stays quiet — this line fires every 60s, so it must not become
    the noise it exists to cut through."""
    mgr = ControllerManager([_FakeController("schema", BlockScope.NONE, [])])
    with caplog.at_level(logging.INFO, logger="ops.manager"):
        await mgr.reconcile("gateway")
        await mgr.reconcile("gateway")
    assert caplog.records == []
