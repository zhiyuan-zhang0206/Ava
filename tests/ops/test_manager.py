"""Controller-manager orchestration: fixed order, short-circuit on the first
blocker, per-dimension last-result recording, and off-thread reconcile so blocking
I/O never stalls the event loop."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from ops import manager
from ops.controllers.base import BlockScope, Controller, ReconcileResult
from ops.manager import ControllerManager, build_controllers


class _FakeController:
    def __init__(self, name: str, blocks: BlockScope, record: list[str]) -> None:
        self.name = name
        self.timeout_s: float | None = None
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


async def test_reports_the_blocking_dimension() -> None:
    """The watchdog's scope resolution needs to know WHICH dimension blocked
    (a pause-scoped ALL block exempts the gateway healthcheck; a pin-scoped one
    does not)."""
    mgr = ControllerManager(
        [
            _FakeController("a", BlockScope.NONE, []),
            _FakeController("b", BlockScope.ALL, []),
        ]
    )
    await mgr.reconcile("gateway")
    assert mgr.blocking_dimension() == "b"
    mgr._controllers = (_FakeController("a", BlockScope.NONE, []),)
    await mgr.reconcile("gateway")
    assert mgr.blocking_dimension() is None


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
        timeout_s: float | None = None

        def reconcile(self, role: str) -> ReconcileResult:
            seen["thread"] = threading.current_thread().name
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)

    await ControllerManager([_ThreadProbe()]).reconcile("gateway")
    assert seen["thread"] != threading.main_thread().name


async def test_controller_timeout_skips_the_rest_of_the_round(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A controller can declare a bounded reconcile: when its worker thread
    exceeds that bound, the manager logs the incident and blocks the remaining
    round.

    The regression is a controller's blocking I/O pinning the entire watchdog
    round forever, which leaves no completed tick to make the watchdog's own
    health endpoint stale.
    """
    started = threading.Event()
    release = threading.Event()
    seen: list[str] = []

    class _TimedOutController:
        name = "slow"
        timeout_s: float | None = 0.01

        def reconcile(self, role: str) -> ReconcileResult:
            del role
            started.set()
            assert release.wait(timeout=1)
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)

    class _FollowingController:
        name = "following"
        timeout_s: float | None = None

        def reconcile(self, role: str) -> ReconcileResult:
            del role
            seen.append(self.name)
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)

    try:
        with caplog.at_level(logging.ERROR, logger="ops.manager"):
            assert (
                await asyncio.wait_for(
                    ControllerManager([_TimedOutController(), _FollowingController()]).reconcile(
                        "gateway"
                    ),
                    timeout=0.2,
                )
                is BlockScope.ALL
            )
    finally:
        release.set()

    assert started.is_set()
    assert seen == []
    assert any("slow reconcile exceeded 0.0s" in message for message in caplog.messages)


async def test_unbounded_controller_timeout_does_not_use_the_deadline_format() -> None:
    """A controller's own TimeoutError is not evidence that an unset deadline expired."""

    class _SocketTimeoutController:
        name = "socket"
        timeout_s: float | None = None

        def reconcile(self, role: str) -> ReconcileResult:
            del role
            raise TimeoutError("socket timed out")

    with pytest.raises(TimeoutError, match="socket timed out"):
        await ControllerManager([_SocketTimeoutController()]).reconcile("gateway")


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
    `pause` blocks, so nothing behind them loses a round to their position.

    `lease` joins them between `rollout` and `pause` for the same "meet the case on a
    paused host" reason: a killed orchestration strands its deploy lease while the
    host it paused sits blocked, so a reclaim behind `pause` would never run — and
    the stranded lease is exactly what refuses the next deploy for the rest of its
    TTL (2026-09-12). It, too, never blocks."""
    assert [c.name for c in build_controllers()] == [
        "updater",
        "rollout",
        "lease",
        "pause",
        "schema",
        "pin",
        "code",
    ]


def test_default_controllers_declare_the_timeout_contract() -> None:
    """Every built-in controller explicitly opts into the manager's optional
    timeout surface, even when it has no narrower deadline than the round."""
    assert all(isinstance(controller, Controller) for controller in build_controllers())


# ─── a block's start, heartbeats and end are never silent ─────────────────────
#
# A skipped round and an all-green round used to look identical in the log, so a
# Windows runner's 3h07m reconcile gap (2026-07-28 22:04 -> 2026-07-29 01:11, blocked
# every minute by "Schema ahead of code") could only be found by counting
# `_run_check` lines per hour. These pin the first-round line and the cadence-bound
# heartbeats that make a gap readable directly — without one line per round.


async def test_blocked_streak_logs_the_first_round_immediately(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first blocked round is not silent — it names the dimension, the scope and
    the streak start. Repeats are cadence-bound (see the next test), never per-round."""
    mgr = ControllerManager([_FakeController("schema", BlockScope.DB_DEPENDENT, [])])
    with caplog.at_level(logging.WARNING, logger="ops.manager"):
        await mgr.reconcile("agent-runner")
        await mgr.reconcile("agent-runner")

    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1, "start line only; the second round sits inside the cadence"
    assert "blocked by schema" in lines[0] and "db-dependent" in lines[0]
    assert "roster NOT fully reconciled" in lines[0]
    assert "1 consecutive round(s)" in lines[0]
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


async def test_blocked_streak_repeats_on_the_alarm_cadence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Heartbeats ride the alarm bound itself (ten rounds ≈ ten minutes) and carry
    the running streak count, so a long block stays countable without one line per
    round (2026-09-23/24: a pin/schema drift window made per-round repeats ~79% of the
    24h error bucket)."""
    mgr = ControllerManager([_FakeController("schema", BlockScope.ALL, [])])
    bound = manager._BLOCKED_ROUND_ALARM_ROUNDS
    with caplog.at_level(logging.WARNING, logger="ops.manager"):
        for _ in range(2 * bound):
            await mgr.reconcile("gateway")

    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 3, "one start line + two cadence heartbeats"
    assert "1 consecutive round(s)" in lines[0]
    assert f"{bound} consecutive round(s)" in lines[1]
    assert f"{2 * bound} consecutive round(s)" in lines[2]
    assert [r.levelno for r in caplog.records] == [
        logging.WARNING,
        logging.ERROR,
        logging.ERROR,
    ]


async def test_no_error_before_the_alarm_bound(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The escalation lands exactly at the bound — the rounds before it stay WARNING
    (and only the first of them logs)."""
    mgr = ControllerManager([_FakeController("schema", BlockScope.ALL, [])])
    bound = manager._BLOCKED_ROUND_ALARM_ROUNDS
    with caplog.at_level(logging.WARNING, logger="ops.manager"):
        for _ in range(bound - 1):
            await mgr.reconcile("gateway")
        assert [r.levelno for r in caplog.records] == [logging.WARNING]
        await mgr.reconcile("gateway")

    levels = [r.levelno for r in caplog.records]
    assert levels == [logging.WARNING, logging.ERROR]
    assert f"{bound} consecutive round(s)" in caplog.records[-1].getMessage()


async def test_pause_block_streak_never_escalates_to_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A paused host is an expected state whose pathology is bounded elsewhere
    (stalled_rollout ahead of pause; the unowned-pause release; the OS hold watchdog):
    its streak must not manufacture ERROR/WARNING events. First round WARNING, later
    heartbeats INFO — an expected dimension must stay out of the error bucket by
    construction (the 2026-09-23/24 drift window's ~2.9k repeats were schema-dominated)."""
    mgr = ControllerManager([_FakeController("pause", BlockScope.ALL, [])])
    bound = manager._BLOCKED_ROUND_ALARM_ROUNDS
    with caplog.at_level(logging.INFO, logger="ops.manager"):
        for _ in range(2 * bound):
            await mgr.reconcile("gateway")

    assert [r.levelno for r in caplog.records] == [
        logging.WARNING,
        logging.INFO,
        logging.INFO,
    ]
    assert f"{2 * bound} consecutive round(s)" in caplog.records[-1].getMessage()


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
