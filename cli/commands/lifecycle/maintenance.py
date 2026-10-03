"""The hold journal's operator exits: `status`, `repair` and `cancel`.

Holds are taken and driven by `ava stop` / `ava restart` and released by
`ava start`; these verbs only read the journal or end a hold that cannot finish
on its own. Each names the exact (operation, acquired-at) generation it acts on.
"""

import argparse
import getpass
import json
import os
import sys
from datetime import UTC, datetime

from base.cluster.machine import machine_name, machine_role
from base.db import Database
from base.deploy.maintenance import admission, hold_driver, pause_owner
from base.deploy.maintenance.state import MaintenanceHold
from base.events.live.bus import EventBus
from cli.commands.lifecycle._pause_resume import exclusive_resources
from ops.agent_pause.probe import host_identity_or_none


def _hold(holder: str, at: datetime) -> MaintenanceHold:
    """This generation's hold, read through the maintenance journal's own door."""
    current = admission.require_operation(holder, at)
    assert current.maintenance is not None  # noqa: S101
    return current.maintenance


def cancel(holder: str, at: datetime) -> None:
    """Abandon a drain that has not started stopping; services stay as they are."""
    from ops.cluster_pause import unpause_local_cluster

    hold = _hold(holder, at)
    if hold.phase not in ("preparing", "draining", "drained"):
        raise RuntimeError(
            "cancel cannot bypass a started stop; re-run `ava stop`, or `ava start` "
            "to bring the unit back and release the hold"
        )
    if "agent-runner" in machine_role() and host_identity_or_none() is None:
        print(
            "  ! agent-host is provably absent (host_running()=false; no process): "
            "proceeding without its identity probe (a refused health dial alone "
            "would not qualify)",
            file=sys.stderr,
        )
    with Database.from_settings().connect() as conn:
        conn.execute("SELECT 1")
    # Preserve the hold if dependency/posture restoration fails. A crash after
    # its release is recovered by existing durable restart-pointer scanning.
    with admission.authorized_start(holder, at):
        unpause_local_cluster(Database.from_settings(), EventBus.from_settings())


@exclusive_resources
def _repair(holder: str, at: datetime, *, operator: str | None) -> None:
    """Sanctioned release of latched blocking failure receipts.

    The exact (holder, acquired_at) capability plus the operator-identity audit
    record is the sanction; the journal tombstone keeps both sides of the CAS
    (`failures` moved verbatim into `repaired`) visible via
    `ava maintenance status`. Refuses while the agent-host still has active
    continuations, so no live receipt can be cleared from under a running
    turn. Only independently proven process absence skips the identity probe;
    a refused health connection alone still refuses. A hold
    that already drained is repairable: a post-drain failure has no other
    sanctioned exit.
    """
    from ops.cluster_pause import unpause_local_cluster

    hold = _hold(holder, at)
    if not hold.failures:
        raise RuntimeError("no failed receipts to repair; `cancel` abandons a failure-free drain")
    if hold.phase not in ("preparing", "draining", "drained"):
        raise RuntimeError(
            "repair cannot bypass a started stop; re-run `ava stop`, or `ava start` "
            "to bring the unit back and release the hold"
        )
    if "agent-runner" in machine_role():
        identity = host_identity_or_none()
        if identity is not None and identity.active:
            raise RuntimeError(
                "agent-host still has active continuations; wait for quiescence "
                "before repairing failed receipts"
            )
    with Database.from_settings().connect() as conn:
        conn.execute("SELECT 1")
    record = _repair_record(operator)
    admission.repair(holder, at, record)
    # Preserve the hold if dependency/posture restoration fails. The repaired
    # journal stays; a partial release is completed by `cancel`.
    with admission.authorized_start(holder, at):
        unpause_local_cluster(Database.from_settings(), EventBus.from_settings())
    print(
        f"Repaired {len(hold.failures)} failed receipt(s) "
        f"({sorted(hold.failures)}); hold released. "
        f"Operator: {record['by']} at {record['at']}",
        file=sys.stderr,
    )


def _repair_record(operator: str | None) -> dict[str, str]:
    """Operator-identity facts for a sanctioned repair.

    The `by` label is explicit when an agent names itself via --operator and
    falls back to the OS login identity; the uid/pid/parent facts are captured
    from the process itself, so a repair is always attributable to the process
    chain that invoked it.
    """
    now = datetime.now(UTC).isoformat()
    return {
        "at": now,
        "by": operator if operator else f"{getpass.getuser()} (local operator)",
        "user": getpass.getuser(),
        "uid": str(getattr(os, "getuid", lambda: 0)()),
        "pid": str(os.getpid()),
        "parent": _parent_process_identity(),
        "machine": machine_name(),
    }


def _parent_process_identity() -> str:
    """The immediate parent's pid + command line, truncated for the journal."""
    import psutil

    try:
        parent = psutil.Process().parent()
    except psutil.Error:
        return "unknown"
    if parent is None:
        return "unknown (reparented)"
    cmdline = " ".join(parent.cmdline() or [])
    return f"pid={parent.pid} {cmdline[:180]}"


def _driver_evidence(driver: hold_driver.HoldDriver | None) -> dict[str, object] | None:
    """The recorded shepherd identity, for humans reading the journal (task #3276).

    `root` is the process the stranded-hold verdict judges; `leader` (the session
    leader at mint time) is display evidence only. Liveness is probed from local
    process state (pid + birth), so `status` stays settings-lite. None when no
    identity was recorded (a pre-#3270 journal or a daemon-driven pause).
    """
    if driver is None:
        return None
    return {
        "liveness": hold_driver.liveness(driver),
        "root": _driver_ref(driver.root),
        "leader": _driver_ref(driver.leader),
    }


def _driver_ref(ref: hold_driver.ProcessRef | None) -> dict[str, object] | None:
    """One recorded process reference as the journal reader needs it: pid + argv."""
    if ref is None:
        return None
    return {"pid": ref.pid, "argv": ref.argv}


def _generation(args: argparse.Namespace) -> datetime:
    """The exact hold generation the verb names."""
    at = datetime.fromisoformat(args.acquired_at)
    if at.tzinfo is None or not args.operation.strip():
        raise ValueError("maintenance requires a nonempty operation and timezone-aware timestamp")
    return at


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
                    "driver": _driver_evidence(current.driver),
                    "scope": "local unit; excludes independent OS-managed extras and remote hosts",
                },
                sort_keys=True,
            )
        )
        return 0
    at = _generation(args)
    if verb == "cancel":
        cancel(args.operation, at)
    elif verb == "repair":
        _repair(args.operation, at, operator=args.operator)
    else:
        raise ValueError(f"unknown maintenance action: {verb}")
    return 0
