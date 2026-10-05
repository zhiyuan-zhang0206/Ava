"""Admission and exact-generation progress for an explicitly held unit.

The existing pause-owner file is authoritative even when Postgres is offline.
Its maintenance payload has no expiry. Ordinary startup releases it only after
readiness, and nothing overrides an incomplete service stop. Business API calls
remain available while an already admitted model/action finishes; this gate only
controls new work.
"""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar  # noqa: TID251

# An explicit CLI-only capability: nested start/unpause can restore dependencies
# without giving child service processes permission to clear the durable hold.
from dataclasses import replace
from datetime import datetime

from base.deploy.maintenance import pause_owner
from base.deploy.maintenance.state import CERTIFIED_PHASES, MaintenanceHold, MaintenancePhase

_authorized_start: ContextVar[tuple[str, datetime] | None] = ContextVar(
    "maintenance_start", default=None
)


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
    SDK calls, so only the following stop/start phases close business requests.
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
        MaintenancePhase.STARTING,
        MaintenancePhase.READY,
    }


# The stop window's phases: the drain has landed, or the unit is mid-stop /
# mid-start. `preparing`/`draining` stay live — an already-admitted turn may
# still be counted down and must finish; `ready` is included so a resume's
# last moments cannot race a background loop with the hold about to release.


def quiesced() -> bool:
    """Whether this unit is inside its stop window (`drained` .. `ready`).

    Background loops consult this before doing database work: while a unit is
    being stopped, held stopped, or brought back up, ownership renewals, turn
    scans, page reconciliation and pool borrows must wait — the window's whole
    point is that the unit stops doing database work until `ava start` releases
    the hold. `preparing`/`draining` read as NOT quiesced (in-flight
    continuations must still run). An unreadable owner reads as quiesced: the
    same refuse-new-work posture `snapshot` enforces by raising, held by
    background loops instead of crashing them.
    """
    try:
        current = snapshot()
    except (RuntimeError, OSError):
        return True
    if current is None or current.maintenance is None:
        return False
    return current.maintenance.phase in CERTIFIED_PHASES


# The stop leg of the maintenance window: drainage is complete and the unit has
# not begun coming back up (`starting`). The host's turn scan reads this
# narrower slice instead of the whole quiesced window: while the stop leg runs,
# the operator's stop owns the agents and a scan would fight it, but from the
# start leg on a booting host must drain its pending workset — recovery may
# not wait for the hold to release, because pub/sub has no replay (task
# #3227).
_STOP_LEG_PHASES = frozenset(
    {MaintenancePhase.DRAINED, MaintenancePhase.STOPPING, MaintenancePhase.STOPPED}
)


def in_stop_leg() -> bool:
    """Whether the unit is in its stop leg (`drained` .. `stopped`).

    `quiesced()` is the wider no-database-work window (`drained` .. `ready`);
    this is the narrower slice where agent work must not be touched at all:
    the drain is complete and the start has not begun. An unreadable owner
    reads as the stop leg — the same refuse-new-work posture `quiesced()`
    takes. The start leg (`starting`/`ready`) reads False: a host booting into
    a still-held unit must be able to scan its pending workset.
    """
    try:
        current = snapshot()
    except (RuntimeError, OSError):
        return True
    if current is None or current.maintenance is None:
        return False
    return current.maintenance.phase in _STOP_LEG_PHASES


def start_authorized() -> bool:
    current = snapshot()
    return current is not None and _authorized_start.get() == (current.holder, current.acquired_at)


def require_start_allowed() -> None:
    current = snapshot()
    if current is not None and _authorized_start.get() != (current.holder, current.acquired_at):
        raise RuntimeError(
            "service startup cannot release maintenance without the authorized ava start boundary"
        )


@contextmanager
def authorized_start(holder: str, acquired_at: datetime) -> Generator[None]:
    """An explicit local start restores dependencies while admission stays held."""
    require_operation(holder, acquired_at)
    token = _authorized_start.set((holder, acquired_at))
    try:
        yield
    finally:
        _authorized_start.reset(token)


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
    if (
        hold.phase != MaintenancePhase.DRAINING
        or agent_id in hold.drained
        or agent_id in hold.failures
    ):
        return None
    return hold.commands.get(agent_id) or None


def _drain_update(
    hold: MaintenanceHold, agent_id: int, command_id: int, failure: str | None
) -> MaintenanceHold | None:
    """The hold after recording one agent's drain (or failure); None when already recorded."""
    if failure is not None:
        return replace(hold, failures={**hold.failures, agent_id: failure})
    if hold.commands.get(agent_id) != command_id:
        raise RuntimeError("drained command does not belong to this maintenance cohort")
    if agent_id in hold.failures:
        raise RuntimeError("failed continuation cannot certify a maintenance drain")
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


def record_drained(agent_id: int, command_id: int, *, failure: str | None = None) -> None:
    """Record only after the host's actual continuation and cleanup returned.

    Compare-and-swap retries only contention between independently finishing
    cohort agents. It never retries an execution or checkpoint write.
    """
    _change_with_retry(lambda hold: _drain_update(hold, agent_id, command_id, failure))


def record_undelivered(agent_id: int, category: str) -> None:
    """Record a crash-equivalent receipt without latching a blocking failure.

    The turn raised a database-outage exception, so its continuation outcome is
    unknown but durable: the restart pointer survives exactly as after a host
    crash. This receipt is kept in the journal for audit only — it never blocks
    resume, and the host re-drives the held-control path (explicit re-flush
    before the restart claim) on the next wake, so the drain still certifies
    only through a genuinely completed continuation.
    """
    _change_with_retry(
        lambda hold: (
            None
            if agent_id in hold.undelivered
            else replace(hold, undelivered={**hold.undelivered, agent_id: category})
        )
    )


def clear_failures() -> dict[int, str]:
    """Drop every blocking failure receipt from the held journal; the receipts cleared.

    `ava start` calls this once it has re-delivered each failed continuation
    (`cli.commands.lifecycle._failed_receipts`), so the hold can release. The
    clear is a compare-and-swap on the journal: a turn that lands another failure
    in between is cleared with the rest, or leaves a failure that keeps the hold
    for the next `ava start`. Undelivered receipts are never touched; they never
    block.
    """
    cleared: dict[int, str] = {}

    def update(hold: MaintenanceHold) -> MaintenanceHold | None:
        if not hold.failures:
            return None
        cleared.clear()
        cleared.update(hold.failures)
        return replace(hold, failures={})

    _change_with_retry(update)
    return cleared


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
        MaintenancePhase.STOPPED: MaintenancePhase.STARTING,
        MaintenancePhase.STARTING: MaintenancePhase.READY,
    }
    if phase != allowed.get(hold.phase):
        raise RuntimeError(f"invalid maintenance transition: {hold.phase} -> {phase}")
    if phase == MaintenancePhase.DRAINED and (
        hold.failures or set(hold.drained) != set(hold.commands)
    ):
        raise RuntimeError("resume cohort has not fully drained")
    updated = MaintenanceHold.decode({**hold.encode(), "phase": phase})
    return pause_owner.change_maintenance(holder, acquired_at, hold, updated)


def record_failure(agent_id: int, category: str) -> None:
    # A failure can occur between hold publication and cohort capture. Keep
    # that evidence too; preparation must not bless a now-idle broken runtime.
    # Callers grade database-outage exceptions into `record_undelivered`
    # instead: those are crash-equivalent and must not block resume. They also
    # drop agents with no continuation left in the hold (`MaintenanceHold`'s
    # `outside_cohort` and `settled_after_drain`).
    record_drained(agent_id, 0, failure=category)
