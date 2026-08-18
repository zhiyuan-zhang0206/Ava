"""Port-conflict preflight — warning-only, runs on every `ava start` / `ava converge`.

The blocking pre-bind gate (`start._refuse_occupied_health_ports`, issue #977)
guards the daemon health ports and stops the start on a terminal occupant. This
step is the OTHER layer, and deliberately does not stop anything: the cluster's
full port block (gateway + data plane) plus this unit's health ports are
bind-checked before anything is launched, and every foreign occupant is printed
and appended to `$AVA_HOME/logs/port_conflicts.log`. A start continues anyway —
the step exists to turn a late, unclear "address already in use" from one
service into an early, explicit warning naming every port and its occupant.

The one subtlety is idempotent restarts: on a cluster that is already up, the
ports are held by the unit's OWN daemons, which must not read as conflicts. A
listener counts as ours when its argv, resolved executable, or working
directory mentions this unit's repo or home path — argv alone lies (services
exec a RELATIVE `.venv/bin/python`, and redis overwrites its own process
title), so the exe and cwd carry the marker instead. Anything else, including
a listener none of the three can be read for, is a conflict.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from cli.commands._converge_spec import ConvergeCtx
from shared import cluster
from shared.port_preflight import (
    env_port_drift,
    occupied_ports,
    unit_port_map,
)

# The listener-scan / ownership predicates live in shared/port_preflight.py
# (the #1603/#1606 lineage) so `ava stop`'s orphan sweep and the rollout's
# readiness gate apply the SAME rule (Task #965); this module keeps its legacy
# private aliases so nothing here re-implements them.
from shared.port_preflight import (
    listener_is_ours as _listener_is_ours,
)
from shared.port_preflight import (
    listeners_on as _listeners_on,
)
from shared.proc import process_cmdline


def _occupant_detail(port: int) -> str:
    """Human-readable description of who holds `port`, for the warning line."""
    parts: list[str] = []
    for pid in _listeners_on(port):
        cmdline = process_cmdline(pid)
        if cmdline:
            parts.append(f"pid {pid} ({Path(cmdline[0]).name})")
        else:
            parts.append(f"pid {pid}")
    return "listener(s): " + (", ".join(parts) if parts else "unknown")


def collect_port_conflicts(ctx: ConvergeCtx) -> list[str]:
    """Warning lines for this unit's ports, or [] when every port is ours or free.

    Two layers, keyed by service name (the `.env` layer OVERRIDES the block
    layer for daemons — for a record-having cluster the two agree, since
    derive_env writes both from the same base; for a record-less enrolled unit
    the block layer is the legacy segment and the `.env` is the truth):

    - the cluster's port block (`expected_cluster_ports`: the registry
      record, or the legacy block for a record-less default home);
    - this unit's health ports from its `.env` (`health_port`), the per-machine
      layer that `ava enroll --health-port-base` moves.
    """
    repo = str(ctx.repo.resolve())
    home = str(ctx.ava_home.resolve())
    markers = (repo, home)

    ports = unit_port_map(ctx.ava_home)

    out: list[str] = []
    for svc, port in occupied_ports(ports, is_ours=lambda p: _listener_is_ours(p, markers)):
        out.append(f"{svc:<22} {port:<6} {_occupant_detail(port)}")
    return out


def ensure_port_preflight(ctx: ConvergeCtx) -> None:
    """Converge step: warn (never block) on foreign port occupants + `.env` drift.

    Best-effort by contract: a preflight must not fail a start, so any exception
    in the scan prints a notice and the start proceeds with the late-failure
    behavior this step exists to make early."""
    try:
        warnings = collect_port_conflicts(ctx)
        rec = cluster.get_record(ctx.ava_home)
        if rec is not None:
            warnings += env_port_drift(ctx.ava_home, rec)
    except Exception as exc:
        print(f"  · port preflight skipped: {exc}", file=sys.stderr)
        return
    if not warnings:
        return

    print(
        "\n⚠  PORT PREFLIGHT — foreign occupant(s) or .env/registry drift (start continues):",
        file=sys.stderr,
    )
    for line in warnings:
        print(f"    {line}", file=sys.stderr)
    print(
        "    A service launching onto a held port dies on 'address already in use' — "
        "resolve the occupant before the next start, or move the port.",
        file=sys.stderr,
    )
    log_dir = ctx.ava_home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with (log_dir / "port_conflicts.log").open("a", encoding="utf-8") as f:
        for line in warnings:
            f.write(f"{stamp} {line}\n")
    print(f"  · details appended to {log_dir / 'port_conflicts.log'}", file=sys.stderr)
