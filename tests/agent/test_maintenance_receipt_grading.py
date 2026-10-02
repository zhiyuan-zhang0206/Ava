"""Receipt grading: database outages are crash-equivalent (recorded, never
blocking), and only the hold's cohort leaves receipts at all."""

import asyncio
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from base.deploy.maintenance import admission, pause_owner
from base.deploy.maintenance.state import MaintenanceHold
from services.agent_host import maintenance as receipts
from tests.agent.test_maintenance import WHEN
from tests.agent.test_maintenance import isolate as isolate


def _held() -> None:
    before = pause_owner.begin_maintenance("grade", WHEN).snapshot
    assert before.maintenance is not None
    pause_owner.change_maintenance(
        "grade", WHEN, before.maintenance, MaintenanceHold("draining", {7: 11, 8: 12})
    )


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (PoolTimeout("pool exhausted"), "PoolTimeout"),
        (psycopg.OperationalError("connection refused"), "OperationalError"),
    ],
)
def test_database_outage_failures_are_recorded_undelivered(
    exc: BaseException, category: str
) -> None:
    """The DB-channel-break family latches an audit receipt, not a blocking one.

    No fence is left behind, so the wake scan re-drives the held-control path
    (explicit re-flush before the restart claim) once the channel recovers.
    """
    _held()
    fences: receipts.FailureFences = {}
    asyncio.run(receipts.record_failure(7, exc, fences))
    assert fences == {}
    current = admission.require_operation("grade", WHEN)
    assert current.maintenance is not None
    assert current.maintenance.failures == {}
    assert current.maintenance.undelivered == {7: category}
    assert current.maintenance.drained == ()


def test_bare_timeout_error_latches_a_blocking_failure() -> None:
    """A bare TimeoutError is not crash-equivalent (issue #2051).

    Since Python 3.11 ``asyncio.TimeoutError`` is the builtin, an LLM
    TTFT/compact timeout raises exactly this. Grading it as undelivered would
    leave no fence and no failure latch — the held-control path would re-drive
    forever and the drain could never certify nor be repaired. It must latch
    like any ordinary failure.
    """
    _held()
    fences: receipts.FailureFences = {}
    asyncio.run(receipts.record_failure(7, TimeoutError("llm ttft bound"), fences))
    current = admission.require_operation("grade", WHEN)
    assert current.maintenance is not None
    assert fences == {7: (current.holder, current.acquired_at)}
    assert current.maintenance.failures == {7: "TimeoutError"}
    assert current.maintenance.undelivered == {}


def test_ordinary_failure_still_latches_blocking_and_fenced() -> None:
    _held()
    fences: receipts.FailureFences = {}
    asyncio.run(receipts.record_failure(7, RuntimeError("node failed"), fences))
    current = admission.require_operation("grade", WHEN)
    assert current.maintenance is not None
    assert fences == {7: (current.holder, current.acquired_at)}
    assert current.maintenance.failures == {7: "RuntimeError"}
    assert current.maintenance.undelivered == {}


def test_failure_outside_a_hold_leaves_no_fence_and_no_receipt() -> None:
    fences: receipts.FailureFences = {}
    asyncio.run(receipts.record_failure(7, RuntimeError("isolated"), fences))
    assert fences == {}
    assert admission.snapshot() is None


def test_undelivered_record_is_idempotent_and_preserves_other_receipts() -> None:
    _held()
    asyncio.run(receipts.record_failure(7, PoolTimeout("again"), {}))
    asyncio.run(receipts.record_failure(7, PoolTimeout("again"), {}))
    asyncio.run(receipts.record_failure(8, psycopg.OperationalError("refused"), {}))
    current = admission.require_operation("grade", WHEN)
    assert current.maintenance is not None
    assert current.maintenance.undelivered == {7: "PoolTimeout", 8: "OperationalError"}
    assert current.maintenance.failures == {}


def _hold_with(
    *, failures: dict[int, str] | None = None, undelivered: dict[int, str] | None = None
) -> None:
    before = pause_owner.begin_maintenance("grade", WHEN).snapshot
    assert before.maintenance is not None
    hold = MaintenanceHold.decode(
        {
            **before.maintenance.encode(),
            "phase": "draining",
            "failures": {str(k): v for k, v in (failures or {}).items()},
            "undelivered": {str(k): v for k, v in (undelivered or {}).items()},
        }
    )
    pause_owner.change_maintenance("grade", WHEN, before.maintenance, hold)


def test_resume_agents_releases_undelivered_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Crash-equivalent receipts never block the release of admission."""
    from unittest.mock import MagicMock

    from ops.agent_pause import resume_agents

    monkeypatch.setattr("ops.agent_pause._wake", MagicMock())
    _hold_with(undelivered={7: "PoolTimeout"})
    resume_agents()
    assert pause_owner.read().status == "resumed"


def test_resume_agents_refuses_blocking_failures_with_repair_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from ops.agent_pause import resume_agents

    monkeypatch.setattr("ops.agent_pause._wake", MagicMock())
    _hold_with(failures={7: "RuntimeError"})
    with pytest.raises(RuntimeError, match="ava maintenance repair --operation"):
        resume_agents()
    assert pause_owner.read().status == "paused"


def test_unpause_releases_undelivered_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from ops.cluster_pause import unpause_local_cluster

    monkeypatch.setattr("ops.cluster_pause._unpause_local_cluster", MagicMock())
    monkeypatch.setattr("ops.agent_pause._wake", MagicMock())
    _hold_with(undelivered={7: "PoolTimeout"})
    unpause_local_cluster()
    assert pause_owner.read().status == "resumed"


# FC-10 F20, the LX unit: the rollout's hold drained this unit's agents 2, 6
# and 7; its stop then cancelled wakes for agents 3, 8 and 9 (fc10-mac2's,
# woken as that host resumed) before their row read could say "not ours".
FC10_HOLDER = "fc10-gw:pid17913"
FC10_AT = datetime(2026, 9, 29, 5, 35, 53, 41351, tzinfo=UTC)
FC10_FOREIGN = (3, 8, 9)


def fc10_hold(
    phase: str = "stopping", drained: tuple[int, ...] = (2, 6, 7), parked: tuple[int, ...] = ()
) -> None:
    """The LX hold, cohort 2, 6, 7; as its services stop began, all drained."""
    before = pause_owner.begin_maintenance(FC10_HOLDER, FC10_AT).snapshot
    assert before.maintenance is not None
    hold = MaintenanceHold.decode(
        {
            "phase": phase,
            "commands": {"2": 34, "6": 35, "7": 36},
            "drained": list(drained),
            "failures": {},
            "parked": list(parked),
        }
    )
    pause_owner.change_maintenance(FC10_HOLDER, FC10_AT, before.maintenance, hold)


def _receipts() -> tuple[dict[int, str], dict[int, str]]:
    current = admission.require_operation(FC10_HOLDER, FC10_AT)
    assert current.maintenance is not None
    return current.maintenance.failures, current.maintenance.undelivered


_FAILURES = pytest.mark.parametrize(
    "exc",
    [asyncio.CancelledError(), RuntimeError("x"), PoolTimeout("x")],
    ids=lambda exc: type(exc).__name__,
)


@_FAILURES
def test_a_failure_outside_the_captured_cohort_is_no_receipt(exc: BaseException) -> None:
    fc10_hold()
    fences: receipts.FailureFences = {}
    for agent in FC10_FOREIGN:
        asyncio.run(receipts.record_failure(agent, exc, fences))
    assert fences == {}
    assert _receipts() == ({}, {})


@_FAILURES
@pytest.mark.parametrize("phase", ["drained", "stopping", "stopped", "starting", "ready"])
def test_a_drained_members_failure_after_the_certified_drain_is_no_receipt(
    phase: str, exc: BaseException
) -> None:
    """Its continuation settled and its held wake claims nothing: no receipt."""
    fc10_hold(phase)
    fences: receipts.FailureFences = {}
    asyncio.run(receipts.record_failure(2, exc, fences))
    assert fences == {}
    assert _receipts() == ({}, {})


def test_before_the_drain_is_certified_every_member_still_latches() -> None:
    """Draining: a member already drained and one still draining both record."""
    fc10_hold("draining", drained=(6, 7))
    fences: receipts.FailureFences = {}
    for agent in (2, 6):
        asyncio.run(receipts.record_failure(agent, asyncio.CancelledError(), fences))
    assert fences == {2: (FC10_HOLDER, FC10_AT), 6: (FC10_HOLDER, FC10_AT)}
    assert _receipts() == ({2: "CancelledError", 6: "CancelledError"}, {})


def test_before_the_cohort_is_captured_every_failure_is_recorded() -> None:
    """A preparing hold proves no agent foreign; its failures stay (repairable)."""
    pause_owner.begin_maintenance(FC10_HOLDER, FC10_AT)
    asyncio.run(receipts.record_failure(3, asyncio.CancelledError(), {}))
    assert _receipts() == ({3: "CancelledError"}, {})


@pytest.mark.parametrize(
    ("phase", "latched"), [("draining", {5: "CancelledError"}), ("stopped", {})]
)
def test_a_parked_member_records_until_the_drain_is_certified(
    phase: str, latched: dict[int, str]
) -> None:
    """Parked agents belong to the unit; like drained members, they have
    nothing to continue once the drain is certified."""
    before = pause_owner.begin_maintenance(FC10_HOLDER, FC10_AT).snapshot
    assert before.maintenance is not None
    hold = MaintenanceHold.decode(
        {"phase": phase, "commands": {"2": 34}, "drained": [2], "failures": {}, "parked": [5]}
    )
    pause_owner.change_maintenance(FC10_HOLDER, FC10_AT, before.maintenance, hold)
    asyncio.run(receipts.record_failure(5, asyncio.CancelledError(), {}))
    asyncio.run(receipts.record_failure(3, asyncio.CancelledError(), {}))
    assert _receipts() == (latched, {})
