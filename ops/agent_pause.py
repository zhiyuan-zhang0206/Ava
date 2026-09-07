"""Pause native agents at their existing durable restart boundary.

The local pause journal closes new admission while existing actions keep their
SDK dependencies. It survives a data-plane outage; the restart itself and its
checkpoint remain in PostgreSQL. No external-agent ownership is acquired here.
"""

import math
import os
import time
from datetime import UTC, datetime
from uuid import uuid4

from ops.agent_pause_probe import host_identity, host_running
from shared import maintenance, maintenance_cohort, pause_owner
from shared.db import connect, publish_inbound_wake
from shared.machine import machine_name, machine_role
from shared.maintenance_state import MaintenanceHold

PAUSE_TIMEOUT_SECONDS = 300.0


def _hold(holder: str, at: datetime) -> MaintenanceHold:
    current = maintenance.require_operation(holder, at)
    assert current.maintenance is not None  # noqa: S101
    return current.maintenance


def _wake(hold: MaintenanceHold) -> None:
    for agent in hold.commands:
        publish_inbound_wake(agent, "maintenance")


def _prepare(holder: str, at: datetime) -> None:
    roles = machine_role()
    identity = host_identity() if "agent-runner" in roles and host_running() else None
    # The actual running daemon, not the checked-out source, must support the
    # admission fence. First deployment of this protocol needs separate proof.
    pause_owner.begin_maintenance(holder, at)
    if "agent-runner" not in roles:
        with connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM agents_meta WHERE machine=%s AND status<>'terminated' LIMIT 1",
                (machine_name(),),
            ).fetchone()
        if row is not None:
            raise RuntimeError("unit has native agents but no running hosted owner")
        current = _hold(holder, at)
        if current.phase == "preparing":
            pause_owner.change_maintenance(holder, at, current, MaintenanceHold("draining"))
        return
    with connect() as conn:
        hold = maintenance_cohort.prepare(
            conn,
            machine=machine_name(),
            host_owner=identity.owner if identity is not None else None,
            holder=holder,
            acquired_at=at,
            host_absent=identity is None,
        )
    if identity is not None and host_identity().owner != identity.owner:
        raise RuntimeError("agent-host changed boot during preparation; hold retained")
    _wake(hold)


def _drain(holder: str, at: datetime, timeout: float) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("drain timeout must be finite and positive")
    deadline = time.monotonic() + timeout
    while True:
        hold = _hold(holder, at)
        if hold.phase == "preparing":
            raise RuntimeError(
                "preparation is incomplete; repeat prepare or explicitly resume --cancel"
            )
        if hold.failures:
            raise RuntimeError(f"continuations failed; hold retained: {sorted(hold.failures)}")
        if set(hold.drained) == set(hold.commands):
            with connect() as conn:
                maintenance_cohort.verify_drained(conn, hold)
            if "agent-runner" in machine_role() and host_running() and host_identity().active:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    raise TimeoutError("agent-host continuations did not finish before deadline")
                time.sleep(min(0.05, budget))
                continue
            if hold.phase == "draining":
                maintenance.set_phase(holder, at, "drained")
            if time.monotonic() > deadline:
                raise TimeoutError("drain verification exceeded its deadline; hold retained")
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pending = sorted(set(hold.commands) - set(hold.drained))
            raise TimeoutError(f"drain timed out without force; hold retained for agents {pending}")
        time.sleep(min(0.2, remaining))


def pause_agents(timeout: float = PAUSE_TIMEOUT_SECONDS) -> None:
    """Idempotently drain this unit, leaving persistent terminals untouched."""
    current = pause_owner.read()
    if current.status == "invalid":
        raise RuntimeError("cannot pause with an unreadable local pause owner")
    if current.status == "paused":
        assert current.holder is not None and current.acquired_at is not None  # noqa: S101
        holder, at = current.holder, current.acquired_at
    else:
        holder, at = f"local-pause:{machine_name()}:{os.getpid()}:{uuid4()}", datetime.now(UTC)
    if (
        current.status != "paused"
        or current.maintenance is None
        or current.maintenance.phase == "preparing"
    ):
        _prepare(holder, at)
    hold = _hold(holder, at)
    if hold.phase in ("preparing", "draining", "drained"):
        _drain(holder, at, timeout)


def resume_agents() -> None:
    """Release the current local admission hold after start or an aborted drain."""
    current = maintenance.snapshot()
    if current is None:
        return
    assert current.maintenance is not None  # noqa: S101
    if current.maintenance.failures:
        raise RuntimeError(
            "cannot resume failed continuation/flush receipts; repair the failure first"
        )
    assert current.holder is not None and current.acquired_at is not None  # noqa: S101
    pause_owner.change_maintenance(
        current.holder, current.acquired_at, current.maintenance, current.maintenance, resumed=True
    )
    _wake(current.maintenance)
