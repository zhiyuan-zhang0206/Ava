"""Admission and exact-generation progress for an explicitly held unit.

The existing pause-owner file is authoritative even when Postgres is offline.
Its maintenance payload has no expiry and is never cleared by normal startup
or stranded-rollout recovery. Business API calls remain available while an
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
            f"{action} cannot override maintenance; use the exact operation's start/resume"
        )


def require_start_allowed() -> None:
    current = snapshot()
    if current is not None and _authorized_start.get() != (current.holder, current.acquired_at):
        raise RuntimeError("normal start cannot release maintenance; use ava maintenance start")


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
    record_drained(agent_id, 0, failure=category)
