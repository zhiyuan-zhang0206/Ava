"""Agent process lifecycle events: spawn, resurrect, terminate, launch, wake, node and halt."""

from __future__ import annotations

from typing import TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class Halt(TypedDict):
    """`halt` payload — compact/idle detection reads the body."""

    body: str


class ServiceStarted(TypedDict):
    """`service_started` payload — base/log/__init__.py."""

    name: str
    pid: int


class IdleWake(TypedDict):
    """`idle_wake` payload."""

    degraded: bool
    elapsed_s: float
    rounds: int
    timeout_s: float
    wake_state: str


class AgentSpawned(TypedDict):
    """`agent_spawned` payload — ops/agents/spawn.py."""

    spawner: str  # "user" | "agent:<id>" | "scheduler" | ...
    forked_from: int | None


class NodeExitEntry(TypedDict):
    """One node's exit inside an aggregated per-turn `node_exit` event."""

    node: str
    outcome: str  # ok | cancelled
    duration_seconds: float


class NodeExit(TypedDict):
    """`node_exit` payload — one aggregated event per graph turn (agent/graph/node_log.py)."""

    count: int
    nodes: list[NodeExitEntry]


class ProcessExit(TypedDict):
    """`process_exit` payload retained for historical process-runner events."""

    reason: str  # normal | signal:<name> | exception:<Type>
    pid: int


class AgentBootFailed(TypedDict):
    """`agent_boot_failed` payload retained for historical process-runner events."""

    model: str
    error_type: str
    error: str


EVENTS: dict[str, EventSpec] = {
    # node / process lifecycle
    "node_enter": telemetry_event(
        "node_enter",
        "LangGraph node entered — sink-filtered out of the event stream (PR #1758); log files only",
        destination="file",
        tier="noise",
    ),
    "node_exit": telemetry_event(
        "node_exit", "LangGraph node exited", payload=NodeExit, tier="noise"
    ),
    "process_exit": telemetry_event(
        "process_exit",
        "agent process exited",
        payload=ProcessExit,
        tier="noise",
        retired=True,
    ),
    "service_started": telemetry_event(
        "service_started", "gateway/daemon started", payload=ServiceStarted, tier="noise"
    ),
    "halt": telemetry_event(
        "halt", "turn stopped (idle/compact/system)", payload=Halt, tier="noise"
    ),
    "agent_restarted": telemetry_event(
        "agent_restarted", "agent restarted (phase2 done)", retired=True
    ),
    "restart_handoff_host_unhealthy": telemetry_event(
        "restart_handoff_host_unhealthy",
        "hosted restart ownership could not transfer: agent-host is unhealthy; row left restarting "
        "for retry",
        tier="anomaly",
        retired=True,
    ),
    "boot_timing": telemetry_event("boot_timing", "boot duration", tier="noise", retired=True),
    "agent_spawned": telemetry_event(
        "agent_spawned",
        "agent process started",
        payload=AgentSpawned,
    ),
    "agent_resurrected": telemetry_event("agent_resurrected", "agent resurrected"),
    "billing_resurrect_run": telemetry_event(
        "billing_resurrect_run",
        "billing batch recovery run finished",
        site=(
            'ops/lifecycle/billing_recovery.py:_record_run_event telemetry.emit("telemetry", ...)'
        ),
    ),
    "auto_resurrect_refused": telemetry_event(
        "auto_resurrect_refused",
        "an automatic resurrect of a terminated agent was refused (e.g. "
        "runtime_cutover_required); the triggering inbound stays queued until an "
        "operator resolves the named reason",
        tier="anomaly",
    ),
    "auto_resurrect_failed": telemetry_event(
        "auto_resurrect_failed",
        "an automatic resurrect failed for a possibly transient reason; the "
        "triggering inbound stays queued for a later or manual resurrect",
    ),
    "agent_terminated": telemetry_event("agent_terminated", "agent terminated", retired=True),
    "agent_revived": telemetry_event("agent_revived", "agent revived", tier="noise", retired=True),
    "respawn_phase1": telemetry_event(
        "respawn_phase1", "restart phase 1", tier="noise", retired=True
    ),
    "respawn_phase2_launch": telemetry_event(
        "respawn_phase2_launch",
        "restart phase 2 launch",
        tier="noise",
        retired=True,
    ),
    "launch_confirm_extended": telemetry_event(
        "launch_confirm_extended",
        "launch confirm extended",
        tier="noise",
        retired=True,
    ),
    "launch_confirm_failed": telemetry_event(
        "launch_confirm_failed",
        "launch confirm failed",
        tier="anomaly",
        retired=True,
    ),
    "agent_boot_failed": telemetry_event(
        "agent_boot_failed",
        "agent boot failed (process exits; crash-loop budget applies)",
        payload=AgentBootFailed,
        tier="anomaly",
        retired=True,
    ),
    "launch_confirm_task_crashed": telemetry_event(
        "launch_confirm_task_crashed",
        "launch confirm task crashed",
        tier="anomaly",
        retired=True,
    ),
    "launch_force_terminated": telemetry_event(
        "launch_force_terminated",
        "launch force-terminated",
        tier="anomaly",
        retired=True,
    ),
    "launch_force_terminated_skipped": telemetry_event(
        "launch_force_terminated_skipped",
        "launch force-terminate skipped",
        tier="noise",
        retired=True,
    ),
    "launch_retry": telemetry_event("launch_retry", "launch retried", retired=True),
    # agent lifecycle / state
    "idle_wake": telemetry_event(
        "idle_wake", "agent woken from idle", payload=IdleWake, tier="noise"
    ),
    "wake_degraded": telemetry_event(
        "wake_degraded",
        "RedisInboundListener wake path degraded (instant pub/sub wake off)",
        tier="anomaly",
    ),
    "wake_restored": telemetry_event(
        "wake_restored",
        "RedisInboundListener wake path recovered (clean consume restored instant wake)",
        tier="noise",
    ),
}
