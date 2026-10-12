"""Admission and exact-generation progress for an explicitly held unit.

The existing pause-owner file is authoritative even when Postgres is offline.
Its maintenance payload has no expiry. Ordinary startup releases it only after
readiness, and nothing overrides an incomplete service stop. Business API calls
remain available while an already admitted model/action finishes; this gate only
controls new work.
"""

from collections.abc import Callable

# An explicit CLI-only capability: nested start/unpause can restore dependencies
# without giving child service processes permission to clear the durable hold.
from dataclasses import replace
from datetime import datetime

from base.deploy.maintenance import pause_owner
from base.deploy.maintenance.state import CERTIFIED_PHASES, MaintenanceHold, MaintenancePhase


def snapshot() -> pause_owner.PauseOwnerSnapshot | None:
    current = pause_owner.read()
    if current.status == "invalid":
        raise RuntimeError("unreadable pause owner; refusing new work until it is removed")
    if current.status == "paused" and current.maintenance is not None:
        return current
    return None


def held() -> bool:
    return snapshot() is not None


def business_paused() -> bool:
    """Fence HTTP only after fleet drain reaches this unit's stop window.

    The same durable journal that releases agent admission controls HTTP. A
    database posture projection or cached read cannot prolong a completed resume.
    Drained local agents do not imply remote continuations have finished their
    SDK calls, so only the following stop phases close business requests.
    Unknown or incomplete pause records keep them closed until the journal is removed by hand.
    """
    try:
        current = pause_owner.read()
    except OSError:
        return True
    if current.status == "invalid":
        return True
    if current.status != "paused":
        return False
    if current.maintenance is None:
        return True
    return current.maintenance.phase in {
        MaintenancePhase.STOPPING,
        MaintenancePhase.STOPPED,
    }


# The stop window's phases: the drain has landed and the unit is stopping or
# held stopped. `preparing`/`draining` stay live — an already-admitted turn may
# still be counted down and must finish.


def quiesced() -> bool:
    """Whether this unit is inside its stop window (`drained` .. `stopped`).

    Background loops consult this before doing database work: while a unit is
    being stopped or held stopped, ownership renewals, turn scans, page
    reconciliation and pool borrows must wait — the window's whole point is
    that the unit stops doing database work until `ava start` releases the
    hold. `preparing`/`draining` read as NOT quiesced (in-flight continuations
    must still run). An unreadable owner reads as quiesced: the same
    refuse-new-work posture `snapshot` enforces by raising, held by background
    loops instead of crashing them.
    """
    try:
        current = snapshot()
    except (RuntimeError, OSError):
        return True
    if current is None or current.maintenance is None:
        return False
    return current.maintenance.phase in CERTIFIED_PHASES


def start_authorized(operation: pause_owner.PauseOwnerSnapshot | None) -> bool:
    current = snapshot()
    return (
        operation is not None
        and operation.holder is not None
        and operation.acquired_at is not None
        and current is not None
        and current.matches(operation.holder, operation.acquired_at)
    )


def require_start_allowed(operation: pause_owner.PauseOwnerSnapshot | None) -> None:
    current = snapshot()
    if current is not None and (
        operation is None
        or operation.holder is None
        or operation.acquired_at is None
        or not current.matches(operation.holder, operation.acquired_at)
    ):
        raise RuntimeError(
            "service startup cannot release maintenance without the authorized ava start boundary"
        )


def authorized_start(holder: str, acquired_at: datetime) -> pause_owner.PauseOwnerSnapshot:
    """Return this exact local operation's authority; children receive no ambient permission."""
    return require_operation(holder, acquired_at)


def require_operation(holder: str, acquired_at: datetime) -> pause_owner.PauseOwnerSnapshot:
    current = snapshot()
    if current is None or not current.matches(holder, acquired_at):
        raise RuntimeError("this unit is not held by the supplied maintenance generation")
    return current


def pending_command(agent_id: int) -> int | None:
    current = snapshot()
    if current is None or current.maintenance is None:
        return None
    hold = current.maintenance
    if hold.phase != MaintenancePhase.DRAINING or agent_id in hold.drained:
        return None
    return hold.commands.get(agent_id) or None


def _drain_update(hold: MaintenanceHold, agent_id: int, command_id: int) -> MaintenanceHold | None:
    """The hold after recording one agent's drain; None when already recorded."""
    if hold.commands.get(agent_id) != command_id:
        raise RuntimeError("drained command does not belong to this maintenance cohort")
    if agent_id in hold.drained:
        return None
    return replace(hold, drained=tuple(sorted((*hold.drained, agent_id))))


def _change_with_retry(update: Callable[[MaintenanceHold], MaintenanceHold | None]) -> None:
    """Compare-and-swap the maintenance hold with `update(hold)`; None from `update` is a no-op.

    Retries only contention: a lost swap where the unit is still held by the
    same generation and the hold moved.
    """
    while True:
        current = snapshot()
        if current is None or current.maintenance is None:
            return
        hold = current.maintenance
        updated = update(hold)
        if updated is None:
            return
        assert current.holder is not None and current.acquired_at is not None  # noqa: S101
        try:
            pause_owner.change_maintenance(
                current.holder,
                current.acquired_at,
                hold,
                updated,
            )
        except RuntimeError:
            newer = snapshot()
            if newer is None or not newer.matches(current.holder, current.acquired_at):
                raise
            if newer.maintenance == hold:
                raise
        else:
            return


def record_drained(agent_id: int, command_id: int) -> None:
    """Record only after the host's actual continuation and cleanup returned.

    Compare-and-swap retries only contention between independently finishing
    cohort agents. It never retries an execution or checkpoint write.
    """
    _change_with_retry(lambda hold: _drain_update(hold, agent_id, command_id))


def set_phase(
    holder: str, acquired_at: datetime, phase: MaintenancePhase
) -> pause_owner.PauseOwnerSnapshot:
    current = require_operation(holder, acquired_at)
    assert current.maintenance is not None  # noqa: S101
    hold = current.maintenance
    allowed = {
        MaintenancePhase.DRAINING: MaintenancePhase.DRAINED,
        MaintenancePhase.DRAINED: MaintenancePhase.STOPPING,
        MaintenancePhase.STOPPING: MaintenancePhase.STOPPED,
    }
    if phase != allowed.get(hold.phase):
        raise RuntimeError(f"invalid maintenance transition: {hold.phase} -> {phase}")
    if phase == MaintenancePhase.DRAINED and set(hold.drained) != set(hold.commands):
        raise RuntimeError("resume cohort has not fully drained")
    updated = MaintenanceHold.decode({**hold.encode(), "phase": phase})
    return pause_owner.change_maintenance(holder, acquired_at, hold, updated)
