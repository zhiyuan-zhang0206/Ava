"""Admission and exact-generation progress for an explicitly held unit.

The existing pause-owner file is authoritative even when Postgres is offline.
Its maintenance payload has no expiry. Ordinary startup releases it only after
readiness; stranded-rollout recovery cannot override an incomplete service stop. Business API calls remain available while an
already admitted model/action finishes; this gate only controls new work.
"""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar  # noqa: TID251

# An explicit CLI-only capability: nested start/unpause can restore dependencies
# without giving child service processes permission to clear the durable hold.
from dataclasses import replace
from datetime import datetime

from shared import pause_owner
from shared.maintenance_state import MaintenanceHold

_authorized_start: ContextVar[tuple[str, datetime] | None] = ContextVar(
    "maintenance_start", default=None
)


def snapshot() -> pause_owner.PauseOwnerSnapshot | None:
    current = pause_owner.read()
    if current.status == "invalid":
        raise RuntimeError("unreadable pause owner; refusing new work until repaired")
    if current.status == "paused" and current.maintenance is not None:
        return current
    return None


def held() -> bool:
    return snapshot() is not None


def require_released(action: str) -> None:
    if held():
        raise RuntimeError(
            f"{action} cannot override maintenance; run ava start to resume this unit first"
        )


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
    if hold.phase != "draining" or agent_id in hold.drained or agent_id in hold.failures:
        return None
    return hold.commands.get(agent_id) or None


def record_drained(agent_id: int, command_id: int, *, failure: str | None = None) -> None:
    """Record only after the host's actual continuation and cleanup returned.

    Compare-and-swap retries only contention between independently finishing
    cohort agents. It never retries an execution or checkpoint write.
    """
    while True:
        current = snapshot()
        if current is None or current.maintenance is None:
            return
        hold = current.maintenance
        if failure is None and hold.commands.get(agent_id) != command_id:
            raise RuntimeError("drained command does not belong to this maintenance cohort")
        if failure is None and agent_id in hold.failures:
            raise RuntimeError("failed continuation cannot certify a maintenance drain")
        if failure is None and agent_id in hold.drained:
            return
        updated = (
            replace(hold, drained=tuple(sorted((*hold.drained, agent_id))))
            if failure is None
            else replace(hold, failures={**hold.failures, agent_id: failure})
        )
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


def record_undelivered(agent_id: int, category: str) -> None:
    """Record a crash-equivalent receipt without latching a blocking failure.

    The turn raised a database-outage exception, so its continuation outcome is
    unknown but durable: the restart pointer survives exactly as after a host
    crash. This receipt is kept in the journal for audit only — it never blocks
    resume, and the host re-drives the held-control path (explicit re-flush
    before the restart claim) on the next wake, so the drain still certifies
    only through a genuinely completed continuation.
    """
    while True:
        current = snapshot()
        if current is None or current.maintenance is None:
            return
        hold = current.maintenance
        if agent_id in hold.undelivered:
            return
        updated = replace(hold, undelivered={**hold.undelivered, agent_id: category})
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


def repair(
    holder: str, acquired_at: datetime, record: dict[str, str]
) -> pause_owner.PauseOwnerSnapshot:
    """Sanctioned release of latched blocking failures, audited in the journal.

    The operator fixed the root cause; this moves `failures` verbatim into
    `repaired` (the CAS "before" side) together with the operator-identity
    `record`, leaving the hold resumable through the ordinary release path.
    Undelivered receipts are never cleared — they never block. The caller is
    responsible for the host-quiescence and reachability proofs.
    """
    from shared.maintenance_state import validate_repair_record

    validated = validate_repair_record(record)
    current = require_operation(holder, acquired_at)
    assert current.maintenance is not None  # noqa: S101
    hold = current.maintenance
    if not hold.failures:
        raise RuntimeError(
            "no failed receipts to repair; resume --cancel abandons a failure-free drain"
        )
    if hold.phase not in ("preparing", "draining"):
        raise RuntimeError(
            "repair cannot bypass a started stop; complete maintenance stop/start/resume"
        )
    updated = replace(
        hold,
        failures={},
        repaired={**hold.repaired, **hold.failures},
        repair_record=validated,
    )
    return pause_owner.change_maintenance(holder, acquired_at, hold, updated)


def set_phase(holder: str, acquired_at: datetime, phase: str) -> pause_owner.PauseOwnerSnapshot:
    current = require_operation(holder, acquired_at)
    assert current.maintenance is not None  # noqa: S101
    hold = current.maintenance
    allowed = {
        "draining": "drained",
        "drained": "stopping",
        "stopping": "stopped",
        "stopped": "starting",
        "starting": "ready",
    }
    if phase != allowed.get(hold.phase):
        raise RuntimeError(f"invalid maintenance transition: {hold.phase} -> {phase}")
    if phase == "drained" and (hold.failures or set(hold.drained) != set(hold.commands)):
        raise RuntimeError("resume cohort has not fully drained")
    updated = MaintenanceHold.decode({**hold.encode(), "phase": phase})
    return pause_owner.change_maintenance(holder, acquired_at, hold, updated)


def record_failure(agent_id: int, category: str) -> None:
    # A failure can occur between hold publication and cohort capture. Keep
    # that evidence too; preparation must not bless a now-idle broken runtime.
    # Callers grade database-outage exceptions into `record_undelivered`
    # instead: those are crash-equivalent and must not block resume.
    record_drained(agent_id, 0, failure=category)
