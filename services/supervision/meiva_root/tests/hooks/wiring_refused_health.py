from functools import partial
from pathlib import Path

from base.daemon.health import DaemonProbe
from services.supervision.meiva_root.health import HealthConfig, HealthMonitor
from services.supervision.meiva_root.probes import ProbeRegistry
from services.supervision.meiva_root.wiring import WiringContext


def _observe(run_dir: Path, unit_id: str) -> DaemonProbe:
    if not (run_dir / "fail-health").exists():
        return DaemonProbe.up("test has not requested the outage")
    if unit_id == "agent-host":
        return DaemonProbe.down("listener closed while native child remains live")
    with (run_dir / f"{unit_id}-probes").open("a") as receipt:
        receipt.write("observed\n")
    return DaemonProbe.up("sibling observation completed")


def build(context: WiringContext) -> HealthMonitor:
    registry = ProbeRegistry()
    for unit_id in ("agent-host", "gateway", "frontend"):
        registry.register(unit_id, partial(_observe, context.run_dir, unit_id))
    monitor = HealthMonitor(
        context.supervisor,
        registry,
        config=HealthConfig(interval_s=0.03, probe_timeout_s=1, startup_grace_s=0),
        tasks=context.participant_tasks,
    )
    context.supervisor.attach_health(monitor)
    return monitor
