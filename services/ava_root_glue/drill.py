"""Drill wiring for the local dry run (`scripts/ava_root_dry_run.py`).

Registers one liveness probe per manifest unit: the unit must still claim
running with a live pid (read through the supervisor's raw tree view). Light
drill units have no endpoint to identify, so identity stops there — this
module is deliberately NOT the production reference path (`glue.build_wiring`)
and exists so the dry run exercises the full hook lifecycle over a throwaway
tree: load, attach, start, probe rounds, stop.

A unit may also publish a heartbeat: when `<run_dir>/heartbeat/<unit_id>.beat`
exists, the probe additionally requires the file fresh — the revive drill's
"process alive, service dead" surface (a SIGSTOPped unit keeps its pid alive
while its beats stop, so the root's HealthMonitor judges it down and the
supervisor replaces the generation).

Intervals are shortened to 10s (production defaults stay 60s in `glue.py`)
so a short drill still records real rounds and verdicts.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol, cast

from services.ava_root.health import HealthConfig
from services.ava_root.probes import Probe, ProbeRegistry
from services.ava_root.selfcheck import SelfCheckConfig
from services.ava_root.wiring import WiringContext, WiringParticipant
from services.ava_root_glue.glue import assemble
from shared.daemon_health import DaemonProbe
from shared.proc import process_alive

_DRILL_INTERVAL_S = 10.0
_HEARTBEAT_TIMEOUT_S = 3.0


class _TreeHost(Protocol):
    """The supervisor slice the liveness probe reads (duck-typed for stubs)."""

    def tree_view(self) -> dict[str, object]:
        """`{"root_pid": int, "units": [{"id", "state", "pid"}, ...]}`."""
        ...


def build_drill_wiring(context: WiringContext) -> list[WiringParticipant]:
    """Liveness (+ heartbeat) probes for every drill manifest unit, plus the two monitors."""
    registry = ProbeRegistry()
    heartbeat_dir = context.run_dir / "heartbeat"
    for manifest in context.registry.units:
        probe = _liveness_probe(context.supervisor, manifest.id, heartbeat_dir=heartbeat_dir)
        registry.register(manifest.id, probe)
    return assemble(
        context,
        registry,
        health_config=HealthConfig(interval_s=_DRILL_INTERVAL_S),
        selfcheck_config=SelfCheckConfig(interval_s=_DRILL_INTERVAL_S),
    )


def _liveness_probe(host: _TreeHost, unit_id: str, *, heartbeat_dir: Path | None = None) -> Probe:
    """`running with a live pid`; a published heartbeat must also be fresh.

    A unit opts into the heartbeat surface by touching
    `<heartbeat_dir>/<unit_id>.beat` on a cadence below `_HEARTBEAT_TIMEOUT_S`;
    while that file exists the probe additionally requires it fresh.
    """

    def probe() -> DaemonProbe:
        try:
            view = host.tree_view()
        except Exception as exc:
            return DaemonProbe.unavailable(f"tree view failed: {exc}")
        for unit in cast("list[dict[str, object]]", view["units"]):
            if unit["id"] == unit_id:
                pid = unit["pid"]
                state = unit["state"]
                if state == "running" and isinstance(pid, int) and process_alive(pid):
                    return _heartbeat_verdict(heartbeat_dir, unit_id, pid)
                return DaemonProbe.down(f"state={state} pid={pid}")
        return DaemonProbe.down(f"unit {unit_id!r} not in the tree view")

    return probe


def _heartbeat_verdict(heartbeat_dir: Path | None, unit_id: str, pid: int) -> DaemonProbe:
    """The liveness half passed; apply the heartbeat half when one is published."""
    if heartbeat_dir is None:
        return DaemonProbe.up(f"pid {pid} alive")
    beat = heartbeat_dir / f"{unit_id}.beat"
    try:
        age = time.time() - beat.stat().st_mtime
    except OSError:
        return DaemonProbe.up(f"pid {pid} alive")
    if age > _HEARTBEAT_TIMEOUT_S:
        return DaemonProbe.down(
            f"pid {pid} alive but heartbeat stale: last beat {age:.1f}s ago"
            f" (timeout {_HEARTBEAT_TIMEOUT_S:.0f}s)"
        )
    return DaemonProbe.up(f"pid {pid} alive; heartbeat {age:.1f}s old")
