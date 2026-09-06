"""Explicit local maintenance; operators coordinate the same generation per host.

Preparation leaves dependency APIs available while existing restart commands
reach the ordinary claim boundary. Stop never escalates to force, never creates
a backup, and proves only this home's declared services and owned descendants.
"""

import argparse
import json
import math
import time
from datetime import datetime

from cli.commands._maintenance_probe import host_identity, ops_quiescent
from cli.commands._maintenance_stop import (
    deadline_after,
    remaining,
    require_no_terminals,
    service_names,
    stop_data_plane,
    stop_services,
)
from shared import maintenance, maintenance_cohort, pause_owner
from shared.db import connect, publish_inbound_wake
from shared.machine import machine_name, machine_role
from shared.maintenance_state import MaintenanceHold


def _hold(holder: str, at: datetime) -> MaintenanceHold:
    current = maintenance.require_operation(holder, at)
    assert current.maintenance is not None  # noqa: S101
    return current.maintenance


def _wake(hold: MaintenanceHold) -> None:
    for agent in hold.commands:
        publish_inbound_wake(agent, "maintenance")


def _prepare(holder: str, at: datetime) -> None:
    from ops.runner_mode import is_hosted

    if not is_hosted():
        raise RuntimeError("maintenance currently requires hosted runner mode")
    roles = machine_role()
    identity = host_identity() if "agent-runner" in roles else None
    # The actual running daemon, not the checked-out source, must support the
    # admission fence. First deployment of this protocol needs separate proof.
    pause_owner.begin_maintenance(holder, at)
    if identity is None:
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
            host_owner=identity.owner,
            holder=holder,
            acquired_at=at,
        )
    if host_identity().owner != identity.owner:
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
            if "agent-runner" in machine_role() and host_identity().active:
                raise RuntimeError("agent-host still has active continuations; hold retained")
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


def _gateway_last(*, confirmed: bool) -> None:
    if "gateway" in machine_role() and not confirmed:
        raise RuntimeError(
            "gateway stop requires --gateway-last after the operator verifies every remote unit; "
            "this command does not verify remote hosts"
        )


def _stop(
    holder: str, at: datetime, timeout: float, *, gateway_last: bool, keep_terminals: bool = False
) -> None:
    deadline = deadline_after(timeout)
    _gateway_last(confirmed=gateway_last)
    hold = _hold(holder, at)
    if hold.phase not in ("drained", "stopping", "stopped"):
        raise RuntimeError("stop requires a completed drain for this operation")
    with connect() as conn:
        maintenance_cohort.verify_drained(conn, hold)
    if "agent-runner" in machine_role() and hold.phase == "drained" and host_identity().active:
        raise RuntimeError("agent-host still has active continuations")
    # Only now close ordinary API admission. In-flight native actions retained
    # their dependency APIs throughout prepare/drain.
    from shared.host_deploy_state import set_posture

    if hold.phase == "drained":
        maintenance.set_phase(holder, at, "stopping")
    set_posture("paused")
    ops_quiescent(remaining(deadline))
    stopped = stop_services(remaining(deadline), keep_terminals=keep_terminals)
    remaining(deadline)
    if hold.phase != "stopped":
        maintenance.set_phase(holder, at, "stopped")
    print(f"Stopped local recorded services: {stopped}; data plane remains available")


def _start(holder: str, at: datetime) -> int:
    from cli.commands.start import cmd_start

    hold = _hold(holder, at)
    if hold.phase not in ("stopped", "starting"):
        raise RuntimeError("maintenance start requires a stopped unit")
    if hold.phase == "stopped":
        maintenance.set_phase(holder, at, "starting")
    with maintenance.authorized_start(holder, at):
        result = cmd_start(persist_services=False)
    if result == 0:
        maintenance.set_phase(holder, at, "ready")
    return result


def _resume(holder: str, at: datetime, *, cancel: bool) -> None:
    from ops.cluster_pause import unpause_local_cluster

    hold = _hold(holder, at)
    if cancel and hold.phase not in ("preparing", "draining", "drained"):
        raise RuntimeError(
            "cancel cannot bypass a started stop; complete maintenance stop/start/resume"
        )
    if not cancel and hold.phase != "ready":
        raise RuntimeError("resume requires maintenance start; use --cancel to abandon a drain")
    if "agent-runner" in machine_role():
        host_identity()
    with connect() as conn:
        conn.execute("SELECT 1")
    # Preserve the hold if dependency/posture restoration fails. A crash after
    # its release is recovered by existing durable restart-pointer scanning.
    with maintenance.authorized_start(holder, at):
        unpause_local_cluster()
    pause_owner.change_maintenance(holder, at, hold, hold, resumed=True)
    _wake(hold)


def _stop_data(
    holder: str, at: datetime, timeout: float, *, gateway_last: bool, keep_terminals: bool = False
) -> None:
    from shared.session_backend import get_backend

    _gateway_last(confirmed=gateway_last)
    if "gateway" not in machine_role() or _hold(holder, at).phase != "stopped":
        raise RuntimeError("data-plane stop requires this gateway's stopped maintenance hold")
    if service_names(get_backend(), keep_terminals=keep_terminals):
        raise RuntimeError("local services are still running; data plane left available")
    if not keep_terminals:
        require_no_terminals()
    stopped = stop_data_plane(timeout)
    print(f"Stopped local data plane: {stopped}; no backup was created")


def run(args: argparse.Namespace) -> int:
    verb: str = args.maintenance_cmd
    if verb == "status":
        current = pause_owner.read()
        print(
            json.dumps(
                {
                    "status": current.status,
                    "operation": current.holder,
                    "acquired_at": current.acquired_at.isoformat() if current.acquired_at else None,
                    "maintenance": current.maintenance.encode() if current.maintenance else None,
                    "scope": "local unit; excludes independent OS-managed extras and remote hosts",
                },
                sort_keys=True,
            )
        )
        return 0
    at = datetime.fromisoformat(args.acquired_at)
    if at.tzinfo is None or not args.operation.strip():
        raise ValueError("maintenance requires a nonempty operation and timezone-aware timestamp")
    if verb == "prepare":
        _prepare(args.operation, at)
    elif verb == "drain":
        _drain(args.operation, at, args.timeout)
    elif verb == "stop":
        _stop(
            args.operation,
            at,
            args.timeout,
            gateway_last=args.gateway_last,
            keep_terminals=args.keep_terminals,
        )
    elif verb == "start":
        return _start(args.operation, at)
    elif verb == "resume":
        _resume(args.operation, at, cancel=args.cancel)
    elif verb == "stop-data-plane":
        _stop_data(
            args.operation,
            at,
            args.timeout,
            gateway_last=args.gateway_last,
            keep_terminals=args.keep_terminals,
        )
    else:
        raise ValueError(f"unknown maintenance action: {verb}")
    return 0
