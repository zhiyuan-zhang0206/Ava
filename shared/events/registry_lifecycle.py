"""Agent process lifecycle event declarations (spawn, resurrect, terminate, revive, launch)."""

from shared.events.payloads import AgentSpawned
from shared.events.registry import _telemetry as _telemetry
from shared.events.system import AgentBootFailed, EventSpec

_EVENTS_LIFECYCLE: dict[str, EventSpec] = {
    "agent_spawned": _telemetry(
        "agent_spawned",
        "agent process started",
        payload=AgentSpawned,
        retention_class="lineage",
    ),
    "agent_resurrected": _telemetry(
        "agent_resurrected", "agent resurrected", retention_class="lineage"
    ),
    "agent_reopened": _telemetry(
        "agent_reopened",
        "an explicit resurrect cleared a closed agent's durable closure marker "
        "(closed_at) — the WARNING-level operator-side marker that the agent is "
        "out of the never-auto-resurrect closure (task #4165)",
    ),
    "billing_resurrect_run": _telemetry(
        "billing_resurrect_run", "billing batch recovery run finished"
    ),
    "agent_terminated": _telemetry("agent_terminated", "agent terminated"),
    "agent_revived": _telemetry("agent_revived", "agent revived", tier="noise"),
    "respawn_phase1": _telemetry("respawn_phase1", "restart phase 1", tier="noise"),
    "respawn_phase2_launch": _telemetry(
        "respawn_phase2_launch", "restart phase 2 launch", tier="noise"
    ),
    "launch_confirm_extended": _telemetry(
        "launch_confirm_extended", "launch confirm extended", tier="noise"
    ),
    "launch_confirm_failed": _telemetry(
        "launch_confirm_failed", "launch confirm failed", tier="anomaly"
    ),
    "agent_boot_failed": _telemetry(
        "agent_boot_failed",
        "agent boot failed (process exits; crash-loop budget applies)",
        payload=AgentBootFailed,
        tier="anomaly",
    ),
    "launch_confirm_task_crashed": _telemetry(
        "launch_confirm_task_crashed", "launch confirm task crashed", tier="anomaly"
    ),
    "launch_force_terminated": _telemetry(
        "launch_force_terminated", "launch force-terminated", tier="anomaly"
    ),
    "launch_force_terminated_skipped": _telemetry(
        "launch_force_terminated_skipped", "launch force-terminate skipped", tier="noise"
    ),
    "launch_retry": _telemetry("launch_retry", "launch retried"),
}
