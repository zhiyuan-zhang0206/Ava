"""Drill wiring for the local dry run (`scripts/ava_root_dry_run.py`).

Registers one liveness probe per manifest unit: the unit must still claim
running with a live pid (read through the supervisor's raw tree view). Light
drill units have no endpoint to identify, so identity stops there — this
module is deliberately NOT the production reference path (`glue.build_wiring`)
and exists so the dry run exercises the full hook lifecycle over a throwaway
tree: load, attach, start, probe rounds, stop.

Intervals are shortened to 10s (production defaults stay 60s in `glue.py`)
so a short drill still records real rounds and verdicts.
"""

from __future__ import annotations

from typing import Protocol, cast

from services.ava_root.health import HealthConfig
from services.ava_root.probes import Probe, ProbeRegistry
from services.ava_root.selfcheck import SelfCheckConfig
from services.ava_root.wiring import WiringContext, WiringParticipant
from services.ava_root_glue.glue import assemble
from shared.daemon_health import DaemonProbe
from shared.proc import process_alive

_DRILL_INTERVAL_S = 10.0


class _TreeHost(Protocol):
    """The supervisor slice the liveness probe reads (duck-typed for stubs)."""

    def tree_view(self) -> dict[str, object]:
        """`{"root_pid": int, "units": [{"id", "state", "pid"}, ...]}`."""
        ...


def build_drill_wiring(context: WiringContext) -> list[WiringParticipant]:
    """Liveness probes for every unit of the drill manifest + the two monitors."""
    registry = ProbeRegistry()
    for manifest in context.registry.units:
        registry.register(manifest.id, _liveness_probe(context.supervisor, manifest.id))
    return assemble(
        context,
        registry,
        health_config=HealthConfig(interval_s=_DRILL_INTERVAL_S),
        selfcheck_config=SelfCheckConfig(interval_s=_DRILL_INTERVAL_S),
    )


def _liveness_probe(host: _TreeHost, unit_id: str) -> Probe:
    """`running and its pid is alive` — the only claim a light unit makes."""

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
                    return DaemonProbe.up(f"pid {pid} alive")
                return DaemonProbe.down(f"state={state} pid={pid}")
        return DaemonProbe.down(f"unit {unit_id!r} not in the tree view")

    return probe
