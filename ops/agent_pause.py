"""Pause native agents at their existing durable restart boundary.

The local pause journal closes new admission while existing actions keep their
SDK dependencies. It survives a data-plane outage; the restart itself and its
checkpoint remain in PostgreSQL. No external-agent ownership is acquired here.
"""

import math
import os
import time
from datetime import UTC, datetime
from typing import NamedTuple
from uuid import UUID, uuid4

from ops.agent_pause_probe import host_identity, host_running
from shared import maintenance, maintenance_cohort, pause_owner
from shared.db import connect, publish_inbound_wake
from shared.hold_driver import HoldDriver
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


def _prepare(holder: str, at: datetime, *, driver: HoldDriver | None = None) -> None:
    """Publish the hold and enqueue restarts.

    `driver` is the shepherding identity of an operator-side entry (task
    #3270); daemon-driven pauses pass None and keep the handoff/outcome as
    their ownership evidence.
    """
    roles = machine_role()
    identity = host_identity() if "agent-runner" in roles and host_running() else None
    # The actual running daemon, not the checked-out source, must support the
    # admission fence. First deployment of this protocol needs separate proof.
    pause_owner.begin_maintenance(holder, at, driver=driver)
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
            pause_owner.change_maintenance(
                holder, at, current, MaintenanceHold("draining"), refresh_driver=driver is not None
            )
        return
    with connect() as conn:
        hold = maintenance_cohort.prepare(
            conn,
            machine=machine_name(),
            host_owner=identity.owner if identity is not None else None,
            holder=holder,
            acquired_at=at,
            host_absent=identity is None,
            driver=driver,
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
            raise RuntimeError(
                f"continuations failed; hold retained: {sorted(hold.failures)} — "
                "fix the root cause, then ava maintenance repair --operation "
                f"{holder} --acquired-at {at.isoformat()}"
            )
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
            raise TimeoutError(
                "drain timed out without force; hold retained for agents "
                f"{pending}\n{_stall_report(hold, pending)}"
            )
        time.sleep(min(0.2, remaining))


def _stall_report(hold: MaintenanceHold, pending: list[int]) -> str:
    """One line per unfinished agent: delivery state, row fences and host view.

    A bare agent list leaves a consuming agent indistinguishable from one
    the host will never admit — issue #2159 spent its whole 300s timeout on
    exactly that ambiguity. The report names the facts an operator routes on: the
    restart command's delivery state, the row's owner/lease/resource facts,
    the agent's last activity, and whether the live host still sees the
    agent's turn running.
    """
    host_owner: UUID | None = None
    host_active: frozenset[int] = frozenset()
    notes: list[str] = []
    try:
        if "agent-runner" in machine_role() and host_running():
            identity = host_identity()
            host_owner, host_active = identity.owner, identity.active
        else:
            notes.append("live agent-host not observed; row facts only")
    except Exception as exc:  # diagnostics never mask the timeout
        notes.append(f"host identity unavailable: {exc}")
    commands = {agent: hold.commands[agent] for agent in pending}
    try:
        with connect() as conn:
            raw_rows = conn.execute(
                "SELECT m.status, m.runtime_kind, m.runtime_owner, "
                "m.lease_expires_at IS NOT NULL AND m.lease_expires_at > clock_timestamp(), "
                "m.incarnation_resources IS NULL, m.last_active_at, "
                "i.status, i.applied_at IS NOT NULL "
                "FROM unnest(%s::int[], %s::bigint[]) AS cohort(agent_id, command_id) "
                "LEFT JOIN agents_meta m ON m.id = cohort.agent_id "
                "LEFT JOIN inbound_messages i ON i.id = cohort.command_id "
                "AND i.agent_id = cohort.agent_id ORDER BY cohort.agent_id",
                (list(commands), list(commands.values())),
            ).fetchall()
    except Exception as exc:  # diagnostics never mask the timeout
        notes.append(f"row diagnostics unavailable: {exc}")
        return "".join(f"  {note}\n" for note in notes).rstrip("\n")
    rows = [_StallRow(*row) for row in raw_rows]
    lines = [
        _stall_line(agent, command, row, host_owner, host_active)
        for (agent, command), row in zip(commands.items(), rows, strict=True)
    ]
    lines.extend(notes)
    return "\n".join(f"  {line}" for line in lines)


class _StallRow(NamedTuple):
    """One unfinished cohort agent's row facts and command delivery state."""

    status: str | None
    kind: str | None
    runtime_owner: UUID | None
    lease_fresh: bool | None
    resources_settled: bool | None
    last_active: datetime | None
    delivery: str | None
    applied: bool | None


def _stall_line(
    agent: int,
    command: int,
    row: _StallRow,
    host_owner: UUID | None,
    host_active: frozenset[int],
) -> str:
    status = row.status
    if status is None:
        return f"agent {agent}: row missing; its restart command {command} is unreachable"
    seen = "yes" if agent in host_active else "no"
    line = (
        f"agent {agent}: command {command} {row.delivery}, "
        f"applied={'yes' if row.applied else 'no'}; "
        f"row {status} kind={row.kind} owner={row.runtime_owner} "
        f"lease_fresh={'yes' if row.lease_fresh else 'no'} "
        f"resources_settled={'yes' if row.resources_settled else 'no'} "
        f"last_active={row.last_active.isoformat() if row.last_active is not None else 'never'}; "
        f"host active={seen}"
    )
    if host_owner is not None and row.runtime_owner is not None and row.runtime_owner != host_owner:
        line += (
            f"\n  fence: runtime owned by {row.runtime_owner}, not the live boot {host_owner} — "
            "a successor host cannot certify its predecessor flushed; resolve explicitly "
            "(ava maintenance status, then resume --cancel or repair)"
        )
    return line


def pause_agents(
    timeout: float = PAUSE_TIMEOUT_SECONDS, *, driver: HoldDriver | None = None
) -> None:
    """Idempotently drain this unit, leaving persistent terminals untouched.

    `driver` is minted by operator-side callers (`ava stop` / `ava pause`);
    daemon-driven callers (`spawn_update`, the update quiesce) pass None so a
    long-lived caller process never masks a dead ladder shepherd.
    """
    current = pause_owner.read()
    if current.status == "invalid":
        raise RuntimeError("cannot pause with an unreadable local pause owner")
    if current.status == "paused":
        assert current.holder is not None and current.acquired_at is not None  # noqa: S101
        holder, at = current.holder, current.acquired_at
        if driver is not None:
            pause_owner.refresh_driver(holder, at, driver=driver)
    else:
        holder, at = f"local-pause:{machine_name()}:{os.getpid()}:{uuid4()}", datetime.now(UTC)
    if (
        current.status != "paused"
        or current.maintenance is None
        or current.maintenance.phase == "preparing"
    ):
        _prepare(holder, at, driver=driver)
    hold = _hold(holder, at)
    if hold.phase in ("preparing", "draining", "drained"):
        _drain(holder, at, timeout)


def resume_agents() -> None:
    """Release the current local admission hold after start or an aborted drain."""
    current = maintenance.snapshot()
    if current is None:
        return
    assert current.maintenance is not None  # noqa: S101
    assert current.holder is not None and current.acquired_at is not None  # noqa: S101
    if current.maintenance.failures:
        raise RuntimeError(
            "cannot resume failed continuation/flush receipts; fix the root cause, "
            "then ava maintenance repair --operation "
            f"{current.holder} --acquired-at {current.acquired_at.isoformat()}"
        )
    pause_owner.change_maintenance(
        current.holder, current.acquired_at, current.maintenance, current.maintenance, resumed=True
    )
    _wake(current.maintenance)
